"""Adaptador de la Shopify App Store. Selectores verificados en docs/recon.md."""
from __future__ import annotations

import json
import logging
import re
from typing import Iterator
from urllib.parse import quote

from selectolax.lexbor import LexborHTMLParser, LexborNode

from ..http import HttpClient
from .base import AppMeta, ListingCard, Review, ReviewPage, StoreAdapter

log = logging.getLogger(__name__)

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}
DATE_RX = re.compile(r"([A-Z][a-z]+) (\d{1,2}), (\d{4})")


def parse_date(text: str) -> str | None:
    """'October 1, 2026' -> '2026-10-01'. Independiente del locale del sistema."""
    m = DATE_RX.search(text or "")
    if not m or m.group(1) not in MONTHS:
        return None
    return f"{int(m.group(3)):04d}-{MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}"


def _clean(text: str) -> str:
    return " ".join(text.split())


# --------------------------------------------------------------------------- sitemaps

def parse_sitemap_locs(xml: str) -> list[str]:
    return re.findall(r"<loc>\s*(.*?)\s*</loc>", xml)


def handles_from_sitemap(xml: str) -> list[str]:
    """Handles de apps del sitemap; cada <loc> es https://apps.shopify.com/{handle}."""
    out, seen = [], set()
    for loc in parse_sitemap_locs(xml):
        h = loc.rstrip("/").rsplit("/", 1)[-1]
        if h and h not in seen:
            seen.add(h)
            out.append(h)
    return out


def root_categories(handles: list[str]) -> list[str]:
    """Raíces del árbol: ningún otro handle es prefijo suyo ("store-design", "sales-channels"...)."""
    hs = list(dict.fromkeys(handles))
    return [h for h in hs if not any(o != h and h.startswith(o + "-") for o in hs)]


def listing_candidates(handles: list[str]) -> list[str]:
    """Categorías cuyo listado /all hay que pedir: todas menos las raíces.

    No se puede saber por el nombre si una categoría es hoja o intermedia:
    "orders-and-shipping-shipping-solutions-shipping" es una hoja, aunque sea prefijo de su hermana
    "...-shipping-rates". Las intermedias devuelven 404 en /all, así que se piden todas y el 404
    marca la categoría como intermedia (docs/recon.md, sección 6)."""
    roots = set(root_categories(handles))
    return [h for h in dict.fromkeys(handles) if h not in roots]


# --------------------------------------------------------------------------- listado de categoría

def parse_category_page(html: str) -> tuple[list[ListingCard], bool, int | None]:
    """(tarjetas, hay_siguiente, total de apps que anuncia la categoría)."""
    tree = LexborHTMLParser(html)
    cards = []
    for node in tree.css('div[data-controller="app-card"]'):
        handle = node.attributes.get("data-app-card-handle-value")
        if not handle:
            continue
        text = node.text(separator=" ")
        m_count = re.search(r"(\d+) total reviews", text)
        m_rating = re.search(r"(\d(?:\.\d)?)\s*out of 5 stars", text)
        pos = node.attributes.get("data-app-card-intra-position-value") or "0"
        cards.append(ListingCard(
            app_id=handle,
            name=node.attributes.get("data-app-card-name-value"),
            rating_shown=float(m_rating.group(1)) if m_rating else None,
            review_count=int(m_count.group(1)) if m_count else 0,
            position=int(pos) if pos.isdigit() else 0,
        ))
    has_next = tree.css_first('a[rel="next"]') is not None
    m_total = re.search(r">\s*([\d,]+) apps?\s*<", html)
    total = int(m_total.group(1).replace(",", "")) if m_total else None
    return cards, has_next, total


# --------------------------------------------------------------------------- ficha de la app

def _json_ld(tree: LexborHTMLParser) -> dict:
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text())
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "SoftwareApplication":
            return data
    return {}


def parse_distribution(html: str, handle: str) -> dict[int, int]:
    """Recuentos exactos por estrella: <a aria-label="2938 total reviews" href="/{handle}/reviews?ratings%5B%5D=5">."""
    counts = {k: 0 for k in range(1, 6)}
    rx = re.compile(r'aria-label="(\d+) total reviews"\s+href="/' + re.escape(handle)
                    + r'/reviews\?ratings%5B%5D=(\d)"')
    for n, star in rx.findall(html):
        counts[int(star)] = int(n)
    return counts


