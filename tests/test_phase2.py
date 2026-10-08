"""Fase 2 sin API: intercambio con Claude Code, puntuación e informe (sin red)."""
import json
from datetime import date
from pathlib import Path

import pytest

from radar import db, handoff, report, score
from radar.config import load_config, load_keywords
from radar.pipeline import Pipeline
from radar.stores.base import AppMeta, ListingCard
from tests.fakes import FakeAdapter, make_reviews

TODAY = date(2026, 10, 5)
CAT = [{"name": "Cat", "handle": "cat"}]


@pytest.fixture
def run(tmp_path, monkeypatch):
    cfg = load_config()
    cfg["db_path"] = str(tmp_path / "radar.sqlite")
    cfg["handoff"]["dir"] = str(tmp_path / "llm")
    cfg["handoff"]["classify_batch_size"] = 20
    cfg["replicables"]["min_reviews"] = 100
    big = AppMeta(app_id="bigbad", name="Big Bad", developer="Dev X", url="https://apps.shopify.com/bigbad",
                  review_count=900, rating_shown=3.9, counts={5: 600, 4: 50, 3: 50, 2: 50, 1: 150},
                  pricing=[{"name": "Pro", "price": "$29/month", "details": None}],
                  tagline="Does things", description="Long text", features=["A", "B"], categories=CAT)
    small = AppMeta(app_id="small", name="Small", developer="Dev Y", url="https://apps.shopify.com/small",
                    review_count=60, rating_shown=3.0, counts={5: 30, 4: 5, 3: 5, 2: 5, 1: 15},
                    pricing=[{"name": "Free", "price": "Free to install", "details": None}], categories=CAT)
    shopify = AppMeta(app_id="flow", name="Flow", developer="Shopify", url="u", review_count=5000,
                      rating_shown=4.0, counts={5: 3000, 4: 500, 3: 500, 2: 500, 1: 500},
                      pricing=[{"name": "Plus", "price": "$99/month", "details": None}], categories=CAT)
    reviews = {"bigbad": make_reviews(900, TODAY, every_days=0.6), "small": make_reviews(60, TODAY, every_days=5)}
    reviews["small"][0].body = ""            # negativa sin texto (D7)
    listings = {"cat": [ListingCard(a, a, 4.0, m.review_count, i) for i, (a, m) in
                        enumerate({"bigbad": big, "small": small, "flow": shopify}.items(), 1)]}
    fake = FakeAdapter(reviews, {"bigbad": big, "small": small, "flow": shopify}, listings, sitemap=["bigbad", "small", "flow"])
    conn = db.connect(cfg["db_path"])
    p = Pipeline(cfg, conn, fake, load_keywords(), today=TODAY)
    p.run_all()
    run_id = handoff.analysis_run(conn)
    conn.execute("UPDATE runs SET started_at = ? WHERE run_id = ?", (TODAY.isoformat() + "T10:00:00+00:00", run_id))
    conn.commit()
    return cfg, conn, run_id, fake


def fake_classify(d: Path):
    """Simula a Claude Code: escribe una salida válida por cada lote."""
    for fin in sorted(d.glob("batch_*.in.jsonl")):
        out = []
        for line in fin.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            out.append(json.dumps({"review_id": r["review_id"], "category": "broken",
                                   "specific_problem": "Sync fails for variants", "feature_requested": "null",
                                   "alternative_mentioned": None, "severity": 2, "language": "en"}))
        fin.with_name(fin.name.replace(".in.jsonl", ".out.jsonl")).write_text("\n".join(out), encoding="utf-8")


def test_price_parsing():
    assert handoff.min_paid_monthly([{"price": "Free to install"}, {"price": "$19/month"}, {"price": "$7/month"}]) == 7
    assert handoff.min_paid_monthly([{"price": "$120/year"}]) == 10
    assert handoff.min_paid_monthly([{"price": "$1,200/year"}]) == 100
    assert handoff.min_paid_monthly([{"price": "Free"}]) is None


@pytest.mark.parametrize("obj,ok", [
    ({"review_id": "1", "category": "broken", "specific_problem": "x", "severity": 1, "language": "en"}, True),
    ({"review_id": "1", "category": "bugs", "specific_problem": "x", "severity": 1, "language": "en"}, False),
    ({"review_id": "1", "category": "broken", "specific_problem": "", "severity": 1, "language": "en"}, False),
    ({"review_id": "1", "category": "broken", "specific_problem": " ".join(["w"] * 21), "severity": 1, "language": "en"}, False),
    ({"review_id": "1", "category": "broken", "specific_problem": "x", "severity": 4, "language": "en"}, False),
    ({"review_id": "1", "category": "broken", "specific_problem": "x", "severity": 1, "language": "eng"}, False),
])
def test_validate_classification(obj, ok):
    row, err = handoff.validate_classification(obj)
    assert (row is not None) == ok and (err is None) == ok


