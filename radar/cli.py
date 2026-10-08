"""CLI del radar (SPEC.md, sección 8). Fase 1: pasos 1-5."""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Optional

import typer

from . import db, handoff
from . import report as report_mod
from . import score as score_mod
from .config import load_config, load_keywords
from .http import Blocked, HttpClient, HttpError, RobotsDisallowed
from .pipeline import Pipeline
from .stores.shopify import ShopifyAdapter

app = typer.Typer(add_completion=False, help="Radar de oportunidades en la Shopify App Store.")
state: dict = {}

EXIT_BLOCKED = 2
EXIT_ROBOTS = 3
EXIT_HTTP = 4


def _setup_logging(log_path: str, verbose: bool) -> None:
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    file = logging.FileHandler(log_path, encoding="utf-8")
    file.setLevel(logging.INFO)
    file.setFormatter(fmt)
    root.handlers[:] = [console, file]
    logging.getLogger("httpx").setLevel(logging.WARNING)


@app.callback()
def main(config: Optional[Path] = typer.Option(None, help="Ruta a config.yaml"),
         verbose: bool = typer.Option(False, "--verbose", "-v")):
    cfg = load_config(config)
    _setup_logging(cfg["log_path"], verbose)
    state["cfg"] = cfg


def _pipeline() -> tuple[Pipeline, HttpClient]:
    cfg = state["cfg"]
    http = HttpClient.from_config(cfg)
    conn = db.connect(cfg["db_path"])
    adapter = ShopifyAdapter.from_config(http, cfg)
    return Pipeline(cfg, conn, adapter, load_keywords(Path(cfg["_base_dir"]) / "keywords.yaml"), http), http


def _current_run(p: Pipeline) -> str:
    row = p.conn.execute("SELECT run_id FROM runs WHERE finished_at IS NULL ORDER BY started_at DESC").fetchone()
    if row is None:
        typer.echo("No hay ninguna ejecución abierta. Empieza con `radar inventory` o `radar run-all`.", err=True)
        raise typer.Exit(1)
    return row["run_id"]


def _guard(fn):
    """Traduce los errores de red a mensajes claros y códigos de salida."""
    try:
        return fn()
    except Blocked as e:
        typer.echo(f"\nPARADA por Cloudflare: {e}\nEl estado está guardado. Repite la misma orden más tarde "
                   f"para reanudar.", err=True)
        raise typer.Exit(EXIT_BLOCKED)
    except RobotsDisallowed as e:
        typer.echo(f"\nPARADA: robots.txt prohíbe {e}. Revisa docs/recon.md antes de seguir.", err=True)
        raise typer.Exit(EXIT_ROBOTS)
    except HttpError as e:
        typer.echo(f"\nPARADA por error HTTP: {e}", err=True)
        raise typer.Exit(EXIT_HTTP)


def _print(title: str, data: dict) -> None:
    typer.echo(f"\n== {title}")
    typer.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _print_summary(s: dict, http: HttpClient) -> None:
    typer.echo(f"\n=== Resumen de la ejecución {s['run_id']}")
    typer.echo(f"Apps en los listados:        {s['listed_apps']}")
    typer.echo(f"Fichas descargadas:          {s['fichas']}")
    typer.echo(f"Apps preseleccionadas:       {s['preselected']}")
    typer.echo(f"Reseñas descargadas:         {s['reviews_stored']}  (de 1-2★: {s['negatives_stored']})")
    typer.echo(f"Reseñas negativas marcadas:  {s['flagged_reviews']}")
    typer.echo("Marcas por categoría:")
    for cat, n in s["flags"].items():
        typer.echo(f"  {cat:<16} {n}")
    if s["windows"]:
        typer.echo("\nVentanas de 365 días:")
        typer.echo(f"  {'app':<42} {'reseñas':>7} {'nota':>9} {'%1-2★':>6} {'método':<10} {'total_12m':>9} {'neg_12m':>7}")
        for w in s["windows"]:
            pct = 100 * (w["n_1"] + w["n_2"]) / w["review_count"] if w["review_count"] else 0
            nota = f"{w['rating_shown']}/{w['rating_computed']:.2f}" if w["rating_computed"] is not None else "-"
            typer.echo(f"  {w['app_id'][:42]:<42} {w['review_count']:>7} {nota:>9} {pct:>6.1f} {w['method']:<10} "
                       f"{w['total_12m']:>9} {w['negatives_12m']:>7}")
    st = http.stats
    typer.echo(f"\nPeticiones: {st['network']} a la red, {st['cache']} desde caché, "
               f"{st['challenges']} bloqueos de Cloudflare, {st['retries']} reintentos.")


