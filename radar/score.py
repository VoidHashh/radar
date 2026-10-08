"""Paso 8: puntuación auditable. Cada componente va de 0 a 1 y se guarda por separado.

Cambios tras la Fase 2 (docs/decisions.md, D13-D16):
- recent_pain, recurrence y neglect se suavizan con una media bayesiana: (x·n + p0·m) / (n + m), donde p0 es la
  proporción agregada de las apps preseleccionadas de la ejecución y m = scoring.bayes_m.
- volume = log10(1 + negatives_12m), normalizado por el máximo de la ejecución.
- competition = 1 - presión competitiva de la categoría principal (radar/competition.py).
- low_sample = 1 si negatives_12m < scoring.low_sample_negatives (solo se marca, no se excluye).
- platform = 1 si el desarrollador es de plataforma, canal o pago (D17): se puntúa, pero el informe lo saca del ranking.
- incident: incidente puntual (D18). Desde D21 no cambia la puntuación: el informe saca la app del ranking.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, timedelta

from .competition import app_competition, category_stats
from .flags import detect_incident, is_platform
from .handoff import BUILDABILITY, demand_score, price_score, run_date

COMPONENTS = ("demand", "pain", "recent_pain", "momentum", "recurrence", "buildability", "neglect",
              "volume", "competition")


def shrink(numerator: float, n: float, p0: float, m: float) -> float:
    """Media bayesiana de una proporción x = numerator / n: (x·n + p0·m) / (n + m)."""
    return (numerator + p0 * m) / (n + m) if n + m > 0 else p0


def raw_inputs(conn: sqlite3.Connection, app_id: str, run_id: str, today: date) -> dict:
    s = conn.execute("SELECT * FROM app_snapshots WHERE app_id = ? AND run_id = ?", (app_id, run_id)).fetchone()
    w = conn.execute("SELECT * FROM app_windows WHERE app_id = ? AND run_id = ?", (app_id, run_id)).fetchone()
    since = (today - timedelta(days=365)).isoformat()
    d90 = (today - timedelta(days=90)).isoformat()
    # Todas las negativas de la ventana, también las que no tienen texto (D7).
    neg = conn.execute("SELECT date_shown, has_dev_reply FROM reviews WHERE app_id = ? AND rating <= 2 "
                       "AND date_shown >= ?", (app_id, since)).fetchall()
    classified = conn.execute("SELECT COUNT(*) FROM review_llm l JOIN reviews r ON r.review_id = l.review_id "
                              "WHERE r.app_id = ?", (app_id,)).fetchone()[0]
    main = conn.execute("SELECT n_reviews, buildability FROM clusters WHERE app_id = ? AND run_id = ? "
                        "AND buildability IS NOT NULL", (app_id, run_id)).fetchone()
    cats, developer = conn.execute("SELECT categories, developer FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    return {
        "review_count": s["review_count"] or 0, "n12": s["n_1"] + s["n_2"],
        "total_12m": w["total_12m"] or 0, "negatives_12m": w["negatives_12m"] or 0,
        "neg_window": len(neg), "neg_noreply": sum(1 for r in neg if not r["has_dev_reply"]),
        "recent90": sum(1 for r in neg if r["date_shown"] >= d90),
        "classified": classified, "main_n": main["n_reviews"] if main else 0,
        "buildability": main["buildability"] if main else None, "categories": cats, "developer": developer,
    }


def score_run(conn: sqlite3.Connection, run_id: str, cfg_or_weights: dict) -> int:
    # Compatibilidad: acepta la configuración completa o solo los pesos (tests antiguos).
    cfg = cfg_or_weights if "weights" in cfg_or_weights else {"weights": cfg_or_weights}
    weights = cfg["weights"]
    sc = cfg.get("scoring", {"bayes_m": 0, "low_sample_negatives": 0})
    m = sc["bayes_m"]
    today = date.fromisoformat(run_date(conn, run_id))
    apps = [r[0] for r in conn.execute("SELECT app_id FROM app_windows WHERE run_id = ?", (run_id,))]
    raw = {a: raw_inputs(conn, a, run_id, today) for a in apps}

    # Proporciones agregadas de la ejecución: la "media del catálogo preseleccionado" (p0).
    def pooled(num: str, den: str) -> float:
        d = sum(r[den] for r in raw.values())
        return sum(r[num] for r in raw.values()) / d if d else 0.0
    p0 = {"recent_pain": pooled("negatives_12m", "total_12m"),
          "recurrence": pooled("main_n", "classified"),
          "neglect": pooled("neg_noreply", "neg_window")}
    max_vol = max((math.log10(1 + r["negatives_12m"]) for r in raw.values()), default=0) or 1
    stats = category_stats(conn, run_id, cfg) if "scoring" in cfg else {}

    conn.execute("DELETE FROM scores WHERE run_id = ?", (run_id,))
    for app_id, r in raw.items():
        older = r["neg_window"] - r["recent90"]
        rate_recent, rate_older = r["recent90"] / 90, older / 275
        momentum = (min(rate_recent / rate_older, 3.0) / 3.0 if rate_older > 0
                    else (1.0 if r["recent90"] > 0 else 0.0))
        comp = app_competition(app_id, r["categories"], stats) if stats else {"competition": 0.0, "category": None}
        c = {
            "demand": demand_score(r["review_count"]),
            "pain": r["n12"] / r["review_count"] if r["review_count"] else 0.0,
            "recent_pain": min(1.0, shrink(r["negatives_12m"], r["total_12m"], p0["recent_pain"], m)),
            "momentum": momentum,
            "recurrence": shrink(r["main_n"], r["classified"], p0["recurrence"], m) if r["classified"] else 0.0,
            "buildability": BUILDABILITY.get(r["buildability"], 0.0),
            "neglect": shrink(r["neg_noreply"], r["neg_window"], p0["neglect"], m),
            "volume": math.log10(1 + r["negatives_12m"]) / max_vol,
            "competition": comp["competition"],
        }
        incident = detect_incident(conn, app_id, run_id, today, cfg) if "incident" in cfg else None
        total = sum(weights.get(k, 0) * v for k, v in c.items())
        low = int(r["negatives_12m"] < sc.get("low_sample_negatives", 0))
        platform = int(is_platform(r["developer"], cfg))
        conn.execute(
            "INSERT INTO scores (app_id, run_id, demand, pain, recent_pain, momentum, recurrence, buildability, "
            "neglect, volume, competition, category, low_sample, platform, incident, total) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (app_id, run_id, *(round(c[k], 4) for k in COMPONENTS), comp["category"], low, platform,
             json.dumps(incident) if incident else None, round(total, 4)))
    conn.execute("UPDATE runs SET notes = json_set(COALESCE(notes, '{}'), '$.scoring_p0', json(?)) WHERE run_id = ?",
                 (json.dumps({k: round(v, 4) for k, v in p0.items()}), run_id))
    conn.commit()
    if stats:
        rescore_replicables(conn, cfg, run_id, stats)
    return len(apps)


def rescore_replicables(conn: sqlite3.Connection, cfg: dict, run_id: str, stats=None) -> int:
    """Recalcula la puntuación de replicables con competencia, sin volver a evaluarlas."""
    stats = stats or category_stats(conn, run_id, cfg)
    w = cfg["replicables"]["weights"]
    rows = conn.execute("SELECT r.app_id, r.min_paid_usd_month, r.buildability, s.review_count, a.categories "
                        "FROM replicables r JOIN app_snapshots s ON s.app_id = r.app_id AND s.run_id = r.run_id "
                        "JOIN apps a ON a.app_id = r.app_id WHERE r.run_id = ?", (run_id,)).fetchall()
    for r in rows:
        comp = app_competition(r["app_id"], r["categories"], stats)
        dem, pr, bs = demand_score(r["review_count"]), price_score(r["min_paid_usd_month"]), BUILDABILITY[r["buildability"]]
        total = (w["demand"] * dem + w["price"] * pr + w["buildability"] * bs
                 + w.get("competition", 0) * comp["competition"])
        conn.execute("UPDATE replicables SET demand = ?, price = ?, buildability_score = ?, competition = ?, "
                     "category = ?, strong_competitors = ?, free_alternatives = NULL, leader_app = ?, leader_share = ?, "
                     "total = ? WHERE app_id = ? AND run_id = ?",
                     (round(dem, 4), round(pr, 4), bs, comp["competition"], comp["category"],
                      comp["strong_competitors"], comp["leader"], comp["leader_share"], round(total, 4),
                      r["app_id"], run_id))
    conn.commit()
    return len(rows)
