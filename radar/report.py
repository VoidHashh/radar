"""Paso 9: informe semanal en Markdown y CSV."""
from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path

from .competition import app_competition, category_stats
from .handoff import run_date
from .score import COMPONENTS

BASE = "https://apps.shopify.com"


def permalink(review_id: str) -> str:
    return f"{BASE}/reviews/{review_id}"


def _n(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def _md(text: str | None) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{100 * x:.0f} %"


SELECT_OPP = (
    "SELECT sc.*, a.name, a.url, a.developer, s.review_count, s.rating_shown, s.rating_computed, s.n_1, s.n_2, "
    "w.total_12m, w.negatives_12m, w.method, a.categories FROM scores sc JOIN apps a ON a.app_id = sc.app_id "
    "JOIN app_snapshots s ON s.app_id = sc.app_id AND s.run_id = sc.run_id "
    "JOIN app_windows w ON w.app_id = sc.app_id AND w.run_id = sc.run_id ")


def opportunities(conn: sqlite3.Connection, run_id: str, top_n: int) -> list[sqlite3.Row]:
    """Ranking de oportunidades, sin apps de plataforma (D17) ni con incidente puntual (D21)."""
    return conn.execute(SELECT_OPP + "WHERE sc.run_id = ? AND COALESCE(sc.platform, 0) = 0 AND sc.incident IS NULL "
                        "ORDER BY sc.total DESC LIMIT ?", (run_id, top_n)).fetchall()


def platform_apps(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute(SELECT_OPP + "WHERE sc.run_id = ? AND (sc.platform = 1 OR sc.incident IS NOT NULL) "
                        "ORDER BY sc.platform DESC, sc.total DESC",
                        (run_id,)).fetchall()


def details(conn: sqlite3.Connection, app_id: str, run_id: str, examples: int) -> dict:
    clusters = conn.execute("SELECT * FROM clusters WHERE app_id = ? AND run_id = ? ORDER BY n_reviews DESC",
                            (app_id, run_id)).fetchall()
    llm = conn.execute("SELECT l.* FROM review_llm l JOIN reviews r ON r.review_id = l.review_id WHERE r.app_id = ?",
                       (app_id,)).fetchall()
    features = Counter(x["feature_requested"].strip() for x in llm if x["feature_requested"])
    alts = Counter(x["alternative_mentioned"].strip() for x in llm if x["alternative_mentioned"])
    out_clusters = []
    for c in clusters:
        ids = json.loads(c["review_ids"])
        out_clusters.append({"label": c["cluster_label"], "n": c["n_reviews"],
                             "examples": latest(conn, ids, examples),
                             "buildability": c["buildability"], "reason": c["buildability_reason"]})
    return {"clusters": out_clusters, "features": features.most_common(8), "alternatives": alts.most_common(8),
            "classified": len(llm)}


def latest(conn: sqlite3.Connection, review_ids: list[str], n: int) -> list[str]:
    """Las `n` reseñas más recientes de la lista (ejemplos con enlace)."""
    if not review_ids:
        return []
    rows = conn.execute(f"SELECT review_id FROM reviews WHERE review_id IN ({','.join('?' * len(review_ids))}) "
                        f"ORDER BY date_shown DESC LIMIT ?", (*review_ids, n)).fetchall()
    return [r[0] for r in rows]


def needs(conn: sqlite3.Connection, cfg: dict, run_id: str) -> list[sqlite3.Row]:
    """Necesidades no cubiertas que pasan los filtros (D20), de más a menos reseñas."""
    nc = cfg["needs"]
    return conn.execute("SELECT * FROM needs WHERE run_id = ? AND scope IS NULL AND n_reviews >= ? AND n_apps >= ? "
                        "ORDER BY n_reviews DESC, n_apps DESC LIMIT ?",
                        (run_id, nc["min_reviews"], nc["min_apps"], nc["top_n"])).fetchall()


def stats(conn: sqlite3.Connection, run_id: str) -> dict:
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    return {
        "catalog": q("SELECT COUNT(*) FROM apps"),
        "fichas": q("SELECT COUNT(*) FROM app_snapshots WHERE run_id = ?", run_id),
        "preselected": q("SELECT COUNT(*) FROM app_windows WHERE run_id = ?", run_id),
        "platforms": q("SELECT COUNT(*) FROM scores WHERE run_id = ? AND platform = 1", run_id),
        "incidents": q("SELECT COUNT(*) FROM scores WHERE run_id = ? AND incident IS NOT NULL", run_id),
        "reviews": q("SELECT COUNT(*) FROM reviews r JOIN app_windows w ON w.app_id = r.app_id AND w.run_id = ?", run_id),
        "negatives": q("SELECT COUNT(*) FROM reviews r JOIN app_windows w ON w.app_id = r.app_id AND w.run_id = ? "
                       "WHERE r.rating <= 2", run_id),
        "negatives_empty": q("SELECT COUNT(*) FROM reviews r JOIN app_windows w ON w.app_id = r.app_id AND w.run_id = ? "
                             "WHERE r.rating <= 2 AND TRIM(COALESCE(r.body, '')) = ''", run_id),
        "classified": q("SELECT COUNT(*) FROM review_llm l JOIN reviews r ON r.review_id = l.review_id "
                        "JOIN app_windows w ON w.app_id = r.app_id AND w.run_id = ?", run_id),
        "clustered_apps": q("SELECT COUNT(DISTINCT app_id) FROM clusters WHERE run_id = ?", run_id),
        "replicables": q("SELECT COUNT(*) FROM replicables WHERE run_id = ?", run_id),
        "needs_all": q("SELECT COUNT(*) FROM needs WHERE run_id = ? AND scope IS NULL", run_id),
        "languages": conn.execute("SELECT language, COUNT(*) FROM review_llm l JOIN reviews r ON r.review_id = l.review_id "
                                  "JOIN app_windows w ON w.app_id = r.app_id AND w.run_id = ? GROUP BY language "
                                  "ORDER BY 2 DESC LIMIT 6", (run_id,)).fetchall(),
    }


def build(conn: sqlite3.Connection, cfg: dict, run_id: str, out_dir: Path,
          link_checks: dict[str, int] | None = None, suffix: str = "") -> tuple[Path, Path, Path]:
    rcfg = cfg["report"]
    day = date.fromisoformat(run_date(conn, run_id))
    year, week, _ = day.isocalendar()
    stem = f"{year}-{week:02d}{suffix}"
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path, csv_path, rep_csv = out_dir / f"{stem}.md", out_dir / f"{stem}.csv", out_dir / f"{stem}-replicables.csv"
    needs_csv = out_dir / f"{stem}-needs.csv"

    opps = opportunities(conn, run_id, rcfg["top_n"])
    st = stats(conn, run_id)
    comp_stats = category_stats(conn, run_id, cfg) if "scoring" in cfg else {}
    sc = cfg.get("scoring", {})
    cc = sc.get("competition", {})
    strong_min = cc.get("strong_min_reviews", 100)
    low_n = sc.get("low_sample_negatives", 0)
    notes = json.loads(conn.execute("SELECT notes FROM runs WHERE run_id = ?", (run_id,)).fetchone()[0] or "{}")
    p0 = notes.get("scoring_p0", {})
    names = {r[0]: r[1] for r in conn.execute("SELECT app_id, name FROM apps")}
    nds = needs(conn, cfg, run_id) if "needs" in cfg else []

    def flag(o) -> str:
        out = " ⚠ muestra baja" if o["low_sample"] else ""
        if o["incident"]:
            out += " ⚡ incidente puntual"
        return out

    reps = conn.execute("SELECT r.*, a.name, a.url, s.review_count, s.rating_computed FROM replicables r "
                        "JOIN apps a ON a.app_id = r.app_id JOIN app_snapshots s ON s.app_id = r.app_id AND s.run_id = r.run_id "
                        "WHERE r.run_id = ? ORDER BY r.total DESC LIMIT ?", (run_id, rcfg["replicables_top_n"])).fetchall()
    L: list[str] = []
    L += [f"# Radar Shopify App Store — semana {year}-{week:02d}{suffix}", "",
          f"Ejecución `{run_id}` del {day.isoformat()}. Ventana de reseñas: 365 días.", "",
          "## Datos de esta ejecución", "",
          "| Medida | Valor |", "|---|---|",
          f"| Apps en el catálogo | {st['catalog']} |",
          f"| Fichas descargadas (≥ 50 reseñas en el listado) | {st['fichas']} |",
          f"| Apps preseleccionadas | {st['preselected']} (de plataforma, fuera del ranking: {st['platforms']}) |",
          f"| Incidentes puntuales detectados | {st['incidents']} |",
          f"| Reseñas descargadas | {st['reviews']} |",
          f"| Reseñas de 1-2★ | {st['negatives']} (sin texto: {st['negatives_empty']}) |",
          f"| Reseñas clasificadas por Claude Code | {st['classified']} |",
          f"| Apps agrupadas por Claude Code | {st['clustered_apps']} |",
          f"| Necesidades identificadas por Claude Code | {st['needs_all']} (pasan los filtros: {len(nds)}) |",
          f"| Apps replicables evaluadas por Claude Code | {st['replicables']} |",
          "", "Idiomas de las negativas clasificadas: "
          + ", ".join(f"{lang} {n}" for lang, n in st["languages"]) + ".", ""]

    # ------------------------------------------------------------------ necesidades no cubiertas (D20)
    if "needs" in cfg:
        nc = cfg["needs"]
        L += [f"## Necesidades no cubiertas (top {len(nds)})", "",
              f"Problemas o funciones que se repiten en apps distintas de una misma categoría. Filtros: "
              f"{nc['min_reviews']} o más reseñas, {nc['min_apps']} o más apps, sin plataformas ni incidentes puntuales. "
              "Construibilidad de una app independiente que haga solo eso.", "",
              "| # | Necesidad | Reseñas | Apps | Categoría | Construibilidad |", "|---|---|---|---|---|---|"]
        for i, n in enumerate(nds, 1):
            L.append(f"| {i} | {_md(n['label'])} | {n['n_reviews']} | {n['n_apps']} | {n['category']} | "
                     f"{n['buildability']} |")
        L.append("")
        for i, n in enumerate(nds, 1):
            ids = json.loads(n["review_ids"])
            ex = ", ".join(f"[{k}]({permalink(r)})" for k, r in enumerate(latest(conn, ids, nc["examples"]), 1))
            apps = ", ".join(names.get(a, a) for a in json.loads(n["app_ids"]))
            L += [f"### N{i}. {n['label']}", "",
                  f"{_n(n['n_reviews'], 'reseña', 'reseñas')} en {_n(n['n_apps'], 'app', 'apps')} de `{n['category']}`: "
                  f"{apps}. Ejemplos: {ex}", "",
                  f"**Construibilidad: {n['buildability']}.** {n['buildability_reason']}", ""]

    # ------------------------------------------------------------------ oportunidades por quejas
    L += [f"## Top {len(opps)} oportunidades por quejas", "",
          f"Sin apps de plataforma, canal o pago (D17; aparecen al final como contexto). "
          f"⚠ muestra baja = menos de {low_n} negativas en 12 meses (se marcan, no se excluyen). "
          "Las apps con incidente puntual (la mayoría de las negativas son un mismo problema en pocas semanas) "
          "también salen del ranking y van a contexto (D21).", "",
          "| # | App | Reseñas | Nota | % 1-2★ | Neg. 12m | Total | " + " | ".join(COMPONENTS) + " |",
          "|---|---|---|---|---|---|---|" + "---|" * len(COMPONENTS)]
    for i, o in enumerate(opps, 1):
        pct = 100 * (o["n_1"] + o["n_2"]) / o["review_count"] if o["review_count"] else 0
        L.append(f"| {i} | [{_md(o['name'])}]({o['url']}){flag(o)} | {o['review_count']} | {o['rating_computed']:.2f} | "
                 f"{pct:.1f} | {o['negatives_12m']} | **{o['total']:.3f}** | "
                 + " | ".join(f"{o[c]:.2f}" for c in COMPONENTS) + " |")
    L.append("")

    for i, o in enumerate(opps, 1):
        d = details(conn, o["app_id"], run_id, rcfg["examples_per_cluster"])
        comp = app_competition(o["app_id"], o["categories"], comp_stats) if comp_stats else None
        L += [f"### {i}. {o['name']}{flag(o)}", "",
              f"[Ficha]({o['url']}) · {_n(o['review_count'], 'reseña', 'reseñas')} · nota calculada {o['rating_computed']:.2f} · "
              f"últimos 12 meses: {_n(o['negatives_12m'], 'negativa', 'negativas')} de {o['total_12m']} ({o['method']}) · "
              f"{_n(d['classified'], 'negativa clasificada', 'negativas clasificadas')}", ""]
        if o["incident"]:
            inc = json.loads(o["incident"])
            L += [f"**Incidente puntual:** {inc['n']} negativas ({_pct(inc['share'])} de las de 12 meses) entre "
                  f"{inc['start']} y {inc['end']}, del grupo \"{inc['cluster']}\".", ""]
        if comp and comp["category"]:
            L += [f"**Competencia** en `{comp['category']}`: {comp['n_apps']} apps, {comp['strong_competitors']} "
                  f"competidores con {strong_min} o más reseñas; líder {names.get(comp['leader'], comp['leader'])} "
                  f"con el {_pct(comp['leader_share'])} de las reseñas de la categoría (puntuación {comp['competition']:.2f}).", ""]
        main = next((c for c in d["clusters"] if c["buildability"]), None)
        if main:
            L += [f"**Construibilidad del problema principal: {main['buildability']}.** {main['reason']}", ""]
        if d["clusters"]:
            L += ["**Grupos de quejas**", ""]
            for c in d["clusters"]:
                links = ", ".join(f"[{k}]({permalink(r)})" for k, r in enumerate(c["examples"], 1))
                L.append(f"- {c['label']}: {_n(c['n'], 'reseña', 'reseñas')}. Ejemplos: {links}")
            L.append("")
        if d["features"]:
            L += ["**Funciones pedidas:** " + "; ".join(f"{f} ({n})" for f, n in d["features"]), ""]
        if d["alternatives"]:
            L += ["**Alternativas mencionadas:** " + "; ".join(f"{a} ({n})" for a, n in d["alternatives"]), ""]
        if link_checks is not None and o["app_id"] in link_checks:
            L += [f"Enlace de ejemplo comprobado: HTTP {link_checks[o['app_id']]}.", ""]

    # ------------------------------------------------------------------ replicables
    L += ["## Apps muy usadas que se pueden replicar o mejorar", "",
          "Sección aparte (D12): apps con muchas reseñas y planes de pago, evaluadas por Claude Code a partir de su ficha.",
          f"Competidores: otras apps de su categoría principal con {strong_min} o más reseñas. "
          "Cuota del líder: reseñas de la app líder / reseñas totales de la categoría (D19).", "",
          "| # | App | Reseñas | Nota | Plan de pago más barato | Construibilidad | Categoría | Competidores "
          f"(≥ {strong_min}) | Líder | Cuota del líder | competition | Total |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(reps, 1):
        strong = "-" if r["strong_competitors"] is None else r["strong_competitors"]
        leader = "esta app" if r["leader_app"] == r["app_id"] else _md(names.get(r["leader_app"], r["leader_app"] or "-"))
        L.append(f"| {i} | [{_md(r['name'])}]({r['url']}) | {r['review_count']} | {r['rating_computed']:.2f} | "
                 f"${r['min_paid_usd_month']:.0f}/mes | {r['buildability']} | {r['category'] or '-'} | {strong} | "
                 f"{leader} | {_pct(r['leader_share'])} | {(r['competition'] or 0):.2f} | **{r['total']:.3f}** |")
    L.append("")
    for i, r in enumerate(reps, 1):
        L += [f"### R{i}. {r['name']}", "", f"**Núcleo:** {r['core']}", "",
              f"**Construibilidad: {r['buildability']}.** {r['buildability_reason']}", "", "**Qué se mejoraría:**", ""]
        L += [f"- {x}" for x in json.loads(r["improvements"])]
        L.append("")

    # ------------------------------------------------------------------ contexto: plataformas (D17)
    plat = platform_apps(conn, run_id) if "platforms" in cfg else []
    if plat:
        L += ["## Contexto: plataformas e incidentes puntuales", "",
              "Fuera del ranking. Plataformas (D17): apps de Shopify, Meta, Google, Amazon y similares, con las que una app "
              "pequeña no compite. Incidentes (D21): apps cuyas quejas se concentran en un único problema de pocas semanas.", "",
              "| App | Motivo | Desarrollador | Reseñas | Nota | Neg. 12m | Grupo de quejas principal |",
              "|---|---|---|---|---|---|---|"]
        for o in plat:
            d = details(conn, o["app_id"], run_id, 1)
            main = d["clusters"][0]["label"] if d["clusters"] else "-"
            why = "plataforma" if o["platform"] else "incidente puntual"
            L.append(f"| [{_md(o['name'])}]({o['url']}) | {why} | {_md(o['developer'])} | {o['review_count']} | "
                     f"{o['rating_computed']:.2f} | {o['negatives_12m']} | {_md(main)} |")
        L.append("")

    # ------------------------------------------------------------------ método
    inc = cfg.get("incident", {})
    L += ["## Método", "",
          "- Puntuación: componentes de 0 a 1 y pesos en `config.yaml` (SPEC.md, paso 8; D13-D20 en docs/decisions.md).",
          "- recent_pain, recurrence y neglect con media bayesiana (x·n + p0·m)/(n + m), m = "
          f"{sc.get('bayes_m', '-')}; p0 de esta ejecución: " + ", ".join(f"{k} {v:.3f}" for k, v in p0.items()) + ".",
          "- volume = log10(1 + negativas en 12 meses), normalizado por el máximo de la ejecución.",
          f"- competition = 1 - presión de la categoría principal; presión = {cc.get('strong')}·norm(log10(1 + apps con "
          f"≥ {strong_min} reseñas)) + {cc.get('leader_share')}·cuota del líder, con los listados ya descargados.",
          f"- Incidente puntual: ≥ {_pct(inc.get('share'))} de las negativas de 12 meses en {inc.get('window_days')} días "
          f"y del mismo grupo de quejas, con al menos {inc.get('min_negatives')} negativas.",
          "- La clasificación, la agrupación, las necesidades y las replicables las hace Claude Code (D10), "
          "con las instrucciones de `docs/prompts/`.",
          "- Las reseñas sin texto cuentan en las proporciones, pero no se clasifican (D7).",
          "- Las fechas de las reseñas editadas son las de edición (docs/recon.md, 4.3).", ""]
    md_path.write_text("\n".join(L), encoding="utf-8")

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["rank", "app_id", "name", "url", "review_count", "rating_shown", "rating_computed", "pct_1_2",
                     "total_12m", "negatives_12m", "low_sample", "incident", "method", "total", *COMPONENTS])
        for i, o in enumerate(opps, 1):
            pct = 100 * (o["n_1"] + o["n_2"]) / o["review_count"] if o["review_count"] else 0
            wr.writerow([i, o["app_id"], o["name"], o["url"], o["review_count"], o["rating_shown"], o["rating_computed"],
                         round(pct, 2), o["total_12m"], o["negatives_12m"], o["low_sample"], int(bool(o["incident"])),
                         o["method"], o["total"], *(o[c] for c in COMPONENTS)])
    with open(rep_csv, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["rank", "app_id", "name", "url", "review_count", "rating_computed", "min_paid_usd_month",
                     "buildability", "category", "strong_competitors", "leader_app", "leader_share", "competition",
                     "demand", "price", "total", "core", "improvements"])
        for i, r in enumerate(reps, 1):
            wr.writerow([i, r["app_id"], r["name"], r["url"], r["review_count"], r["rating_computed"],
                         r["min_paid_usd_month"], r["buildability"], r["category"], r["strong_competitors"],
                         r["leader_app"], r["leader_share"], r["competition"], r["demand"], r["price"], r["total"],
                         r["core"], "; ".join(json.loads(r["improvements"]))])
    if "needs" in cfg:
        with open(needs_csv, "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(["rank", "label", "n_reviews", "n_apps", "category", "buildability", "buildability_reason",
                         "app_ids", "review_ids"])
            for i, n in enumerate(nds, 1):
                wr.writerow([i, n["label"], n["n_reviews"], n["n_apps"], n["category"], n["buildability"],
                             n["buildability_reason"], " ".join(json.loads(n["app_ids"])),
                             " ".join(json.loads(n["review_ids"]))])
    return md_path, csv_path, rep_csv


def example_links(conn: sqlite3.Connection, run_id: str, top_n: int) -> dict[str, str]:
    """Un enlace de ejemplo por oportunidad (el más reciente del grupo principal) para comprobarlo."""
    out = {}
    for o in opportunities(conn, run_id, top_n):
        d = details(conn, o["app_id"], run_id, 1)
        if d["clusters"] and d["clusters"][0]["examples"]:
            out[o["app_id"]] = permalink(d["clusters"][0]["examples"][0])
    return out


def build_needs(conn: sqlite3.Connection, cfg: dict, run_id: str, out_dir: Path, suffix: str = "d") -> tuple[Path, Path]:
    """D23: informe solo con "Necesidades no cubiertas" sobre el material ampliado (scope = 'wide')."""
    nw = cfg["needs_wide"]
    day = date.fromisoformat(run_date(conn, run_id))
    year, week, _ = day.isocalendar()
    stem = f"{year}-{week:02d}{suffix}"
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path, csv_path = out_dir / f"{stem}.md", out_dir / f"{stem}-needs.csv"
    names = {r[0]: r[1] for r in conn.execute("SELECT app_id, name FROM apps")}
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    all_n = q("SELECT COUNT(*) FROM needs WHERE run_id = ? AND scope = 'wide'", run_id)
    reg_n = q("SELECT COUNT(*) FROM needs WHERE run_id = ? AND scope = 'wide' AND regulated = 1 "
              "AND n_reviews >= ? AND n_apps >= ?", run_id, nw["min_reviews"], nw["min_apps"])
    rows = conn.execute("SELECT * FROM needs WHERE run_id = ? AND scope = 'wide' AND n_reviews >= ? AND n_apps >= ? "
                        + ("AND COALESCE(regulated, 0) = 0 " if nw.get("exclude_regulated") else "")
                        + "ORDER BY n_reviews DESC, n_apps DESC", (run_id, nw["min_reviews"], nw["min_apps"])).fetchall()
    crawl = conn.execute("SELECT COUNT(DISTINCT app_id), SUM(n_reviews) FROM needs_crawl WHERE run_id = ?",
                         (run_id,)).fetchone()
    L = [f"# Necesidades no cubiertas — semana {year}-{week:02d}{suffix}", "",
         f"Ejecución `{run_id}`. Material (D22): reseñas de 1, 2 y 3 estrellas de los últimos {nw['months']} meses de "
         f"{crawl[0]} apps con 50 o más reseñas ({crawl[1]} reseñas descargadas), sin plataformas ni incidentes "
         "puntuales. Claude Code clasificó las reseñas y unió las necesidades equivalentes de apps distintas de una "
         "misma categoría.", "",
         f"Filtros (D23): {nw['min_reviews']} o más reseñas, {nw['min_apps']} o más apps, sin regulación (impuestos, "
         f"aduanas, pagos y datos de salud). Pasan {len(rows)} de {all_n} necesidades; {reg_n} más pasarían los filtros "
         "de tamaño pero son reguladas.", "",
         "| # | Necesidad | Reseñas | Apps | Categoría | Construibilidad | Justificación | Ejemplos |",
         "|---|---|---|---|---|---|---|---|"]
    for i, n in enumerate(rows, 1):
        ex = ", ".join(f"[{k}]({permalink(r)})" for k, r in
                       enumerate(latest(conn, json.loads(n["review_ids"]), nw["examples"]), 1))
        L.append(f"| {i} | {_md(n['label'])} | {n['n_reviews']} | {n['n_apps']} | {n['category']} | "
                 f"{n['buildability']} | {_md(n['buildability_reason'])} | {ex} |")
    L.append("")
    for i, n in enumerate(rows, 1):
        apps = ", ".join(names.get(a, a) for a in json.loads(n["app_ids"]))
        L += [f"- **{i}.** Apps: {apps}."]
    L.append("")
    any_s = any(n["buildability"] == "S" for n in rows)
    L += [f"**¿Hay al menos una necesidad con construibilidad S que pase los filtros?** {'Sí' if any_s else 'No'}.", ""]
    md_path.write_text("\n".join(L), encoding="utf-8")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["rank", "label", "n_reviews", "n_apps", "category", "buildability", "buildability_reason",
                     "app_ids", "example_links"])
        for i, n in enumerate(rows, 1):
            wr.writerow([i, n["label"], n["n_reviews"], n["n_apps"], n["category"], n["buildability"],
                         n["buildability_reason"], " ".join(json.loads(n["app_ids"])),
                         " ".join(permalink(r) for r in latest(conn, json.loads(n["review_ids"]), nw["examples"]))])
    return md_path, csv_path
