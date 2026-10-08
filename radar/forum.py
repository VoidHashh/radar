"""D27-D28: foro de la comunidad de Shopify (Discourse) como fuente de necesidades.

Solo endpoints JSON permitidos por robots.txt (sin /search ni login). Datos mínimos por hilo: URL, título, extracto
del primer mensaje, fecha, respuestas, vistas, si está resuelto y etiquetas. Ningún nombre de usuario.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import date, timedelta

from . import db
from .handoff import ImportReport, _step_dir, pending_outputs, validate_classification
from .http import HttpClient
from .pipeline import Progress

log = logging.getLogger(__name__)

BASE = "https://community.shopify.com"
# Categorías de comerciantes (D27). community.shopify.dev es un foro de desarrolladores y queda fuera.
CATEGORIES = [("shopify-apps", 186), ("technical-qa", 211), ("store-design", 133), ("shopify-discussion", 95),
              ("payments-shipping-fulfilment", 217), ("shopify-plus", 88), ("retail-point-of-sale", 180),
              ("shopify-flow", 304), ("accounting-taxes", 223), ("store-feedback", 125)]

SCHEMA = """
CREATE TABLE IF NOT EXISTS forum_topics (
  topic_id INTEGER PRIMARY KEY, site TEXT, category TEXT, url TEXT,
  title TEXT, excerpt TEXT, created_at TEXT,
  reply_count INTEGER, views INTEGER, solved INTEGER, tags TEXT,
  candidate INTEGER, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS forum_llm (
  topic_id INTEGER PRIMARY KEY, asks_for_app INTEGER,
  specific_problem TEXT, feature_requested TEXT, app_category TEXT, batch_id TEXT
);
"""

# Hilos que piden una app que no encuentran o un apaño (D28). Se aplica a título + extracto.
DISCOVERY_RX = re.compile(
    r"(?i)\b(is there (an|any) app|any apps? (that|which|to|for)|an app (that|which|to)|app (that|which) can|"
    r"looking for (an|a) app|recommend (an|a|any) app|app recommendation|how (can|do) (i|we) automatically|"
    r"is there a way to automatically|automatically (add|send|tag|apply|hide|show|sync|update)|workaround|"
    r"without an app|no app (that|does|for)|can'?t find (an|any) app)\b")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def crawl(conn: sqlite3.Connection, http: HttpClient, months: int = 24, categories=CATEGORIES,
          max_pages: int = 400) -> dict:
    """Hilos creados en los últimos `months` meses, por categoría y orden de creación. Reanudable vía caché."""
    ensure_schema(conn)
    since = (date.today() - timedelta(days=round(months * 365 / 12))).isoformat()
    saved = 0
    prog = Progress("Foro: categorías", len(categories), every=1)
    for slug, cid in categories:
        for page in range(max_pages):
            r = http.get(f"{BASE}/c/{slug}/{cid}/l/latest.json?order=created&page={page}")
            if r.status != 200:
                log.warning("%s página %d: estado %s", slug, page, r.status)
                break
            topics = json.loads(r.text)["topic_list"]["topics"]
            if not topics:
                break
            rows = []
            for t in topics:
                if t.get("pinned") or t.get("pinned_globally"):
                    continue
                created = (t.get("created_at") or "")[:10]
                if created < since:
                    continue
                text = f"{t.get('title', '')} {t.get('excerpt', '')}"
                rows.append((t["id"], "community.shopify.com", slug, f"{BASE}/t/{t['slug']}/{t['id']}", t.get("title"),
                             t.get("excerpt"), t.get("created_at"), t.get("reply_count"), t.get("views"),
                             int(bool(t.get("has_accepted_answer"))),
                             json.dumps([x["name"] if isinstance(x, dict) else x for x in t.get("tags") or []]),
                             int(bool(DISCOVERY_RX.search(text))), db.now_iso()))
            conn.executemany("INSERT OR REPLACE INTO forum_topics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
            conn.commit()
            saved += len(rows)
            oldest = min((t.get("created_at") or "9")[:10] for t in topics if not t.get("pinned"))
            if oldest < since:
                break
        prog.tick()
    total, cand = conn.execute("SELECT COUNT(*), SUM(candidate) FROM forum_topics").fetchone()
    return {"since": since, "saved_now": saved, "topics_total": total, "candidates": cand}


# ====================================================================== clasificación (subagentes, sin API)

def classify_export(conn: sqlite3.Connection, cfg: dict, batch_size: int = 200) -> dict:
    ensure_schema(conn)
    d = _step_dir(cfg, "forum_classify", "forum")
    exported = set()
    for f in d.glob("batch_*.in.jsonl"):
        exported |= {json.loads(l)["topic_id"] for l in f.read_text(encoding="utf-8").splitlines() if l}
    rows = conn.execute("SELECT topic_id, category, title, excerpt FROM forum_topics WHERE candidate = 1 AND "
                        "topic_id NOT IN (SELECT topic_id FROM forum_llm) ORDER BY topic_id").fetchall()
    todo = [dict(r) for r in rows if r["topic_id"] not in exported]
    n0 = len(list(d.glob("batch_*.in.jsonl")))
    files = 0
    for i in range(0, len(todo), batch_size):
        (d / f"batch_{n0 + files + 1:04d}.in.jsonl").write_text(
            "\n".join(json.dumps(x, ensure_ascii=False) for x in todo[i:i + batch_size]) + "\n", encoding="utf-8")
        files += 1
    return {"dir": str(d), "new_batches": files, "new_topics": len(todo),
            "pending": len(pending_outputs(d, "batch_*.in.jsonl", ".in.jsonl", ".out.jsonl"))}


def classify_import(conn: sqlite3.Connection, cfg: dict) -> ImportReport:
    ensure_schema(conn)
    d = _step_dir(cfg, "forum_classify", "forum")
    rep = ImportReport()
    for fin in sorted(d.glob("batch_*.in.jsonl")):
        fout = d / fin.name.replace(".in.jsonl", ".out.jsonl")
        if not fout.exists():
            rep.pending.append(fin.name)
            continue
        rep.files += 1
        expected = {json.loads(l)["topic_id"] for l in fin.read_text(encoding="utf-8").splitlines() if l.strip()}
        got = set()
        for n, line in enumerate(fout.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                o = json.loads(line)
                tid = int(o["topic_id"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                rep.errors.append(f"{fout.name}:{n}: {e}")
                continue
            if tid not in expected or not isinstance(o.get("asks_for_app"), bool):
                rep.errors.append(f"{fout.name}:{n}: topic_id desconocido o asks_for_app no booleano")
                continue
            # Mismo esquema de necesidad que las reseñas: specific_problem <= 20 palabras, feature_requested o null.
            row, err = validate_classification({"review_id": str(tid), "specific_problem": o.get("specific_problem"),
                                                "feature_requested": o.get("feature_requested")}, relaxed=True)
            if err:
                rep.errors.append(f"{fout.name}:{n}: {err}")
                continue
            conn.execute("INSERT OR REPLACE INTO forum_llm VALUES (?, ?, ?, ?, ?, ?)",
                         (tid, int(o["asks_for_app"]), row["specific_problem"], row["feature_requested"],
                          o.get("app_category"), fin.name.replace(".in.jsonl", "")))
            got.add(tid)
        rep.imported += len(got)
        rep.missing += len(expected - got)
    conn.commit()
    return rep


# ====================================================================== D28: necesidades nuevas del foro

NEEDS_SCHEMA = """
CREATE TABLE IF NOT EXISTS forum_needs (
  need_id INTEGER PRIMARY KEY, label TEXT, app_category TEXT, search_terms TEXT, topic_ids TEXT,
  n_topics INTEGER, replies INTEGER, views INTEGER
);
CREATE TABLE IF NOT EXISTS forum_need_apps (
  need_id INTEGER PRIMARY KEY, verdict TEXT, apps TEXT, justification TEXT
);
"""


def needs_export(conn: sqlite3.Connection, cfg: dict) -> dict:
    """Todos los hilos que piden una app, en un fichero, para que Claude Code una las peticiones equivalentes."""
    conn.executescript(NEEDS_SCHEMA)
    d = _step_dir(cfg, "forum_needs", "forum")
    rows = [dict(r) for r in conn.execute(
        "SELECT t.topic_id, t.url, t.title, l.specific_problem, l.feature_requested, l.app_category, t.reply_count, "
        "t.views, t.solved FROM forum_topics t JOIN forum_llm l ON l.topic_id = t.topic_id WHERE l.asks_for_app = 1 "
        "ORDER BY l.app_category, t.topic_id")]
    f = d / "part_1.in.json"
    f.write_text(json.dumps({"topics": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"dir": str(d), "topics": len(rows)}


def needs_import(conn: sqlite3.Connection, cfg: dict) -> ImportReport:
    conn.executescript(NEEDS_SCHEMA)
    d = _step_dir(cfg, "forum_needs", "forum")
    rep = ImportReport()
    fin, fout = d / "part_1.in.json", d / "part_1.out.json"
    if not fout.exists():
        rep.pending.append(fin.name)
        return rep
    rep.files = 1
    topics = {t["topic_id"]: t for t in json.loads(fin.read_text(encoding="utf-8"))["topics"]}
    o = json.loads(fout.read_text(encoding="utf-8"))
    conn.execute("DELETE FROM forum_needs")
    seen: set[int] = set()
    for i, n in enumerate(o.get("needs", []), 1):
        ids = [int(x) for x in n.get("topic_ids") or []]
        if not n.get("label") or len(ids) < 2 or set(ids) - set(topics) or seen & set(ids):
            rep.errors.append(f"necesidad {i}: sin etiqueta, < 2 hilos, ids desconocidos o repetidos")
            continue
        seen |= set(ids)
        conn.execute("INSERT INTO forum_needs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (i, n["label"], n.get("app_category"), json.dumps(n.get("search_terms") or []), json.dumps(ids),
                      len(ids), sum(topics[t]["reply_count"] or 0 for t in ids),
                      sum(topics[t]["views"] or 0 for t in ids)))
        rep.imported += 1
    conn.commit()
    return rep


def need_apps_export(conn: sqlite3.Connection, cfg: dict, max_apps: int = 30) -> dict:
    """Para cada necesidad del foro, apps candidatas por sus términos de búsqueda en el índice de tarjetas."""
    d = _step_dir(cfg, "forum_need_apps", "forum")
    cards = [dict(a) for a in conn.execute("SELECT app_id, name, subtitle, review_count, rating, categories FROM app_cards")]
    out = []
    for n in conn.execute("SELECT * FROM forum_needs ORDER BY n_topics DESC").fetchall():
        terms = [t.lower() for t in json.loads(n["search_terms"]) if t.strip()]
        hits = [a for a in cards if any(t in f"{a['name']} {a['subtitle']}".lower() for t in terms)]
        hits.sort(key=lambda a: (n["app_category"] not in (a["categories"] or ""), -a["review_count"]))
        topics = [dict(t) for t in conn.execute(
            f"SELECT topic_id, url, title, excerpt FROM forum_topics WHERE topic_id IN "
            f"({','.join('?' * len(json.loads(n['topic_ids'])))}) LIMIT 4", json.loads(n["topic_ids"]))]
        out.append({"need_id": n["need_id"], "label": n["label"], "app_category": n["app_category"],
                    "n_topics": n["n_topics"], "sample_topics": topics,
                    "app_candidates_total": len(hits), "app_candidates": hits[:max_apps]})
    for i in range(0, len(out), 25):
        (d / f"part_{i // 25 + 1}.in.json").write_text(json.dumps({"needs": out[i:i + 25]}, ensure_ascii=False, indent=1),
                                                     encoding="utf-8")
    return {"dir": str(d), "needs": len(out), "files": -(-len(out) // 25)}


def need_apps_import(conn: sqlite3.Connection, cfg: dict) -> ImportReport:
    conn.executescript(NEEDS_SCHEMA)
    d = _step_dir(cfg, "forum_need_apps", "forum")
    rep = ImportReport()
    for fin in sorted(d.glob("part_*.in.json")):
        fout = d / fin.name.replace(".in.json", ".out.json")
        if not fout.exists():
            rep.pending.append(fin.name)
            continue
        rep.files += 1
        payload = {n["need_id"]: {a["app_id"] for a in n["app_candidates"]}
                   for n in json.loads(fin.read_text(encoding="utf-8"))["needs"]}
        for r in json.loads(fout.read_text(encoding="utf-8")).get("needs", []):
            nid, v = r.get("need_id"), (r.get("verdict") or "").strip().lower()
            apps = r.get("apps") or []
            if nid not in payload or v not in ("hay app", "sin app") or {a.get("app_id") for a in apps} - payload[nid] \
                    or (v == "hay app" and not apps):
                rep.errors.append(f"{fout.name}: need {nid}: veredicto o apps no válidos")
                continue
            conn.execute("INSERT OR REPLACE INTO forum_need_apps VALUES (?, ?, ?, ?)",
                         (nid, v, json.dumps(apps, ensure_ascii=False), r.get("justification") or ""))
            rep.imported += 1
        rep.missing += len(set(payload) - {r.get("need_id") for r in json.loads(fout.read_text(encoding="utf-8")).get("needs", [])})
    conn.commit()
    return rep
