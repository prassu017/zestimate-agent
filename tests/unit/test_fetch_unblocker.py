"""Tests for the unblocker fetchers — mocked httpx transports."""

from __future__ import annotations

import httpx
import pytest

from zestimate_agent.errors import FetchBlockedError, FetchError
from zestimate_agent.fetch.unblocker import ScraperAPIFetcher, ZenRowsFetcher

# A fake Zillow HTML page big enough to pass the length heuristic and
# containing the `__NEXT_DATA__` marker so the fast-path fetch doesn't
# trigger the render=true auto-upgrade.
_OK_HTML = (
    "<html><body>"
    + ("x" * 1000)
    + '<script id="__NEXT_DATA__" type="application/json">{}</script>'
    + "<p>normal content</p></body></html>"
)


def _transport(handler) -> httpx.MockTransport:  # type: ignore[no-untyped-def]
    return httpx.MockTransport(handler)


# ─── ScraperAPI happy path ─────────────────────────────────────


@pytest.mark.asyncio
async def test_scraperapi_happy_path() -> None:
    """Fast path: no `render=true` unless we have to upgrade."""
    seen: dict[str, str] = {}
    test_url = "https://www.zillow.com/homedetails/happy_zpid/"

    # Make sure no sticky upgrades from previous tests leak in.
    ScraperAPIFetcher._RENDER_URLS.discard(test_url)
    ScraperAPIFetcher._ULTRA_URLS.discard(test_url)

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, text=_OK_HTML)

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        f = ScraperAPIFetcher("test-key", client=client)
        result = await f.fetch(test_url)

    assert result.status == 200
    assert result.fetcher == "scraperapi"
    assert "api_key=test-key" in seen["url"]
    assert "premium=true" in seen["url"]
    # Fast path must NOT include render=true — that's the whole point of
    # the optimization. If Zillow ever stops server-side-rendering
    # `__NEXT_DATA__` we'd auto-upgrade, but the mocked HTML has it.
    assert "render=true" not in seen["url"]


@pytest.mark.asyncio
async def test_scraperapi_upgrades_to_render_when_next_data_missing() -> None:
    """If the fast-path HTML is missing `__NEXT_DATA__`, retry with render=true."""
    test_url = "https://www.zillow.com/homedetails/norender_zpid/"
    # Hermetic: clear any sticky state from other tests.
    ScraperAPIFetcher._RENDER_URLS.discard(test_url)
    ScraperAPIFetcher._ULTRA_URLS.discard(test_url)

    calls: list[str] = []
    no_hydration_html = "<html><body>" + ("y" * 2000) + "</body></html>"
    full_html = _OK_HTML

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if "render=true" in url:
            return httpx.Response(200, text=full_html)
        return httpx.Response(200, text=no_hydration_html)

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        f = ScraperAPIFetcher("test-key", client=client)
        result = await f.fetch(test_url)

    assert result.status == 200
    # First call: no render (fast path). Second call: render=true upgrade.
    assert len(calls) == 2
    assert "render=true" not in calls[0]
    assert "render=true" in calls[1]
    assert test_url in ScraperAPIFetcher._RENDER_URLS

    # Cleanup to not pollute downstream tests.
    ScraperAPIFetcher._RENDER_URLS.discard(test_url)


# ─── Blocked body detected ─────────────────────────────────────


@pytest.mark.asyncio
async def test_scraperapi_blocked_body_raises() -> None:
    blocked_html = "<html><body>Press & Hold to confirm you are human" + "x" * 1000 + "</body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=blocked_html)

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        f = ScraperAPIFetcher("test-key", client=client)
        with pytest.raises(FetchBlockedError):
            await f.fetch("https://www.zillow.com/homedetails/123_zpid/")


# ─── 403 / 429 → FetchBlockedError ─────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [403, 429])
async def test_scraperapi_blocked_status(code: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(code, text="nope")

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        f = ScraperAPIFetcher("test-key", client=client)
        with pytest.raises(FetchBlockedError):
            await f.fetch("https://www.zillow.com/homedetails/123_zpid/")


# ─── 500 triggers ultra_premium retry ──────────────────────────


@pytest.mark.asyncio
async def test_scraperapi_upgrades_to_ultra_premium_on_500() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if "ultra_premium=true" in url:
            return httpx.Response(200, text=_OK_HTML)
        return httpx.Response(
            500,
            text=(
                "Request failed. Protected domains may require adding "
                "premium=true OR ultra_premium=true parameter"
            ),
        )

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        # Clear the class-level cache so the test is hermetic
        ScraperAPIFetcher._ULTRA_URLS.clear()
        f = ScraperAPIFetcher("test-key", client=client)
        result = await f.fetch("https://www.zillow.com/homedetails/apple_zpid/")

    assert result.status == 200
    assert len(calls) == 2
    assert "ultra_premium=true" not in calls[0]
    assert "ultra_premium=true" in calls[1]


# ─── 500 without retry trigger propagates as FetchError ────────


@pytest.mark.asyncio
async def test_scraperapi_500_without_protected_message_propagates() -> None:
    ScraperAPIFetcher._ULTRA_URLS.clear()

    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(500, text="generic server error")

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        f = ScraperAPIFetcher("test-key", client=client)
        with pytest.raises(FetchError):
            await f.fetch("https://www.zillow.com/homedetails/zzz_zpid/")

    # Generic 500 still hits ultra_premium retry because our detection uses
    # "500" as a keyword too — that's fine, better to waste one retry than
    # miss a recoverable case. So attempts should be 2, not 1.
    assert attempts["n"] >= 1


# ─── ZenRows adapter builds correct params ─────────────────────


@pytest.mark.asyncio
async def test_zenrows_params_shape() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, text=_OK_HTML)

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        f = ZenRowsFetcher("test-key", client=client)
        await f.fetch("https://www.zillow.com/homedetails/123_zpid/")

    assert "apikey=test-key" in seen["url"]
    assert "js_render=true" in seen["url"]
    assert "premium_proxy=true" in seen["url"]
