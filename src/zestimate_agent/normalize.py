"""Address normalization — parses a raw US address string into a `NormalizedAddress`.

The pipeline:

    raw string
       │
       ▼
    ┌─────────────────────┐
    │ 1. Whitespace clean │
    └──────────┬──────────┘
               ▼
    ┌─────────────────────┐
    │ 2. usaddress.tag()  │──── reject: PO Box / Intersection / Ambiguous
    └──────────┬──────────┘
               ▼
    ┌─────────────────────┐
    │ 3. Complete?        │── no ─► Geocoder fallback (if configured)
    └──────────┬──────────┘                     │
               │ yes                            ▼
               │                        (fill missing fields)
               ▼                                │
    ┌─────────────────────┐◄──────────────────┘
    │ 4. State abbr.      │
    │ 5. Title-case       │
    │ 6. Canonical form   │
    └──────────┬──────────┘
               ▼
       NormalizedAddress

Design notes
------------
* `usaddress` is the primary parser. It's pure-Python, fast, and gets the
  vast majority of well-formed inputs right. It does NOT normalize case,
  expand state names, or correct typos — those are our job.
* The `Geocoder` is a `Protocol` so tests can inject a fake with zero
  network calls, and future providers (Mapbox, Nominatim) can drop in.
* `parse_confidence` is the signal the downstream resolver uses to decide
  whether to aggressively disambiguate or trust the top candidate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx
import usaddress
from pydantic import BaseModel

from zestimate_agent.config import get_settings
from zestimate_agent.errors import NormalizationError
from zestimate_agent.logging import get_logger
from zestimate_agent.models import NormalizedAddress

log = get_logger(__name__)


# ─── State tables ───────────────────────────────────────────────

US_STATE_ABBRS: frozenset[str] = frozenset(
    [
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
        "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
        "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
        "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
        "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
        "DC", "PR", "VI", "GU", "MP", "AS",
    ]
)

US_STATE_NAMES: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC", "washington dc": "DC", "washington d.c.": "DC",
    "puerto rico": "PR", "virgin islands": "VI",
    "u.s. virgin islands": "VI", "us virgin islands": "VI",
    "guam": "GU", "northern mariana islands": "MP", "american samoa": "AS",
}

_DIRECTIONALS = frozenset({"N", "S", "E", "W", "NE", "NW", "SE", "SW"})

# usaddress keys that contribute to the street portion, in canonical order.
_STREET_KEYS: tuple[str, ...] = (
    "AddressNumberPrefix",
    "AddressNumber",
    "AddressNumberSuffix",
    "StreetNamePreDirectional",
    "StreetNamePreModifier",
    "StreetNamePreType",
    "StreetName",
    "StreetNamePostType",
    "StreetNamePostDirectional",
    "StreetNamePostModifier",
)


# ─── Helpers ────────────────────────────────────────────────────


def normalize_state(s: str) -> str | None:
    """Return the 2-letter USPS code for a state name/abbreviation, or None."""
    if not s:
        return None
    cleaned = s.strip().rstrip(".").replace(".", "")
    upper = cleaned.upper()
    if len(upper) == 2 and upper in US_STATE_ABBRS:
        return upper
    lower = " ".join(cleaned.lower().split())
    return US_STATE_NAMES.get(lower)


def _titlecase_street(s: str) -> str:
    """Title-case a street, preserving directionals (N/NE/...) and # prefixes."""
    out: list[str] = []
    for w in s.split():
        bare = w.rstrip(".,").upper()
        if bare in _DIRECTIONALS:
            out.append(bare)
        elif w.startswith("#"):
            out.append(w.upper())
        else:
            out.append(w.capitalize())
    return " ".join(out)


def _titlecase_city(s: str) -> str:
    return " ".join(w.capitalize() for w in s.split())


# ─── Intermediate parsed state ──────────────────────────────────


@dataclass
class _Parsed:
    street: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    lat: float | None = None
    lon: float | None = None
    partial: bool = False

    @property
    def complete(self) -> bool:
        return bool(self.street and self.city and self.state and self.zip)

    def missing(self) -> list[str]:
        return [name for name in ("street", "city", "state", "zip") if not getattr(self, name)]


def _from_usaddress(components: dict[str, str]) -> _Parsed:
    street_parts = [
        components[k].rstrip(",")
        for k in _STREET_KEYS
        if components.get(k)
    ]

    unit_type = components.get("OccupancyType", "").strip().rstrip(",")
    unit_id = components.get("OccupancyIdentifier", "").strip().rstrip(",")
    if unit_type or unit_id:
        street_parts.append(f"{unit_type} {unit_id}".strip())

    street = " ".join(p for p in street_parts if p).strip()
    city = components.get("PlaceName", "").strip().rstrip(",")
    state = components.get("StateName", "").strip().rstrip(",")
    zip_raw = components.get("ZipCode", "").strip().rstrip(",")
    zip_ = zip_raw[:5] if zip_raw else ""

    return _Parsed(street=street, city=city, state=state, zip=zip_)


# ─── Geocoder protocol + Google implementation ──────────────────


class GeocodeResult(BaseModel):
    street: str
    city: str
    state: str
    zip: str
    lat: float | None = None
    lon: float | None = None
    formatted: str = ""
    partial_match: bool = False


@runtime_checkable
class Geocoder(Protocol):
    name: str

    def geocode(self, raw: str) -> GeocodeResult | None: ...


