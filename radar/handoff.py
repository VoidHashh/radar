"""Intercambio con Claude Code para los pasos 6, 7 y 8b (sin API, D10).

`radar` exporta ficheros de entrada a `data/llm/<paso>/<run_id>/`; Claude Code escribe al lado un fichero de
salida por cada entrada siguiendo `docs/prompts/<paso>.md`; `radar` valida e importa. Todo es reanudable:
lo ya importado no se vuelve a exportar y un lote sin salida simplemente queda pendiente.
"""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import db

log = logging.getLogger(__name__)

CATEGORIES = {"broken", "pricing", "support", "missing_feature", "abandoned", "performance", "other"}
BUILDABILITY = {"S": 1.0, "M": 0.6, "L": 0.2}
LANG_RX = re.compile(r"^[a-z]{2}$")
PRICE_RX = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s*/\s*(month|year)", re.IGNORECASE)


@dataclass
class ImportReport:
    files: int = 0
    imported: int = 0
    pending: list[str] = field(default_factory=list)       # entradas sin fichero de salida
    errors: list[str] = field(default_factory=list)
    missing: int = 0                                        # elementos de la entrada sin resultado

    def as_dict(self) -> dict:
        return {"files": self.files, "imported": self.imported, "pending": len(self.pending),
                "missing_items": self.missing, "errors": len(self.errors), "error_examples": self.errors[:10]}


def _null(v):
    if v is None:
        return None
    if isinstance(v, str) and v.strip().lower() in ("", "null", "none", "n/a"):
        return None
    return v


def _step_dir(cfg: dict, step: str, run_id: str) -> Path:
    d = Path(cfg["handoff"]["dir"]) / step / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def analysis_run(conn: sqlite3.Connection, run_id: str | None = None) -> str:
    """Ejecución sobre la que se analiza: la indicada o la última con ventanas de reseñas."""
    if run_id:
        return run_id
    row = conn.execute("SELECT w.run_id FROM app_windows w JOIN runs r ON r.run_id = w.run_id "
                       "GROUP BY w.run_id ORDER BY r.started_at DESC LIMIT 1").fetchone()
    if row is None:
        raise RuntimeError("No hay ninguna ejecución con reseñas descargadas. Ejecuta antes `radar run-all`.")
    return row[0]


def run_date(conn: sqlite3.Connection, run_id: str) -> str:
    return conn.execute("SELECT substr(started_at, 1, 10) FROM runs WHERE run_id = ?", (run_id,)).fetchone()[0]


# ====================================================================== paso 6: clasificación

def classify_export(conn: sqlite3.Connection, cfg: dict, run_id: str, batch_size: int | None = None) -> dict:
    """Lotes JSONL con las negativas CON texto de las apps de la ejecución que aún no están clasificadas
    ni exportadas en un lote pendiente."""
    d = _step_dir(cfg, "classify", run_id)
    batch_size = batch_size or cfg["handoff"]["classify_batch_size"]
    exported = set()
    for f in d.glob("batch_*.in.jsonl"):
        exported |= {json.loads(line)["review_id"] for line in f.read_text(encoding="utf-8").splitlines() if line}
    rows = conn.execute(
        "SELECT r.review_id, r.app_id, a.name AS app_name, r.rating, r.body FROM reviews r "
        "JOIN app_windows w ON w.app_id = r.app_id AND w.run_id = ? "
        "LEFT JOIN apps a ON a.app_id = r.app_id "
        "WHERE r.rating <= 2 AND TRIM(COALESCE(r.body, '')) <> '' "
        "AND r.review_id NOT IN (SELECT review_id FROM review_llm) "
        "ORDER BY r.app_id, CAST(r.review_id AS INTEGER) DESC", (run_id,)).fetchall()
    todo = [dict(r) for r in rows if r["review_id"] not in exported]
    existing = sorted(d.glob("batch_*.in.jsonl"))
    n0 = len(existing)
    files = []
    for i in range(0, len(todo), batch_size):
        chunk = todo[i:i + batch_size]
        f = d / f"batch_{n0 + len(files) + 1:04d}.in.jsonl"
        f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in chunk) + "\n", encoding="utf-8")
        files.append(f.name)
    skipped_empty = conn.execute(
        "SELECT COUNT(*) FROM reviews r JOIN app_windows w ON w.app_id = r.app_id AND w.run_id = ? "
        "WHERE r.rating <= 2 AND TRIM(COALESCE(r.body, '')) = ''", (run_id,)).fetchone()[0]
    return {"run_id": run_id, "dir": str(d), "new_batches": len(files), "new_reviews": len(todo),
            "pending_batches": len(pending_outputs(d, "batch_*.in.jsonl", ".in.jsonl", ".out.jsonl")),
            "skipped_without_text": skipped_empty}


