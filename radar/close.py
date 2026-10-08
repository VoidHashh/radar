"""Cierre de la Fase 2 (D24-D26): concentración, "¿lo cubre el líder?" y nicho de monitorización."""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, timedelta

from . import db
from .handoff import ImportReport, _step_dir, pending_outputs, run_date

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS need_leaders (      -- D25
  run_id TEXT, category TEXT, label TEXT, app_id TEXT,
  verdict TEXT CHECK (verdict IN ('cubierta', 'parcial', 'no cubierta')),
  quote TEXT,
  PRIMARY KEY (run_id, category, label, app_id)
);
CREATE TABLE IF NOT EXISTS monitoring_apps (   -- D26
  run_id TEXT, app_id TEXT, in_niche INTEGER,
  checks TEXT,          -- JSON {ping, cart, checkout, app_widgets, order_data: {value, quote}}
  other_checks TEXT, weaknesses TEXT, summary TEXT,
  PRIMARY KEY (run_id, app_id)
);
"""

MONITORING_CANDIDATES = [
    "uptime", "shoptest", "checkout-tester", "revenue-shield", "alero", "checkout-assurance-beta", "simply-monitor",
    "blimey-uptime-monitoring", "alertly", "storeguard-5", "bloodhound", "observa", "opsdeck", "statusbird",
    "checkout-sentinel", "firebell", "test-cart", "testingbot-1", "upsnap", "trackentis", "virtevo-pulse",
    "mystoreguardian", "store-guardian", "cartoviq", "apppulse-app-uptime-health", "store-health-monitor", "revcure",
    "raygun-crash-reporting", "revenue-guard-1", "store-health-monitor-1", "cassian", "help1",
]
CHECKS = ("ping", "cart", "checkout", "app_widgets", "order_data")
VERDICTS = ("cubierta", "parcial", "no cubierta")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(needs)")}
    for col, typ in (("top_app", "TEXT"), ("top_share", "REAL")):
        if col not in cols:
            conn.execute(f"ALTER TABLE needs ADD COLUMN {col} {typ}")
    conn.commit()


# ====================================================================== D24: concentración

def concentration(conn: sqlite3.Connection, run_id: str) -> int:
    """Rellena needs.top_app / top_share: la app con más reseñas de cada necesidad y su proporción."""
    ensure_schema(conn)
    n = 0
    for rowid, ids in conn.execute("SELECT rowid, review_ids FROM needs WHERE run_id = ? AND scope = 'wide'",
                                   (run_id,)).fetchall():
        ids = json.loads(ids)
        top = conn.execute(f"SELECT app_id, COUNT(*) FROM reviews WHERE review_id IN ({','.join('?' * len(ids))}) "
                           "GROUP BY app_id ORDER BY 2 DESC LIMIT 1", ids).fetchone()
        conn.execute("UPDATE needs SET top_app = ?, top_share = ? WHERE rowid = ?",
                     (top[0], round(top[1] / len(ids), 4), rowid))
        n += 1
    conn.commit()
    return n


def filtered_needs(conn: sqlite3.Connection, cfg: dict, run_id: str, single_app: bool = False,
                   buildability: str | None = None) -> list[sqlite3.Row]:
    """Necesidades D23 (>= 10 reseñas, >= 3 apps, sin regulación) separadas por concentración (D24)."""
    nw = cfg["needs_wide"]
    limit = cfg.get("close", {}).get("single_app_share", 0.5)
    sql = ("SELECT rowid AS id, * FROM needs WHERE run_id = ? AND scope = 'wide' AND n_reviews >= ? AND n_apps >= ? "
           "AND COALESCE(regulated, 0) = 0 AND top_share " + (">= ?" if single_app else "< ?"))
    params: list = [run_id, nw["min_reviews"], nw["min_apps"], limit]
    if buildability:
        sql += " AND buildability = ?"
        params.append(buildability)
    return conn.execute(sql + " ORDER BY n_reviews DESC, n_apps DESC", params).fetchall()


# ====================================================================== D25: líderes

def category_leaders(conn: sqlite3.Connection, run_id: str, category: str, n: int = 3) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT app_id, MAX(review_count) rc FROM app_listings WHERE run_id = ? AND category = ? "
        "GROUP BY app_id ORDER BY rc DESC LIMIT ?", (run_id, category, n))]


def _meta_payload(meta, review_count: int | None = None) -> dict:
    return {"app_id": meta.app_id, "name": meta.name, "developer": meta.developer,
            "review_count": review_count if review_count is not None else meta.review_count,
            "rating": meta.rating_computed, "tagline": meta.tagline, "description": meta.description,
            "features": meta.features, "feature_tags": meta.feature_tags,
            "pricing": meta.pricing, "launched": meta.launched}


def leaders_export(conn: sqlite3.Connection, cfg: dict, run_id: str, adapter, buildability: str = "S") -> dict:
    """D25 (S) y D30 (M): un fichero por categoría con sus necesidades y las fichas de sus 3 líderes."""
    ensure_schema(conn)
    d = _step_dir(cfg, "leaders" if buildability == "S" else f"leaders_{buildability.lower()}", run_id)
    needs = filtered_needs(conn, cfg, run_id, buildability=buildability)
    by_cat: dict[str, list] = {}
    for n in needs:
        by_cat.setdefault(n["category"], []).append(n)
    written = 0
    for cat, items in by_cat.items():
        f = d / f"{cat}.in.json"
        if f.exists():
            continue
        leaders = []
        for app_id in category_leaders(conn, run_id, cat):
            meta = adapter.fetch_app_meta(app_id)       # ficha completa; sale de la caché si es reciente
            if meta:
                leaders.append(_meta_payload(meta))
        payload_needs = []
        for n in items:
            ids = json.loads(n["review_ids"])
            ex = conn.execute(f"SELECT specific_problem, feature_requested FROM review_llm WHERE review_id IN "
                              f"({','.join('?' * len(ids))}) LIMIT 6", ids).fetchall()
            payload_needs.append({"label": n["label"], "n_reviews": n["n_reviews"], "n_apps": n["n_apps"],
                                  "buildability_reason": n["buildability_reason"],
                                  "examples": [dict(e) for e in ex]})
        f.write_text(json.dumps({"category": cat, "needs": payload_needs, "leaders": leaders},
                                ensure_ascii=False, indent=1), encoding="utf-8")
        written += 1
    return {"dir": str(d), f"needs_{buildability}": len(needs), "categories": len(by_cat), "new_files": written,
            "pending": len(pending_outputs(d, "*.in.json", ".in.json", ".out.json"))}


def leaders_import(conn: sqlite3.Connection, cfg: dict, run_id: str, buildability: str = "S") -> ImportReport:
    ensure_schema(conn)
    d = _step_dir(cfg, "leaders" if buildability == "S" else f"leaders_{buildability.lower()}", run_id)
    rep = ImportReport()
    for fin in sorted(d.glob("*.in.json")):
        fout = d / fin.name.replace(".in.json", ".out.json")
        if not fout.exists():
            rep.pending.append(fin.name)
            continue
        rep.files += 1
        payload = json.loads(fin.read_text(encoding="utf-8"))
        labels = {n["label"] for n in payload["needs"]}
        leaders = {l["app_id"] for l in payload["leaders"]}
        try:
            obj = json.loads(fout.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            rep.errors.append(f"{fout.name}: JSON inválido ({e})")
            continue
        got = {}
        for a in obj.get("assessments", []):
            label = a.get("need_label")
            if label not in labels:
                rep.errors.append(f"{fout.name}: necesidad desconocida '{label}'")
                continue
            for l in a.get("leaders", []):
                v = (l.get("verdict") or "").strip().lower()
                if l.get("app_id") not in leaders or v not in VERDICTS:
                    rep.errors.append(f"{fout.name}: '{label}': líder o veredicto no válido ({l.get('app_id')}, {v})")
                    continue
                if v != "no cubierta" and not (l.get("quote") or "").strip():
                    rep.errors.append(f"{fout.name}: '{label}' / {l['app_id']}: falta la cita de la ficha")
                    continue
                got[(label, l["app_id"])] = (v, (l.get("quote") or "").strip())
        missing = {(lb, ap) for lb in labels for ap in leaders} - set(got)
        if missing:
            rep.errors.append(f"{fout.name}: faltan {len(missing)} veredictos")
            rep.missing += len(missing)
            continue
        conn.execute(f"DELETE FROM need_leaders WHERE run_id = ? AND category = ? AND label IN "
                     f"({','.join('?' * len(labels))})", (run_id, payload["category"], *labels))
        for (label, app_id), (v, q) in got.items():
            conn.execute("INSERT INTO need_leaders VALUES (?, ?, ?, ?, ?, ?)",
                         (run_id, payload["category"], label, app_id, v, q))
            rep.imported += 1
    conn.commit()
    return rep


# ====================================================================== D26: monitorización

def monitoring_run(conn: sqlite3.Connection) -> str:
    row = db.get_open_run(conn, {"category": "monitoring"})
    if row is None:
        for r in conn.execute("SELECT run_id, notes FROM runs ORDER BY started_at DESC"):
            if json.loads(r["notes"] or "{}").get("scope") == {"category": "monitoring"}:
                return r["run_id"]
        return db.create_run(conn, {"category": "monitoring"})
    return row["run_id"]


def monitoring_collect(conn: sqlite3.Connection, cfg: dict, adapter, handles: list[str] | None = None,
                       months: int = 24, max_pages: int = 10) -> dict:
    """Fichas y reseñas de 1-3★ (24 meses) de las candidatas. Ejecución propia, para no tocar la del ranking."""
    ensure_schema(conn)
    run_id = monitoring_run(conn)
    since = date.fromisoformat(run_date(conn, run_id)) - timedelta(days=round(months * 365 / 12))
    handles = handles or MONITORING_CANDIDATES
    done = {r[0] for r in conn.execute("SELECT app_id FROM app_snapshots WHERE run_id = ?", (run_id,))}
    fetched = gone = reviews = 0
    for h in handles:
        if h in done:
            continue
        meta = adapter.fetch_app_meta(h)
        if meta is None:
            gone += 1
            continue
        db.save_meta(conn, meta, run_id, adapter.store)
        fetched += 1
        for star in (1, 2, 3):
            if meta.counts.get(star):
                rs, _ = adapter.fetch_reviews(h, since, max_pages, ratings=(star,))
                reviews += db.save_reviews(conn, h, rs)
    return {"run_id": run_id, "fichas": fetched, "gone": gone, "reviews_saved": reviews}


def monitoring_export(conn: sqlite3.Connection, cfg: dict, adapter, chunk: int = 16) -> dict:
    run_id = monitoring_run(conn)
    d = _step_dir(cfg, "monitoring", run_id)
    apps = []
    for (app_id,) in conn.execute("SELECT app_id FROM app_snapshots WHERE run_id = ? ORDER BY review_count DESC",
                                  (run_id,)):
        meta = adapter.fetch_app_meta(app_id)
        if not meta:
            continue
        p = _meta_payload(meta)
        p["low_star_reviews"] = [dict(r) for r in conn.execute(
            "SELECT r.review_id, r.rating, r.date_shown, l.specific_problem, l.feature_requested "
            "FROM reviews r LEFT JOIN review_llm l ON l.review_id = r.review_id WHERE r.app_id = ? AND r.rating <= 3 "
            "ORDER BY r.date_shown DESC", (app_id,))]
        apps.append(p)
    files = 0
    for i in range(0, len(apps), chunk):
        f = d / f"part_{i // chunk + 1}.in.json"
        if not f.exists():
            f.write_text(json.dumps({"apps": apps[i:i + chunk]}, ensure_ascii=False, indent=1), encoding="utf-8")
            files += 1
    return {"run_id": run_id, "dir": str(d), "apps": len(apps), "new_files": files,
            "pending": len(pending_outputs(d, "*.in.json", ".in.json", ".out.json"))}


def monitoring_import(conn: sqlite3.Connection, cfg: dict) -> ImportReport:
    ensure_schema(conn)
    run_id = monitoring_run(conn)
    d = _step_dir(cfg, "monitoring", run_id)
    rep = ImportReport()
    for fin in sorted(d.glob("*.in.json")):
        fout = d / fin.name.replace(".in.json", ".out.json")
        if not fout.exists():
            rep.pending.append(fin.name)
            continue
        rep.files += 1
        expected = {a["app_id"] for a in json.loads(fin.read_text(encoding="utf-8"))["apps"]}
        try:
            obj = json.loads(fout.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            rep.errors.append(f"{fout.name}: JSON inválido ({e})")
            continue
        seen = set()
        for a in obj.get("apps", []):
            app_id = a.get("app_id")
            checks = a.get("checks") or {}
            bad = [c for c in CHECKS if (checks.get(c) or {}).get("value") not in ("sí", "no", "no consta")]
            if app_id not in expected or bad or not isinstance(a.get("in_niche"), bool):
                rep.errors.append(f"{fout.name}: {app_id}: app desconocida, in_niche o checks no válidos {bad}")
                continue
            conn.execute("INSERT OR REPLACE INTO monitoring_apps VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (run_id, app_id, int(a["in_niche"]), json.dumps(checks, ensure_ascii=False),
                          a.get("other_checks") or "", json.dumps(a.get("weaknesses") or [], ensure_ascii=False),
                          a.get("summary") or ""))
            seen.add(app_id)
            rep.imported += 1
        rep.missing += len(expected - seen)
    conn.commit()
    return rep
