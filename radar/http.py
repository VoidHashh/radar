"""Cliente HTTP educado: ritmo 2-3 s, caché gzip en disco, reintentos, robots.txt y Cloudflare.

Política de Cloudflare (docs/decisions.md, D5):
- 403 de verificación -> pausa de 15 min y reintento de la misma URL.
- 3 bloqueos seguidos -> se lanza `Blocked`; el pipeline guarda estado y para.
- Nunca se cachea una página de verificación. Nunca se intenta esquivar.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

CHALLENGE_TITLE = "Verifying your connection"


class HttpError(Exception):
    """Fallo definitivo tras agotar los reintentos."""


class Blocked(Exception):
    """Cloudflare bloqueó `max_consecutive_blocks` veces seguidas."""


class RobotsDisallowed(Exception):
    """robots.txt prohíbe la URL pedida."""


@dataclass
class Response:
    url: str
    status: int
    text: str
    from_cache: bool = False


def is_challenge(status: int, headers: dict[str, str] | httpx.Headers, text: str) -> bool:
    """True si la respuesta es la página de verificación de Cloudflare."""
    if status != 403:
        return False
    mitigated = (headers.get("cf-mitigated") or "").lower()
    return mitigated == "challenge" or CHALLENGE_TITLE in text[:5000]


# --------------------------------------------------------------------------- robots.txt

class Robots:
    """Intérprete mínimo de robots.txt con comodines `*` y `$` (semántica de Google).

    Gana la regla más larga; en empate, gana Allow.
    """

    def __init__(self, text: str, user_agent: str):
        self.rules: list[tuple[bool, re.Pattern, int]] = []
        self.sitemaps: list[str] = []
        groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
        agents: list[str] = []
        rules: list[tuple[bool, str]] = []
        last_was_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field, value = (s.strip() for s in line.split(":", 1))
            field = field.lower()
            if field == "user-agent":
                if not last_was_agent and agents:
                    groups.append((agents, rules))
                    agents, rules = [], []
                agents.append(value.lower())
                last_was_agent = True
            elif field in ("allow", "disallow"):
                rules.append((field == "allow", value))
                last_was_agent = False
            elif field == "sitemap":
                self.sitemaps.append(value)
        if agents:
            groups.append((agents, rules))

        token = user_agent.split("/")[0].lower()
        chosen = next((r for a, r in groups if any(x != "*" and x in token for x in a)), None)
        if chosen is None:
            chosen = next((r for a, r in groups if "*" in a), [])
        for allow, pattern in chosen:
            if pattern == "" and not allow:
                continue  # "Disallow:" vacío = todo permitido
            self.rules.append((allow, self._compile(pattern), len(pattern)))

    @staticmethod
    def _compile(pattern: str) -> re.Pattern:
        anchored = pattern.endswith("$")
        body = pattern[:-1] if anchored else pattern
        rx = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
        return re.compile(rx + ("$" if anchored else ""))

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        best: tuple[int, bool] | None = None
        for allow, rx, length in self.rules:
            if rx.match(target):
                if best is None or length > best[0] or (length == best[0] and allow):
                    best = (length, allow)
        return True if best is None else best[1]


# --------------------------------------------------------------------------- cliente

class HttpClient:
    def __init__(
        self,
        cache_dir: str | Path,
        user_agent: str,
        accept_language: str = "en-US,en;q=0.9",
        min_interval_s: float = 2.0,
        max_interval_s: float = 3.0,
        timeout_s: float = 30,
        max_retries: int = 5,
        backoff_base_s: float = 5,
        cache_max_age_hours: float = 144,
        challenge_pause_s: float = 900,
        max_consecutive_blocks: int = 3,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_interval_s = min_interval_s
        self.max_interval_s = max_interval_s
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self.cache_max_age = timedelta(hours=cache_max_age_hours)
        self.challenge_pause_s = challenge_pause_s
        self.max_consecutive_blocks = max_consecutive_blocks
        self.consecutive_blocks = 0
        self.robots: Robots | None = None
        self.user_agent = user_agent
        self._sleep = sleep
        self._clock = clock
        self._last_request: float | None = None
        self.stats = {"network": 0, "cache": 0, "challenges": 0, "retries": 0}
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Language": accept_language},
            follow_redirects=True,
            timeout=timeout_s,
            transport=transport,
        )

    @classmethod
    def from_config(cls, cfg: dict, **overrides) -> "HttpClient":
        h = cfg["http"]
        kwargs = dict(
            cache_dir=cfg["cache_dir"],
            user_agent=h["user_agent"],
            accept_language=h.get("accept_language", "en-US,en;q=0.9"),
            min_interval_s=h["min_interval_s"],
            max_interval_s=h["max_interval_s"],
            timeout_s=h["timeout_s"],
            max_retries=h["max_retries"],
            backoff_base_s=h["backoff_base_s"],
            cache_max_age_hours=h["cache_max_age_hours"],
            challenge_pause_s=h["challenge_pause_s"],
            max_consecutive_blocks=h["max_consecutive_blocks"],
        )
        kwargs.update(overrides)
        return cls(**kwargs)

    def close(self) -> None:
        self._client.close()

    # ---- robots
    def load_robots(self, robots_url: str) -> Robots:
        resp = self.get(robots_url, check_robots=False)
        if resp.status != 200:
            raise HttpError(f"robots.txt devolvió {resp.status}: {robots_url}")
        self.robots = Robots(resp.text, self.user_agent)
        return self.robots

    # ---- caché
    def _cache_paths(self, url: str) -> tuple[Path, Path]:
        h = hashlib.sha1(url.encode()).hexdigest()
        d = self.cache_dir / h[:2]
        return d / f"{h}.gz", d / f"{h}.json"

    def _read_cache(self, url: str) -> Response | None:
        body_p, meta_p = self._cache_paths(url)
        if not (body_p.exists() and meta_p.exists()):
            return None
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(meta["fetched_at"])
        if datetime.now(timezone.utc) - fetched > self.cache_max_age:
            return None
        text = gzip.decompress(body_p.read_bytes()).decode("utf-8")
        return Response(url=meta.get("final_url", url), status=meta["status"], text=text, from_cache=True)

    def _write_cache(self, url: str, resp: Response) -> None:
        body_p, meta_p = self._cache_paths(url)
        body_p.parent.mkdir(parents=True, exist_ok=True)
        body_p.write_bytes(gzip.compress(resp.text.encode("utf-8")))
        meta = {"url": url, "final_url": resp.url, "status": resp.status,
                "fetched_at": datetime.now(timezone.utc).isoformat()}
        meta_p.write_text(json.dumps(meta), encoding="utf-8")

    # ---- red
    def _pace(self) -> None:
        if self._last_request is not None:
            wait = random.uniform(self.min_interval_s, self.max_interval_s)
            elapsed = self._clock() - self._last_request
            if elapsed < wait:
                self._sleep(wait - elapsed)
        self._last_request = self._clock()

    def get(self, url: str, use_cache: bool = True, check_robots: bool = True) -> Response:
        """GET con caché. Devuelve 200 y 404/410; reintenta 429/5xx; aplica la política de Cloudflare."""
        if check_robots and self.robots is not None and not self.robots.allowed(url):
            raise RobotsDisallowed(url)
        if use_cache:
            cached = self._read_cache(url)
            if cached is not None:
                self.stats["cache"] += 1
                return cached

        attempt = 0
        while True:
            self._pace()
            self.stats["network"] += 1
            try:
                r = self._client.get(url)
                status, text, headers = r.status_code, r.text, r.headers
                final_url = str(r.url)
            except httpx.TransportError as e:
                status, text, headers, final_url = None, "", {}, url
                log.warning("Error de red en %s: %s", url, e)

            if status is not None and is_challenge(status, headers, text):
                self.stats["challenges"] += 1
                self.consecutive_blocks += 1
                log.warning("Cloudflare: verificación en %s (bloqueo %d seguido)", url, self.consecutive_blocks)
                if self.consecutive_blocks >= self.max_consecutive_blocks:
                    raise Blocked(f"{self.consecutive_blocks} bloqueos seguidos de Cloudflare; último: {url}")
                log.warning("Pausa de %.0f s antes de reintentar", self.challenge_pause_s)
                self._sleep(self.challenge_pause_s)
                continue

            if status is not None and status not in (429,) and status < 500:
                self.consecutive_blocks = 0
                resp = Response(url=final_url, status=status, text=text)
                if status == 200:
                    self._write_cache(url, resp)
                return resp

            attempt += 1
            self.stats["retries"] += 1
            if attempt > self.max_retries:
                raise HttpError(f"{url}: sin éxito tras {self.max_retries} reintentos (último estado {status})")
            retry_after = headers.get("retry-after") if status is not None else None
            delay = float(retry_after) if retry_after and retry_after.isdigit() else self.backoff_base_s * 2 ** (attempt - 1)
            log.warning("Estado %s en %s; reintento %d en %.0f s", status, url, attempt, delay)
            self._sleep(delay)
