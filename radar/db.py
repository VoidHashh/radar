"""Esquema SQLite (SPEC.md, sección 5) y helpers."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .stores.base import AppMeta, ListingCard, Review

SCHEMA = """
CREATE TABLE IF NOT EXISTS apps (
  app_id TEXT PRIMARY KEY,
  store TEXT NOT NULL DEFAULT 'shopify',
  name TEXT, developer TEXT, url TEXT,
  categories TEXT,
  pricing TEXT,
  first_seen TEXT, last_seen TEXT
);
CREATE TABLE IF NOT EXISTS app_listings (
  app_id TEXT, category TEXT, run_id TEXT,
  rating_shown REAL, review_count INTEGER, position INTEGER,
  PRIMARY KEY (app_id, category, run_id)
);
CREATE TABLE IF NOT EXISTS app_snapshots (
  app_id TEXT, taken_at TEXT, run_id TEXT,
  rating_shown REAL, rating_computed REAL,
  review_count INTEGER,
  n_5 INTEGER, n_4 INTEGER, n_3 INTEGER, n_2 INTEGER, n_1 INTEGER,
  PRIMARY KEY (app_id, taken_at)
);
CREATE TABLE IF NOT EXISTS app_windows (
  app_id TEXT, run_id TEXT,
  total_12m INTEGER,
  method TEXT CHECK (method IN ('exact', 'estimated')),
  negatives_12m INTEGER,
  PRIMARY KEY (app_id, run_id)
);
CREATE TABLE IF NOT EXISTS reviews (
  review_id TEXT PRIMARY KEY,
  app_id TEXT, rating INTEGER,
  date_shown TEXT,
  edited INTEGER,
  country TEXT, usage_duration TEXT,
  body TEXT, has_dev_reply INTEGER, dev_reply_date TEXT,
  fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS review_flags (
  review_id TEXT, category TEXT, matched TEXT,
  PRIMARY KEY (review_id, category)
);
CREATE TABLE IF NOT EXISTS review_llm (
  review_id TEXT PRIMARY KEY,
  category TEXT, specific_problem TEXT,
  feature_requested TEXT, alternative_mentioned TEXT,
  severity INTEGER, language TEXT,
  model TEXT, batch_id TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS clusters (
  app_id TEXT, run_id TEXT, cluster_label TEXT,
  n_reviews INTEGER, review_ids TEXT,
  buildability TEXT,
  buildability_reason TEXT
);
CREATE TABLE IF NOT EXISTS scores (
  app_id TEXT, run_id TEXT,
  demand REAL, pain REAL, recent_pain REAL, momentum REAL,
  recurrence REAL, buildability REAL, neglect REAL,
  total REAL,
  PRIMARY KEY (app_id, run_id)
);
CREATE TABLE IF NOT EXISTS replicables (
  app_id TEXT, run_id TEXT,
  core TEXT,
  buildability TEXT, buildability_reason TEXT,
  improvements TEXT,
  min_paid_usd_month REAL,
  demand REAL, price REAL, buildability_score REAL, total REAL,
  PRIMARY KEY (app_id, run_id)
);
CREATE TABLE IF NOT EXISTS needs (           -- D20: necesidades no cubiertas
  run_id TEXT, category TEXT, label TEXT,
  n_reviews INTEGER, n_apps INTEGER,
  review_ids TEXT, app_ids TEXT,              -- JSON
  buildability TEXT, buildability_reason TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT,
  phase TEXT, notes TEXT, llm_cost_usd REAL
);
CREATE INDEX IF NOT EXISTS idx_reviews_app ON reviews(app_id, rating);
CREATE INDEX IF NOT EXISTS idx_listings_run ON app_listings(run_id, category);
CREATE INDEX IF NOT EXISTS idx_snapshots_run ON app_snapshots(run_id);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Columnas añadidas después de la Fase 2 (D13-D16). `connect` las crea si faltan.
MIGRATIONS = [
    ("scores", "volume", "REAL"),
    ("scores", "competition", "REAL"),
    ("scores", "category", "TEXT"),
    ("scores", "low_sample", "INTEGER"),
    ("replicables", "competition", "REAL"),
    ("replicables", "category", "TEXT"),
    ("replicables", "strong_competitors", "INTEGER"),
    ("replicables", "free_alternatives", "INTEGER"),   # obsoleta desde D19; se deja vacía
    ("scores", "platform", "INTEGER"),                  # D17
    ("scores", "incident", "TEXT"),                     # D18: JSON con el grupo y la ventana
    ("replicables", "leader_app", "TEXT"),              # D19
    ("replicables", "leader_share", "REAL"),            # D19
    ("needs", "scope", "TEXT"),                         # D22: NULL = D20 (preseleccionadas, 12 m); 'wide' = 24 m
    ("needs", "regulated", "INTEGER"),                  # D23
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, col, typ in MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    conn.commit()


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


# --------------------------------------------------------------------------- runs

def get_open_run(conn: sqlite3.Connection, scope: dict) -> sqlite3.Row | None:
    """Última ejecución sin terminar con el mismo alcance (para reanudar)."""
    for row in conn.execute("SELECT * FROM runs WHERE finished_at IS NULL ORDER BY started_at DESC"):
        notes = json.loads(row["notes"] or "{}")
        if notes.get("scope") == scope:
            return row
    return None


def create_run(conn: sqlite3.Connection, scope: dict) -> str:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    conn.execute("INSERT INTO runs (run_id, started_at, phase, notes) VALUES (?, ?, ?, ?)",
                 (run_id, now_iso(), "start", json.dumps({"scope": scope})))
    conn.commit()
    return run_id


def update_run(conn: sqlite3.Connection, run_id: str, phase: str | None = None,
               finished: bool = False, **note_updates) -> None:
    row = conn.execute("SELECT notes FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    notes = json.loads(row["notes"] or "{}") if row else {}
    notes.update(note_updates)
    conn.execute(
        "UPDATE runs SET phase = COALESCE(?, phase), notes = ?, finished_at = CASE WHEN ? THEN ? ELSE finished_at END "
        "WHERE run_id = ?",
        (phase, json.dumps(notes, ensure_ascii=False), finished, now_iso(), run_id))
    conn.commit()


def latest_run(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    return row["run_id"] if row else None


# --------------------------------------------------------------------------- apps

def upsert_app_ids(conn: sqlite3.Connection, app_ids: Iterable[str], store: str, base_url: str) -> int:
    ts = now_iso()
    rows = [(a, store, f"{base_url}/{a}", ts, ts) for a in app_ids]
    conn.executemany(
        "INSERT INTO apps (app_id, store, url, first_seen, last_seen) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(app_id) DO UPDATE SET last_seen = excluded.last_seen", rows)
    conn.commit()
    return len(rows)


def save_listing(conn: sqlite3.Connection, category: str, run_id: str, cards: list[ListingCard]) -> None:
    conn.execute("DELETE FROM app_listings WHERE category = ? AND run_id = ?", (category, run_id))
    conn.executemany(
        "INSERT OR REPLACE INTO app_listings (app_id, category, run_id, rating_shown, review_count, position) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(c.app_id, category, run_id, c.rating_shown, c.review_count, c.position) for c in cards])
    conn.commit()


def save_meta(conn: sqlite3.Connection, meta: AppMeta, run_id: str, store: str) -> None:
    ts = now_iso()
    conn.execute(
        "INSERT INTO apps (app_id, store, name, developer, url, categories, pricing, first_seen, last_seen) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(app_id) DO UPDATE SET "
        "name = excluded.name, developer = excluded.developer, url = excluded.url, "
        "categories = excluded.categories, pricing = excluded.pricing, last_seen = excluded.last_seen",
        (meta.app_id, store, meta.name, meta.developer, meta.url,
         json.dumps(meta.categories, ensure_ascii=False), json.dumps(meta.pricing, ensure_ascii=False), ts, ts))
    c = meta.counts
    conn.execute(
        "INSERT OR REPLACE INTO app_snapshots (app_id, taken_at, run_id, rating_shown, rating_computed, review_count, "
        "n_5, n_4, n_3, n_2, n_1) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (meta.app_id, ts, run_id, meta.rating_shown, meta.rating_computed, meta.review_count,
         c.get(5, 0), c.get(4, 0), c.get(3, 0), c.get(2, 0), c.get(1, 0)))
    conn.commit()


def save_reviews(conn: sqlite3.Connection, app_id: str, reviews: list[Review]) -> int:
    ts = now_iso()
    conn.executemany(
        "INSERT INTO reviews (review_id, app_id, rating, date_shown, edited, country, usage_duration, body, "
        "has_dev_reply, dev_reply_date, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(review_id) DO UPDATE SET rating = excluded.rating, date_shown = excluded.date_shown, "
        "edited = excluded.edited, country = excluded.country, usage_duration = excluded.usage_duration, "
        "body = excluded.body, has_dev_reply = excluded.has_dev_reply, dev_reply_date = excluded.dev_reply_date, "
        "fetched_at = excluded.fetched_at",
        [(r.review_id, app_id, r.rating, r.date_shown, int(r.edited), r.country, r.usage_duration, r.body,
          int(r.has_dev_reply), r.dev_reply_date, ts) for r in reviews])
    conn.commit()
    return len(reviews)


def save_window(conn: sqlite3.Connection, app_id: str, run_id: str, total_12m: int, method: str,
                negatives_12m: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO app_windows (app_id, run_id, total_12m, method, negatives_12m) VALUES (?, ?, ?, ?, ?)",
        (app_id, run_id, total_12m, method, negatives_12m))
    conn.commit()