@app.command()
def inventory(category: Optional[str] = typer.Option(None, help="Solo esta categoría hoja")):
    """Paso 1: sitemap de apps y listados de categoría."""
    p, http = _pipeline()

    def go():
        p.check_robots()
        run_id = p.open_run(category)
        _print("inventory", p.step_inventory(run_id, category).data)
    _guard(go)


@app.command()
def meta(category: Optional[str] = typer.Option(None), limit: Optional[int] = typer.Option(None)):
    """Paso 2: ficha de cada app con suficientes reseñas en el listado."""
    p, http = _pipeline()

    def go():
        p.check_robots()
        _print("meta", p.step_meta(_current_run(p), category, limit).data)
    _guard(go)


@app.command()
def reviews(since_days: int = typer.Option(365, help="Ventana en días"),
            category: Optional[str] = typer.Option(None)):
    """Pasos 3-4: preselección y descarga de reseñas."""
    p, http = _pipeline()

    def go():
        p.check_robots()
        _print("reviews", p.step_reviews(_current_run(p), category, since_days).data)
    _guard(go)


@app.command()
def keywords(category: Optional[str] = typer.Option(None)):
    """Paso 5: marcado por palabras clave de las reseñas de 1-2 estrellas."""
    p, http = _pipeline()
    run_id = _current_run(p)
    _print("keywords", p.step_keywords(run_id, category).data)


@app.command("run-all")
def run_all(category: Optional[str] = typer.Option(None, help="Solo esta categoría hoja")):
    """Pasos 1-5 en orden. Reanudable: repetir la orden continúa donde se paró."""
    p, http = _pipeline()

    def go():
        result = p.run_all(category)
        inv = result["steps"]["inventory"]
        if "coverage" in inv:
            _print("cobertura de los listados", {k: v for k, v in inv["coverage"].items()
                                                  if k != "mismatched_categories"})
        _print_summary(result["summary"], http)
    _guard(go)


# ====================================================================== Fase 2 (sin API, D10)

classify_app = typer.Typer(help="Paso 6: clasificación de negativas por Claude Code.")
cluster_app = typer.Typer(help="Paso 7: agrupación de quejas por Claude Code.")
replicables_app = typer.Typer(help="Paso 8b: apps replicables evaluadas por Claude Code.")
app.add_typer(classify_app, name="classify")
app.add_typer(cluster_app, name="cluster")
app.add_typer(replicables_app, name="replicables")

RunOpt = typer.Option(None, "--run-id", help="Ejecución a analizar (por defecto, la última con reseñas)")


def _conn():
    return db.connect(state["cfg"]["db_path"])


def _import_report(name: str, rep) -> None:
    _print(name, rep.as_dict())
    if rep.errors:
        typer.echo(f"{len(rep.errors)} errores de validación: corrige los ficheros de salida y repite el import.", err=True)


@classify_app.command("export")
def classify_export(run_id: Optional[str] = RunOpt, batch_size: Optional[int] = typer.Option(None)):
    conn = _conn()
    _print("classify export", handoff.classify_export(conn, state["cfg"], handoff.analysis_run(conn, run_id), batch_size))


@classify_app.command("import")
def classify_import(run_id: Optional[str] = RunOpt):
    conn = _conn()
    _import_report("classify import", handoff.classify_import(conn, state["cfg"], handoff.analysis_run(conn, run_id)))


