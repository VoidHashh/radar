"""D24-D26: informe de cierre de la Fase 2 (reports/AAAA-SSe.md)."""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

from .close import CHECKS, ensure_schema, filtered_needs, monitoring_run
from .handoff import run_date
from .report import _md, _n, latest, permalink

VERDICT_ICON = {"cubierta": "✔ cubierta", "parcial": "◐ parcial", "no cubierta": "✘ no cubierta"}
CHECK_NAMES = {"ping": "Caída / ping", "cart": "Carrito", "checkout": "Checkout", "app_widgets": "Widgets de apps",
               "order_data": "Datos al pedido"}


def build(conn: sqlite3.Connection, cfg: dict, run_id: str, out_dir: Path, suffix: str = "e") -> Path:
    ensure_schema(conn)
    day = date.fromisoformat(run_date(conn, run_id))
    year, week, _ = day.isocalendar()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{year}-{week:02d}{suffix}.md"
    names = {r[0]: r[1] for r in conn.execute("SELECT app_id, name FROM apps")}
    share = cfg.get("close", {}).get("single_app_share", 0.5)

    kept = filtered_needs(conn, cfg, run_id)
    single = filtered_needs(conn, cfg, run_id, single_app=True)
    s_needs = [n for n in kept if n["buildability"] == "S"]
    verdicts: dict[tuple, list] = {}
    for r in conn.execute("SELECT * FROM need_leaders WHERE run_id = ?", (run_id,)):
        verdicts.setdefault((r["category"], r["label"]), []).append(r)
    uncovered = [n for n in s_needs if verdicts.get((n["category"], n["label"]))
                 and not any(v["verdict"] == "cubierta" for v in verdicts[(n["category"], n["label"])])]
    strict = [n for n in uncovered if all(v["verdict"] == "no cubierta" for v in verdicts[(n["category"], n["label"])])]
    unjudged = [n for n in s_needs if not verdicts.get((n["category"], n["label"]))]

    L = [f"# Cierre de la Fase 2 — semana {year}-{week:02d}{suffix}", "",
         f"Ejecución `{run_id}`. Parte del material de `{year}-{week:02d}d` (D22-D23) y aplica D24-D26.", "",
         "## (a) Necesidades S no cubiertas por los líderes", "",
         f"- D24: de las {len(kept) + len(single)} necesidades que pasaban los filtros de D23, {len(single)} se apartan "
         f"como \"problemas de una app\" porque una sola app reúne el {int(share * 100)} % o más de sus reseñas. "
         f"Quedan {len(kept)}, de ellas {len(s_needs)} con construibilidad S.",
         "- D25: para cada categoría, las 3 apps con más reseñas del listado. Claude Code leyó su ficha completa "
         "(lema, descripción, funciones, etiquetas y planes) y juzgó cada necesidad: cubierta, parcial o no cubierta, "
         "citando la ficha.",
         f"- **No cubierta** = ningún líder la cubre del todo. {len(uncovered)} de {len(s_needs)}. En {len(strict)} "
         "de ellas, además, ninguno de los tres la menciona siquiera de forma parcial.", ""]
    if unjudged:
        L += [f"Sin juicio de líderes (no se pudo evaluar): {len(unjudged)}.", ""]
    for i, n in enumerate(uncovered, 1):
        ids = json.loads(n["review_ids"])
        ex = ", ".join(f"[{k}]({permalink(r)})" for k, r in enumerate(latest(conn, ids, 3), 1))
        L += [f"### {i}. {n['label']}", "",
              f"{_n(n['n_reviews'], 'reseña', 'reseñas')} en {_n(n['n_apps'], 'app', 'apps')} de `{n['category']}` · "
              f"app con más reseñas: {names.get(n['top_app'], n['top_app'])} ({int(100 * n['top_share'])} %) · "
              f"ejemplos: {ex}", "",
              f"**Construibilidad S.** {n['buildability_reason']}", "",
              "| Líder | Reseñas | Veredicto |", "|---|---|---|"]   # D34: sin citas de la ficha
        for v in sorted(verdicts[(n["category"], n["label"])], key=lambda v: v["app_id"]):
            rc = conn.execute("SELECT MAX(review_count) FROM app_listings WHERE run_id = ? AND app_id = ?",
                              (run_id, v["app_id"])).fetchone()[0]
            L.append(f"| {_md(names.get(v['app_id'], v['app_id']))} | {rc} | {VERDICT_ICON[v['verdict']]} |")
        L.append("")
    covered = [n for n in s_needs if n not in uncovered and n not in unjudged]
    if covered:
        L += ["**Necesidades S que algún líder ya cubre** (descartadas):", ""]
        for n in covered:
            who = [names.get(v["app_id"], v["app_id"]) for v in verdicts[(n["category"], n["label"])]
                   if v["verdict"] == "cubierta"]
            L.append(f"- {n['label']} ({n['n_reviews']} reseñas): cubierta por {', '.join(who)}.")
        L.append("")
    L += [f"**Problemas de una app** (D24, {len(single)}): la necesidad aparece en 3 o más apps, pero una sola reúne "
          f"el {int(share * 100)} % o más de las reseñas.", "",
          "| Necesidad | Reseñas | App dominante | % | Construibilidad |", "|---|---|---|---|---|"]
    for n in single:
        L.append(f"| {_md(n['label'])} | {n['n_reviews']} | {_md(names.get(n['top_app'], n['top_app']))} | "
                 f"{int(100 * n['top_share'])} % | {n['buildability']} |")
    L.append("")

    # ------------------------------------------------------------------ (b) monitorización
    mrun = monitoring_run(conn)
    rows = conn.execute(
        "SELECT m.*, a.name, a.url, a.pricing, s.review_count, s.rating_shown, s.rating_computed "
        "FROM monitoring_apps m JOIN apps a ON a.app_id = m.app_id "
        "JOIN app_snapshots s ON s.app_id = m.app_id AND s.run_id = m.run_id WHERE m.run_id = ? "
        "ORDER BY m.in_niche DESC, s.review_count DESC", (mrun,)).fetchall()
    in_niche = [r for r in rows if r["in_niche"]]
    neg = {r[0]: r[1] for r in conn.execute(
        "SELECT r.app_id, COUNT(*) FROM reviews r JOIN monitoring_apps m ON m.app_id = r.app_id AND m.run_id = ? "
        "WHERE r.rating <= 3 GROUP BY r.app_id", (mrun,))}
    L += ["## (b) Mapa de competidores de monitorización", "",
          f"Candidatas buscadas en los 27.662 listados por nombre y descripción (sin `q=`): {len(rows)} revisadas, "
          f"{len(in_niche)} son del nicho (vigilan que la tienda o sus apps funcionen). Entre las {len(rows)} suman "
          f"{sum(r['review_count'] for r in rows)} reseñas y {sum(neg.values())} de 1-3★: "
          + ("se clasificaron con el mismo método que el resto." if sum(neg.values()) else
             "no hay ninguna que clasificar, así que los puntos débiles salen de lo que la ficha no cubre.") + "", "",
          "| App | Reseñas | Nota | Primer plan de la ficha | Lanzada | " + " | ".join(CHECK_NAMES.values()) + " | 1-3★ |",
          "|---|---|---|---|---|" + "---|" * len(CHECKS) + "---|"]
    for r in in_niche:
        checks = json.loads(r["checks"])
        pricing = json.loads(r["pricing"] or "[]")
        cheapest = next((p["price"] for p in pricing if p.get("price")), "-")
        launch = _launched(conn, r["app_id"])
        cells = " | ".join({"sí": "✔", "no": "✘", "no consta": "?"}[checks[c]["value"]] for c in CHECKS)
        L.append(f"| [{_md(r['name'])}]({r['url']}) | {r['review_count']} | "
                 f"{r['rating_shown'] if r['rating_shown'] is not None else '-'} | {_md(cheapest)} | {launch or '-'} | "
                 f"{cells} | {neg.get(r['app_id'], 0)} |")
    L += ["", "✔ la ficha lo dice · ✘ la ficha dice que no · ? no consta en la ficha.", ""]
    for r in in_niche:
        checks = json.loads(r["checks"])
        L += [f"### {r['name']}", "", r["summary"] or "", ""]
        if r["other_checks"]:
            L += [f"Otras comprobaciones: {r['other_checks']}", ""]
        L += ["Puntos débiles:", ""] + [f"- {w}" for w in json.loads(r["weaknesses"])] + [""]
    out = [r for r in rows if not r["in_niche"]]
    if out:
        L += ["Revisadas y fuera del nicho: " + ", ".join(f"{r['name']} ({r['summary'][:80]})" for r in out) + ".", ""]

    L += ["---", "", f"**Necesidades S no cubiertas: {len(uncovered)}**", ""]
    path.write_text("\n".join(L), encoding="utf-8")
    return path


def _launched(conn: sqlite3.Connection, app_id: str) -> str | None:
    """Fecha de lanzamiento de la ficha cacheada, si la hay (no se guarda en la base de datos)."""
    from .config import load_config
    from .http import HttpClient
    from .stores.shopify import ShopifyAdapter
    cfg = load_config()
    http = HttpClient.from_config(cfg)
    cached = http._read_cache(f"{cfg['store']['base_url']}/{app_id}")
    if cached is None:
        return None
    from .stores.shopify import parse_app_page
    return parse_app_page(cached.text, app_id).launched