def parse_app_page(html: str, handle: str, base_url: str = "https://apps.shopify.com") -> AppMeta:
    tree = LexborHTMLParser(html)
    ld = _json_ld(tree)
    agg = ld.get("aggregateRating") or {}

    categories, seen = [], set()
    feature_tags: list[str] = []
    for a in tree.css('a[href*="/categories/"][href*="surface_type=app_details"]'):
        href = a.attributes.get("href") or ""
        if "feature_handles" in href:
            tag = a.text(strip=True)       # etiquetas de funciones, no categorías
            if tag and tag not in feature_tags:
                feature_tags.append(tag)
            continue
        cat = href.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
        if cat not in seen:
            seen.add(cat)
            categories.append({"name": a.text(strip=True), "handle": cat})

    pricing = []
    for card in tree.css('[data-pricing-component-target="cardHeading"]'):
        name = card.css_first('[data-test-id="name"]')
        price = card.css_first('[data-pricing-component-target="cardHeadingPrice"] h3')
        extra = card.css_first('[data-test-id="additional-charges"]')
        pricing.append({
            "name": name.text(strip=True) if name else None,
            "price": price.attributes.get("aria-label") if price else None,
            "details": _clean(extra.text()) if extra else None,
        })

    dev = tree.css_first('a[href*="/partners/"]')
    canonical = tree.css_first('link[rel="canonical"]')
    m_launched = re.search(r"Launched\s*</p>\s*<p[^>]*>\s*([A-Z][a-z]+ \d{1,2}, \d{4})", html)

    details = tree.css_first("#app-details")
    tagline = description = None
    features: list[str] = []
    if details is not None:
        h2, para = details.css_first("h2"), details.css_first("p")
        tagline = _clean(h2.text()) if h2 else None
        description = _clean(para.text()) if para else None
        features = [_clean(li.text()) for li in details.css("ul li") if _clean(li.text())]

    counts = parse_distribution(html, handle)
    review_count = int(agg.get("ratingCount") or 0)
    if sum(counts.values()) != review_count:
        log.warning("%s: la distribución suma %d y ratingCount es %d", handle, sum(counts.values()), review_count)

    return AppMeta(
        app_id=handle,
        name=ld.get("name"),
        developer=ld.get("brand") or (dev.text(strip=True) if dev else None),
        url=canonical.attributes.get("href") if canonical else f"{base_url}/{handle}",
        categories=categories,
        pricing=pricing,
        rating_shown=float(agg["ratingValue"]) if agg.get("ratingValue") is not None else None,
        review_count=review_count,
        counts=counts,
        launched=parse_date(m_launched.group(1)) if m_launched else None,
        tagline=tagline,
        description=description,
        features=features,
        feature_tags=feature_tags,
    )


# --------------------------------------------------------------------------- reseñas

def _next_div(node: LexborNode) -> LexborNode | None:
    sib = node.next
    while sib is not None and sib.tag != "div":
        sib = sib.next
    return sib


def _parse_review(node: LexborNode) -> Review | None:
    rid = node.attributes.get("data-review-content-id")
    stars = node.css_first('[aria-label$="out of 5 stars"]')
    if not rid or stars is None:
        return None
    m = re.match(r"(\d) out of 5 stars", stars.attributes.get("aria-label") or "")
    rating = int(m.group(1)) if m else None

    date_div = _next_div(stars)
    date_text = _clean(date_div.text()) if date_div else ""
    edited = date_text.startswith("Edited")

    body_node = node.css_first("[data-truncate-review]:not([data-reply-id]) [data-truncate-content-copy]")
    if body_node is not None:
        paragraphs = [_clean(p.text()) for p in body_node.css("p")]
        body = "\n".join(p for p in paragraphs if p) or _clean(body_node.text())
    else:
        body = ""

    # País y tiempo de uso: divs hermanos de la fila con el nombre de tienda (que NO se guarda).
    country = usage = None
    share = node.css_first("[data-review-share-link]")
    container = share.parent.parent if share is not None and share.parent is not None else None
    if container is not None:
        for child in container.iter():
            if child.tag != "div" or child.css_first("[data-review-share-link]") is not None:
                continue
            text = _clean(child.text())
            if not text:
                continue
            if text.endswith("using the app"):
                usage = text[: -len("using the app")].strip()
            elif country is None:
                country = text

    reply = node.css_first("[data-merchant-review-reply]")
    has_reply = reply is not None and reply.css_first("[data-reply-id]") is not None
    reply_date = None
    if has_reply:
        m_reply = re.search(r"replied\s+([A-Z][a-z]+ \d{1,2}, \d{4})", _clean(reply.text(separator=" ")))
        reply_date = parse_date(m_reply.group(1)) if m_reply else None

    return Review(
        review_id=rid,
        rating=rating,
        date_shown=parse_date(date_text),
        edited=edited,
        country=country,
        usage_duration=usage,
        body=body,
        has_dev_reply=has_reply,
        dev_reply_date=reply_date,
    )