@cluster_app.command("export")
def cluster_export(run_id: Optional[str] = RunOpt):
    conn = _conn()
    _print("cluster export", handoff.cluster_export(conn, state["cfg"], handoff.analysis_run(conn, run_id)))


@cluster_app.command("import")
def cluster_import(run_id: Optional[str] = RunOpt):
    conn = _conn()
    _import_report("cluster import", handoff.cluster_import(conn, state["cfg"], handoff.analysis_run(conn, run_id)))


@replicables_app.command("export")
def replicables_export(run_id: Optional[str] = RunOpt):
    p, http = _pipeline()

    def go():
        rid = handoff.analysis_run(p.conn, run_id)
        _print("replicables export", handoff.replicables_export(p.conn, state["cfg"], rid, p.adapter))
    _guard(go)


@replicables_app.command("import")
def replicables_import(run_id: Optional[str] = RunOpt):
    conn = _conn()
    _import_report("replicables import", handoff.replicables_import(conn, state["cfg"], handoff.analysis_run(conn, run_id)))


needs_app = typer.Typer(help="D20: necesidades no cubiertas, agrupadas por Claude Code.")
app.add_typer(needs_app, name="needs")


@needs_app.command("collect")
def needs_collect(run_id: Optional[str] = RunOpt, months: int = typer.Option(24), max_pages: int = typer.Option(10)):
    """D22: reseñas de 1-3 estrellas de todas las fichas (sin plataformas), para las necesidades."""
    from . import needs_collect as nc
    p, http = _pipeline()

    def go():
        p.check_robots()
        rid = handoff.analysis_run(p.conn, run_id)
        _print("needs collect", nc.collect(p.conn, state["cfg"], p.adapter, rid, months, max_pages))
    _guard(go)


@needs_app.command("classify-export")
def needs_classify_export(run_id: Optional[str] = RunOpt, batch_size: Optional[int] = typer.Option(None)):
    """D22: lotes de reseñas 1-3★ de 24 meses aún sin clasificar."""
    conn = _conn()
    _print("needs classify-export", handoff.wide_classify_export(conn, state["cfg"], handoff.analysis_run(conn, run_id),
                                                                 batch_size))


@needs_app.command("classify-import")
def needs_classify_import(run_id: Optional[str] = RunOpt):
    conn = _conn()
    _import_report("needs classify-import", handoff.classify_import(conn, state["cfg"], handoff.analysis_run(conn, run_id),
                                                                    step="classify_wide"))


@needs_app.command("wide-export")
def needs_wide_export(run_id: Optional[str] = RunOpt):
    """D22: necesidades con el material ampliado, un fichero por categoría."""
    conn = _conn()
    _print("needs wide-export", handoff.wide_needs_export(conn, state["cfg"], handoff.analysis_run(conn, run_id)))


@needs_app.command("wide-import")
def needs_wide_import(run_id: Optional[str] = RunOpt):
    conn = _conn()
    _import_report("needs wide-import", handoff.wide_needs_import(conn, state["cfg"], handoff.analysis_run(conn, run_id)))


@needs_app.command("report")
def needs_report(run_id: Optional[str] = RunOpt, suffix: str = typer.Option("d")):
    """D23: informe solo de necesidades no cubiertas."""
    conn = _conn()
    rid = handoff.analysis_run(conn, run_id)
    for path in report_mod.build_needs(conn, state["cfg"], rid, Path(state["cfg"]["_base_dir"]) / "reports", suffix):
        typer.echo(f"Escrito {path}")


close_app = typer.Typer(help="Cierre de la Fase 2 (D24-D26).")
app.add_typer(close_app, name="close")


@close_app.command("concentration")
def close_concentration(run_id: Optional[str] = RunOpt):
    from . import close
    conn = _conn()
    typer.echo(f"Necesidades actualizadas: {close.concentration(conn, handoff.analysis_run(conn, run_id))}")


@close_app.command("leaders-export")
def close_leaders_export(run_id: Optional[str] = RunOpt, buildability: str = typer.Option("S")):
    from . import close
    p, http = _pipeline()

    def go():
        _print("leaders export", close.leaders_export(p.conn, state["cfg"], handoff.analysis_run(p.conn, run_id),
                                                      p.adapter, buildability.upper()))
    _guard(go)


