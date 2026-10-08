"""Marcado por palabras clave: insensible a mayúsculas y por palabra completa.

Es una señal más (docs/decisions.md, D3): no filtra lo que pasa a Claude.
"""
from __future__ import annotations

import json
import re
import sqlite3

APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'"})


def normalize(text: str) -> str:
    return (text or "").translate(APOSTROPHES)


def compile_keywords(keywords: dict[str, list[str]]) -> dict[str, re.Pattern]:
    """Una expresión por categoría. `(?<!\\w)` y `(?!\\w)` exigen palabra completa también
    en frases que empiezan o acaban con signos ("can't customize")."""
    out = {}
    for category, phrases in keywords.items():
        alts = sorted((re.escape(normalize(p)) for p in phrases), key=len, reverse=True)
        out[category] = re.compile(r"(?<!\w)(?:" + "|".join(alts) + r")(?!\w)", re.IGNORECASE)
    return out


def match_review(text: str, compiled: dict[str, re.Pattern]) -> dict[str, list[str]]:
    """{categoría: [frases encontradas, en minúsculas y sin repetir]}."""
    norm = normalize(text)
    found = {}
    for category, rx in compiled.items():
        hits = list(dict.fromkeys(m.group(0).lower() for m in rx.finditer(norm)))
        if hits:
            found[category] = hits
    return found


def flag_reviews(conn: sqlite3.Connection, keywords: dict[str, list[str]],
                 app_ids: list[str] | None = None, max_rating: int = 2) -> dict[str, int]:
    """Recalcula `review_flags` para las reseñas de 1-2 estrellas. Devuelve marcas por categoría."""
    compiled = compile_keywords(keywords)
    sql = "SELECT review_id, body FROM reviews WHERE rating <= ?"
    params: list = [max_rating]
    if app_ids is not None:
        if not app_ids:
            return {c: 0 for c in keywords}
        sql += f" AND app_id IN ({','.join('?' * len(app_ids))})"
        params += app_ids
    rows = conn.execute(sql, params).fetchall()
    counts = {c: 0 for c in keywords}
    ids = [r["review_id"] for r in rows]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        conn.execute(f"DELETE FROM review_flags WHERE review_id IN ({','.join('?' * len(chunk))})", chunk)
    for r in rows:
        for category, hits in match_review(r["body"], compiled).items():
            conn.execute("INSERT INTO review_flags (review_id, category, matched) VALUES (?, ?, ?)",
                         (r["review_id"], category, json.dumps(hits, ensure_ascii=False)))
            counts[category] += 1
    conn.commit()
    return counts
