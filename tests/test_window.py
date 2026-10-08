"""Ventana de 365 días (D1): corte por fecha, reseñas editadas y búsqueda binaria del total."""
from datetime import date, timedelta

import pytest

from tests.fakes import FakeAdapter, make_reviews

TODAY = date(2026, 10, 5)
SINCE = TODAY - timedelta(days=365)


def exact_in_window(reviews):
    """Total real: reseñas anteriores a la primera NO editada que cae fuera de la ventana."""
    for i, r in enumerate(reviews):
        if not r.edited and r.date_shown < SINCE.isoformat():
            return i
    return len(reviews)


def test_fetch_reviews_stops_at_cutoff():
    reviews = make_reviews(120, TODAY, every_days=5)          # 74 dentro de la ventana
    fake = FakeAdapter({"a": reviews})
    got, complete = fake.fetch_reviews("a", SINCE, max_pages=30)
    assert complete
    assert len(got) == exact_in_window(reviews) == 74
    assert max(p for _, p, _ in fake.page_calls) == 8         # no sigue paginando tras el corte


def test_edited_review_with_recent_date_does_not_stop_or_leak():
    """Una reseña editada muestra una fecha reciente aunque se creara antes: el corte no se mueve."""
    reviews = make_reviews(120, TODAY, every_days=5, edited_every=7)
    got, _ = FakeAdapter({"a": reviews}).fetch_reviews("a", SINCE, max_pages=30)
    assert len(got) == exact_in_window(reviews)


def test_fetch_reviews_reports_truncation():
    reviews = make_reviews(600, TODAY, every_days=0.5)
    got, complete = FakeAdapter({"a": reviews}).fetch_reviews("a", SINCE, max_pages=30)
    assert not complete and len(got) == 300


def test_fetch_reviews_negatives_only():
    reviews = make_reviews(400, TODAY, every_days=1)
    fake = FakeAdapter({"a": reviews})
    got, complete = fake.fetch_reviews("a", SINCE, max_pages=30, ratings=(1, 2))
    assert complete
    assert all(r.rating <= 2 for r in got)
    assert len(got) == sum(1 for r in reviews[:exact_in_window(reviews)] if r.rating <= 2)
    assert all(c[2] == (1, 2) for c in fake.page_calls)


@pytest.mark.parametrize("n,every", [(3352, 0.3), (1200, 1), (466, 5), (301, 0.5), (5000, 0.01)])
def test_estimate_total_matches_exact(n, every):
    reviews = make_reviews(n, TODAY, every_days=every, edited_every=11)
    fake = FakeAdapter({"a": reviews})
    est = fake.estimate_total_since("a", SINCE, review_count=n)
    assert est == exact_in_window(reviews)
    pages = -(-n // 10)
    assert len(fake.page_calls) <= pages.bit_length() + 1    # búsqueda binaria


def test_estimate_total_when_everything_is_recent():
    reviews = make_reviews(350, TODAY, every_days=0.5)
    assert FakeAdapter({"a": reviews}).estimate_total_since("a", SINCE, 350) == 350


def test_estimate_total_when_nothing_is_recent():
    reviews = make_reviews(350, TODAY - timedelta(days=400), every_days=1)
    assert FakeAdapter({"a": reviews}).estimate_total_since("a", SINCE, 350) == 0


def test_estimate_total_when_no_reviews_are_readable():
    assert FakeAdapter({"a": []}).estimate_total_since("a", SINCE, 500) == 0