def pending_outputs(d: Path, pattern: str, in_suffix: str, out_suffix: str) -> list[str]:
    return [f.name for f in sorted(d.glob(pattern)) if not (d / f.name.replace(in_suffix, out_suffix)).exists()]


def validate_classification(obj: dict, relaxed: bool = False) -> tuple[dict | None, str | None]:
    """relaxed (reseñas de 3 estrellas, D22): basta con specific_problem y feature_requested."""
    if relaxed:
        rid = str(obj.get("review_id") or "")
        problem = (obj.get("specific_problem") or "").strip()
        if not rid or not problem:
            return None, f"{rid or '?'}: faltan review_id o specific_problem"
        if len(problem.split()) > 20:
            return None, f"{rid}: specific_problem con más de 20 palabras"
        cat = obj.get("category") if obj.get("category") in CATEGORIES else None
        try:
            sev = int(obj["severity"]) if obj.get("severity") is not None else None
        except (TypeError, ValueError):
            sev = None
        lang = (obj.get("language") or "").strip().lower()
        return {"review_id": rid, "category": cat, "specific_problem": problem,
                "feature_requested": _null(obj.get("feature_requested")),
                "alternative_mentioned": _null(obj.get("alternative_mentioned")),
                "severity": sev if sev in (1, 2, 3) else None,
                "language": lang if LANG_RX.match(lang) else None}, None
    try:
        rid = str(obj["review_id"])
        cat = obj["category"]
        problem = (obj.get("specific_problem") or "").strip()
        severity = int(obj["severity"])
        lang = (obj.get("language") or "").strip().lower()
    except (KeyError, TypeError, ValueError) as e:
        return None, f"campo ausente o inválido: {e}"
    if cat not in CATEGORIES:
        return None, f"{rid}: categoría '{cat}' no válida"
    if not problem:
        return None, f"{rid}: specific_problem vacío"
    if len(problem.split()) > 20:
        return None, f"{rid}: specific_problem con más de 20 palabras"
    if severity not in (1, 2, 3):
        return None, f"{rid}: severity {severity} fuera de 1-3"
    if not LANG_RX.match(lang):
        return None, f"{rid}: language '{lang}' no es ISO 639-1"
    return {"review_id": rid, "category": cat, "specific_problem": problem,
            "feature_requested": _null(obj.get("feature_requested")),
            "alternative_mentioned": _null(obj.get("alternative_mentioned")),
            "severity": severity, "language": lang}, None


def classify_import(conn: sqlite3.Connection, cfg: dict, run_id: str, step: str = "classify") -> ImportReport:
    d = _step_dir(cfg, step, run_id)
    rep = ImportReport()
    model = cfg["handoff"]["model_label"]
    for fin in sorted(d.glob("batch_*.in.jsonl")):
        fout = d / fin.name.replace(".in.jsonl", ".out.jsonl")
        if not fout.exists():
            rep.pending.append(fin.name)
            continue
        rep.files += 1
        ratings = {json.loads(l)["review_id"]: json.loads(l).get("rating")
                   for l in fin.read_text(encoding="utf-8").splitlines() if l.strip()}
        expected = set(ratings)
        got = set()
        batch_id = fin.name.replace(".in.jsonl", "")
        for n, line in enumerate(fout.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                rep.errors.append(f"{fout.name}:{n}: JSON inválido ({e})")
                continue
            row, err = validate_classification(obj, relaxed=ratings.get(str(obj.get("review_id"))) == 3)
            if err:
                rep.errors.append(f"{fout.name}:{n}: {err}")
                continue
            if row["review_id"] not in expected:
                rep.errors.append(f"{fout.name}:{n}: {row['review_id']} no está en el lote")
                continue
            conn.execute(
                "INSERT OR REPLACE INTO review_llm (review_id, category, specific_problem, feature_requested, "
                "alternative_mentioned, severity, language, model, batch_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row["review_id"], row["category"], row["specific_problem"], row["feature_requested"],
                 row["alternative_mentioned"], row["severity"], row["language"], model, batch_id, db.now_iso()))
            got.add(row["review_id"])
        rep.imported += len(got)
        rep.missing += len(expected - got)
    conn.commit()
    return rep


