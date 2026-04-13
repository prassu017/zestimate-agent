"""VCR-style resolver tests — replay recorded Zillow autocomplete responses.

These cassettes capture real Zillow autocomplete API responses, serialized
as JSON fixtures. Replaying them in CI:

- Detects schema drift in Zillow's autocomplete response shape
- Tests the full resolver scoring + disambiguation logic
- Costs zero API credits
- Is fully deterministic (no network)

Cassette format::

    {
      "description": "...",
      "request": {"method": "GET", "url": "...", "params": {...}},
      "response": {"status": 200, "body": {...}}
    }
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from zestimate_agent.errors import PropertyNotFoundError
from zestimate_agent.models import NormalizedAddress
from zestimate_agent.resolve import ZillowResolver

CASSETTES_DIR = Path(__file__).parent.parent / "fixtures" / "cassettes"


def _load_cassette(name: str) -> dict:
    path = CASSETTES_DIR / f"{name}.json"
    return json.loads(path.read_text())


def _addr(
    *,
    street: str = "500 5th Ave W",
    city: str = "Seattle",
    state: str = "WA",
    zip_: str = "98119",
) -> NormalizedAddress:
    return NormalizedAddress(
        raw=f"{street}, {city}, {state} {zip_}",
        street=street,
        city=city,
        state=state,
        zip=zip_,
        canonical=f"{street}, {city}, {state} {zip_}",
    )


def _cassette_transport(cassette: dict) -> httpx.MockTransport:
    """Build an httpx mock transport that replays the cassette response."""
    resp = cassette["response"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(resp["status"], json=resp["body"])

    return httpx.MockTransport(handler)


# ─── Seattle condo (happy path with unit disambiguation) ──────


@pytest.mark.asyncio
async def test_cassette_seattle_condo() -> None:
    """Replays real Zillow autocomplete for 500 5th Ave W #705, Seattle."""
    cassette = _load_cassette("resolve_seattle_condo")
    transport = _cassette_transport(cassette)

    async with httpx.AsyncClient(transport=transport) as client:
        resolver = ZillowResolver(client=client)
        result = await resolver.resolve(_addr())

    assert result.zpid == "82362438"
    assert "500 5th Ave W" in result.matched_address
    assert result.match_confidence >= 0.85
    # Should have at least one alternate (the building-level entry).
    assert len(result.alternates) >= 1


# ─── Empire State Building (resolves, but zpid exists) ────────


@pytest.mark.asyncio
async def test_cassette_empire_state() -> None:
    """Empire State Building resolves — zpid exists, but no Zestimate."""
    cassette = _load_cassette("resolve_empire_state")
    transport = _cassette_transport(cassette)

    async with httpx.AsyncClient(transport=transport) as client:
        resolver = ZillowResolver(client=client)
        result = await resolver.resolve(
            _addr(street="350 5th Ave", city="New York", state="NY", zip_="10118")
        )

    assert result.zpid == "2096485434"
    assert result.match_confidence >= 0.8


# ─── Not found ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cassette_not_found() -> None:
    """Nonexistent address returns empty results."""
    cassette = _load_cassette("resolve_not_found")
    transport = _cassette_transport(cassette)

    async with httpx.AsyncClient(transport=transport) as client:
        resolver = ZillowResolver(client=client)
        with pytest.raises(PropertyNotFoundError):
            await resolver.resolve(
                _addr(
                    street="123456 Nonexistent Street",
                    city="Nowhere",
                    state="XY",
                    zip_="00000",
                )
            )


# ─── Ambiguous Main St ───────────────────────────────────────


@pytest.mark.asyncio
async def test_cassette_ambiguous_main_st() -> None:
    """Multiple Main St matches — resolver picks best by zip match."""
    cassette = _load_cassette("resolve_ambiguous_main_st")
    transport = _cassette_transport(cassette)

    async with httpx.AsyncClient(transport=transport) as client:
        resolver = ZillowResolver(client=client)
        result = await resolver.resolve(
            _addr(street="100 Main St", city="Portland", state="OR", zip_="97201")
        )

    # Should prefer the Portland match (exact zip).
    assert result.zpid == "53929338"
    assert result.match_confidence > 0.0
    # Should list alternates.
    assert len(result.alternates) >= 1


@pytest.mark.asyncio
async def test_cassette_ambiguous_wrong_zip() -> None:
    """When the queried zip doesn't match any candidate well, confidence drops."""
    cassette = _load_cassette("resolve_ambiguous_main_st")
    transport = _cassette_transport(cassette)

    async with httpx.AsyncClient(transport=transport) as client:
        resolver = ZillowResolver(client=client)
        # Query with a zip that doesn't match any candidate.
        result = await resolver.resolve(
            _addr(street="100 Main St", city="Portland", state="OR", zip_="99999")
        )

    # Still returns a result, but with reduced confidence.
    assert result.zpid is not None
    assert result.match_confidence < 1.0