class GoogleGeocoder:
    """Google Maps Geocoding API client. Sync — called from a thread if needed."""

    name = "google"
    GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self._client = client  # injectable for tests

    def geocode(self, raw: str) -> GeocodeResult | None:
        owns_client = self._client is None
        client = self._client or httpx.Client(timeout=self._timeout)
        try:
            r = client.get(
                self.GEOCODE_URL,
                params={"address": raw, "key": self._api_key, "region": "us"},
            )
            r.raise_for_status()
            data = r.json()
        finally:
            if owns_client:
                client.close()

        if data.get("status") != "OK" or not data.get("results"):
            log.debug("geocoder no result", status=data.get("status"))
            return None

        return _parse_google_result(data["results"][0])


def _parse_google_result(result: dict[str, Any]) -> GeocodeResult | None:
    by_type: dict[str, dict[str, Any]] = {}
    for c in result.get("address_components", []):
        for t in c.get("types", []):
            by_type.setdefault(t, c)

    street_number = by_type.get("street_number", {}).get("long_name", "")
    route = by_type.get("route", {}).get("short_name", "")
    street = f"{street_number} {route}".strip()

    locality = (
        by_type.get("locality")
        or by_type.get("postal_town")
        or by_type.get("sublocality")
        or by_type.get("sublocality_level_1")
    )
    city = (locality or {}).get("long_name", "")
    state = by_type.get("administrative_area_level_1", {}).get("short_name", "")
    zip_full = by_type.get("postal_code", {}).get("long_name", "")
    zip_ = zip_full[:5]

    if not (street and city and state and zip_):
        return None

    loc = result.get("geometry", {}).get("location", {})
    return GeocodeResult(
        street=street,
        city=city,
        state=state,
        zip=zip_,
        lat=loc.get("lat"),
        lon=loc.get("lng"),
        formatted=result.get("formatted_address", ""),
        partial_match=bool(result.get("partial_match", False)),
    )


# ─── Normalizer ─────────────────────────────────────────────────


class Normalizer:
    """Stateless address normalizer. Thread-safe; reuse a single instance."""

    def __init__(self, geocoder: Geocoder | None = None) -> None:
        self.geocoder = geocoder

    def normalize(self, raw: str) -> NormalizedAddress:
        if raw is None or not raw.strip():
            raise NormalizationError("empty address")

        cleaned = " ".join(raw.strip().split())

        parsed: _Parsed | None = None
        usaddress_clean = False

        try:
            tagged, addr_type = usaddress.tag(cleaned)
        except usaddress.RepeatedLabelError as e:
            log.debug("usaddress ambiguous", error=str(e))
            tagged, addr_type = None, "Ambiguous"

        if addr_type == "PO Box":
            raise NormalizationError("PO boxes are not supported by Zestimate")
        if addr_type == "Intersection":
            raise NormalizationError("intersections are not supported")

        if tagged:
            parsed = _from_usaddress(dict(tagged))
            usaddress_clean = parsed.complete

        if parsed is None or not parsed.complete:
            parsed = self._fallback_to_geocoder(cleaned, parsed)

        state_abbr = normalize_state(parsed.state)
        if not state_abbr:
            raise NormalizationError(f"unrecognized state: {parsed.state!r}")

        street = _titlecase_street(parsed.street)
        city = _titlecase_city(parsed.city)
        zip_ = parsed.zip
        canonical = f"{street}, {city}, {state_abbr} {zip_}"

        # Confidence scoring
        confidence = 1.0
        if not usaddress_clean:
            confidence = 0.85  # needed geocoder to complete
        if parsed.partial:
            confidence *= 0.8

        addr = NormalizedAddress(
            raw=raw,
            street=street,
            city=city,
            state=state_abbr,
            zip=zip_,
            canonical=canonical,
            lat=parsed.lat,
            lon=parsed.lon,
            parse_confidence=confidence,
        )
        log.debug(
            "normalized",
            raw=raw,
            canonical=addr.canonical,
            confidence=addr.parse_confidence,
            used_geocoder=not usaddress_clean,
        )
        return addr

    # ─── Internal ───────────────────────────────────────────────

    def _fallback_to_geocoder(self, cleaned: str, partial: _Parsed | None) -> _Parsed:
        if self.geocoder is None:
            missing = partial.missing() if partial else ["street", "city", "state", "zip"]
            raise NormalizationError(
                f"could not parse address: missing {', '.join(missing)}"
            )

        geo = self.geocoder.geocode(cleaned)
        if geo is None:
            raise NormalizationError("geocoder returned no result")

        base = partial or _Parsed()
        merged = _Parsed(
            street=base.street or geo.street,
            city=base.city or geo.city,
            state=base.state or geo.state,
            zip=base.zip or geo.zip,
            lat=base.lat or geo.lat,
            lon=base.lon or geo.lon,
            partial=base.partial or geo.partial_match,
        )
        if not merged.complete:
            raise NormalizationError(
                f"geocoder incomplete result: missing {', '.join(merged.missing())}"
            )
        return merged


# ─── Module-level convenience ───────────────────────────────────


def _make_default_geocoder() -> Geocoder | None:
    settings = get_settings()
    if settings.google_key:
        return GoogleGeocoder(settings.google_key)
    return None


def default_normalizer() -> Normalizer:
    """Build a Normalizer from current settings. Not cached — cheap to construct."""
    return Normalizer(_make_default_geocoder())


def normalize(raw: str) -> NormalizedAddress:
    """Convenience: normalize with settings-driven defaults."""
    return default_normalizer().normalize(raw)