# ====================================================================== paso 7: agrupación

def cluster_export(conn: sqlite3.Connection, cfg: dict, run_id: str) -> dict:
    """Un fichero por app con sus negativas clasificadas. Se exportan todas las apps de la ejecución con al menos
    una negativa clasificada, tengan pocas o muchas (D8)."""
    d = _step_dir(cfg, "cluster", run_id)
    done = {r[0] for r in conn.execute("SELECT DISTINCT app_id FROM clusters WHERE run_id = ?", (run_id,))}
    apps = conn.execute(
        "SELECT w.app_id, a.name, a.developer, a.pricing, a.categories, s.review_count, s.rating_computed "
        "FROM app_windows w JOIN apps a ON a.app_id = w.app_id "
        "JOIN app_snapshots s ON s.app_id = w.app_id AND s.run_id = w.run_id WHERE w.run_id = ?", (run_id,)).fetchall()
    written = 0
    for app in apps:
        if app["app_id"] in done:
            continue
        reviews = conn.execute(
            "SELECT l.review_id, r.date_shown AS date, r.rating, l.category, l.specific_problem, "
            "l.feature_requested, l.alternative_mentioned, l.severity "
            "FROM review_llm l JOIN reviews r ON r.review_id = l.review_id WHERE r.app_id = ? "
            "ORDER BY r.date_shown DESC", (app["app_id"],)).fetchall()
        if not reviews:
            continue
        f = d / f"{app['app_id']}.in.json"
        if f.exists():
            continue
        payload = {
            "app_id": app["app_id"], "name": app["name"], "developer": app["developer"],
            "review_count": app["review_count"], "rating_computed": app["rating_computed"],
            "categories": json.loads(app["categories"] or "[]"), "pricing": json.loads(app["pricing"] or "[]"),
            "reviews": [dict(r) for r in reviews],
        }
        f.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        written += 1
    return {"run_id": run_id, "dir": str(d), "new_files": written,
            "pending": len(pending_outputs(d, "*.in.json", ".in.json", ".out.json"))}


def validate_clusters(obj: dict, expected_ids: set[str]) -> tuple[dict | None, str | None]:
    clusters = obj.get("clusters")
    if not isinstance(clusters, list) or not clusters:
        return None, "sin grupos"
    if len(clusters) > 5:
        return None, f"{len(clusters)} grupos (máximo 5)"
    seen: set[str] = set()
    out = []
    for c in clusters:
        label = (c.get("label") or "").strip()
        ids = [str(x) for x in c.get("review_ids") or []]
        if not label or not ids:
            return None, "grupo sin etiqueta o sin reseñas"
        unknown = set(ids) - expected_ids
        if unknown:
            return None, f"review_ids que no son de la app: {sorted(unknown)[:3]}"
        if seen & set(ids):
            return None, "una reseña aparece en dos grupos"
        seen |= set(ids)
        out.append({"label": label, "review_ids": ids})
    b = (obj.get("main_buildability") or "").strip().upper()
    if b not in BUILDABILITY:
        return None, f"main_buildability '{b}' no es S, M ni L"
    reason = (obj.get("main_buildability_reason") or "").strip()
    if not reason:
        return None, "falta main_buildability_reason"
    out.sort(key=lambda c: len(c["review_ids"]), reverse=True)
    return {"clusters": out, "buildability": b, "reason": reason}, None


