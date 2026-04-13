"""Zillow address resolver — maps a normalized address to a zpid + URL.

Uses Zillow's public autocomplete endpoint
(`zillowstatic.com/autocomplete/v3/suggestions`). This endpoint is
reachable with plain `httpx` — it's not behind the PerimeterX WAF — so
the resolver is fast and doesn't burn unblocker credits.

Response shape::

    {
      "results": [
        {
          "display": "123 Main St, Seattle, WA 98101",
          "resultType": "Address",
          "metaData": {
            "streetNumber": "123", "streetName": "Main St", "unitNumber": "",
            "city": "Seattle", "state": "WA", "zipCode": "98101",
            "zpid": 12345, "lat": 47.6, "lng": -122.3
          }
        },
        ...
      ]
    }

Disambiguation strategy when multiple `Address` results come back:

1. Score each candidate against the input's (street number, street name, city, state, zip)
2. Prefer exact zip match, then exact street number + name, then city
3. If top score is unique → return it
4. If top score is tied → return the first one but flag with `AMBIGUOUS`
5. If no Address candidates → `PropertyNotFoundError`
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from zestimate_agent.config import get_settings
from zestimate_agent.errors import AmbiguousAddressError, PropertyNotFoundError, ResolverError
from zestimate_agent.logging import get_logger
from zestimate_agent.models import NormalizedAddress, ResolvedProperty

log = get_logger(__name__)

AUTOCOMPLETE_URL = "https://www.zillowstatic.com/autocomplete/v3/suggestions"
ZILLOW_HOMEDETAILS = "https://www.zillow.com/homedetails/{zpid}_zpid/"

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


class ZillowResolver:
    """Resolver backed by Zillow's public autocomplete endpoint.

    Connection pooling: when no external client is injected, the resolver
    lazily creates a shared ``httpx.AsyncClient`` on first use and reuses
    it across calls. This avoids a fresh TLS handshake per resolve (~100ms
    saved per call after the first).
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float | None = None,
    ) -> None:
        settings = get_settings()
        self._ext_client = client
        self._timeout = timeout or settings.http_timeout_seconds
        self._own_client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._ext_client is not None:
            return self._ext_client
        if self._own_client is None:
            self._own_client = httpx.AsyncClient(
                timeout=self._timeout,
                headers=_DEFAULT_HEADERS,
            )
        return self._own_client

    async def resolve(self, address: NormalizedAddress) -> ResolvedProperty:
        query = address.canonical
        log.debug("resolver query", q=query)

        client = self._get_client()
        try:
            r = await client.get(AUTOCOMPLETE_URL, params={"q": query})
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            raise ResolverError(f"autocomplete request failed: {e}") from e

        return self._pick_best(address, data)

    async def aclose(self) -> None:
        if self._own_client is not None:
            await self._own_client.aclose()
            self._own_client = None
        if self._ext_client is not None:
            await self._ext_client.aclose()

    # ─── Internal ───────────────────────────────────────────────

    def _pick_best(
        self,
        address: NormalizedAddress,
        data: dict[str, Any],
    ) -> ResolvedProperty:
        results = data.get("results") or []
        address_results = [
            r for r in results if r.get("resultType") == "Address" and r.get("metaData")
        ]
        if not address_results:
            raise PropertyNotFoundError(f"no Zillow match for {address.canonical!r}")

        scored = [
            (self._score(address, r["metaData"]), r) for r in address_results
        ]
        scored.sort(key=lambda x: x[0], reverse=True)

        top_score, top_result = scored[0]
        meta = top_result["metaData"]
        zpid = meta.get("zpid")
        if not zpid:
            raise PropertyNotFoundError("top match has no zpid")

        alternates: list[dict[str, Any]] = []
        for score, res in scored[1:5]:
            m = res.get("metaData", {})
            alternates.append(
                {
                    "display": res.get("display"),
                    "zpid": m.get("zpid"),
                    "score": round(score, 3),
                }
            )

        # Confidence = top score, penalized if the runner-up is close.
        confidence = top_score
        if len(scored) > 1:
            runner_up = scored[1][0]
            if runner_up >= top_score - 0.05:
                confidence *= 0.85  # ambiguous — multiple near-equal matches
                log.info(
                    "resolver ambiguous",
                    top=top_score,
                    runner_up=runner_up,
                    query=address.canonical,
                )

        if confidence < 0.4:
            raise AmbiguousAddressError(
                f"no confident match for {address.canonical!r}; top score={top_score:.2f}"
            )

        return ResolvedProperty(
            zpid=str(zpid),
            url=ZILLOW_HOMEDETAILS.format(zpid=zpid),
            matched_address=top_result.get("display") or address.canonical,
            match_confidence=min(1.0, confidence),
            alternates=alternates,
        )

    @staticmethod
    def _score(address: NormalizedAddress, meta: dict[str, Any]) -> float:
        """Score a candidate 0..1 by component overlap with the input."""
        score = 0.0
        weights = {"zip": 0.35, "street_num": 0.25, "street_name": 0.2, "city": 0.15, "state": 0.05}

        # ZIP match
        if _clean(meta.get("zipCode")) == address.zip:
            score += weights["zip"]

        # Street number
        input_num = _first_number(address.street)
        meta_num = _clean(meta.get("streetNumber"))
        if input_num and input_num == meta_num:
            score += weights["street_num"]

        # Street name (case-insensitive, ignore suffixes)
        input_street_name = _normalize_street_name(address.street)
        meta_street_name = _normalize_street_name(meta.get("streetName") or "")
        if input_street_name and meta_street_name and (
            input_street_name == meta_street_name
            or input_street_name in meta_street_name
            or meta_street_name in input_street_name
        ):
            score += weights["street_name"]

        # City
        if _clean(meta.get("city")).lower() == address.city.lower():
            score += weights["city"]

        # State
        if _clean(meta.get("state")).upper() == address.state:
            score += weights["state"]

        return score


def _clean(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def _first_number(s: str) -> str:
    m = re.match(r"\s*(\d+)", s)
    return m.group(1) if m else ""


def _normalize_street_name(s: str) -> str:
    """Lowercase, drop number prefix, drop common suffixes for comparison."""
    s = re.sub(r"^\s*\d+\s*", "", s).lower().strip()
    suffixes = {
        "st", "street", "ave", "avenue", "blvd", "boulevard", "rd", "road",
        "ln", "lane", "dr", "drive", "ct", "court", "way", "pl", "place",
        "ter", "terrace", "pkwy", "parkway", "cir", "circle", "hwy", "highway",
        "loop", "trl", "trail",
    }
    tokens = [t for t in re.split(r"[\s,]+", s) if t and t not in suffixes]
    # Strip trailing punctuation
    return " ".join(t.rstrip(".") for t in tokens).strip()


# ─── Module-level convenience ───────────────────────────────────


async def resolve(address: NormalizedAddress) -> ResolvedProperty:
    return await ZillowResolver().resolve(address)


def resolve_sync(address: NormalizedAddress) -> ResolvedProperty:
    return asyncio.run(resolve(address))
