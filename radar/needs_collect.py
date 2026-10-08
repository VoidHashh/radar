"""D22: material ampliado para "Necesidades no cubiertas".

Reseñas de 1, 2 y 3 estrellas de los últimos 24 meses de todas las apps con ficha (>= 50 reseñas), salvo plataformas,
con el filtro `ratings[]` de una sola estrella y como máximo `max_pages` páginas por app y estrella.
Reanudable: cada (app, estrella) terminada queda en la tabla `needs_crawl`.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta

from . import db
from .flags import is_platform
from .handoff import run_date
from .pipeline import Progress

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS needs_crawl (
  run_id TEXT, app_id TEXT, star INTEGER,
  n_reviews INTEGER, complete INTEGER, done_at TEXT,
  PRIMARY KEY (run_id, app_id, star)
);
"""


def plan(conn: sqlite3.Connection, cfg: dict, run_id: str, stars=(1, 2, 3)) -> list[tuple[str, int]]:
    conn.executescript(SCHEMA)
    done = {(r[0], r[1]) for r in conn.execute("SELECT app_id, star FROM needs_crawl WHERE run_id = ?", (run_id,))}
    todo = []
    for r in conn.execute("SELECT s.app_id, s.n_1, s.n_2, s.n_3, s.review_count, a.developer FROM app_snapshots s "
                          "JOIN apps a ON a.app_id = s.app_id WHERE s.run_id = ? ORDER BY s.review_count DESC",
                          (run_id,)):
        if is_platform(r["developer"], cfg):
            continue
        for star in stars:
            if r[f"n_{star}"] and (r["app_id"], star) not in done:
                todo.append((r["app_id"], star))
    return todo


def collect(conn: sqlite3.Connection, cfg: dict, adapter, run_id: str, months: int = 24, max_pages: int = 10,
            stars=(1, 2, 3)) -> dict:
    since = date.fromisoformat(run_date(conn, run_id)) - timedelta(days=round(months * 365 / 12))
    todo = plan(conn, cfg, run_id, stars)
    prog = Progress("Necesidades: reseñas por app y estrella", len(todo), every=50)
    saved = truncated = 0
    for app_id, star in todo:
        reviews, complete = adapter.fetch_reviews(app_id, since, max_pages, ratings=(star,))
        saved += db.save_reviews(conn, app_id, reviews)
        truncated += 0 if complete else 1
        conn.execute("INSERT OR REPLACE INTO needs_crawl VALUES (?, ?, ?, ?, ?, ?)",
                     (run_id, app_id, star, len(reviews), int(complete), db.now_iso()))
        conn.commit()
        prog.tick()
    total = conn.execute("SELECT COUNT(*), SUM(n_reviews), SUM(1 - complete) FROM needs_crawl WHERE run_id = ?",
                         (run_id,)).fetchone()
    return {"since": since.isoformat(), "pairs_done_now": len(todo), "reviews_saved_now": saved,
            "pairs_total": total[0], "reviews_total": total[1], "truncated_pairs": total[2]}