def cluster_import(conn: sqlite3.Connection, cfg: dict, run_id: str) -> ImportReport:
    d = _step_dir(cfg, "cluster", run_id)
    rep = ImportReport()
    for fin in sorted(d.glob("*.in.json")):
        fout = d / fin.name.replace(".in.json", ".out.json")
        if not fout.exists():
            rep.pending.append(fin.name)
            continue
        rep.files += 1
        payload = json.loads(fin.read_text(encoding="utf-8"))
        app_id = payload["app_id"]
        try:
            obj = json.loads(fout.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            rep.errors.append(f"{fout.name}: JSON inválido ({e})")
            continue
        res, err = validate_clusters(obj, {r["review_id"] for r in payload["reviews"]})
        if err:
            rep.errors.append(f"{fout.name}: {err}")
            continue
        conn.execute("DELETE FROM clusters WHERE app_id = ? AND run_id = ?", (app_id, run_id))
        for i, c in enumerate(res["clusters"]):
            main = i == 0
            conn.execute(
                "INSERT INTO clusters (app_id, run_id, cluster_label, n_reviews, review_ids, buildability, "
                "buildability_reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (app_id, run_id, c["label"], len(c["review_ids"]), json.dumps(c["review_ids"]),
                 res["buildability"] if main else None, res["reason"] if main else None))
        rep.imported += 1
        rep.missing += len({r["review_id"] for r in payload["reviews"]}) - sum(len(c["review_ids"]) for c in res["clusters"])
    conn.commit()
    return rep


# ====================================================================== paso 8b: replicables

def min_paid_monthly(pricing: list[dict]) -> float | None:
    """Precio mensual del plan de pago más barato. '$19/month' -> 19; '$120/year' -> 10."""
    prices = []
    for plan in pricing or []:
        m = PRICE_RX.search(plan.get("price") or "")
        if m:
            value = float(m.group(1).replace(",", ""))
            monthly = value if m.group(2).lower() == "month" else value / 12
            if monthly > 0:
                prices.append(monthly)
    return min(prices) if prices else None


def replicable_candidates(conn: sqlite3.Connection, cfg: dict, run_id: str) -> list[dict]:
    r = cfg["replicables"]
    excluded = {x.lower() for x in r["exclude_developers"]}
    rows = conn.execute(
        "SELECT s.app_id, s.review_count, s.rating_shown, s.rating_computed, s.n_1, s.n_2, s.n_3, s.n_4, s.n_5, "
        "a.name, a.developer, a.pricing, a.categories FROM app_snapshots s JOIN apps a ON a.app_id = s.app_id "
        "WHERE s.run_id = ? AND s.review_count >= ? ORDER BY s.review_count DESC", (run_id, r["min_reviews"])).fetchall()
    out = []
    for row in rows:
        if (row["developer"] or "").lower() in excluded:
            continue
        price = min_paid_monthly(json.loads(row["pricing"] or "[]"))
        if price is None or price < r["min_paid_usd_month"]:
            continue
        out.append({**dict(row), "min_paid_usd_month": price})
        if len(out) >= r["max_candidates"]:
            break
    return out


def replicables_export(conn: sqlite3.Connection, cfg: dict, run_id: str, adapter) -> dict:
    d = _step_dir(cfg, "replicables", run_id)
    done = {x[0] for x in conn.execute("SELECT app_id FROM replicables WHERE run_id = ?", (run_id,))}
    written = 0
    cands = replicable_candidates(conn, cfg, run_id)
    for c in cands:
        f = d / f"{c['app_id']}.in.json"
        if c["app_id"] in done or f.exists():
            continue
        meta = adapter.fetch_app_meta(c["app_id"])   # sale de la caché si la ficha es reciente
        payload = {
            "app_id": c["app_id"], "name": c["name"], "developer": c["developer"],
            "review_count": c["review_count"], "rating_computed": c["rating_computed"],
            "counts": {str(k): c[f"n_{k}"] for k in range(1, 6)},
            "min_paid_usd_month": c["min_paid_usd_month"],
            "categories": json.loads(c["categories"] or "[]"), "pricing": json.loads(c["pricing"] or "[]"),
            "tagline": meta.tagline if meta else None, "description": meta.description if meta else None,
            "features": meta.features if meta else [], "launched": meta.launched if meta else None,
        }
        f.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        written += 1
    return {"run_id": run_id, "dir": str(d), "candidates": len(cands), "new_files": written,
            "pending": len(pending_outputs(d, "*.in.json", ".in.json", ".out.json"))}


