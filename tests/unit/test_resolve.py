"""Resolver tests — mocked httpx against Zillow autocomplete."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from zestimate_agent.errors import AmbiguousAddressError, PropertyNotFoundError, ResolverError
from zestimate_agent.models import NormalizedAddress
from zestimate_agent.resolve import ZillowResolver, _normalize_street_name


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


def _mock_transport(payload: dict[str, Any], status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


# ─── _normalize_street_name ────────────────────────────────────


@pytest.mark.parametrize(
    "inp,out",
    [
        ("500 5th Ave W", "5th w"),
        ("123 Main St", "main"),
        ("Main Street", "main"),
        ("1600 Pennsylvania Ave NW", "pennsylvania nw"),
        ("", ""),
    ],
)
def test_normalize_street_name(inp: str, out: str) -> None:
    assert _normalize_street_name(inp) == out


# ─── Happy path ─────────────────────────────────────────────────


_ONE_GOOD_RESULT: dict[str, Any] = {
    "results": [
        {
            "display": "500 5th Ave W #705, Seattle, WA 98119",
            "resultType": "Address",
            "metaData": {
                "streetNumber": "500",
                "streetName": "5th Ave W",
                "unitNumber": "705",
                "city": "Seattle",
                "state": "WA",
                "zipCode": "98119",
                "zpid": 82362438,
                "lat": 47.62,
                "lng": -122.36,
            },
        }
    ]
}


@pytest.mark.asyncio
async def test_resolve_happy_path() -> None:
    transport = _mock_transport(_ONE_GOOD_RESULT)
    async with httpx.AsyncClient(transport=transport) as client:
        resolver = ZillowResolver(client=client)
        result = await resolver.resolve(_addr())

    assert result.zpid == "82362438"
    assert result.url == "https://www.zillow.com/homedetails/82362438_zpid/"
    assert result.matched_address.startswith("500 5th Ave W")
    assert result.match_confidence >= 0.9


# ─── No results ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_no_results_raises_not_found() -> None:
    transport = _mock_transport({"results": []})
    async with httpx.AsyncClient(transport=transport) as client:
        resolver = ZillowResolver(client=client)
        with pytest.raises(PropertyNotFoundError):
            await resolver.resolve(_addr())


@pytest.mark.asyncio
async def test_resolve_only_non_address_results_raises_not_found() -> None:
    payload = {
        "results": [
            {"display": "Seattle, WA", "resultType": "Region", "metaData": {}},
        ]
    }
    transport = _mock_transport(payload)
    async with httpx.AsyncClient(transport=transport) as client:
        resolver = ZillowResolver(client=client)
        with pytest.raises(PropertyNotFoundError):
            await resolver.resolve(_addr())


# ─── Disambiguation ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_prefers_exact_zip_match() -> None:
    payload = {
        "results": [
            {
                "display": "500 5th Ave W, Seattle, WA 99999",
                "resultType": "Address",
                "metaData": {
                    "streetNumber": "500",
                    "streetName": "5th Ave W",
                    "city": "Seattle",
                    "state": "WA",
                    "zipCode": "99999",  # wrong zip
                    "zpid": 1,
                },
            },
            {
                "display": "500 5th Ave W, Seattle, WA 98119",
                "resultType": "Address",
                "metaData": {
                    "streetNumber": "500",
                    "streetName": "5th Ave W",
                    "city": "Seattle",
                    "state": "WA",
                    "zipCode": "98119",  # correct zip
                    "zpid": 2,
                },
            },
        ]
    }
    transport = _mock_transport(payload)
    async with httpx.AsyncClient(transport=transport) as client:
        resolver = ZillowResolver(client=client)
        result = await resolver.resolve(_addr())
    assert result.zpid == "2"  # picked correct-zip candidate


@pytest.mark.asyncio
async def test_resolve_flags_ambiguous_when_close_runnerup() -> None:
    """Two candidates with same score → top wins but confidence is reduced."""
    payload = {
        "results": [
            {
                "display": "500 5th Ave W #1, Seattle, WA 98119",
                "resultType": "Address",
                "metaData": {
                    "streetNumber": "500",
                    "streetName": "5th Ave W",
                    "city": "Seattle",
                    "state": "WA",
                    "zipCode": "98119",
                    "zpid": 1,
                },
            },
            {
                "display": "500 5th Ave W #2, Seattle, WA 98119",
                "resultType": "Address",
                "metaData": {
                    "streetNumber": "500",
                    "streetName": "5th Ave W",
                    "city": "Seattle",
                    "state": "WA",
                    "zipCode": "98119",
                    "zpid": 2,
                },
            },
        ]
    }
    transport = _mock_transport(payload)
    async with httpx.AsyncClient(transport=transport) as client:
        resolver = ZillowResolver(client=client)
        result = await resolver.resolve(_addr())
    # Returns the top one, but confidence is penalized
    assert result.zpid in {"1", "2"}
    assert result.match_confidence < 1.0
    assert len(result.alternates) >= 1


# ─── Error propagation ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_http_error_raises_resolver_error() -> None:
    transport = _mock_transport({}, status=500)
    async with httpx.AsyncClient(transport=transport) as client:
        resolver = ZillowResolver(client=client)
        with pytest.raises(ResolverError):
            await resolver.resolve(_addr())


# ─── Wrong street entirely — should still be rejected as low-confidence ───


@pytest.mark.asyncio
async def test_resolve_unrelated_result_rejected() -> None:
    payload = {
        "results": [
            {
                "display": "999 Elsewhere Ln, Miami, FL 33101",
                "resultType": "Address",
                "metaData": {
                    "streetNumber": "999",
                    "streetName": "Elsewhere Ln",
                    "city": "Miami",
                    "state": "FL",
                    "zipCode": "33101",
                    "zpid": 777,
                },
            }
        ]
    }
    transport = _mock_transport(payload)
    async with httpx.AsyncClient(transport=transport) as client:
        resolver = ZillowResolver(client=client)
        with pytest.raises(AmbiguousAddressError):
            await resolver.resolve(_addr())
