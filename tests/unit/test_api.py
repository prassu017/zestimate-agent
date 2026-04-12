"""Unit tests for the FastAPI layer.

We use `httpx.AsyncClient` with `ASGITransport` instead of `TestClient` so
async fixtures + lifespan context behave correctly. A stub `ZestimateAgent`
is injected via `create_app(agent=...)` so no network or cache touches disk.

Coverage:
    * /healthz          — always 200
    * /readyz           — 200 with checks dict
    * /version          — reports package version
    * /metrics          — Prometheus exposition format
    * /lookup OK        — 200 + full LookupResponse
    * /lookup NO_ZESTIMATE — 200 with status=no_zestimate
    * /lookup NOT_FOUND — 404
    * /lookup AMBIGUOUS — 409
    * /lookup BLOCKED   — 502
    * /lookup ERROR     — 502
    * /lookup validation — 422 on empty body
    * API key auth      — 401 when configured and missing
    * API key auth      — 200 when configured and correct
    * API key auth      — open when unconfigured
    * Metrics counter   — increments on each /lookup
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from zestimate_agent import __version__
from zestimate_agent.api import create_app
from zestimate_agent.api import metrics as api_metrics
from zestimate_agent.config import Settings
from zestimate_agent.models import CrossCheck, ZestimateResult, ZestimateStatus

# ─── Stub agent ─────────────────────────────────────────────────


class _StubAgent:
    """A drop-in for ZestimateAgent that returns a queued `ZestimateResult`.

    Every call to `aget()` pops the next queued result. If the queue is
    empty it returns a default OK result. Records every call for assertions.
    """

    def __init__(self) -> None:
        self._queue: list[ZestimateResult] = []
        self.calls: list[dict[str, Any]] = []
        self._closed = False

    def queue(self, result: ZestimateResult) -> None:
        self._queue.append(result)

    async def aget(
        self,
        address: str,
        *,
        skip_crosscheck: bool = False,
        force_crosscheck: bool = False,
        use_cache: bool = True,
    ) -> ZestimateResult:
        self.calls.append(
            {
                "address": address,
                "skip_crosscheck": skip_crosscheck,
                "force_crosscheck": force_crosscheck,
                "use_cache": use_cache,
            }
        )
        if self._queue:
            return self._queue.pop(0)
        return _ok_result()

    async def aclose(self) -> None:
        self._closed = True


def _ok_result(value: int = 500_000) -> ZestimateResult:
    return ZestimateResult(
        status=ZestimateStatus.OK,
        value=value,
        zpid="12345",
        matched_address="123 Main St, Seattle, WA 98101",
        zillow_url="https://www.zillow.com/homedetails/12345_zpid/",
        confidence=0.95,
        fetcher="unblocker",
        trace_id="test-trace",
        crosscheck=CrossCheck(
            provider="rentcast",
            estimate=510_000,
            delta_pct=2.0,
            within_tolerance=True,
        ),
    )


def _error_result(
    status: ZestimateStatus, error: str = "boom"
) -> ZestimateResult:
    return ZestimateResult(status=status, error=error, trace_id="test-trace")


# ─── Fixtures ───────────────────────────────────────────────────


def _settings(**overrides: Any) -> Settings:
    """Build a Settings instance with env-neutral defaults for tests."""
    base: dict[str, Any] = {
        "cache_backend": "none",
        "crosscheck_provider": "none",
        "api_host": "127.0.0.1",
        "api_port": 0,
        "api_key": None,
        "cors_origins": "",
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def stub_agent() -> _StubAgent:
    return _StubAgent()


@pytest.fixture
async def client(stub_agent: _StubAgent) -> AsyncIterator[AsyncClient]:
    # Reset per-IP rate limiter so tests don't poison each other.
    from zestimate_agent.api.deps import reset_rate_limiter

    reset_rate_limiter()
    app = create_app(agent=stub_agent, settings=_settings())  # type: ignore[arg-type]
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as c,
        app.router.lifespan_context(app),
    ):
        yield c


# ─── GET / landing page ─────────────────────────────────────────


class TestLanding:
    async def test_root_returns_html(self, client: AsyncClient) -> None:
        r = await client.get("/")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        body = r.text
        # Smoke-check that the inlined UI shipped and wires to /lookup.
        assert "Zestimate Agent" in body
        assert "/lookup" in body
        assert "<form" in body

    async def test_root_bypasses_api_key_auth(self, stub_agent: _StubAgent) -> None:
        """Even when auth is configured, the landing page is public."""
        app = create_app(
            agent=stub_agent,  # type: ignore[arg-type]
            settings=_settings(api_key="secret-sauce"),
        )
        transport = ASGITransport(app=app)
        async with (
            AsyncClient(transport=transport, base_url="http://test") as c,
            app.router.lifespan_context(app),
        ):
            r = await c.get("/")
        assert r.status_code == 200
        assert "Zestimate Agent" in r.text


# ─── /healthz ───────────────────────────────────────────────────


class TestHealth:
    async def test_healthz_always_ok(self, client: AsyncClient) -> None:
        r = await client.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"

    async def test_readyz_reports_checks(self, client: AsyncClient) -> None:
        r = await client.get("/readyz")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["checks"]["agent"] == "ok"
        assert body["checks"]["cache_backend"] == "none"


# ─── /version ───────────────────────────────────────────────────


class TestVersion:
    async def test_version_reports_package(self, client: AsyncClient) -> None:
        r = await client.get("/version")
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "zestimate-agent"
        assert body["version"] == __version__


# ─── /metrics ───────────────────────────────────────────────────


class TestMetrics:
    async def test_metrics_exposition_format(self, client: AsyncClient) -> None:
        r = await client.get("/metrics")
        assert r.status_code == 200
        assert "text/plain" in r.headers["content-type"]
        body = r.text
        # Core metric families should be declared even at zero counts.
        assert "zestimate_lookups_total" in body
        assert "zestimate_lookup_duration_seconds" in body
        assert "zestimate_http_requests_total" in body

    async def test_lookup_increments_counter(
        self, client: AsyncClient, stub_agent: _StubAgent
    ) -> None:
        before = _counter_value("zestimate_lookups_total", status="ok")
        stub_agent.queue(_ok_result())
        r = await client.post("/lookup", json={"address": "123 Main St"})
        assert r.status_code == 200
        after = _counter_value("zestimate_lookups_total", status="ok")
        assert after == before + 1


def _counter_value(name: str, **labels: str) -> float:
    """Read a labelled counter value out of the default Prometheus registry."""
    from prometheus_client import REGISTRY

    val = REGISTRY.get_sample_value(name + "_total", labels) or 0.0
    # Fallback: some client versions already include _total in the metric name.
    if val == 0.0:
        val = REGISTRY.get_sample_value(name, labels) or 0.0
    return val


# ─── /lookup happy path ────────────────────────────────────────


class TestLookup:
    async def test_ok_returns_200_with_full_payload(
        self, client: AsyncClient, stub_agent: _StubAgent
    ) -> None:
        stub_agent.queue(_ok_result(650_000))
        r = await client.post(
            "/lookup",
            json={"address": "123 Main St, Seattle, WA 98101"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["ok"] is True
        assert body["value"] == 650_000
        assert body["zpid"] == "12345"
        assert body["confidence"] == pytest.approx(0.95)
        assert body["crosscheck"]["provider"] == "rentcast"
        assert body["crosscheck"]["within_tolerance"] is True

    async def test_forwards_flags_to_agent(
        self, client: AsyncClient, stub_agent: _StubAgent
    ) -> None:
        stub_agent.queue(_ok_result())
        r = await client.post(
            "/lookup",
            json={
                "address": "123 Main St",
                "skip_crosscheck": True,
                "force_crosscheck": False,
                "use_cache": False,
            },
        )
        assert r.status_code == 200
        call = stub_agent.calls[-1]
        assert call["skip_crosscheck"] is True
        assert call["use_cache"] is False

    async def test_no_zestimate_returns_200(
        self, client: AsyncClient, stub_agent: _StubAgent
    ) -> None:
        stub_agent.queue(_error_result(ZestimateStatus.NO_ZESTIMATE, "no zestimate"))
        r = await client.post("/lookup", json={"address": "Empire State Bldg"})
        assert r.status_code == 200
        assert r.json()["status"] == "no_zestimate"

    async def test_not_found_returns_404(
        self, client: AsyncClient, stub_agent: _StubAgent
    ) -> None:
        stub_agent.queue(_error_result(ZestimateStatus.NOT_FOUND, "no match"))
        r = await client.post("/lookup", json={"address": "999 Fake St"})
        assert r.status_code == 404
        assert r.json()["status"] == "not_found"

    async def test_ambiguous_returns_409(
        self, client: AsyncClient, stub_agent: _StubAgent
    ) -> None:
        stub_agent.queue(_error_result(ZestimateStatus.AMBIGUOUS, "multiple"))
        r = await client.post("/lookup", json={"address": "Main St"})
        assert r.status_code == 409

    async def test_blocked_returns_502(
        self, client: AsyncClient, stub_agent: _StubAgent
    ) -> None:
        stub_agent.queue(_error_result(ZestimateStatus.BLOCKED, "captcha"))
        r = await client.post("/lookup", json={"address": "123 Main St"})
        assert r.status_code == 502

    async def test_error_returns_502(
        self, client: AsyncClient, stub_agent: _StubAgent
    ) -> None:
        stub_agent.queue(_error_result(ZestimateStatus.ERROR, "boom"))
        r = await client.post("/lookup", json={"address": "123 Main St"})
        assert r.status_code == 502

    async def test_validation_rejects_empty_body(
        self, client: AsyncClient
    ) -> None:
        r = await client.post("/lookup", json={})
        assert r.status_code == 422

    async def test_validation_rejects_short_address(
        self, client: AsyncClient
    ) -> None:
        r = await client.post("/lookup", json={"address": "a"})
        assert r.status_code == 422

    async def test_validation_rejects_extra_fields(
        self, client: AsyncClient
    ) -> None:
        r = await client.post(
            "/lookup",
            json={"address": "123 Main St", "unknown": "field"},
        )
        assert r.status_code == 422


# ─── API key auth ───────────────────────────────────────────────


class TestApiKeyAuth:
    async def test_open_when_unconfigured(self, stub_agent: _StubAgent) -> None:
        app = create_app(agent=stub_agent, settings=_settings())  # type: ignore[arg-type]
        transport = ASGITransport(app=app)
        async with (
            AsyncClient(transport=transport, base_url="http://test") as c,
            app.router.lifespan_context(app),
        ):
            stub_agent.queue(_ok_result())
            r = await c.post("/lookup", json={"address": "123 Main St"})
        assert r.status_code == 200

    async def test_rejects_missing_key_when_configured(
        self, stub_agent: _StubAgent
    ) -> None:
        app = create_app(
            agent=stub_agent,  # type: ignore[arg-type]
            settings=_settings(api_key="secret-sauce"),
        )
        transport = ASGITransport(app=app)
        async with (
            AsyncClient(transport=transport, base_url="http://test") as c,
            app.router.lifespan_context(app),
        ):
            r = await c.post("/lookup", json={"address": "123 Main St"})
        assert r.status_code == 401
        body = r.json()
        assert body["error"] == "unauthorized"

    async def test_rejects_wrong_key(self, stub_agent: _StubAgent) -> None:
        app = create_app(
            agent=stub_agent,  # type: ignore[arg-type]
            settings=_settings(api_key="secret-sauce"),
        )
        transport = ASGITransport(app=app)
        async with (
            AsyncClient(transport=transport, base_url="http://test") as c,
            app.router.lifespan_context(app),
        ):
            r = await c.post(
                "/lookup",
                json={"address": "123 Main St"},
                headers={"X-API-Key": "wrong"},
            )
        assert r.status_code == 401

    async def test_accepts_correct_key(self, stub_agent: _StubAgent) -> None:
        app = create_app(
            agent=stub_agent,  # type: ignore[arg-type]
            settings=_settings(api_key="secret-sauce"),
        )
        transport = ASGITransport(app=app)
        async with (
            AsyncClient(transport=transport, base_url="http://test") as c,
            app.router.lifespan_context(app),
        ):
            stub_agent.queue(_ok_result())
            r = await c.post(
                "/lookup",
                json={"address": "123 Main St"},
                headers={"X-API-Key": "secret-sauce"},
            )
        assert r.status_code == 200

    async def test_healthz_bypasses_auth(self, stub_agent: _StubAgent) -> None:
        """Liveness probe must work without an API key."""
        app = create_app(
            agent=stub_agent,  # type: ignore[arg-type]
            settings=_settings(api_key="secret-sauce"),
        )
        transport = ASGITransport(app=app)
        async with (
            AsyncClient(transport=transport, base_url="http://test") as c,
            app.router.lifespan_context(app),
        ):
            r = await c.get("/healthz")
        assert r.status_code == 200


# ─── Metrics helper smoke test ──────────────────────────────────


class TestObserveLookup:
    def test_observe_lookup_handles_all_statuses(self) -> None:
        for status in ZestimateStatus:
            api_metrics.observe_lookup(
                ZestimateResult(status=status, value=1 if status == ZestimateStatus.OK else None),
                elapsed_seconds=0.01,
            )
        # If we got here without raising, the helper is bulletproof.

    def test_set_rentcast_usage_updates_gauges(self) -> None:
        api_metrics.set_rentcast_usage(used=7, cap=40)
        from prometheus_client import REGISTRY

        used = REGISTRY.get_sample_value("zestimate_rentcast_usage")
        cap = REGISTRY.get_sample_value("zestimate_rentcast_cap")
        assert used == 7.0
        assert cap == 40.0