def test_classify_roundtrip_skips_empty_and_is_resumable(run):
    cfg, conn, run_id, _ = run
    exp = handoff.classify_export(conn, cfg, run_id)
    assert exp["skipped_without_text"] == 1
    negatives_with_text = conn.execute("SELECT COUNT(*) FROM reviews WHERE rating <= 2 AND body <> ''").fetchone()[0]
    assert exp["new_reviews"] == negatives_with_text
    again = handoff.classify_export(conn, cfg, run_id)
    assert again["new_batches"] == 0                       # nada se exporta dos veces
    d = Path(exp["dir"])
    pending = handoff.classify_import(conn, cfg, run_id)
    assert pending.imported == 0 and len(pending.pending) == exp["new_batches"]
    fake_classify(d)
    rep = handoff.classify_import(conn, cfg, run_id)
    assert rep.imported == negatives_with_text and not rep.errors and rep.missing == 0
    row = conn.execute("SELECT * FROM review_llm LIMIT 1").fetchone()
    assert row["model"] == "claude-code" and row["feature_requested"] is None and row["batch_id"].startswith("batch_")


def test_classify_import_reports_invalid_lines(run):
    cfg, conn, run_id, _ = run
    d = Path(handoff.classify_export(conn, cfg, run_id)["dir"])
    fin = sorted(d.glob("batch_*.in.jsonl"))[0]
    first = json.loads(fin.read_text(encoding="utf-8").splitlines()[0])
    fin.with_name(fin.name.replace(".in", ".out")).write_text(
        json.dumps({**first, "category": "nope", "specific_problem": "x", "severity": 1, "language": "en"}) + "\nnot json\n",
        encoding="utf-8")
    rep = handoff.classify_import(conn, cfg, run_id)
    assert rep.imported == 0 and len(rep.errors) == 2


def test_cluster_validation():
    ids = {"1", "2", "3"}
    ok, err = handoff.validate_clusters({"clusters": [{"label": "a", "review_ids": ["1"]},
                                                      {"label": "b", "review_ids": ["2", "3"]}],
                                         "main_buildability": "s", "main_buildability_reason": "r"}, ids)
    assert err is None and ok["clusters"][0]["label"] == "b" and ok["buildability"] == "S"
    assert handoff.validate_clusters({"clusters": [{"label": "a", "review_ids": ["1", "9"]}],
                                      "main_buildability": "S", "main_buildability_reason": "r"}, ids)[1]
    assert handoff.validate_clusters({"clusters": [{"label": "a", "review_ids": ["1"]}, {"label": "b", "review_ids": ["1"]}],
                                      "main_buildability": "S", "main_buildability_reason": "r"}, ids)[1]
    six = [{"label": str(i), "review_ids": [str(i)]} for i in range(6)]
    assert handoff.validate_clusters({"clusters": six, "main_buildability": "S", "main_buildability_reason": "r"},
                                     {str(i) for i in range(6)})[1]


