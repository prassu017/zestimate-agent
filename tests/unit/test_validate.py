"""Validator tests — sanity bounds + cross-check confidence adjustment."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from zestimate_agent.crosscheck import RentcastClient, UsageCounter
from zestimate_agent.models import (
    CrossCheck,
    NormalizedAddress,
    ZestimateResult,
    ZestimateStatus,
)
from zestimate_agent.validate import cross_check, sanity_check, validate


def _ok_result(value: int = 636_500, confidence: float = 0.9) -> ZestimateResult:
    return ZestimateResult(
        status=ZestimateStatus.OK,
        value=value,
        zpid="82362438",
        matched_address="500 5th Ave W, Seattle, WA 98119",
        zillow_url="https://www.zillow.com/homedetails/82362438_zpid/",
        fetcher="scraperapi",
        fetched_at=datetime(2026, 4, 10, tzinfo=UTC),
        confidence=confidence,
    )


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


def _mock_client(handler: Any, tmp_path: Path, *, tolerance_pct: float = 10.0) -> RentcastClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return RentcastClient(
        api_key="test-key",
        counter=UsageCounter(tmp_path / "usage.json", cap=40),
        tolerance_pct=tolerance_pct,
        client=http,
    )


# ─── Sanity ─────────────────────────────────────────────────────


class TestSanityCheck:
    def test_reasonable_value_passes(self) -> None:
        result = _ok_result(value=636_500)
        assert sanity_check(result) is result or sanity_check(result).status == ZestimateStatus.OK

    def test_value_below_floor_becomes_error(self) -> None:
        result = _ok_result(value=42)
        checked = sanity_check(result)
        assert checked.status == ZestimateStatus.ERROR
        assert checked.error is not None and "floor" in checked.error

    def test_value_above_ceiling_becomes_error(self) -> None:
        result = _ok_result(value=999_000_000_000)
        checked = sanity_check(result)
        assert checked.status == ZestimateStatus.ERROR
        assert checked.error is not None and "ceiling" in checked.error

    def test_non_ok_result_passes_through(self) -> None:
        result = ZestimateResult(status=ZestimateStatus.NOT_FOUND)
        assert sanity_check(result).status == ZestimateStatus.NOT_FOUND

    def test_none_value_passes_through(self) -> None:
        result = ZestimateResult(status=ZestimateStatus.NO_ZESTIMATE, zpid="1")
        assert sanity_check(result).status == ZestimateStatus.NO_ZESTIMATE


# ─── Cross-check ────────────────────────────────────────────────


class TestCrossCheck:
    @pytest.mark.asyncio
    async def test_none_client_is_no_op(self, tmp_path: Path) -> None:
        result = _ok_result(confidence=0.9)
        out = await cross_check(result, client=None, address=_address())
        assert out.confidence == 0.9
        assert out.crosscheck is None

    @pytest.mark.asyncio
    async def test_non_ok_result_unchanged(self, tmp_path: Path) -> None:
        result = ZestimateResult(status=ZestimateStatus.BLOCKED, confidence=0.5)
        client = _mock_client(
            lambda _r: httpx.Response(200, json={"price": 500_000}), tmp_path
        )
        try:
            out = await cross_check(result, client=client, address=_address())
        finally:
            await client.aclose()
        assert out.crosscheck is None
        assert out.confidence == 0.5

    @pytest.mark.asyncio
    async def test_agreement_preserves_confidence(self, tmp_path: Path) -> None:
        result = _ok_result(value=636_500, confidence=0.9)

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"price": 640_000})

        client = _mock_client(handler, tmp_path, tolerance_pct=10.0)
        try:
            out = await cross_check(result, client=client, address=_address())
        finally:
            await client.aclose()

        assert out.confidence == 0.9  # unchanged
        assert out.crosscheck is not None
        assert out.crosscheck.within_tolerance is True

    @pytest.mark.asyncio
    async def test_disagreement_halves_confidence(self, tmp_path: Path) -> None:
        result = _ok_result(value=500_000, confidence=0.9)

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"price": 1_200_000})  # +140%

        client = _mock_client(handler, tmp_path, tolerance_pct=10.0)
        try:
            out = await cross_check(result, client=client, address=_address())
        finally:
            await client.aclose()

        assert out.crosscheck is not None
        assert out.crosscheck.within_tolerance is False
        assert out.confidence == pytest.approx(0.45, abs=0.001)

    @pytest.mark.asyncio
    async def test_skipped_crosscheck_preserves_confidence(self, tmp_path: Path) -> None:
        result = _ok_result(confidence=0.9)

        # cap=0 → immediately skipped
        counter = UsageCounter(tmp_path / "usage.json", cap=0)
        transport = httpx.MockTransport(lambda _r: httpx.Response(200, json={}))
        http = httpx.AsyncClient(transport=transport)
        client = RentcastClient(
            api_key="k", counter=counter, tolerance_pct=10.0, client=http
        )
        try:
            out = await cross_check(result, client=client, address=_address())
        finally:
            await client.aclose()

        assert out.confidence == 0.9
        assert out.crosscheck is not None
        assert out.crosscheck.skipped is True

    @pytest.mark.asyncio
    async def test_missing_address_sets_skipped(self, tmp_path: Path) -> None:
        result = _ok_result(confidence=0.9)
        client = _mock_client(
            lambda _r: httpx.Response(200, json={"price": 636_000}), tmp_path
        )
        try:
            out = await cross_check(result, client=client, address=None)
        finally:
            await client.aclose()

        assert out.confidence == 0.9
        assert out.crosscheck is not None
        assert out.crosscheck.skipped is True
        assert "address" in (out.crosscheck.skipped_reason or "")


# ─── validate() top-level wrapper ───────────────────────────────


class TestValidatePipeline:
    @pytest.mark.asyncio
    async def test_skip_crosscheck_runs_sanity_only(self, tmp_path: Path) -> None:
        result = _ok_result(value=42)  # below floor
        out = await validate(result, client=None, address=_address(), skip_crosscheck=True)
        assert out.status == ZestimateStatus.ERROR
        assert out.crosscheck is None

    @pytest.mark.asyncio
    async def test_sanity_error_short_circuits_crosscheck(self, tmp_path: Path) -> None:
        """If sanity flips status to ERROR, we should not run the cross-check."""
        result = _ok_result(value=1)  # absurd

        call_count = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(200, json={"price": 500_000})

        client = _mock_client(handler, tmp_path)
        try:
            out = await validate(result, client=client, address=_address())
        finally:
            await client.aclose()

        assert out.status == ZestimateStatus.ERROR
        assert call_count["n"] == 0  # cross-check did not run

    @pytest.mark.asyncio
    async def test_happy_path_agreement(self, tmp_path: Path) -> None:
        result = _ok_result(value=636_500, confidence=0.9)

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"price": 645_000})

        client = _mock_client(handler, tmp_path, tolerance_pct=10.0)
        try:
            out = await validate(result, client=client, address=_address())
        finally:
            await client.aclose()

        assert out.status == ZestimateStatus.OK
        assert out.value == 636_500  # Zillow still ground truth
        assert out.crosscheck is not None
        assert out.crosscheck.within_tolerance is True
        assert out.confidence == 0.9


# ─── CrossCheck model basics ────────────────────────────────────


def test_crosscheck_defaults() -> None:
    cc = CrossCheck(provider="rentcast")
    assert cc.skipped is False
    assert cc.estimate is None
    assert cc.within_tolerance is None
