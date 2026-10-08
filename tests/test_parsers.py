"""Parsers del adaptador Shopify contra los fixtures de la Fase 0 (sin red)."""
from pathlib import Path

import pytest

from radar.http import Robots, is_challenge
from radar.stores.shopify import (handles_from_sitemap, listing_candidates, parse_app_page, parse_category_page,
                                  parse_date, parse_reviews_page, root_categories)

FIX = Path(__file__).parent / "fixtures"


def read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# ------------------------------------------------------------------ fechas

@pytest.mark.parametrize("text,iso", [
    ("October 1, 2026", "2026-10-01"),
    ("Edited October 2, 2026", "2026-10-02"),
    ("  May 8, 2013 ", "2013-05-08"),
    ("Klaviyo replied\n September 21, 2026", "2026-09-21"),
    ("sin fecha", None),
])
def test_parse_date(text, iso):
    assert parse_date(text) == iso


# ------------------------------------------------------------------ robots y sitemaps

def test_robots_rules():
    robots = Robots(read("robots.txt"), "RadarResearchBot/0.1 (test)")
    base = "https://apps.shopify.com"
    assert robots.allowed(f"{base}/klaviyo-email-marketing")
    assert robots.allowed(f"{base}/sufio/reviews?ratings%5B%5D=1&ratings%5B%5D=2&sort_by=newest&page=2")
    assert robots.allowed(f"{base}/categories/store-design/all?page=3")
    assert robots.allowed(f"{base}/sitemap_apps_en.xml")
    assert not robots.allowed(f"{base}/search?q=reviews")
    assert not robots.allowed(f"{base}/internal/x")
    assert not robots.allowed(f"{base}/services/x")
    assert not robots.allowed(f"{base}/sufio?shpxid=1")
    assert robots.sitemaps == ["https://apps.shopify.com/sitemap.xml"]


def test_sitemap_index_and_apps():
    locs = handles_from_sitemap(read("sitemap_index.xml"))
    assert "sitemap_apps_en.xml" in locs and "sitemap_categories_en.xml" in locs
    apps = handles_from_sitemap(read("sitemap_apps_en_sample.xml"))
    assert len(apps) == 50 == len(set(apps))
    assert apps[0] == "avada-shipping-labels"


def test_root_and_listing_candidates():
    handles = ["store-design", "store-design-content", "store-design-content-blogs",
               "orders-and-shipping", "orders-and-shipping-shipping-solutions",
               "orders-and-shipping-shipping-solutions-shipping",          # hoja real (1.098 apps)
               "orders-and-shipping-shipping-solutions-shipping-rates",    # su hermana, no su hija
               "sales-channels"]
    assert root_categories(handles) == ["store-design", "orders-and-shipping", "sales-channels"]
    cands = listing_candidates(handles)
    assert "orders-and-shipping-shipping-solutions-shipping" in cands
    assert "store-design" not in cands and "sales-channels" not in cands


# ------------------------------------------------------------------ listado de categoría

def test_category_page():
    cards, has_next, total = parse_category_page(read("category_email-marketing_all_p1.html"))
    assert total == 412
    assert has_next
    assert len(cards) == 24 == len({c.app_id for c in cards})
    first = cards[0]
    assert (first.app_id, first.rating_shown, first.review_count, first.position) == \
        ("klaviyo-email-marketing", 4.7, 3352, 1)
    assert cards[-1].app_id == "back-in-stock-alert-engine" and cards[-1].position == 24
    assert all(c.review_count > 0 for c in cards)


# ------------------------------------------------------------------ ficha

@pytest.mark.parametrize("handle,name,developer,rating,count,counts,cats,n_plans,launched", [
    ("klaviyo-email-marketing", "Klaviyo: Email Marketing & SMS", "Klaviyo", 4.7, 3352,
     {5: 2938, 4: 83, 3: 31, 2: 37, 1: 263},
     ["marketing-and-conversion-marketing-email-marketing", "marketing-and-conversion-marketing-sms-marketing"],
     3, "2012-09-20"),
    ("sufio", "Sufio: Invoice You Can Trust", "Sufio", 4.9, 466, {5: 439, 4: 13, 3: 3, 2: 4, 1: 7},
     ["orders-and-shipping-orders-invoices-and-receipts", "store-management-finances-taxes"], 4, "2013-07-22"),
    ("dropify-5", "Dropify", "Dropi", 3.5, 53, {5: 26, 4: 4, 3: 5, 2: 4, 1: 14},
     ["finding-products-sourcing-options-sourcing-options-other"], 1, "2021-07-19"),
])
def test_app_page(handle, name, developer, rating, count, counts, cats, n_plans, launched):
    m = parse_app_page(read(f"app_{handle}.html"), handle)
    assert (m.app_id, m.name, m.developer) == (handle, name, developer)
    assert m.rating_shown == rating
    assert m.review_count == count
    assert m.counts == counts
    assert sum(m.counts.values()) == m.review_count
    assert [c["handle"] for c in m.categories] == cats
    assert len(m.pricing) == n_plans
    assert m.launched == launched
    assert m.url == f"https://apps.shopify.com/{handle}"


