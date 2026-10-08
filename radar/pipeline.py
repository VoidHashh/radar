"""Pasos 1-5 del pipeline (SPEC.md, sección 6). Cada paso es idempotente y reanudable:
lo ya hecho en la ejecución en curso se salta, y las páginas descargadas salen de la caché."""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from . import db
from .http import Blocked, HttpClient, RobotsDisallowed
from .keywords import flag_reviews
from .stores.base import StoreAdapter

log = logging.getLogger(__name__)


@dataclass
class StepResult:
    name: str
    data: dict = field(default_factory=dict)


class Progress:
    """Log de avance cada `every` elementos, con estimación de tiempo restante."""

    def __init__(self, label: str, total: int, every: int = 10):
        self.label, self.total, self.every = label, total, every
        self.done = 0
        self.t0 = time.monotonic()

    def tick(self) -> None:
        self.done += 1
        if self.done % self.every == 0 or self.done == self.total:
            el = time.monotonic() - self.t0
            eta = el / self.done * (self.total - self.done)
            log.info("%s: %d/%d (%.0f s, faltan ~%.0f min)", self.label, self.done, self.total, el, eta / 60)


class Pipeline:
    def __init__(self, cfg: dict, conn: sqlite3.Connection, adapter: StoreAdapter,
                 keywords: dict[str, list[str]], http: HttpClient | None = None, today: date | None = None):
        self.cfg = cfg
        self.conn = conn
        self.adapter = adapter
        self.keywords = keywords
        self.http = http
        self.today = today or date.today()

    # ------------------------------------------------------------------ ejecuciones
    def open_run(self, category: str | None) -> str:
        scope = {"category": category or "*"}
        row = db.get_open_run(self.conn, scope)
        if row is not None:
            log.info("Reanudando la ejecución %s (fase %s)", row["run_id"], row["phase"])
            return row["run_id"]
        run_id = db.create_run(self.conn, scope)
        log.info("Nueva ejecución %s, alcance %s", run_id, scope)
        return run_id

    def check_robots(self) -> None:
        """Carga robots.txt y comprueba que las rutas que usa el pipeline están permitidas."""
        if self.http is None:
            return
        base = self.cfg["store"]["base_url"]
        robots = self.http.load_robots(f"{base}/robots.txt")
        needed = [self.cfg["store"]["sitemap_apps"], self.cfg["store"]["sitemap_categories"],
                  f"{base}/categories/x/all?page=2", f"{base}/x", f"{base}/x/reviews?sort_by=newest&page=2",
                  f"{base}/x/reviews?ratings%5B%5D=1&ratings%5B%5D=2&sort_by=newest&page=2"]
        bad = [u for u in needed if not robots.allowed(u)]
        if bad:
            raise RobotsDisallowed("robots.txt prohíbe rutas necesarias: " + ", ".join(bad))

    # ------------------------------------------------------------------ paso 1
    def step_inventory(self, run_id: str, category: str | None = None) -> StepResult:
        db.update_run(self.conn, run_id, phase="inventory")
        handles = self.adapter.list_apps()
        db.upsert_app_ids(self.conn, handles, self.adapter.store, self.cfg["store"]["base_url"])
        log.info("Sitemap: %d apps", len(handles))

        candidates = self.adapter.list_categories()
        if category is not None and category not in candidates:
            raise ValueError(f"'{category}' no es una categoría con listado. Ejemplos: {', '.join(candidates[:5])}")
        categories = [category] if category else candidates

        notes = json.loads(self.conn.execute("SELECT notes FROM runs WHERE run_id = ?",
                                             (run_id,)).fetchone()["notes"] or "{}")
        cat_stats = notes.get("categories", {})
        done = set(cat_stats) | {r["category"] for r in self.conn.execute(
            "SELECT DISTINCT category FROM app_listings WHERE run_id = ?", (run_id,))}
        prog = Progress("Listados", len(categories), every=5)
        for cat in categories:
            if cat in done:
                prog.tick()
                continue
            listing = self._list_category(cat)
            if listing is None:
                if category is not None:
                    raise ValueError(f"'{category}' es una categoría intermedia: no tiene listado propio")
                cat_stats[cat] = {"listing": False}
                db.update_run(self.conn, run_id, categories=cat_stats)
                prog.tick()
                continue
            cards, announced = listing
            unique = {c.app_id for c in cards}
            cat_stats[cat] = {"cards": len(cards), "unique": len(unique), "announced": announced}
            if announced is not None and announced != len(unique):
                log.warning("%s: anuncia %s apps y el listado trae %d distintas", cat, announced, len(unique))
            db.save_listing(self.conn, cat, run_id, cards)
            db.update_run(self.conn, run_id, categories=cat_stats)
            prog.tick()

        data = {"sitemap_apps": len(handles),
                "categories_with_listing": sum(1 for c in categories if cat_stats.get(c, {}).get("listing", True)),
                "categories_without_listing": sum(1 for c in categories if cat_stats.get(c, {}).get("listing") is False),
                "listed_apps": self._count("SELECT COUNT(DISTINCT app_id) FROM app_listings WHERE run_id = ?"
                                           + (" AND category = ?" if category else ""),
                                           (run_id, category) if category else (run_id,))}
        if category is None:
            data["coverage"] = self.coverage(run_id, handles, cat_stats)
        return StepResult("inventory", data)

    def _list_category(self, cat: str) -> tuple[list, int | None] | None:
        listing = getattr(self.adapter, "category_listing", None)
        if listing is not None:
            return listing(cat)
        return list(self.adapter.list_category(cat)), None

    def coverage(self, run_id: str, sitemap_handles: list[str], cat_stats: dict) -> dict:
        """¿Cubren los listados todas las apps del sitemap? (docs/decisions.md, D2)"""
        sitemap = set(sitemap_handles)
        listed = {r[0] for r in self.conn.execute(
            "SELECT DISTINCT app_id FROM app_listings WHERE run_id = ?", (run_id,))}
        rows = self._count("SELECT COUNT(*) FROM app_listings WHERE run_id = ?", (run_id,))
        dup_in_category = sum(s["cards"] - s["unique"] for s in cat_stats.values() if "cards" in s)
        mismatched = {c: s for c, s in cat_stats.items()
                      if s.get("announced") is not None and s["announced"] != s["unique"]}
        missing = sorted(sitemap - listed)
        outside = sorted(listed - sitemap)
        multi = self._count("SELECT COUNT(*) FROM (SELECT app_id FROM app_listings WHERE run_id = ? "
                            "GROUP BY app_id HAVING COUNT(*) > 1)", (run_id,))
        report = {
            "sitemap_apps": len(sitemap), "listed_unique": len(listed), "listing_rows": rows,
            "apps_in_several_categories": multi, "duplicates_within_category": dup_in_category,
            "in_sitemap_not_listed": len(missing), "listed_not_in_sitemap": len(outside),
            "categories_count_mismatch": len(mismatched),
            "examples_missing": missing[:20], "examples_outside": outside[:20],
            "mismatched_categories": mismatched,
        }
        out = Path(self.cfg["db_path"]).parent / f"coverage_{run_id}.json"
        out.write_text(json.dumps({**report, "missing": missing, "outside": outside}, indent=1), encoding="utf-8")
        report["file"] = str(out)
        return report

    # ------------------------------------------------------------------ paso 2
    def step_meta(self, run_id: str, category: str | None = None, limit: int | None = None) -> StepResult:
        db.update_run(self.conn, run_id, phase="meta")
        min_reviews = self.cfg["meta"]["min_listing_reviews"]
        sql = ("SELECT app_id, MAX(review_count) AS rc FROM app_listings WHERE run_id = ?"
               + (" AND category = ?" if category else "") + " GROUP BY app_id HAVING rc >= ? ORDER BY rc DESC")
        params = (run_id, category, min_reviews) if category else (run_id, min_reviews)
        candidates = [r["app_id"] for r in self.conn.execute(sql, params)]
        done = {r[0] for r in self.conn.execute("SELECT app_id FROM app_snapshots WHERE run_id = ?", (run_id,))}
        todo = [a for a in candidates if a not in done]
        if limit is not None:
            todo = todo[:limit]
        gone = []
        prog = Progress("Fichas", len(todo))
        for app_id in todo:
            meta = self.adapter.fetch_app_meta(app_id)
            if meta is None:
                gone.append(app_id)
                log.warning("%s: la ficha ya no existe", app_id)
            else:
                db.save_meta(self.conn, meta, run_id, self.adapter.store)
            prog.tick()
        if gone:
            db.update_run(self.conn, run_id, gone_apps=gone)
        return StepResult("meta", {"candidates": len(candidates), "fetched": len(todo) - len(gone),
                                   "already_done": len(candidates) - len(todo) if limit is None else None,
                                   "gone": len(gone)})

    # ------------------------------------------------------------------ paso 3
    def preselect(self, run_id: str, category: str | None = None) -> list[sqlite3.Row]:
        p = self.cfg["preselect"]
        sql = ("SELECT s.* FROM app_snapshots s WHERE s.run_id = ? AND s.review_count >= ? AND ("
               " 100.0 * (s.n_1 + s.n_2) / s.review_count >= ? OR s.rating_computed <= ?)")
        params: list = [run_id, p["min_review_count"], p["min_pct_negative"], p["max_rating_computed"]]
        if category:
            sql += " AND s.app_id IN (SELECT app_id FROM app_listings WHERE run_id = ? AND category = ?)"
            params += [run_id, category]
        sql += " ORDER BY s.review_count DESC"
        return self.conn.execute(sql, params).fetchall()

    # ------------------------------------------------------------------ paso 4
    def step_reviews(self, run_id: str, category: str | None = None, since_days: int | None = None) -> StepResult:
        db.update_run(self.conn, run_id, phase="reviews")
        w = self.cfg["window"]
        since = self.today - timedelta(days=since_days or w["since_days"])
        apps = self.preselect(run_id, category)
        done = {r[0] for r in self.conn.execute("SELECT app_id FROM app_windows WHERE run_id = ?", (run_id,))}
        saved = 0
        truncated = []
        prog = Progress("Reseñas", len(apps), every=1)
        for snap in apps:
            app_id, review_count = snap["app_id"], snap["review_count"]
            if app_id in done:
                prog.tick()
                continue
            if review_count <= w["exact_max_reviews"]:
                pages = math.ceil(w["exact_max_reviews"] / 10) + 2
                reviews, complete = self.adapter.fetch_reviews(app_id, since, pages)
                total, method = len(reviews), "exact"
                negatives = sum(1 for r in reviews if r.rating is not None and r.rating <= 2)
            else:
                reviews, complete = self.adapter.fetch_reviews(app_id, since, w["max_pages"], ratings=(1, 2))
                total = self.adapter.estimate_total_since(app_id, since, review_count)
                method, negatives = "estimated", len(reviews)
            if not complete:
                truncated.append(app_id)
                log.warning("%s: se alcanzó el máximo de páginas antes del corte de %s", app_id, since)
            saved += db.save_reviews(self.conn, app_id, reviews)
            db.save_window(self.conn, app_id, run_id, total, method, negatives)
            log.info("%s: %s, total_12m=%d, negativas_12m=%d, guardadas=%d",
                     app_id, method, total, negatives, len(reviews))
            prog.tick()
        if truncated:
            db.update_run(self.conn, run_id, truncated_windows=truncated)
        return StepResult("reviews", {"preselected": len(apps), "reviews_saved_now": saved,
                                      "since": since.isoformat(), "truncated": truncated})

    # ------------------------------------------------------------------ paso 5
    def run_app_ids(self, run_id: str, category: str | None = None) -> list[str]:
        sql = "SELECT app_id FROM app_windows WHERE run_id = ?"
        params: list = [run_id]
        if category:
            sql += " AND app_id IN (SELECT app_id FROM app_listings WHERE run_id = ? AND category = ?)"
            params += [run_id, category]
        return [r[0] for r in self.conn.execute(sql, params)]

    def step_keywords(self, run_id: str, category: str | None = None) -> StepResult:
        db.update_run(self.conn, run_id, phase="keywords")
        app_ids = self.run_app_ids(run_id, category)
        counts = flag_reviews(self.conn, self.keywords, app_ids)
        return StepResult("keywords", {"flags": counts})

    # ------------------------------------------------------------------ todo
    def summary(self, run_id: str, category: str | None = None) -> dict:
        app_ids = self.run_app_ids(run_id, category)
        ph = ",".join("?" * len(app_ids)) or "''"
        q = lambda sql, *a: self._count(sql.replace("{ids}", ph), (*a, *app_ids))
        windows = self.conn.execute(
            f"SELECT w.*, s.review_count, s.rating_shown, s.rating_computed, s.n_1, s.n_2 FROM app_windows w "
            f"JOIN app_snapshots s ON s.app_id = w.app_id AND s.run_id = w.run_id "
            f"WHERE w.run_id = ? AND w.app_id IN ({ph}) ORDER BY s.review_count DESC", (run_id, *app_ids)).fetchall()
        flags = {c: 0 for c in self.keywords}
        for r in self.conn.execute(
                f"SELECT f.category, COUNT(*) AS n FROM review_flags f JOIN reviews r ON r.review_id = f.review_id "
                f"WHERE r.app_id IN ({ph}) GROUP BY f.category", app_ids):
            flags[r["category"]] = r["n"]
        listing_scope = (" AND category = ?", (run_id, category)) if category else ("", (run_id,))
        return {
            "run_id": run_id,
            "listed_apps": self._count("SELECT COUNT(DISTINCT app_id) FROM app_listings WHERE run_id = ?"
                                       + listing_scope[0], listing_scope[1]),
            "fichas": self._count("SELECT COUNT(*) FROM app_snapshots WHERE run_id = ?"
                                  + (" AND app_id IN (SELECT app_id FROM app_listings WHERE run_id = ? AND category = ?)"
                                     if category else ""), (run_id, run_id, category) if category else (run_id,)),
            "preselected": len(self.preselect(run_id, category)),
            "reviews_stored": q("SELECT COUNT(*) FROM reviews WHERE app_id IN ({ids})"),
            "negatives_stored": q("SELECT COUNT(*) FROM reviews WHERE rating <= 2 AND app_id IN ({ids})"),
            "flags": flags,
            "flagged_reviews": q("SELECT COUNT(DISTINCT f.review_id) FROM review_flags f JOIN reviews r "
                                 "ON r.review_id = f.review_id WHERE r.app_id IN ({ids})"),
            "windows": [dict(w) for w in windows],
        }

    def run_all(self, category: str | None = None) -> dict:
        self.check_robots()
        run_id = self.open_run(category)
        results = {}
        try:
            for step in ("inventory", "meta", "reviews", "keywords"):
                fn = getattr(self, f"step_{step}")
                results[step] = fn(run_id, category).data
        except Blocked as e:
            db.update_run(self.conn, run_id, blocked=str(e))
            raise
        db.update_run(self.conn, run_id, phase="phase1-done", finished=True)
        return {"steps": results, "summary": self.summary(run_id, category)}

    def _count(self, sql: str, params=()) -> int:
        return self.conn.execute(sql, params).fetchone()[0]