def demand_score(review_count: int) -> float:
    return min(1.0, math.log10(review_count) / 4) if review_count and review_count > 0 else 0.0


def price_score(monthly: float | None) -> float:
    if not monthly or monthly <= 1:
        return 0.0
    return min(1.0, math.log10(monthly) / math.log10(300))


def replicables_import(conn: sqlite3.Connection, cfg: dict, run_id: str) -> ImportReport:
    d = _step_dir(cfg, "replicables", run_id)
    w = cfg["replicables"]["weights"]
    rep = ImportReport()
    for fin in sorted(d.glob("*.in.json")):
        fout = d / fin.name.replace(".in.json", ".out.json")
        if not fout.exists():
            rep.pending.append(fin.name)
            continue
        rep.files += 1
        payload = json.loads(fin.read_text(encoding="utf-8"))
        try:
            obj = json.loads(fout.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            rep.errors.append(f"{fout.name}: JSON inválido ({e})")
            continue
        b = (obj.get("buildability") or "").strip().upper()
        core = (obj.get("core") or "").strip()
        reason = (obj.get("buildability_reason") or "").strip()
        improvements = obj.get("improvements")
        if b not in BUILDABILITY or not core or not reason or not isinstance(improvements, list) or not improvements:
            rep.errors.append(f"{fout.name}: faltan core, buildability S/M/L, buildability_reason o improvements")
            continue
        dem = demand_score(payload["review_count"])
        pr = price_score(payload["min_paid_usd_month"])
        bs = BUILDABILITY[b]
        total = w["demand"] * dem + w["price"] * pr + w["buildability"] * bs
        conn.execute(
            "INSERT OR REPLACE INTO replicables (app_id, run_id, core, buildability, buildability_reason, improvements, "
            "min_paid_usd_month, demand, price, buildability_score, total) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (payload["app_id"], run_id, core, b, reason, json.dumps([str(x) for x in improvements], ensure_ascii=False),
             payload["min_paid_usd_month"], round(dem, 4), round(pr, 4), bs, round(total, 4)))
        rep.imported += 1
    conn.commit()
    return rep


# ====================================================================== D20: necesidades no cubiertas

NEED_CATEGORIES = ("missing_feature", "broken")