def parse_reviews_page(html: str) -> ReviewPage:
    tree = LexborHTMLParser(html)
    reviews = []
    for node in tree.css("div[data-merchant-review]"):
        r = _parse_review(node)
        if r is not None:
            reviews.append(r)
    has_next = tree.css_first('a[rel="next"]') is not None
    return ReviewPage(reviews=reviews, has_next=has_next)


# --------------------------------------------------------------------------- adaptador

class ShopifyAdapter(StoreAdapter):
    store = "shopify"

    def __init__(self, http: HttpClient, base_url: str = "https://apps.shopify.com",
                 sitemap_apps: str | None = None, sitemap_categories: str | None = None,
                 max_category_pages: int = 300):
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.sitemap_apps = sitemap_apps or f"{self.base_url}/sitemap_apps_en.xml"
        self.sitemap_categories = sitemap_categories or f"{self.base_url}/sitemap_categories_en.xml"
        self.max_category_pages = max_category_pages

    @classmethod
    def from_config(cls, http: HttpClient, cfg: dict) -> "ShopifyAdapter":
        s = cfg["store"]
        return cls(http, s["base_url"], s["sitemap_apps"], s["sitemap_categories"], s["max_category_pages"])

    def _get_ok(self, url: str) -> str | None:
        resp = self.http.get(url)
        if resp.status == 200:
            return resp.text
        if resp.status in (404, 410):
            return None
        raise RuntimeError(f"Estado inesperado {resp.status} en {url}")

    def list_apps(self) -> list[str]:
        xml = self._get_ok(self.sitemap_apps)
        if xml is None:
            raise RuntimeError(f"No se encontró el sitemap de apps: {self.sitemap_apps}")
        return handles_from_sitemap(xml)

    def list_categories(self) -> list[str]:
        xml = self._get_ok(self.sitemap_categories)
        if xml is None:
            raise RuntimeError(f"No se encontró el sitemap de categorías: {self.sitemap_categories}")
        return listing_candidates(handles_from_sitemap(xml))

    def category_url(self, category: str, page: int) -> str:
        url = f"{self.base_url}/categories/{quote(category)}/all"
        return url if page == 1 else f"{url}?page={page}"

    def category_listing(self, category: str) -> tuple[list[ListingCard], int | None] | None:
        """Todas las tarjetas de la categoría y el total que anuncia su página 1.

        None si la categoría no tiene listado propio (categoría intermedia: /all devuelve 404)."""
        out: list[ListingCard] = []
        announced = None
        for page in range(1, self.max_category_pages + 1):
            html = self._get_ok(self.category_url(category, page))
            if html is None:
                if page == 1:
                    return None
                break
            cards, has_next, total = parse_category_page(html)
            if page == 1:
                announced = total
            for c in cards:
                c.position = len(out) + 1
                out.append(c)
            if not has_next or not cards:
                break
        else:
            log.warning("%s: se alcanzó el tope de %d páginas", category, self.max_category_pages)
        return out, announced

    def list_category(self, category: str) -> Iterator[ListingCard]:
        listing = self.category_listing(category)
        if listing is not None:
            yield from listing[0]

    def fetch_app_meta(self, app_id: str) -> AppMeta | None:
        html = self._get_ok(f"{self.base_url}/{quote(app_id)}")
        return parse_app_page(html, app_id, self.base_url) if html else None

    def reviews_url(self, app_id: str, page: int, ratings: tuple[int, ...] | None = None) -> str:
        params = [f"ratings%5B%5D={r}" for r in (ratings or ())] + ["sort_by=newest"]
        if page > 1:
            params.append(f"page={page}")
        return f"{self.base_url}/{quote(app_id)}/reviews?" + "&".join(params)

    def fetch_review_page(self, app_id: str, page: int, ratings: tuple[int, ...] | None = None) -> ReviewPage:
        html = self._get_ok(self.reviews_url(app_id, page, ratings))
        return parse_reviews_page(html) if html else ReviewPage(reviews=[], has_next=False)

    def permalink(self, review_id: str) -> str:
        return f"{self.base_url}/reviews/{review_id}"
