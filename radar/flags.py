"""Marcas que afectan al ranking: plataformas (D17) e incidentes puntuales (D18)."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta


def is_platform(developer: str | None, cfg: dict) -> bool:
    """Desarrollador de plataforma, canal o pago. Coincide el nombre exacto o seguido de un espacio
    ("Google LLC" -> Google, "PINTEREST inc" -> Pinterest), sin distinguir mayúsculas."""
    d = (developer or "").strip().lower()
    for p in cfg.get("platforms", {}).get("developers", []):
        p = p.strip().lower()
        if d == p or d.startswith(p + " "):
            return True
    return False


def detect_incident(conn: sqlite3.Connection, app_id: str, run_id: str, today: date, cfg: dict) -> dict | None:
    """Incidente puntual: >= `share` de las negativas de 12 meses en una ventana de `window_days` días y del mismo
    grupo de quejas. Devuelve {"cluster", "start", "end", "n", "share"} o None."""
    inc = cfg.get("incident")
    if not inc:
        return None
    w = conn.execute("SELECT negatives_12m FROM app_windows WHERE app_id = ? AND run_id = ?", (app_id, run_id)).fetchone()
    total = (w[0] or 0) if w else 0
    if total < inc["min_negatives"]:
        return None
    since = (today - timedelta(days=365)).isoformat()
    groups: dict[str, list[str]] = {}
    if inc.get("same_cluster", True):
        for label, ids in conn.execute("SELECT cluster_label, review_ids FROM clusters WHERE app_id = ? AND run_id = ?",
                                       (app_id, run_id)):
            ids = json.loads(ids)
            dates = [r[0] for r in conn.execute(
                f"SELECT date_shown FROM reviews WHERE review_id IN ({','.join('?' * len(ids))}) AND date_shown >= ?",
                (*ids, since))]
            groups[label] = dates
    else:
        groups["*"] = [r[0] for r in conn.execute(
            "SELECT date_shown FROM reviews WHERE app_id = ? AND rating <= 2 AND date_shown >= ?", (app_id, since))]
    best = None
    span = timedelta(days=inc["window_days"] - 1)
    for label, dates in groups.items():
        ds = sorted(date.fromisoformat(d) for d in dates if d)
        j = 0
        for i in range(len(ds)):
            while ds[i] - ds[j] > span:
                j += 1
            n = i - j + 1
            if best is None or n > best["n"]:
                best = {"cluster": label, "start": ds[j].isoformat(), "end": ds[i].isoformat(), "n": n}
    if best and best["n"] >= inc["share"] * total:
        best["share"] = round(best["n"] / total, 3)
        return best
    return None