def needs_export(conn: sqlite3.Connection, cfg: dict, run_id: str) -> dict:
    """Un fichero por categoría de app con las quejas candidatas a "necesidad no cubierta":
    negativas clasificadas con función pedida o de categoría missing_feature / broken.
    Sin apps de plataforma (D17) ni reseñas del grupo de un incidente puntual (D18).
    Solo categorías que pueden pasar los filtros (>= min_apps apps y >= min_reviews reseñas)."""
    from .competition import category_stats, primary_category
    nc = cfg["needs"]
    d = _step_dir(cfg, "needs", run_id)
    stats = category_stats(conn, run_id, cfg)
    scores = {r["app_id"]: r for r in conn.execute("SELECT app_id, platform, incident FROM scores WHERE run_id = ?",
                                                   (run_id,))}
    excluded_reviews: set[str] = set()
    for app_id, s in scores.items():
        if s["incident"]:
            label = json.loads(s["incident"])["cluster"]
            row = conn.execute("SELECT review_ids FROM clusters WHERE app_id = ? AND run_id = ? AND cluster_label = ?",
                               (app_id, run_id, label)).fetchone()
            if row:
                excluded_reviews |= set(json.loads(row[0]))
    rows = conn.execute(
        "SELECT l.review_id, r.app_id, a.name AS app_name, a.categories, r.date_shown AS date, l.category, "
        "l.specific_problem, l.feature_requested FROM review_llm l JOIN reviews r ON r.review_id = l.review_id "
        "JOIN app_windows w ON w.app_id = r.app_id AND w.run_id = ? JOIN apps a ON a.app_id = r.app_id "
        f"WHERE l.feature_requested IS NOT NULL OR l.category IN ({','.join('?' * len(NEED_CATEGORIES))})",
        (run_id, *NEED_CATEGORIES)).fetchall()
    by_cat: dict[str, list[dict]] = {}
    skipped = {"platform": 0, "incident": 0, "no_category": 0}
    for r in rows:
        if scores.get(r["app_id"]) and scores[r["app_id"]]["platform"]:
            skipped["platform"] += 1
            continue
        if r["review_id"] in excluded_reviews:
            skipped["incident"] += 1
            continue
        cat = primary_category(r["categories"], stats)
        if cat is None:
            skipped["no_category"] += 1
            continue
        item = {k: r[k] for k in ("review_id", "app_id", "app_name", "date", "category", "specific_problem",
                                  "feature_requested")}
        by_cat.setdefault(cat, []).append(item)
    written = 0
    kept = 0
    for cat, items in sorted(by_cat.items()):
        if len(items) < nc["min_reviews"] or len({i["app_id"] for i in items}) < nc["min_apps"]:
            continue
        kept += len(items)
        f = d / f"{cat}.in.json"
        if not f.exists():
            f.write_text(json.dumps({"category": cat, "items": items}, ensure_ascii=False, indent=1), encoding="utf-8")
            written += 1
    return {"run_id": run_id, "dir": str(d), "categories": len(list(d.glob("*.in.json"))), "new_files": written,
            "items": kept, "skipped": skipped,
            "pending": len(pending_outputs(d, "*.in.json", ".in.json", ".out.json"))}


def validate_needs(obj: dict, items: dict[str, dict]) -> tuple[list[dict] | None, str | None]:
    needs = obj.get("needs")
    if not isinstance(needs, list):
        return None, "falta la lista needs"
    seen: set[str] = set()
    out = []
    for n in needs:
        label = (n.get("label") or "").strip()
        ids = [str(x) for x in n.get("review_ids") or []]
        b = (n.get("buildability") or "").strip().upper()
        reason = (n.get("buildability_reason") or "").strip()
        if not label or not ids:
            return None, "necesidad sin etiqueta o sin reseñas"
        unknown = set(ids) - set(items)
        if unknown:
            return None, f"review_ids que no están en la entrada: {sorted(unknown)[:3]}"
        if seen & set(ids):
            return None, f"una reseña aparece en dos necesidades ({label})"
        if b not in BUILDABILITY or not reason:
            return None, f"buildability S/M/L o su justificación ausente en '{label}'"
        seen |= set(ids)
        out.append({"label": label, "review_ids": ids, "buildability": b, "reason": reason,
                    "app_ids": sorted({items[i]["app_id"] for i in ids})})
    return out, None


def needs_import(conn: sqlite3.Connection, cfg: dict, run_id: str) -> ImportReport:
    d = _step_dir(cfg, "needs", run_id)
    rep = ImportReport()
    for fin in sorted(d.glob("*.in.json")):
        fout = d / fin.name.replace(".in.json", ".out.json")
        if not fout.exists():
            rep.pending.append(fin.name)
            continue
        rep.files += 1
        payload = json.loads(fin.read_text(encoding="utf-8"))
        items = {i["review_id"]: i for i in payload["items"]}
        try:
            obj = json.loads(fout.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            rep.errors.append(f"{fout.name}: JSON inválido ({e})")
            continue
        needs, err = validate_needs(obj, items)
        if err:
            rep.errors.append(f"{fout.name}: {err}")
            continue
        conn.execute("DELETE FROM needs WHERE run_id = ? AND category = ?", (run_id, payload["category"]))
        for n in needs:
            conn.execute("INSERT INTO needs (run_id, category, label, n_reviews, n_apps, review_ids, app_ids, "
                         "buildability, buildability_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (run_id, payload["category"], n["label"], len(n["review_ids"]), len(n["app_ids"]),
                          json.dumps(n["review_ids"]), json.dumps(n["app_ids"]), n["buildability"], n["reason"]))
            rep.imported += 1
    conn.commit()
    return rep


