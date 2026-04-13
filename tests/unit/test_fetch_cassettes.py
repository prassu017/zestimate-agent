"""VCR-style fetcher tests — replay recorded ScraperAPI responses.

These tests verify the fetch → parse pipeline end-to-end using
cassette-style fixtures without hitting the network. They complement
the eval dataset's synthetic cases by testing with the unblocker
response format.
"""

from __future__ import annotations

import json

import httpx
import pytest

from zestimate_agent.fetch.unblocker import ScraperAPIFetcher
from zestimate_agent.parse import parse


def _synthetic_zillow_html(prop: dict) -> str:
    """Build a minimal but realistic Zillow page with __NEXT_DATA__."""
    gdp = {
        'ForSalePriorityQuery{"zpid":' + str(prop.get("zpid", 999)) + "}": {
            "property": prop,
        },
    }
    nd = {
        "props": {
            "pageProps": {
                "componentProps": {"gdpClientCache": json.dumps(gdp)},
            }
        }
    }
    # Pad to pass the length heuristic (>500 chars).
    pad = "<!-- " + ("x" * 800) + " -->"
    return (
        f"<html><head><title>Zillow | {prop.get('streetAddress', 'Property')}</title></head>"
        f"<body>{pad}"
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script>'
        "</body></html>"
    )


# ─── SFH with full property details ───────────────────────────

_SFH_PROP = {
    "zpid": 61400567,
    "zestimate": 487000,
    "streetAddress": "4215 NE Prescott St",
    "homeType": "SINGLE_FAMILY",
    "bedrooms": 3,
    "bathrooms": 2,
    "livingArea": 1850,
    "lotSize": 5000,
    "yearBuilt": 1948,
    "rentZestimate": 2400,
    "taxAssessedValue": 380000,
    "taxAssessedYear": 2025,
    "homeStatus": "OFF_MARKET",
    "latitude": 45.5355,
    "longitude": -122.6165,
    "county": "Multnomah",
    "address": {
        "streetAddress": "4215 NE Prescott St",
        "city": "Portland",
        "state": "OR",
        "zipcode": "97218",
    },
    "priceHistory": [
        {"event": "Sold", "price": 315000, "date": "2018-06-15"},
        {"event": "Listed for sale", "price": 329000, "date": "2018-04-20"},
    ],
}

_CONDO_PROP = {
    "zpid": 2077104735,
    "zestimate": 1250000,
    "streetAddress": "425 1st St #4602",
    "homeType": "CONDO",
    "bedrooms": 2,
    "bathrooms": 2.0,
    "livingArea": 1185,
    "yearBuilt": 2005,
    "rentZestimate": 4500,
    "homeStatus": "FOR_SALE",
    "price": 1299000,
    "daysOnZillow": 45,
    "latitude": 37.7864,
    "longitude": -122.3927,
    "monthlyHoaFee": 850,
    "address": {
        "streetAddress": "425 1st St #4602",
        "city": "San Francisco",
        "state": "CA",
        "zipcode": "94105",
    },
}

_LUXURY_PROP = {
    "zpid": 20485783,
    "zestimate": 12500000,
    "streetAddress": "1 Belvedere Ave",
    "homeType": "SINGLE_FAMILY",
    "bedrooms": 5,
    "bathrooms": 6.0,
    "livingArea": 7200,
    "lotSize": 18000,
    "yearBuilt": 2015,
    "homeStatus": "FOR_SALE",
    "price": 13900000,
    "rentZestimate": 35000,
    "latitude": 37.8716,
    "longitude": -122.4625,
    "county": "Marin",
    "address": {
        "streetAddress": "1 Belvedere Ave",
        "city": "Belvedere Tiburon",
        "state": "CA",
        "zipcode": "94920",
    },
    "zestimateLowPercent": -5.2,
    "zestimateHighPercent": 5.8,
}


class TestFetchParseIntegration:
    """Cassette-driven fetch → parse pipeline tests."""

    @pytest.mark.asyncio
    async def test_sfh_portland_full_details(self) -> None:
        """SFH with full property details flows through fetch → parse."""
        html = _synthetic_zillow_html(_SFH_PROP)
        url = "https://www.zillow.com/homedetails/61400567_zpid/"

        # Clean sticky state.
        ScraperAPIFetcher._RENDER_URLS.discard(url)
        ScraperAPIFetcher._ULTRA_URLS.discard(url)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=html)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            fetcher = ScraperAPIFetcher("test-key", client=client)
            fetch_result = await fetcher.fetch(url)

        result = parse(fetch_result)
        assert result.value == 487000
        assert result.zpid == "61400567"
        assert result.property_details is not None
        pd = result.property_details
        assert pd.bedrooms == 3
        assert pd.bathrooms == 2.0
        assert pd.living_area_sqft == 1850
        assert pd.year_built == 1948
        assert pd.home_type == "SINGLE_FAMILY"
        assert pd.rent_zestimate == 2400
        assert pd.county == "Multnomah"
        assert pd.last_sold_price == 315000
        assert pd.last_sold_date == "2018-06-15"

    @pytest.mark.asyncio
    async def test_condo_sf_for_sale(self) -> None:
        """Condo with FOR_SALE status and HOA fee."""
        html = _synthetic_zillow_html(_CONDO_PROP)
        url = "https://www.zillow.com/homedetails/2077104735_zpid/"

        ScraperAPIFetcher._RENDER_URLS.discard(url)
        ScraperAPIFetcher._ULTRA_URLS.discard(url)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=html)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            fetcher = ScraperAPIFetcher("test-key", client=client)
            fetch_result = await fetcher.fetch(url)

        result = parse(fetch_result)
        assert result.value == 1250000
        assert result.property_details is not None
        pd = result.property_details
        assert pd.home_type == "CONDO"
        assert pd.monthly_hoa_fee == 850
        assert pd.home_status == "FOR_SALE"
        assert pd.price == 1299000
        assert pd.days_on_zillow == 45

    @pytest.mark.asyncio
    async def test_luxury_belvedere_zestimate_range(self) -> None:
        """Luxury property with Zestimate range percentages."""
        html = _synthetic_zillow_html(_LUXURY_PROP)
        url = "https://www.zillow.com/homedetails/20485783_zpid/"

        ScraperAPIFetcher._RENDER_URLS.discard(url)
        ScraperAPIFetcher._ULTRA_URLS.discard(url)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=html)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            fetcher = ScraperAPIFetcher("test-key", client=client)
            fetch_result = await fetcher.fetch(url)

        result = parse(fetch_result)
        assert result.value == 12500000
        assert result.property_details is not None
        pd = result.property_details
        assert pd.bedrooms == 5
        assert pd.bathrooms == 6.0
        assert pd.living_area_sqft == 7200
        # Zestimate range should be computed from percentages.
        assert pd.zestimate_range_low is not None
        assert pd.zestimate_range_high is not None
        assert pd.zestimate_range_low < 12500000
        assert pd.zestimate_range_high > 12500000
