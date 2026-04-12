"""Tests for the Rentcast cross-check client and persistent usage counter.

These tests use `httpx.MockTransport` + injected clients — no real network.
Usage-counter tests use a tmp path so they don't touch the shared `.cache/`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from zestimate_agent.crosscheck import (
    RentcastClient,
    RentcastError,
    UsageCounter,
    _current_month,
)
from zestimate_agent.models import NormalizedAddress

# ─── Fixtures ───────────────────────────────────────────────────


def _address() -> NormalizedAddress:
    return NormalizedAddress(
        raw="500 5th Ave W #705, Seattle, WA 98119",
        street="500 5th Ave W #705",
        city="Seattle",
        state="WA",
        zip="98119",
        canonical="500 5th Ave W #705, Seattle, WA 98119",
        parse_confidence=1.0,
    )


def _client_with_handler(
    handler: Any,
    *,
    counter: UsageCounter,
    tolerance_pct: float = 15.0,
) -> RentcastClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return RentcastClient(
        api_key="test-key",
        counter=counter,
        tolerance_pct=tolerance_pct,
        client=http,
    )


# ─── UsageCounter ───────────────────────────────────────────────


class TestUsageCounter:
    def test_empty_counter_starts_at_zero(self, tmp_path: Path) -> None:
        counter = UsageCounter(tmp_path / "usage.json", cap=40)
        snap = counter.snapshot()
        assert snap.used == 0
        assert snap.remaining == 40
        assert not snap.exhausted

    def test_increment_persists_to_disk(self, tmp_path: Path) -> None:
        path = tmp_path / "usage.json"
        counter = UsageCounter(path, cap=40)
        counter.increment()
        counter.increment()
        # Re-read from a brand-new counter to prove persistence
        counter2 = UsageCounter(path, cap=40)
        assert counter2.snapshot().used == 2

    def test_try_consume_refuses_at_cap(self, tmp_path: Path) -> None:
        counter = UsageCounter(tmp_path / "usage.json", cap=3)
        assert counter.try_consume()[0] is True
        assert counter.try_consume()[0] is True
        assert counter.try_consume()[0] is True
        ok, snap = counter.try_consume()
        assert ok is False
        assert snap.used == 3  # did NOT increment past cap
        assert snap.exhausted

    def test_try_consume_honors_overage_flag(self, tmp_path: Path) -> None:
        counter = UsageCounter(tmp_path / "usage.json", cap=1)
        counter.try_consume()  # use up the 1
        ok, snap = counter.try_consume(allow_overage=True)
        assert ok is True
        assert snap.used == 2  # exceeded cap by request

    def test_corrupt_file_is_reset(self, tmp_path: Path) -> None:
        path = tmp_path / "usage.json"
        path.write_text("not json {]")
        counter = UsageCounter(path, cap=40)
        # Should behave like empty and not raise
        snap = counter.snapshot()
        assert snap.used == 0

    def test_month_isolation(self, tmp_path: Path) -> None:
        path = tmp_path / "usage.json"
        path.write_text(json.dumps({"2099-01": 10, "2099-02": 0}))
        counter = UsageCounter(path, cap=40)
        jan = counter.snapshot(month="2099-01")
        feb = counter.snapshot(month="2099-02")
        assert jan.used == 10
        assert feb.used == 0

    def test_reset_specific_month(self, tmp_path: Path) -> None:
        counter = UsageCounter(tmp_path / "usage.json", cap=40)
        counter.increment()
        counter.increment()
        month = _current_month()
        counter.reset(month=month)
        assert counter.snapshot().used == 0


# ─── RentcastClient (mocked HTTP) ───────────────────────────────


class TestRentcastClient:
    @pytest.mark.asyncio
    async def test_happy_path_within_tolerance(self, tmp_path: Path) -> None:
        counter = UsageCounter(tmp_path / "usage.json", cap=40)

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["X-Api-Key"] == "test-key"
            assert "500" in request.url.params["address"]
            return httpx.Response(
                200,
                json={
                    "price": 650_000,
                    "priceRangeLow": 620_000,
                    "priceRangeHigh": 680_000,
                },
            )

        client = _client_with_handler(handler, counter=counter, tolerance_pct=10.0)
        try:
            cc = await client.cross_check(address=_address(), zillow_value=636_500)
        finally:
            await client.aclose()

        assert cc.skipped is False
        assert cc.estimate == 650_000
        assert cc.range_low == 620_000
        assert cc.range_high == 680_000
        assert cc.within_tolerance is True
        assert cc.delta_pct is not None and abs(cc.delta_pct - 2.12) < 0.1
        assert counter.snapshot().used == 1

    @pytest.mark.asyncio
    async def test_outside_tolerance_flags_disagreement(self, tmp_path: Path) -> None:
        counter = UsageCounter(tmp_path / "usage.json", cap=40)

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"price": 900_000})

        client = _client_with_handler(handler, counter=counter, tolerance_pct=10.0)
        try:
            cc = await client.cross_check(address=_address(), zillow_value=636_500)
        finally:
            await client.aclose()

        assert cc.within_tolerance is False
        assert cc.estimate == 900_000
        assert cc.delta_pct is not None and cc.delta_pct > 10.0

    @pytest.mark.asyncio
    async def test_cap_reached_skips_without_http_call(self, tmp_path: Path) -> None:
        counter = UsageCounter(tmp_path / "usage.json", cap=1)
        counter.try_consume()  # exhaust

        call_count = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(200, json={"price": 500_000})

        client = _client_with_handler(handler, counter=counter)
        try:
            cc = await client.cross_check(address=_address(), zillow_value=636_500)
        finally:
            await client.aclose()

        assert cc.skipped is True
        assert "cap" in (cc.skipped_reason or "").lower()
        assert cc.estimate is None
        assert call_count["n"] == 0  # no HTTP call made

    @pytest.mark.asyncio
    async def test_force_bypasses_cap(self, tmp_path: Path) -> None:
        counter = UsageCounter(tmp_path / "usage.json", cap=1)
        counter.try_consume()  # exhaust

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"price": 500_000})

        client = _client_with_handler(handler, counter=counter)
        try:
            cc = await client.cross_check(
                address=_address(), zillow_value=500_000, force=True
            )
        finally:
            await client.aclose()

        assert cc.skipped is False
        assert cc.estimate == 500_000
        assert counter.snapshot().used == 2  # cap exceeded by override

    @pytest.mark.asyncio
    async def test_401_returns_skipped_not_exception(self, tmp_path: Path) -> None:
        counter = UsageCounter(tmp_path / "usage.json", cap=40)

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="unauthorized")

        client = _client_with_handler(handler, counter=counter)
        try:
            cc = await client.cross_check(address=_address(), zillow_value=500_000)
        finally:
            await client.aclose()

        assert cc.skipped is True
        assert "unauthorized" in (cc.skipped_reason or "").lower()

    @pytest.mark.asyncio
    async def test_404_returns_skipped(self, tmp_path: Path) -> None:
        counter = UsageCounter(tmp_path / "usage.json", cap=40)

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(404, text="not found")

        client = _client_with_handler(handler, counter=counter)
        try:
            cc = await client.cross_check(address=_address(), zillow_value=500_000)
        finally:
            await client.aclose()

        assert cc.skipped is True
        assert cc.estimate is None

    @pytest.mark.asyncio
    async def test_response_missing_price_is_skipped(self, tmp_path: Path) -> None:
        counter = UsageCounter(tmp_path / "usage.json", cap=40)

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"latitude": 47.6})

        client = _client_with_handler(handler, counter=counter)
        try:
            cc = await client.cross_check(address=_address(), zillow_value=500_000)
        finally:
            await client.aclose()

        assert cc.skipped is True
        assert "price" in (cc.skipped_reason or "").lower()

    @pytest.mark.asyncio
    async def test_list_response_shape_handled(self, tmp_path: Path) -> None:
        """Rentcast sometimes wraps the AVM in a 1-element list — we unwrap it."""
        counter = UsageCounter(tmp_path / "usage.json", cap=40)

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"price": 700_000}])

        client = _client_with_handler(handler, counter=counter)
        try:
            cc = await client.cross_check(address=_address(), zillow_value=700_000)
        finally:
            await client.aclose()

        assert cc.estimate == 700_000
        assert cc.within_tolerance is True

    @pytest.mark.asyncio
    async def test_transport_error_is_skipped(self, tmp_path: Path) -> None:
        counter = UsageCounter(tmp_path / "usage.json", cap=40)

        def handler(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        client = _client_with_handler(handler, counter=counter)
        try:
            cc = await client.cross_check(address=_address(), zillow_value=500_000)
        finally:
            await client.aclose()

        assert cc.skipped is True
        assert "transport" in (cc.skipped_reason or "").lower() or "boom" in (cc.skipped_reason or "").lower()


# ─── RentcastError is used internally only ──────────────────────


def test_rentcast_error_is_exception() -> None:
    assert issubclass(RentcastError, Exception)
