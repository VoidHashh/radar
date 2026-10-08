"""Pasos 1-5 de extremo a extremo con un adaptador simulado y las fichas reales de los fixtures."""
from datetime import date
from pathlib import Path

import pytest

from radar import db
from radar.config import load_config, load_keywords
from radar.pipeline import Pipeline
from radar.stores.base import AppMeta, ListingCard
from radar.stores.shopify import parse_app_page
from tests.fakes import FakeAdapter, make_reviews

FIX = Path(__file__).parent / "fixtures"
TODAY = date(2026, 10, 5)


def fixture_meta(handle):
    return parse_app_page((FIX / f"app_{handle}.html").read_text(encoding="utf-8"), handle)


@pytest.fixture
def setup(tmp_path):
    cfg = load_config()
    cfg["db_path"] = str(tmp_path / "radar.sqlite")
    big = AppMeta(app_id="bigbad", name="Big Bad", developer="X", url="u", review_count=900,
                  rating_shown=3.9, counts={5: 600, 4: 50, 3: 50, 2: 50, 1: 150})
    tiny = AppMeta(app_id="tiny", name="Tiny", developer="Y", url="u", review_count=20,
                   rating_shown=2.0, counts={5: 2, 4: 0, 3: 0, 2: 3, 1: 15})
    metas = {"dropify-5": fixture_meta("dropify-5"), "sufio": fixture_meta("sufio"), "bigbad": big, "tiny": tiny}
    listings = {
        "cat-a": [ListingCard("dropify-5", "Dropify", 3.5, 53, 1), ListingCard("sufio", "Sufio", 4.9, 466, 2),
                  ListingCard("tiny", "Tiny", 2.0, 20, 3)],
        "cat-b": [ListingCard("bigbad", "Big Bad", 3.9, 900, 1), ListingCard("sufio", "Sufio", 4.9, 466, 2)],
    }
    reviews = {"dropify-5": make_reviews(53, TODAY, every_days=10),
               "bigbad": make_reviews(900, TODAY, every_days=0.6)}
    fake = FakeAdapter(reviews, metas, listings, sitemap=["dropify-5", "sufio", "bigbad", "tiny", "orphan"],
                       intermediates=["cat-parent"])
    conn = db.connect(cfg["db_path"])
    return Pipeline(cfg, conn, fake, load_keywords(), today=TODAY), fake


def test_run_all_end_to_end(setup):
    p, fake = setup
    result = p.run_all(category=None)
    s = result["summary"]

    cov = result["steps"]["inventory"]["coverage"]
    assert cov["sitemap_apps"] == 5 and cov["listed_unique"] == 4
    assert cov["in_sitemap_not_listed"] == 1 and cov["examples_missing"] == ["orphan"]
    assert cov["apps_in_several_categories"] == 1          # sufio
    assert result["steps"]["inventory"]["categories_with_listing"] == 2
    assert result["steps"]["inventory"]["categories_without_listing"] == 1   # intermedia: 404

    # Paso 2: "tiny" (20 reseñas en el listado) no llega a pedir ficha.
    assert result["steps"]["meta"]["candidates"] == 3
    assert s["fichas"] == 3

    # Paso 3: dropify (34 % negativas) y bigbad (22 %) pasan; sufio (2,4 %, 4,87) no.
    assert s["preselected"] == 2
    windows = {w["app_id"]: w for w in s["windows"]}
    assert set(windows) == {"dropify-5", "bigbad"}

    # Paso 4: <= 300 exacto con todas las reseñas; > 300 solo negativas y total estimado.
    assert windows["dropify-5"]["method"] == "exact"
    assert windows["dropify-5"]["total_12m"] == 37
    assert windows["bigbad"]["method"] == "estimated"
    assert windows["bigbad"]["total_12m"] == 610
    stored = p.conn.execute("SELECT rating, COUNT(*) FROM reviews WHERE app_id = 'bigbad' GROUP BY rating").fetchall()
    assert {r[0] for r in stored} == {1}                   # solo negativas de la app grande
    assert windows["bigbad"]["negatives_12m"] == 153

    # Paso 5: marcas sobre las negativas.
    assert s["flags"]["broken"] == s["negatives_stored"] > 0
    assert p.conn.execute("SELECT finished_at FROM runs").fetchone()[0] is not None


def test_run_all_is_resumable(setup):
    p, fake = setup
    run_id = p.open_run(None)
    p.step_inventory(run_id)
    p.step_meta(run_id)
    calls_before = len(fake.page_calls)
    p.step_reviews(run_id)
    first = len(fake.page_calls) - calls_before
    p.step_reviews(run_id)                                   # segunda vez: nada que hacer
    assert len(fake.page_calls) - calls_before == first
    assert p.open_run(None) == run_id                        # la ejecución abierta se reanuda


def test_category_scope(setup):
    p, _ = setup
    result = p.run_all(category="cat-a")
    s = result["summary"]
    assert "coverage" not in result["steps"]["inventory"]
    assert s["listed_apps"] == 3
    assert [w["app_id"] for w in s["windows"]] == ["dropify-5"]


def test_unknown_category_is_rejected(setup):
    p, _ = setup
    with pytest.raises(ValueError):
        p.run_all(category="no-such-category")


def test_intermediate_category_is_rejected_as_scope(setup):
    p, _ = setup
    with pytest.raises(ValueError, match="intermedia"):
        p.run_all(category="cat-parent")


def test_intermediate_category_not_requested_again_on_resume(setup):
    p, fake = setup
    run_id = p.open_run(None)
    p.step_inventory(run_id)
    calls = []
    original = fake.category_listing
    fake.category_listing = lambda c: calls.append(c) or original(c)
    p.step_inventory(run_id)
    assert calls == []