# ====================================================================== D22-D23: necesidades con material ampliado

def _incident_windows(conn: sqlite3.Connection, run_id: str) -> dict[str, tuple[str, str]]:
    return {r[0]: (json.loads(r[1])["start"], json.loads(r[1])["end"]) for r in conn.execute(
        "SELECT app_id, incident FROM scores WHERE run_id = ? AND incident IS NOT NULL", (run_id,))}


def _wide_reviews_sql(include_classified: bool) -> str:
    return ("SELECT r.review_id, r.app_id, a.name AS app_name, a.developer, a.categories, r.rating, r.date_shown, r.body "
            "FROM reviews r JOIN app_snapshots s ON s.app_id = r.app_id AND s.run_id = ? "
            "JOIN apps a ON a.app_id = r.app_id WHERE r.rating <= 3 AND r.date_shown >= ? "
            "AND TRIM(COALESCE(r.body, '')) <> ''"
            + ("" if include_classified else " AND r.review_id NOT IN (SELECT review_id FROM review_llm)"))


def _wide_since(conn: sqlite3.Connection, cfg: dict, run_id: str) -> str:
    from datetime import date, timedelta
    months = cfg["needs_wide"]["months"]
    return (date.fromisoformat(run_date(conn, run_id)) - timedelta(days=round(months * 365 / 12))).isoformat()


def _excluded(r, cfg: dict, incidents: dict) -> str | None:
    from .flags import is_platform
    if is_platform(r["developer"], cfg):
        return "platform"
    win = incidents.get(r["app_id"])
    if win and win[0] <= (r["date_shown"] or "") <= win[1]:
        return "incident"
    return None


def wide_classify_export(conn: sqlite3.Connection, cfg: dict, run_id: str, batch_size: int | None = None) -> dict:
    """Lotes con las reseñas 1-3★ con texto, de 24 meses, aún sin clasificar (sin plataformas ni incidentes)."""
    d = _step_dir(cfg, "classify_wide", run_id)
    batch_size = batch_size or cfg["handoff"]["classify_batch_size"]
    exported = set()
    for f in d.glob("batch_*.in.jsonl"):
        exported |= {json.loads(l)["review_id"] for l in f.read_text(encoding="utf-8").splitlines() if l}
    incidents = _incident_windows(conn, run_id)
    skipped = {"platform": 0, "incident": 0}
    todo = []
    for r in conn.execute(_wide_reviews_sql(False) + " ORDER BY r.app_id, CAST(r.review_id AS INTEGER) DESC",
                          (run_id, _wide_since(conn, cfg, run_id))):
        why = _excluded(r, cfg, incidents)
        if why:
            skipped[why] += 1
            continue
        if r["review_id"] not in exported:
            todo.append({"review_id": r["review_id"], "app_id": r["app_id"], "app_name": r["app_name"],
                         "rating": r["rating"], "body": r["body"]})
    n0 = len(list(d.glob("batch_*.in.jsonl")))
    files = 0
    for i in range(0, len(todo), batch_size):
        f = d / f"batch_{n0 + files + 1:04d}.in.jsonl"
        f.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in todo[i:i + batch_size]) + "\n",
                     encoding="utf-8")
        files += 1
    return {"run_id": run_id, "dir": str(d), "new_batches": files, "new_reviews": len(todo),
            "by_rating": {s: sum(1 for x in todo if x["rating"] == s) for s in (1, 2, 3)}, "skipped": skipped,
            "pending_batches": len(pending_outputs(d, "batch_*.in.jsonl", ".in.jsonl", ".out.jsonl"))}


