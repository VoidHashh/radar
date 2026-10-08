"""Cliente HTTP con transporte simulado: caché, reintentos y política de Cloudflare (sin red)."""
from pathlib import Path

import httpx
import pytest

from radar.http import Blocked, HttpClient, HttpError, RobotsDisallowed, Robots

FIX = Path(__file__).parent / "fixtures"
CHALLENGE = (FIX / "cloudflare_challenge_403.html").read_text(encoding="utf-8")


class Script:
    """Devuelve las respuestas en orden y registra cada petición."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        status, headers, body = self.responses.pop(0)
        return httpx.Response(status, headers=headers, text=body)


def make_client(tmp_path, script, **kw):
    sleeps = []
    client = HttpClient(cache_dir=tmp_path / "cache", user_agent="RadarResearchBot/0.1 (test)",
                        transport=httpx.MockTransport(script), sleep=sleeps.append,
                        min_interval_s=0, max_interval_s=0, **kw)
    return client, sleeps


OK = (200, {}, "<html>ok</html>")
CF = (403, {"cf-mitigated": "challenge"}, CHALLENGE)


def test_user_agent_is_identifiable(tmp_path):
    seen = {}

    def handler(request):
        seen["ua"] = request.headers["user-agent"]
        return httpx.Response(200, text="ok")
    client = HttpClient(cache_dir=tmp_path, user_agent="RadarResearchBot/0.1 (test)",
                        transport=httpx.MockTransport(handler), sleep=lambda s: None)
    client.get("https://apps.shopify.com/x")
    assert seen["ua"].startswith("RadarResearchBot/")


def test_cache_hit_avoids_network(tmp_path):
    script = Script(OK)
    client, _ = make_client(tmp_path, script)
    assert client.get("https://apps.shopify.com/a").text == "<html>ok</html>"
    again = client.get("https://apps.shopify.com/a")
    assert again.from_cache and len(script.calls) == 1


def test_challenge_pauses_15_min_and_retries_without_caching(tmp_path):
    script = Script(CF, OK)
    client, sleeps = make_client(tmp_path, script, challenge_pause_s=900)
    resp = client.get("https://apps.shopify.com/a")
    assert resp.status == 200
    assert 900 in sleeps
    assert len(script.calls) == 2
    assert client.consecutive_blocks == 0


def test_challenge_is_never_cached(tmp_path):
    script = Script(CF, CF, CF)
    client, _ = make_client(tmp_path, script, challenge_pause_s=0)
    with pytest.raises(Blocked):
        client.get("https://apps.shopify.com/a")
    assert not list((tmp_path / "cache").rglob("*.gz"))


def test_three_consecutive_blocks_raise(tmp_path):
    script = Script(CF, CF, CF, OK)
    client, sleeps = make_client(tmp_path, script, challenge_pause_s=900, max_consecutive_blocks=3)
    with pytest.raises(Blocked):
        client.get("https://apps.shopify.com/a")
    assert len(script.calls) == 3          # el tercer bloqueo para, sin cuarto intento
    assert sleeps.count(900) == 2


def test_block_counter_spans_urls_and_resets_on_success(tmp_path):
    script = Script(CF, OK, CF, OK)
    client, _ = make_client(tmp_path, script, challenge_pause_s=0)
    client.get("https://apps.shopify.com/a")
    client.get("https://apps.shopify.com/b")
    assert client.consecutive_blocks == 0


def test_429_and_5xx_backoff(tmp_path):
    script = Script((429, {}, ""), (503, {}, ""), OK)
    client, sleeps = make_client(tmp_path, script, backoff_base_s=5)
    assert client.get("https://apps.shopify.com/a").status == 200
    assert sleeps[-2:] == [5, 10]


def test_retry_after_is_respected(tmp_path):
    script = Script((429, {"retry-after": "42"}, ""), OK)
    client, sleeps = make_client(tmp_path, script)
    client.get("https://apps.shopify.com/a")
    assert 42 in sleeps


def test_gives_up_after_max_retries(tmp_path):
    script = Script(*[(500, {}, "")] * 3)
    client, _ = make_client(tmp_path, script, max_retries=2)
    with pytest.raises(HttpError):
        client.get("https://apps.shopify.com/a")


def test_404_is_returned_not_cached(tmp_path):
    script = Script((404, {}, "nope"), (404, {}, "nope"))
    client, _ = make_client(tmp_path, script)
    assert client.get("https://apps.shopify.com/gone").status == 404
    assert client.get("https://apps.shopify.com/gone").status == 404
    assert len(script.calls) == 2


def test_robots_disallowed_url_is_refused(tmp_path):
    script = Script()
    client, _ = make_client(tmp_path, script)
    client.robots = Robots((FIX / "robots.txt").read_text(encoding="utf-8"), "RadarResearchBot/0.1")
    with pytest.raises(RobotsDisallowed):
        client.get("https://apps.shopify.com/search?q=email")
    assert script.calls == []


def test_pacing_waits_between_requests(tmp_path):
    clock = iter([0.0, 0.5, 2.0]).__next__
    sleeps = []
    client = HttpClient(cache_dir=tmp_path, user_agent="RadarResearchBot/0.1 (test)",
                        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="ok")),
                        sleep=sleeps.append, clock=clock, min_interval_s=2, max_interval_s=2)
    client.get("https://apps.shopify.com/a")
    client.get("https://apps.shopify.com/b")
    assert sleeps == [pytest.approx(1.5)]