@close_app.command("leaders-import")
def close_leaders_import(run_id: Optional[str] = RunOpt, buildability: str = typer.Option("S")):
    from . import close
    conn = _conn()
    _import_report("leaders import", close.leaders_import(conn, state["cfg"], handoff.analysis_run(conn, run_id),
                                                          buildability.upper()))


@close_app.command("monitoring-collect")
def close_monitoring_collect():
    from . import close
    p, http = _pipeline()

    def go():
        p.check_robots()
        _print("monitoring collect", close.monitoring_collect(p.conn, state["cfg"], p.adapter))
    _guard(go)


@close_app.command("monitoring-classify-export")
def close_monitoring_classify_export():
    from . import close
    conn = _conn()
    _print("monitoring classify-export", handoff.wide_classify_export(conn, state["cfg"], close.monitoring_run(conn)))


@close_app.command("monitoring-classify-import")
def close_monitoring_classify_import():
    from . import close
    conn = _conn()
    _import_report("monitoring classify-import",
                   handoff.classify_import(conn, state["cfg"], close.monitoring_run(conn), step="classify_wide"))


@close_app.command("monitoring-export")
def close_monitoring_export():
    from . import close
    p, http = _pipeline()

    def go():
        _print("monitoring export", close.monitoring_export(p.conn, state["cfg"], p.adapter))
    _guard(go)


@close_app.command("monitoring-import")
def close_monitoring_import():
    from . import close
    conn = _conn()
    _import_report("monitoring import", close.monitoring_import(conn, state["cfg"]))


@close_app.command("report")
def close_report(run_id: Optional[str] = RunOpt, suffix: str = typer.Option("e")):
    from . import close_report as cr
    conn = _conn()
    rid = handoff.analysis_run(conn, run_id)
    typer.echo(f"Escrito {cr.build(conn, state['cfg'], rid, Path(state['cfg']['_base_dir']) / 'reports', suffix)}")


@needs_app.command("export")
def needs_export(run_id: Optional[str] = RunOpt):
    conn = _conn()
    _print("needs export", handoff.needs_export(conn, state["cfg"], handoff.analysis_run(conn, run_id)))


@needs_app.command("import")
def needs_import(run_id: Optional[str] = RunOpt):
    conn = _conn()
    _import_report("needs import", handoff.needs_import(conn, state["cfg"], handoff.analysis_run(conn, run_id)))


@app.command()
def score(run_id: Optional[str] = RunOpt):
    """Paso 8: puntuación de las apps preseleccionadas."""
    conn = _conn()
    rid = handoff.analysis_run(conn, run_id)
    n = score_mod.score_run(conn, rid, state["cfg"])
    typer.echo(f"Puntuadas {n} apps de la ejecución {rid}.")


@app.command()
def report(run_id: Optional[str] = RunOpt,
           check_links: bool = typer.Option(False, "--check-links", help="Comprueba un enlace por oportunidad"),
           suffix: str = typer.Option("", "--suffix", help="Sufijo del fichero: 'b' -> 2026-41b.md")):
    """Paso 9: informe semanal en Markdown y CSV."""
    p, http = _pipeline()
    rid = handoff.analysis_run(p.conn, run_id)
    checks = None

    def go():
        nonlocal checks
        if check_links:
            p.check_robots()
            checks = {}
            for app_id, url in report_mod.example_links(p.conn, rid, state["cfg"]["report"]["top_n"]).items():
                checks[app_id] = http.get(url).status
            bad = {a: s for a, s in checks.items() if s != 200}
            typer.echo(f"Enlaces comprobados: {len(checks)}, fallidos: {len(bad)} {bad if bad else ''}")
    _guard(go)
    out = Path(state["cfg"]["_base_dir"]) / "reports"
    paths = report_mod.build(p.conn, state["cfg"], rid, out, checks, suffix)
    for path in paths:
        typer.echo(f"Escrito {path}")


if __name__ == "__main__":
    app()
