"""Smoke tests — confirm the scaffold imports cleanly and models work.

These run in Step 1 before any real logic exists, so CI is green from day 1.
"""

from __future__ import annotations

from zestimate_agent import (
    NormalizedAddress,
    ResolvedProperty,
    ZestimateResult,
    ZestimateStatus,
    __version__,
)
from zestimate_agent.config import get_settings, reset_settings_cache


def test_version() -> None:
    assert __version__ == "0.1.0"


def test_settings_loads_defaults() -> None:
    reset_settings_cache()
    s = get_settings()
    assert s.fetcher_primary in {"unblocker", "playwright"}
    assert s.cache_backend in {"sqlite", "memory", "none"}
    assert s.http_timeout_seconds > 0


def test_normalized_address_roundtrip() -> None:
    addr = NormalizedAddress(
        raw="123 Main St, Seattle, WA 98101",
        street="123 Main St",
        city="Seattle",
        state="wa",  # lowercased — validator should upper
        zip="98101-1234",  # +4 — validator should strip
        canonical="123 Main St, Seattle, WA 98101",
    )
    assert addr.state == "WA"
    assert addr.zip == "98101"


def test_resolved_property_confidence_bounds() -> None:
    rp = ResolvedProperty(
        zpid="12345",
        url="https://www.zillow.com/homedetails/12345_zpid/",
        matched_address="123 Main St, Seattle, WA 98101",
        match_confidence=0.97,
    )
    assert 0.0 <= rp.match_confidence <= 1.0


def test_zestimate_result_display() -> None:
    r = ZestimateResult(
        status=ZestimateStatus.OK,
        value=1_234_567,
        zpid="12345",
        matched_address="123 Main St, Seattle, WA 98101",
    )
    assert r.ok
    assert "$1,234,567" in r.to_display()

    r2 = ZestimateResult(status=ZestimateStatus.NOT_FOUND, error="no match")
    assert not r2.ok
    assert "not_found" in r2.to_display()