def wide_needs_export(conn: sqlite3.Connection, cfg: dict, run_id: str) -> dict:
    """Material ampliado (D22): quejas clasificadas de 1-3★ y 24 meses de todas las fichas, por categoría principal.
    1-2★: con función pedida o de categoría missing_feature / broken. 3★: todas (ya tienen specific_problem)."""
    from .competition import category_stats, primary_category
    nw = cfg["needs_wide"]
    d = _step_dir(cfg, "needs_wide", run_id)
    stats = category_stats(conn, run_id, cfg)
    incidents = _incident_windows(conn, run_id)
    incident_reviews: set[str] = set()
    for app_id, s in conn.execute("SELECT app_id, incident FROM scores WHERE run_id = ? AND incident IS NOT NULL",
                                  (run_id,)):
        row = conn.execute("SELECT review_ids FROM clusters WHERE app_id = ? AND run_id = ? AND cluster_label = ?",
                           (app_id, run_id, json.loads(s)["cluster"])).fetchone()
        if row:
            incident_reviews |= set(json.loads(row[0]))
    skipped = {"platform": 0, "incident": 0, "not_need": 0}
    by_cat: dict[str, list[dict]] = {}
    for r in conn.execute(
            "SELECT x.*, l.category AS llm_category, l.specific_problem, l.feature_requested FROM ("
            + _wide_reviews_sql(True) + ") x JOIN review_llm l ON l.review_id = x.review_id",
            (run_id, _wide_since(conn, cfg, run_id))):
        why = _excluded(r, cfg, incidents) or ("incident" if r["review_id"] in incident_reviews else None)
        if why:
            skipped[why] += 1
            continue
        if r["rating"] <= 2 and not r["feature_requested"] and r["llm_category"] not in NEED_CATEGORIES:
            skipped["not_need"] += 1
            continue
        cat = primary_category(r["categories"], stats)
        if cat is None:
            continue
        by_cat.setdefault(cat, []).append({
            "review_id": r["review_id"], "app_id": r["app_id"], "app_name": r["app_name"], "rating": r["rating"],
            "date": r["date_shown"], "category": r["llm_category"], "specific_problem": r["specific_problem"],
            "feature_requested": r["feature_requested"]})
    written = kept = 0
    for cat, items in sorted(by_cat.items()):
        if len(items) < nw["min_reviews"] or len({i["app_id"] for i in items}) < nw["min_apps"]:
            continue
        kept += len(items)
        f = d / f"{cat}.in.json"
        if not f.exists():
            f.write_text(json.dumps({"category": cat, "items": items}, ensure_ascii=False, indent=1), encoding="utf-8")
            written += 1
    return {"run_id": run_id, "dir": str(d), "categories": len(list(d.glob("*.in.json"))), "new_files": written,
            "items": kept, "skipped": skipped,
            "pending": len(pending_outputs(d, "*.in.json", ".in.json", ".out.json"))}


def wide_needs_import(conn: sqlite3.Connection, cfg: dict, run_id: str) -> ImportReport:
    d = _step_dir(cfg, "needs_wide", run_id)
    rep = ImportReport()
    regulated_cats = set(cfg["needs_wide"].get("regulated_categories", []))
    for fin in sorted(d.glob("*.in.json")):
        fout = d / fin.name.replace(".in.json", ".out.json")
        if not fout.exists():
            rep.pending.append(fin.name)
            continue
        rep.files += 1
        payload = json.loads(fin.read_text(encoding="utf-8"))
        items = {i["review_id"]: i for i in payload["items"]}
        try:
            obj = json.loads(fout.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            rep.errors.append(f"{fout.name}: JSON inválido ({e})")
            continue
        needs, err = validate_needs(obj, items)
        if err:
            rep.errors.append(f"{fout.name}: {err}")
            continue
        conn.execute("DELETE FROM needs WHERE run_id = ? AND category = ? AND scope = 'wide'",
                     (run_id, payload["category"]))
        by_label = {(n.get("label") or "").strip(): bool(n.get("regulated")) for n in obj["needs"]}
        for n in needs:
            regulated = by_label.get(n["label"], False) or payload["category"] in regulated_cats
            conn.execute("INSERT INTO needs (run_id, category, label, n_reviews, n_apps, review_ids, app_ids, "
                         "buildability, buildability_reason, scope, regulated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'wide', ?)",
                         (run_id, payload["category"], n["label"], len(n["review_ids"]), len(n["app_ids"]),
                          json.dumps(n["review_ids"]), json.dumps(n["app_ids"]), n["buildability"], n["reason"],
                          int(regulated)))
            rep.imported += 1
    conn.commit()
    return rep
