"""Adaptador simulado para probar la lógica de ventana y el pipeline sin red."""
from __future__ import annotations

import itertools
from datetime import date, timedelta

from radar.stores.base import AppMeta, ListingCard, Review, ReviewPage, StoreAdapter


_ID_BASE = itertools.count(1)


def make_reviews(n: int, start: date, every_days: float, edited_every: int = 0,
                 neg_every: int = 4) -> list[Review]:
    """n reseñas de la más reciente a la más antigua, una cada `every_days` días."""
    base = next(_ID_BASE) * 1_000_000      # ids únicos entre llamadas, decrecientes dentro de una
    out = []
    for i in range(n):
        created = start - timedelta(days=int(i * every_days))
        edited = bool(edited_every) and i % edited_every == edited_every - 1
        shown = (start if edited else created).isoformat()
        rating = 1 if i % neg_every == 0 else 5
        out.append(Review(review_id=str(base + 999_999 - i), rating=rating, date_shown=shown, edited=edited,
                          country="Spain", usage_duration="2 months", body=f"review {i} doesn't work",
                          has_dev_reply=i % 2 == 0, dev_reply_date=None))
    return out


class FakeAdapter(StoreAdapter):
    store = "fake"

    def __init__(self, reviews_by_app: dict[str, list[Review]] | None = None,
                 metas: dict[str, AppMeta] | None = None, listings: dict[str, list[ListingCard]] | None = None,
                 sitemap: list[str] | None = None, intermediates: list[str] | None = None):
        self.reviews_by_app = reviews_by_app or {}
        self.metas = metas or {}
        self.listings = listings or {}
        self.sitemap = sitemap or []
        self.intermediates = intermediates or []
        self.page_calls: list[tuple] = []

    def list_apps(self):
        return list(self.sitemap)

    def list_categories(self):
        return list(self.listings) + list(self.intermediates)

    def category_listing(self, category):
        if category in self.intermediates:
            return None                      # /all devuelve 404
        cards = self.listings[category]
        return list(cards), len({c.app_id for c in cards})

    def list_category(self, category):
        yield from self.listings[category]

    def fetch_app_meta(self, app_id):
        return self.metas.get(app_id)

    def fetch_review_page(self, app_id, page, ratings=None):
        self.page_calls.append((app_id, page, ratings))
        rs = self.reviews_by_app.get(app_id, [])
        if ratings:
            rs = [r for r in rs if r.rating in ratings]
        chunk = rs[(page - 1) * 10: page * 10]
        return ReviewPage(reviews=chunk, has_next=page * 10 < len(rs))
