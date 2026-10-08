"""Interfaz común de las tiendas (Shopify ahora; Chrome Web Store en la Fase 4)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Iterator


@dataclass
class ListingCard:
    app_id: str
    name: str | None
    rating_shown: float | None
    review_count: int
    position: int


@dataclass
class AppMeta:
    app_id: str
    name: str | None
    developer: str | None
    url: str
    categories: list[dict] = field(default_factory=list)   # [{"name", "handle"}]
    pricing: list[dict] = field(default_factory=list)      # [{"name", "price", "details"}]
    rating_shown: float | None = None
    review_count: int = 0
    counts: dict[int, int] = field(default_factory=dict)   # {5: n_5, ..., 1: n_1}
    launched: str | None = None
    tagline: str | None = None
    description: str | None = None
    features: list[str] = field(default_factory=list)
    feature_tags: list[str] = field(default_factory=list)   # etiquetas de funciones de la ficha

    @property
    def rating_computed(self) -> float | None:
        total = sum(self.counts.values())
        if not total:
            return None
        return round(sum(k * v for k, v in self.counts.items()) / total, 4)


@dataclass
class Review:
    review_id: str
    rating: int
    date_shown: str          # ISO AAAA-MM-DD; puede ser la fecha de edición
    edited: bool
    country: str | None
    usage_duration: str | None
    body: str
    has_dev_reply: bool
    dev_reply_date: str | None


@dataclass
class ReviewPage:
    reviews: list[Review]
    has_next: bool


class StoreAdapter(ABC):
    store: str

    @abstractmethod
    def list_apps(self) -> list[str]:
        """Identificadores de todas las apps del catálogo."""

    @abstractmethod
    def list_categories(self) -> list[str]:
        """Categorías que se recorren en el inventario (las hojas del árbol)."""

    @abstractmethod
    def list_category(self, category: str) -> Iterator[ListingCard]:
        """Tarjetas del listado de una categoría, en orden."""

    @abstractmethod
    def fetch_app_meta(self, app_id: str) -> AppMeta | None:
        """Ficha de la app; None si ya no existe."""

    @abstractmethod
    def fetch_review_page(self, app_id: str, page: int, ratings: tuple[int, ...] | None = None) -> ReviewPage:
        """Una página de reseñas ordenadas de más reciente a más antigua."""

    def fetch_reviews(self, app_id: str, since: date, max_pages: int,
                      ratings: tuple[int, ...] | None = None) -> tuple[list[Review], bool]:
        """Reseñas creadas desde `since`, de más reciente a más antigua.

        El orden sigue la fecha de creación, pero la fecha visible de una reseña editada es la de
        edición. Por eso solo se para al encontrar una reseña NO editada anterior a `since`.
        Devuelve (reseñas, completo). `completo` es False si se agotó `max_pages` antes del corte.
        """
        cutoff = since.isoformat()
        out: list[Review] = []
        for page in range(1, max_pages + 1):
            rp = self.fetch_review_page(app_id, page, ratings)
            for r in rp.reviews:
                if not r.edited and r.date_shown < cutoff:
                    return out, True
                out.append(r)
            if not rp.has_next or not rp.reviews:
                return out, True
        return out, False

    def estimate_total_since(self, app_id: str, since: date, review_count: int) -> int:
        """Número de reseñas creadas desde `since`, con búsqueda binaria de la página de corte.

        Busca la primera página que contiene una reseña no editada anterior a `since`.
        total = 10 * (página - 1) + posición del corte dentro de la página.
        """
        cutoff = since.isoformat()
        per_page = 10
        last_page = max(1, -(-review_count // per_page))

        def boundary(page: int) -> tuple[int | None, int]:
            """(índice de la primera reseña anterior al corte o None, nº de reseñas de la página)."""
            rp = self.fetch_review_page(app_id, page)
            for i, r in enumerate(rp.reviews):
                if not r.edited and r.date_shown < cutoff:
                    return i, len(rp.reviews)
            return None, len(rp.reviews)

        lo, hi = 1, last_page
        found: tuple[int, int] | None = None
        any_reviews = False
        while lo <= hi:
            mid = (lo + hi) // 2
            idx, n = boundary(mid)
            if n == 0:            # página vacía: el catálogo tiene menos páginas de lo esperado
                hi = mid - 1
                continue
            any_reviews = True
            if idx is None:
                lo = mid + 1
            else:
                found = (mid, idx)
                hi = mid - 1
        if found is None:
            # Sin corte: o todas las reseñas están dentro de la ventana, o no se pudo leer ninguna.
            return review_count if any_reviews else 0
        page, idx = found
        return per_page * (page - 1) + idx
