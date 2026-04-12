"""Live integration tests for the resolver — hit real Zillow autocomplete.

These tests use Zillow's public autocomplete endpoint directly (no unblocker
credits) so they're safe to run on every CI build. Marked `@live` so they
only run when `RUN_LIVE_TESTS=1` is set in the environment.

These protect against two very real failure modes:
1. Zillow changes the autocomplete endpoint or response shape
2. Our scoring regresses and picks the wrong candidate for common addresses
"""

from __future__ import annotations

import os

import pytest

from zestimate_agent.normalize import normalize
from zestimate_agent.resolve import ZillowResolver

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_TESTS") != "1",
    reason="RUN_LIVE_TESTS=1 not set",
)


# Addresses that should reliably resolve to a Zillow zpid.
# These are long-standing properties that won't disappear.
_LIVE_CASES = [
    "500 5th Ave W #705, Seattle, WA 98119",  # Seattle condo (our main fixture)
    "350 5th Ave, New York, NY 10118",         # Empire State Building
    "1600 Pennsylvania Ave NW, Washington, DC 20500",  # White House
    "11 Wall St, New York, NY 10005",          # NYSE area
]


@pytest.mark.live
@pytest.mark.parametrize("address", _LIVE_CASES)
async def test_resolver_live(address: str) -> None:
    normalized = normalize(address)
    resolver = ZillowResolver()
    try:
        result = await resolver.resolve(normalized)
    finally:
        await resolver.aclose()

    assert result.zpid
    assert result.url.startswith("https://www.zillow.com/homedetails/")
    assert result.match_confidence >= 0.5
