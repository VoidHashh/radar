"""Competencia por categoría (D15, revisada en D19), con los listados ya descargados, sin rastrear.

Para cada categoría con listado:
- strong: apps con >= `scoring.competition.strong_min_reviews` reseñas (100),
- leader_share: reseñas de la app líder / reseñas totales de la categoría.

presión = w_strong·norm(log10(1 + nº de apps fuertes)) + w_leader·leader_share
competition = 1 - presión   (menos competencia fuerte = más puntos)

Cada app se mide en su categoría principal: la primera de su ficha que tenga listado.
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field


@dataclass
class CategoryStats:
    category: str
    n_apps: int = 0
    total_reviews: int = 0
    leader: str | None = None
    leader_reviews: int = 0
    strong: set[str] = field(default_factory=set)
    score: float = 0.0

    @property
    def leader_share(self) -> float:
        return self.leader_reviews / self.total_reviews if self.total_reviews else 0.0


def category_stats(conn: sqlite3.Connection, run_id: str, cfg: dict) -> dict[str, CategoryStats]:
    cc = cfg["scoring"]["competition"]
    stats: dict[str, CategoryStats] = {}
    for cat, app_id, rc in conn.execute(
            "SELECT category, app_id, MAX(review_count) FROM app_listings WHERE run_id = ? GROUP BY category, app_id",
            (run_id,)):
        rc = rc or 0
        st = stats.setdefault(cat, CategoryStats(cat))
        st.n_apps += 1
        st.total_reviews += rc
        if rc > st.leader_reviews:
            st.leader, st.leader_reviews = app_id, rc
        if rc >= cc["strong_min_reviews"]:
            st.strong.add(app_id)
    if not stats:
        return stats
    max_strong = max(math.log10(1 + len(s.strong)) for s in stats.values()) or 1
    for s in stats.values():
        pressure = cc["strong"] * math.log10(1 + len(s.strong)) / max_strong + cc["leader_share"] * s.leader_share
        s.score = round(1 - pressure, 4)
    return stats


def primary_category(categories_json: str | None, stats: dict[str, CategoryStats]) -> str | None:
    for c in json.loads(categories_json or "[]"):
        if c.get("handle") in stats:
            return c["handle"]
    return None


def app_competition(app_id: str, categories_json: str | None, stats: dict[str, CategoryStats]) -> dict:
    """Puntuación de competencia de la app y datos para el informe (competidores sin contar la propia app)."""
    cat = primary_category(categories_json, stats)
    if cat is None:
        return {"category": None, "competition": 0.5, "n_apps": None, "strong_competitors": None,
                "leader": None, "leader_share": None}
    s = stats[cat]
    return {"category": cat, "competition": s.score, "n_apps": s.n_apps,
            "strong_competitors": len(s.strong - {app_id}),
            "leader": s.leader, "leader_share": round(s.leader_share, 4)}
