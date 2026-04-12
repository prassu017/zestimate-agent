"""Tests for the address normalizer."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from zestimate_agent.errors import NormalizationError
from zestimate_agent.normalize import (
    GeocodeResult,
    GoogleGeocoder,
    Normalizer,
    _parse_google_result,
    normalize,
    normalize_state,
)

# ─── Fake geocoder for tests that need one ─────────────────────


class FakeGeocoder:
    name = "fake"

    def __init__(self, result: GeocodeResult | None) -> None:
        self.result = result
        self.calls: list[str] = []

    def geocode(self, raw: str) -> GeocodeResult | None:
        self.calls.append(raw)
        return self.result


# ─── State normalization ───────────────────────────────────────


@pytest.mark.parametrize(
    "inp,out",
    [
        ("WA", "WA"),
        ("wa", "WA"),
        ("Wa", "WA"),
        ("Washington", "WA"),
        ("washington", "WA"),
        ("  Washington  ", "WA"),
        ("D.C.", "DC"),
        ("DC", "DC"),
        ("District of Columbia", "DC"),
        ("New York", "NY"),
        ("new york", "NY"),
        ("XX", None),
        ("Freedonia", None),
        ("", None),
    ],
)
def test_normalize_state(inp: str, out: str | None) -> None:
    assert normalize_state(inp) == out


# ─── Happy-path normalization (no geocoder) ────────────────────


def test_clean_address() -> None:
    a = normalize("123 Main St, Seattle, WA 98101")
    assert a.street == "123 Main St"
    assert a.city == "Seattle"
    assert a.state == "WA"
    assert a.zip == "98101"
    assert a.canonical == "123 Main St, Seattle, WA 98101"
    assert a.parse_confidence == 1.0
    assert a.raw == "123 Main St, Seattle, WA 98101"


def test_lowercase_input() -> None:
    a = normalize("123 main st, seattle, wa 98101")
    assert a.state == "WA"
    assert a.street == "123 Main St"
    assert a.city == "Seattle"
    assert a.canonical == "123 Main St, Seattle, WA 98101"


def test_full_state_name() -> None:
    a = normalize("123 Main St, Seattle, Washington 98101")
    assert a.state == "WA"
    assert a.canonical == "123 Main St, Seattle, WA 98101"


def test_directional_address() -> None:
    a = normalize("1600 Pennsylvania Ave NW, Washington, DC 20500")
    assert "NW" in a.street
    assert a.street == "1600 Pennsylvania Ave NW"
    assert a.city == "Washington"
    assert a.state == "DC"
    assert a.zip == "20500"


def test_address_with_unit() -> None:
    a = normalize("123 Main St Apt 4B, Seattle, WA 98101")
    assert "Apt" in a.street
    assert "4B" in a.street.upper()
    assert a.city == "Seattle"


def test_extra_whitespace_collapsed() -> None:
    a = normalize("  123 Main St,   Seattle,   WA   98101  ")
    assert a.canonical == "123 Main St, Seattle, WA 98101"


def test_9_digit_zip_truncated_to_5() -> None:
    a = normalize("123 Main St, Seattle, WA 98101-1234")
    assert a.zip == "98101"
    assert a.canonical.endswith(" 98101")


def test_multiword_city() -> None:
    a = normalize("350 5th Ave, New York, NY 10118")
    assert a.city == "New York"
    assert a.street == "350 5th Ave"
    assert a.state == "NY"


def test_one_word_street_type() -> None:
    a = normalize("1 Infinite Loop, Cupertino, CA 95014")
    assert a.street == "1 Infinite Loop"
    assert a.city == "Cupertino"
    assert a.state == "CA"


# ─── Rejections ────────────────────────────────────────────────


def test_empty_string_rejected() -> None:
    with pytest.raises(NormalizationError, match="empty"):
        normalize("")


def test_whitespace_only_rejected() -> None:
    with pytest.raises(NormalizationError, match="empty"):
        normalize("   \t  ")


def test_po_box_rejected() -> None:
    with pytest.raises(NormalizationError, match=r"(?i)po box"):
        normalize("PO Box 123, Seattle, WA 98101")


def test_missing_zip_no_geocoder_raises() -> None:
    n = Normalizer(geocoder=None)
    with pytest.raises(NormalizationError, match="zip"):
        n.normalize("123 Main St, Seattle, WA")


def test_unrecognized_state_raises() -> None:
    # usaddress will still tag XX as StateName; our validator rejects it.
    n = Normalizer(geocoder=None)
    with pytest.raises(NormalizationError, match=r"(?i)state"):
        n.normalize("123 Main St, Seattle, XX 98101")


# ─── Geocoder fallback ─────────────────────────────────────────


def test_missing_zip_with_geocoder_fills_in() -> None:
    geo = FakeGeocoder(
        GeocodeResult(
            street="123 Main St",
            city="Seattle",
            state="WA",
            zip="98101",
            lat=47.6,
            lon=-122.3,
            partial_match=False,
        )
    )
    n = Normalizer(geocoder=geo)
    a = n.normalize("123 Main St, Seattle, WA")
    assert a.zip == "98101"
    assert a.lat == 47.6
    assert a.lon == -122.3
    # Had to consult the geocoder, so confidence < 1.0
    assert a.parse_confidence < 1.0
    assert len(geo.calls) == 1


def test_partial_match_further_reduces_confidence() -> None:
    geo = FakeGeocoder(
        GeocodeResult(
            street="Main St",
            city="Seattle",
            state="WA",
            zip="98101",
            partial_match=True,
        )
    )
    n = Normalizer(geocoder=geo)
    a = n.normalize("main street seattle washington")
    assert a.parse_confidence < 0.8


def test_geocoder_none_result_raises() -> None:
    geo = FakeGeocoder(None)
    n = Normalizer(geocoder=geo)
    with pytest.raises(NormalizationError, match="geocoder"):
        n.normalize("totally not an address nowhere")


# ─── GoogleGeocoder parser ─────────────────────────────────────


_GOOGLE_RESPONSE: dict[str, Any] = {
    "status": "OK",
    "results": [
        {
            "address_components": [
                {"long_name": "123", "short_name": "123", "types": ["street_number"]},
                {
                    "long_name": "Main Street",
                    "short_name": "Main St",
                    "types": ["route"],
                },
                {
                    "long_name": "Seattle",
                    "short_name": "Seattle",
                    "types": ["locality", "political"],
                },
                {
                    "long_name": "King County",
                    "short_name": "King County",
                    "types": ["administrative_area_level_2", "political"],
                },
                {
                    "long_name": "Washington",
                    "short_name": "WA",
                    "types": ["administrative_area_level_1", "political"],
                },
                {
                    "long_name": "United States",
                    "short_name": "US",
                    "types": ["country", "political"],
                },
                {"long_name": "98101", "short_name": "98101", "types": ["postal_code"]},
            ],
            "formatted_address": "123 Main St, Seattle, WA 98101, USA",
            "geometry": {"location": {"lat": 47.6062, "lng": -122.3321}},
            "partial_match": False,
        }
    ],
}


def test_parse_google_result_happy_path() -> None:
    result = _parse_google_result(_GOOGLE_RESPONSE["results"][0])
    assert result is not None
    assert result.street == "123 Main St"
    assert result.city == "Seattle"
    assert result.state == "WA"
    assert result.zip == "98101"
    assert result.lat == 47.6062
    assert result.lon == -122.3321
    assert result.partial_match is False


def test_parse_google_result_missing_fields_returns_none() -> None:
    incomplete = {
        "address_components": [
            {"long_name": "Seattle", "short_name": "Seattle", "types": ["locality"]},
        ],
        "geometry": {"location": {"lat": 0, "lng": 0}},
    }
    assert _parse_google_result(incomplete) is None


def test_google_geocoder_with_mock_client() -> None:
    mock_client = MagicMock(spec=httpx.Client)
    mock_response = MagicMock()
    mock_response.json.return_value = _GOOGLE_RESPONSE
    mock_response.raise_for_status = MagicMock()
    mock_client.get.return_value = mock_response

    geo = GoogleGeocoder("fake-key", client=mock_client)
    result = geo.geocode("123 main st seattle")

    assert result is not None
    assert result.street == "123 Main St"
    assert result.state == "WA"

    # Verify the request was built correctly
    call_args = mock_client.get.call_args
    assert call_args.args[0] == GoogleGeocoder.GEOCODE_URL
    params = call_args.kwargs["params"]
    assert params["address"] == "123 main st seattle"
    assert params["key"] == "fake-key"
    assert params["region"] == "us"


def test_google_geocoder_zero_results_returns_none() -> None:
    mock_client = MagicMock(spec=httpx.Client)
    mock_response = MagicMock()
    mock_response.json.return_value = {"status": "ZERO_RESULTS", "results": []}
    mock_response.raise_for_status = MagicMock()
    mock_client.get.return_value = mock_response

    geo = GoogleGeocoder("fake-key", client=mock_client)
    assert geo.geocode("nowhere") is None