def test_rating_computed_differs_from_shown():
    """D4: Klaviyo muestra 4,7 pero la media de su distribución es 4,61."""
    m = parse_app_page(read("app_klaviyo-email-marketing.html"), "klaviyo-email-marketing")
    assert m.rating_shown == 4.7
    assert m.rating_computed == pytest.approx(4.6098, abs=1e-4)


def test_paid_plan_prices():
    m = parse_app_page(read("app_sufio.html"), "sufio")
    assert [(p["name"], p["price"]) for p in m.pricing] == [
        ("STARTER", "$7/month"), ("GROWTH", "$19/month"), ("PROFESSIONAL", "$49/month"), ("PREMIUM", "$129/month")]


def test_feature_tags_are_not_categories():
    m = parse_app_page(read("app_klaviyo-email-marketing.html"), "klaviyo-email-marketing")
    assert len(m.categories) == 2   # la ficha tiene decenas de etiquetas de funciones


def test_app_page_ignores_based_in():
    """Regla 4 / D6: "Based in" depende de la IP del visitante y no se extrae."""
    m = parse_app_page(read("app_klaviyo-email-marketing.html"), "klaviyo-email-marketing")
    assert "Spain" not in repr(m)


# ------------------------------------------------------------------ reseñas

def test_reviews_default_page():
    page = parse_reviews_page(read("reviews_klaviyo-email-marketing_p1_default.html"))
    assert len(page.reviews) == 10 and page.has_next
    r = page.reviews[0]
    assert r.review_id == "2380223"
    assert r.rating == 1
    assert r.date_shown == "2026-10-01" and not r.edited
    assert r.country == "New Zealand"
    assert r.usage_duration == "Almost 3 years"
    assert r.body == "REDACTED REVIEW TEXT"          # D33: texto literal eliminado de los fixtures
    assert r.has_dev_reply and r.dev_reply_date == "2026-10-02"
    edited = page.reviews[1]
    assert edited.edited and edited.date_shown == "2026-10-02"
    assert all(x.has_dev_reply for x in page.reviews)


def test_reviews_newest_sorted_and_edge_cases():
    page = parse_reviews_page(read("reviews_klaviyo-email-marketing_newest_p5.html"))
    assert len(page.reviews) == 10 and page.has_next
    ids = [int(r.review_id) for r in page.reviews]
    assert ids == sorted(ids, reverse=True)
    assert any(not r.has_dev_reply and r.dev_reply_date is None for r in page.reviews)
    assert any(r.usage_duration is None for r in page.reviews)
    assert any(r.body == "" for r in page.reviews)          # valoración sin texto
    assert all(r.country for r in page.reviews)


def test_reviews_star_filter_union():
    page = parse_reviews_page(read("reviews_sufio_r1r2_newest_p1.html"))
    ratings = [r.rating for r in page.reviews]
    assert set(ratings) == {1, 2}
    assert ratings.count(1) == 7 and ratings.count(2) == 3
    assert sum(r.edited for r in page.reviews) == 2


def test_reviews_last_and_empty_pages():
    last = parse_reviews_page(read("reviews_dropify-5_newest_p6_last.html"))
    assert len(last.reviews) == 3 and not last.has_next
    assert last.reviews[0].body == "REDACTED REVIEW TEXT"   # D33: texto literal eliminado
    empty = parse_reviews_page(read("reviews_dropify-5_newest_p7_empty.html"))
    assert empty.reviews == [] and not empty.has_next


def test_permalink_page():
    page = parse_reviews_page(read("review_permalink_2380223.html"))
    assert [r.review_id for r in page.reviews] == ["2380223"]


def test_no_store_names_are_parsed():
    """Regla 4: el nombre de la tienda no aparece en ningún campo de la reseña."""
    page = parse_reviews_page(read("reviews_klaviyo-email-marketing_p1_default.html"))
    for r in page.reviews:
        assert "REDACTED STORE" not in (r.country or "") + (r.usage_duration or "") + r.body


# ------------------------------------------------------------------ Cloudflare

def test_challenge_detection():
    html = read("cloudflare_challenge_403.html")
    assert is_challenge(403, {"cf-mitigated": "challenge"}, html)
    assert is_challenge(403, {}, html)                     # sin cabecera, por el título
    assert not is_challenge(200, {}, html)
    assert not is_challenge(403, {}, "<html>Forbidden</html>")