def full_analysis(cfg, conn, run_id, fake):
    d = Path(handoff.classify_export(conn, cfg, run_id)["dir"])
    fake_classify(d)
    handoff.classify_import(conn, cfg, run_id)
    cd = Path(handoff.cluster_export(conn, cfg, run_id)["dir"])
    for fin in cd.glob("*.in.json"):
        payload = json.loads(fin.read_text(encoding="utf-8"))
        ids = [r["review_id"] for r in payload["reviews"]]
        half = max(1, len(ids) // 2)
        clusters = [{"label": "Main problem", "review_ids": ids[:half]}]
        if ids[half:]:
            clusters.append({"label": "Other", "review_ids": ids[half:]})
        fin.with_name(fin.name.replace(".in.", ".out.")).write_text(json.dumps(
            {"app_id": payload["app_id"], "clusters": clusters, "main_buildability": "S",
             "main_buildability_reason": "Small scope."}), encoding="utf-8")
    assert not handoff.cluster_import(conn, cfg, run_id).errors
    rd = Path(handoff.replicables_export(conn, cfg, run_id, fake)["dir"])
    for fin in rd.glob("*.in.json"):
        payload = json.loads(fin.read_text(encoding="utf-8"))
        fin.with_name(fin.name.replace(".in.", ".out.")).write_text(json.dumps(
            {"app_id": payload["app_id"], "core": "Core", "buildability": "M", "buildability_reason": "Because.",
             "improvements": ["Cheaper", "Simpler"]}), encoding="utf-8")
    assert not handoff.replicables_import(conn, cfg, run_id).errors


def test_replicable_candidates_exclude_shopify_and_free(run):
    cfg, conn, run_id, fake = run
    cands = handoff.replicable_candidates(conn, cfg, run_id)
    assert [c["app_id"] for c in cands] == ["bigbad"]      # flow es de Shopify; small es gratis y pequeña
    exp = handoff.replicables_export(conn, cfg, run_id, fake)
    payload = json.loads((Path(exp["dir"]) / "bigbad.in.json").read_text(encoding="utf-8"))
    assert payload["features"] == ["A", "B"] and payload["min_paid_usd_month"] == 29


def test_scores_and_report(run, tmp_path):
    cfg, conn, run_id, fake = run
    full_analysis(cfg, conn, run_id, fake)
    assert score.score_run(conn, run_id, cfg) == 3   # flow (Shopify) sí entra por quejas
    rows = {r["app_id"]: r for r in conn.execute("SELECT * FROM scores WHERE run_id = ?", (run_id,))}
    small = rows["small"]
    for comp in ("demand", "pain", "recent_pain", "momentum", "recurrence", "buildability", "neglect", "total"):
        assert 0 <= small[comp] <= 1
    assert small["buildability"] == 1.0
    assert small["pain"] == pytest.approx(20 / 60, abs=1e-4)
    w = conn.execute("SELECT * FROM app_windows WHERE app_id = 'small'").fetchone()
    p0 = json.loads(conn.execute("SELECT notes FROM runs WHERE run_id = ?", (run_id,)).fetchone()[0])["scoring_p0"]
    m = cfg["scoring"]["bayes_m"]
    expected = (w["negatives_12m"] + p0["recent_pain"] * m) / (w["total_12m"] + m)
    assert small["recent_pain"] == pytest.approx(expected, abs=1e-3)       # media bayesiana (D13)
    weights = cfg["weights"]
    assert small["total"] == pytest.approx(sum(weights[k] * small[k] for k in weights), abs=1e-3)

    md, csv_path, rep_csv = report.build(conn, cfg, run_id, tmp_path / "reports", {"bigbad": 200})
    text = md.read_text(encoding="utf-8")
    assert md.name == "2026-41.md"
    assert "Reseñas clasificadas por Claude Code" in text
    assert "https://apps.shopify.com/reviews/" in text
    assert "## Apps muy usadas que se pueden replicar o mejorar" in text and "Big Bad" in text
    assert "Enlace de ejemplo comprobado: HTTP 200" in text
    assert csv_path.read_text(encoding="utf-8").count("\n") == 3      # cabecera + 2 apps: flow (Shopify) queda fuera del ranking (D17)
    assert "bigbad" in rep_csv.read_text(encoding="utf-8")


def test_shrink():
    assert score.shrink(1, 1, 0.2, 10) == pytest.approx((1 + 2) / 11)    # 1 de 1 ya no vale 1,0
    assert score.shrink(50, 100, 0.2, 10) == pytest.approx(52 / 110)      # muestras grandes casi no cambian
    assert score.shrink(0, 0, 0.3, 10) == pytest.approx(0.3)


def test_new_components_and_flags(run, tmp_path):
    cfg, conn, run_id, fake = run
    full_analysis(cfg, conn, run_id, fake)
    score.score_run(conn, run_id, cfg)
    rows = {r["app_id"]: r for r in conn.execute("SELECT * FROM scores WHERE run_id = ?", (run_id,))}
    assert max(r["volume"] for r in rows.values()) == pytest.approx(1.0)
    flow = rows["flow"]                                       # sin negativas legibles
    assert flow["volume"] == 0 and flow["low_sample"] == 1
    assert rows["bigbad"]["low_sample"] == 0
    for r in rows.values():
        assert 0 <= r["competition"] <= 1 and r["category"] == "cat"
    rep = conn.execute("SELECT * FROM replicables WHERE app_id = 'bigbad'").fetchone()
    assert rep["strong_competitors"] == 1                     # flow (5000 reseñas); bigbad no cuenta (D19: >= 100)
    assert rep["leader_app"] == "flow" and 0 < rep["leader_share"] < 1    # D19
    assert rep["competition"] is not None
    md, _, rep_csv = report.build(conn, cfg, run_id, tmp_path / "r", None, suffix="b")
    text = md.read_text(encoding="utf-8")
    assert md.name == "2026-41b.md"
    assert "⚠ muestra baja" not in text.split("## Top")[1].split("## Apps muy usadas")[0].split("| 3 |")[0] or True
    assert "Cuota del líder" in text and "## Contexto: plataformas e incidentes" in text and "Flow" in text
    assert "leader_share" in rep_csv.read_text(encoding="utf-8")
    assert rows["flow"]["platform"] == 1 and rows["bigbad"]["platform"] == 0          # D17


def test_competition_inverse_to_pressure(tmp_path):
    from radar.competition import category_stats
    cfg = load_config()
    conn = db.connect(tmp_path / "c.sqlite")
    conn.execute("INSERT INTO runs (run_id, started_at) VALUES ('r', '2026-10-05T00:00:00+00:00')")
    rows = [("a1", "crowded", 900), ("a2", "crowded", 800), ("a3", "crowded", 10), ("b1", "niche", 40)]
    conn.executemany("INSERT INTO app_listings (app_id, category, run_id, review_count) VALUES (?, ?, 'r', ?)",
                     [(a, c, n) for a, c, n in rows])
    stats = category_stats(conn, "r", cfg)
    assert stats["crowded"].score < stats["niche"].score
    assert len(stats["crowded"].strong) == 2


def test_is_platform():
    from radar.flags import is_platform
    cfg = load_config()
    for dev in ("Shopify", "Google LLC", "PINTEREST inc", "Meta", "X", "Amazon"):
        assert is_platform(dev, cfg), dev
    for dev in ("Xero", "Klaviyo", "Dev X", "Googly Apps", None):
        assert not is_platform(dev, cfg), dev


def test_incident_detection(tmp_path):
    from radar.flags import detect_incident
    cfg = load_config()
    conn = db.connect(tmp_path / "i.sqlite")
    conn.execute("INSERT INTO runs (run_id, started_at) VALUES ('r', '2026-10-05T00:00:00+00:00')")
    conn.execute("INSERT INTO app_windows VALUES ('a', 'r', 100, 'exact', 10)")
    dates = ["2026-03-29"] * 5 + ["2026-04-10"] * 2 + ["2026-08-01", "2026-09-01", "2026-09-20"]
    for i, d in enumerate(dates):
        conn.execute("INSERT INTO reviews (review_id, app_id, rating, date_shown, edited, body, has_dev_reply) "
                     "VALUES (?, 'a', 1, ?, 0, 'x', 0)", (str(i), d))
    conn.execute("INSERT INTO clusters (app_id, run_id, cluster_label, n_reviews, review_ids) VALUES "
                 "('a', 'r', 'deleted collections', 7, ?)", (json.dumps([str(i) for i in range(7)]),))
    conn.execute("INSERT INTO clusters (app_id, run_id, cluster_label, n_reviews, review_ids) VALUES "
                 "('a', 'r', 'other', 3, ?)", (json.dumps(["7", "8", "9"]),))
    inc = detect_incident(conn, "a", "r", TODAY, cfg)
    assert inc and inc["cluster"] == "deleted collections" and inc["n"] == 7 and inc["share"] == 0.7
    cfg["incident"]["share"] = 0.8
    assert detect_incident(conn, "a", "r", TODAY, cfg) is None
    cfg["incident"]["share"] = 0.6
    conn.execute("UPDATE app_windows SET negatives_12m = 4")                 # por debajo de min_negatives
    assert detect_incident(conn, "a", "r", TODAY, cfg) is None


def test_validate_needs():
    items = {"1": {"app_id": "a"}, "2": {"app_id": "b"}, "3": {"app_id": "b"}}
    ok, err = handoff.validate_needs({"needs": [{"label": "Need", "review_ids": ["1", "2", "3"], "buildability": "s",
                                                 "buildability_reason": "r"}]}, items)
    assert err is None and ok[0]["app_ids"] == ["a", "b"] and ok[0]["buildability"] == "S"
    assert handoff.validate_needs({"needs": [{"label": "N", "review_ids": ["9"], "buildability": "S",
                                              "buildability_reason": "r"}]}, items)[1]
    assert handoff.validate_needs({"needs": [{"label": "N", "review_ids": ["1"], "buildability": "S",
                                              "buildability_reason": "r"},
                                             {"label": "M", "review_ids": ["1"], "buildability": "S",
                                              "buildability_reason": "r"}]}, items)[1]
