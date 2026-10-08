"""D27-D30: informe de la Fase 2b (reports/AAAA-SSf.md)."""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

from .close import filtered_needs
from .handoff import run_date
from .report import _md

VERDICT_ORDER = {"hueco real": 0, "evidencia débil": 1, "cubierta por una app especializada": 2}


def _forum_stats(conn: sqlite3.Connection, ids: list[int]) -> tuple[int, int, int, list]:
    if not ids:
        return 0, 0, 0, []
    rows = conn.execute(f"SELECT url, title, reply_count, views FROM forum_topics WHERE topic_id IN "
                        f"({','.join('?' * len(ids))}) ORDER BY views DESC", ids).fetchall()
    return len(rows), sum(r["reply_count"] or 0 for r in rows), sum(r["views"] or 0 for r in rows), rows


def _links(rows, n: int = 3) -> str:
    return ", ".join(f"[{k}]({r['url']})" for k, r in enumerate(rows[:n], 1)) or "—"


def build(conn: sqlite3.Connection, cfg: dict, run_id: str, out_dir: Path, suffix: str = "f") -> Path:
    day = date.fromisoformat(run_date(conn, run_id))
    year, week, _ = day.isocalendar()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{year}-{week:02d}{suffix}.md"
    names = {r[0]: r[1] for r in conn.execute("SELECT app_id, name FROM app_cards")}
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    n_topics = q("SELECT COUNT(*) FROM forum_topics")
    n_cand = q("SELECT COUNT(*) FROM forum_topics WHERE candidate = 1")
    n_asks = q("SELECT COUNT(*) FROM forum_llm WHERE asks_for_app = 1")

    L = [f"# Fase 2b — semana {year}-{week:02d}{suffix}", "",
         "Vía 2 (foro de la comunidad) y vía 1 (necesidades S no cubiertas). Decisiones D27-D30 en `docs/decisions.md`.", "",
         f"Foro `community.shopify.com`, últimos 24 meses, 10 categorías de comerciantes: {n_topics} hilos. "
         f"{n_cand} coinciden con los patrones de \"busco una app\" y Claude Code confirmó {n_asks}. "
         "Sin búsqueda (`/search` está prohibido en robots.txt) y sin nombres de usuario.", ""]

    # ------------------------------------------------------------------ (a)
    rows = conn.execute("SELECT v.*, n.label, n.category, n.n_reviews, n.n_apps FROM via1 v "
                        "JOIN needs n ON n.rowid = v.need_id WHERE v.run_id = ?", (run_id,)).fetchall()
    rows = sorted(rows, key=lambda r: (VERDICT_ORDER.get(r["verdict"], 9), -r["n_reviews"]))
    L += ["## (a) Las 12 necesidades S no cubiertas por los líderes", "",
          "La de personalización (textos y archivos que llegan al pedido) se analizó aparte y no está en la tabla.", "",
          "| Necesidad | Reseñas / apps | Hilos | Respuestas | Vistas | Apps especializadas | Veredicto | Ejemplos |",
          "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        nt, rep, views, trows = _forum_stats(conn, json.loads(r["forum_topic_ids"]))
        spec = json.loads(r["specialized_apps"])
        spec_txt = "; ".join(_md(names.get(s["app_id"], s["app_id"])) for s in spec) or "ninguna"
        L.append(f"| {_md(r['label'])} | {r['n_reviews']} / {r['n_apps']} | {nt} | {rep} | {views} | {spec_txt} | "
                 f"**{r['verdict']}** | {_links(trows)} |")
    L.append("")
    for r in rows:
        spec = json.loads(r["specialized_apps"])
        L += [f"- **{r['label']}** ({r['verdict']}): {r['justification']}"]
        for s in spec:
            L.append(f"  - {names.get(s['app_id'], s['app_id'])}: {s.get('why', '')}")
    L.append("")

    # ------------------------------------------------------------------ (b)
    fneeds = conn.execute("SELECT f.*, a.verdict, a.apps, a.justification FROM forum_needs f "
                          "LEFT JOIN forum_need_apps a ON a.need_id = f.need_id ORDER BY f.n_topics DESC, f.views DESC"
                          ).fetchall()
    sin_app = [f for f in fneeds if f["verdict"] == "sin app"]
    hay_app = [f for f in fneeds if f["verdict"] == "hay app"]
    L += ["## (b) Necesidades nuevas del foro sin app que las resuelva", "",
          f"Claude Code agrupó los hilos que piden una app en {len(fneeds)} necesidades con 2 o más hilos. Para cada una "
          "buscó apps candidatas en los nombres y descripciones de las 27.662 tarjetas de la App Store. "
          f"En {len(sin_app)} ninguna lo promete explícitamente; en {len(hay_app)} sí hay app.", "",
          "| Necesidad | Hilos | Respuestas | Vistas | Categoría | Por qué no hay app | Ejemplos |", "|---|---|---|---|---|---|---|"]
    for f in sin_app:
        _, _, _, trows = _forum_stats(conn, json.loads(f["topic_ids"]))
        L.append(f"| {_md(f['label'])} | {f['n_topics']} | {f['replies']} | {f['views']} | {f['app_category'] or '-'} | "
                 f"{_md(f['justification'])} | {_links(trows)} |")
    L.append("")
    if hay_app:
        L += ["Necesidades del foro que ya resuelve alguna app (descartadas):", ""]
        for f in hay_app:
            apps = json.loads(f["apps"])
            L.append(f"- {f['label']} ({f['n_topics']} hilos): "
                     + "; ".join(names.get(a['app_id'], a['app_id']) for a in apps[:2]))   # D34: sin citas
        L.append("")

    # ------------------------------------------------------------------ (c)
    m = filtered_needs(conn, cfg, run_id, buildability="M")
    verdicts: dict[tuple, list] = {}
    for v in conn.execute("SELECT * FROM need_leaders WHERE run_id = ?", (run_id,)):
        verdicts.setdefault((v["category"], v["label"]), []).append(v)
    unc = [n for n in m if verdicts.get((n["category"], n["label"]))
           and not any(v["verdict"] == "cubierta" for v in verdicts[(n["category"], n["label"])])]
    L += ["## (c) Necesidades M no cubiertas por los líderes (D30, sin análisis profundo)", "",
          f"Filtro D25 aplicado a las {len(m)} necesidades M (≥ 10 reseñas, ≥ 3 apps, sin regulación ni concentración "
          f"en una app). Ningún líder cubre del todo {len(unc)}.", "",
          "| Necesidad | Reseñas | Apps | Categoría | Líderes (parcial / no cubierta) |", "|---|---|---|---|---|"]
    for n in unc:
        vs = verdicts[(n["category"], n["label"])]
        L.append(f"| {_md(n['label'])} | {n['n_reviews']} | {n['n_apps']} | {n['category']} | "
                 f"{sum(v['verdict'] == 'parcial' for v in vs)} / {sum(v['verdict'] == 'no cubierta' for v in vs)} |")
    L.append("")
    huecos = sum(1 for r in rows if r["verdict"] == "hueco real")
    L += ["---", "", f"**Necesidades con veredicto \"hueco real\": {huecos}**", ""]
    path.write_text("\n".join(L), encoding="utf-8")
    return path
