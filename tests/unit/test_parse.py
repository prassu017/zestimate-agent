"""Parser tests against recorded Zillow fixtures and synthetic inputs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from zestimate_agent.errors import NoZestimateError, ParseError
from zestimate_agent.models import FetchResult, ZestimateStatus
from zestimate_agent.parse import parse

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _make_fetch_result(html: str, *, url: str = "https://www.zillow.com/homedetails/82362438_zpid/") -> FetchResult:
    return FetchResult(
        html=html,
        status=200,
        final_url=url,
        fetcher="scraperapi",
        fetched_at=datetime(2026, 4, 10, 12, 0, tzinfo=UTC),
        elapsed_ms=1234,
    )


# ─── Against real captured fixture ──────────────────────────────


def test_parse_real_zillow_page() -> None:
    """Full end-to-end parse against a real Zillow HTML we captured live."""
    html = (FIXTURES / "zillow_82362438.html").read_text()
    result = parse(_make_fetch_result(html))

    assert result.status == ZestimateStatus.OK
    assert result.value == 636500  # exact — this is the test oracle
    assert result.zpid == "82362438"
    assert result.matched_address is not None
    assert "Seattle" in result.matched_address
    assert "WA" in result.matched_address
    assert result.zillow_url is not None
    assert "82362438" in result.zillow_url
    assert result.fetcher == "scraperapi"
    assert result.confidence == 1.0


# ─── Synthetic inputs ───────────────────────────────────────────


def _build_next_data(prop: dict) -> str:
    """Build a minimal __NEXT_DATA__ script wrapping a property dict.

    Pads to > 500 bytes so it doesn't trip the blocked-page length heuristic.
    """
    gdp = {
        'ForSalePriorityQuery{"zpid":123}': {
            "property": prop,
        }
    }
    nd = {
        "props": {
            "pageProps": {
                "componentProps": {
                    "gdpClientCache": json.dumps(gdp),
                }
            }
        }
    }
    pad = "<!-- " + ("x" * 600) + " -->"
    return (
        f"<html><body>{pad}"
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script>'
        "</body></html>"
    )


def test_parse_synthetic_happy_path() -> None:
    html = _build_next_data(
        {
            "zpid": 999,
            "zestimate": 1_234_567,
            "streetAddress": "1 Test Lane",
            "address": {
                "streetAddress": "1 Test Lane",
                "city": "Testville",
                "state": "CA",
                "zipcode": "94000",
            },
        }
    )
    result = parse(_make_fetch_result(html))
    assert result.status == ZestimateStatus.OK
    assert result.value == 1_234_567
    assert result.zpid == "999"
    assert result.matched_address == "1 Test Lane, Testville, CA 94000"


def test_parse_no_zestimate_raises_no_zestimate_error() -> None:
    html = _build_next_data(
        {
            "zpid": 999,
            "zestimate": None,
            "streetAddress": "1 Test Lane",
            "address": {
                "streetAddress": "1 Test Lane",
                "city": "Testville",
                "state": "CA",
                "zipcode": "94000",
            },
        }
    )
    with pytest.raises(NoZestimateError):
        parse(_make_fetch_result(html))


def test_parse_prefers_property_with_zestimate_when_multiple() -> None:
    """If gdpClientCache has multiple queries (e.g. off-market + for-sale),
    prefer the one that actually has a zestimate."""
    gdp = {
        "OffMarketPriorityQuery{...}": {
            "property": {
                "zpid": 999,
                "zestimate": None,
                "streetAddress": "1 Test Lane",
                "address": {
                    "streetAddress": "1 Test Lane",
                    "city": "Testville",
                    "state": "CA",
                    "zipcode": "94000",
                },
            }
        },
        'ForSalePriorityQuery{"zpid":999}': {
            "property": {
                "zpid": 999,
                "zestimate": 555_000,
                "streetAddress": "1 Test Lane",
                "address": {
                    "streetAddress": "1 Test Lane",
                    "city": "Testville",
                    "state": "CA",
                    "zipcode": "94000",
                },
            }
        },
    }
    nd = {"props": {"pageProps": {"componentProps": {"gdpClientCache": json.dumps(gdp)}}}}
    pad = "<!-- " + ("x" * 600) + " -->"
    html = (
        f"<html><body>{pad}"
        f'<script id="__NEXT_DATA__">{json.dumps(nd)}</script></body></html>'
    )
    result = parse(_make_fetch_result(html))
    assert result.value == 555_000


def test_parse_html_regex_fallback() -> None:
    """If __NEXT_DATA__ is missing, fall back to parsing the rendered HTML."""
    html = (
        "<html><body>" + "x" * 1000 +  # pad to bypass length check
        '<div>$1,500,000<!-- -->Zestimate<sup>&reg;</sup></div>'
        + "</body></html>"
    )
    result = parse(_make_fetch_result(html))
    assert result.status == ZestimateStatus.OK
    assert result.value == 1_500_000
    assert result.confidence < 1.0  # lower confidence on fallback path


def test_parse_blocked_page_raises_parse_error() -> None:
    html = "<html><body>Press & Hold to confirm you are human</body></html>" + "x" * 1000
    with pytest.raises(ParseError, match=r"(?i)block"):
        parse(_make_fetch_result(html))


def test_parse_empty_html_raises() -> None:
    with pytest.raises(ParseError):
        parse(_make_fetch_result("<html></html>"))


def test_parse_next_data_present_but_no_property() -> None:
    nd = {"props": {"pageProps": {"componentProps": {"gdpClientCache": "{}"}}}}
    pad = "<!-- " + ("x" * 600) + " -->"
    html = (
        f"<html><body>{pad}"
        f'<script id="__NEXT_DATA__">{json.dumps(nd)}</script></body></html>'
    )
    with pytest.raises(ParseError):
        parse(_make_fetch_result(html))


# ─── Deep-walk fallback for schema drift ────────────────────────


def test_parse_deep_walk_fallback_finds_property_under_unknown_path() -> None:
    """Schema-drift scenario: gdpClientCache is gone, but the property is
    still present somewhere else under __NEXT_DATA__."""
    nd = {
        "props": {
            "pageProps": {
                "brandNewKey": {
                    "initialState": {
                        "apollo": {
                            "Property:555": {
                                "zpid": 555,
                                "zestimate": 2_345_678,
                                "streetAddress": "9 Elm St",
                                "address": {
                                    "streetAddress": "9 Elm St",
                                    "city": "Portland",
                                    "state": "OR",
                                    "zipcode": "97201",
                                },
                            }
                        }
                    }
                }
            }
        }
    }
    pad = "<!-- " + ("x" * 600) + " -->"
    html = (
        f"<html><body>{pad}"
        f'<script id="__NEXT_DATA__">{json.dumps(nd)}</script></body></html>'
    )
    result = parse(_make_fetch_result(html))
    assert result.status == ZestimateStatus.OK
    assert result.value == 2_345_678
    assert result.zpid == "555"


# ─── JSON regex last-resort fallback ───────────────────────────


def test_parse_json_regex_fallback() -> None:
    """No __NEXT_DATA__ at all — parser should still find the embedded JSON."""
    html = (
        "<html><body>" + ("x" * 1000) +
        '<script>window.__INITIAL={"zestimate":777000,"zpid":1}</script>'
        + "</body></html>"
    )
    result = parse(_make_fetch_result(html))
    assert result.status == ZestimateStatus.OK
    assert result.value == 777_000
    assert result.confidence <= 0.7


def test_parse_json_regex_picks_most_common_value() -> None:
    """Multiple zestimate fields (adTargets, property, etc.) — mode wins."""
    html = (
        "<html><body>" + ("x" * 1000) +
        '<script>"zestimate":"500000"</script>'
        '<script>"zestimate":500000,"other":"foo"</script>'
        '<script>"zestimate":500000,"bar":1</script>'
        '<script>"zestimate":999999</script>'  # outlier, only 1 copy
        + "</body></html>"
    )
    result = parse(_make_fetch_result(html))
    assert result.value == 500_000
