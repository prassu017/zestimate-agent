"""Eval dataset — curated `EvalCase` records with golden oracle values.

Each case is tagged with a **mode** (how it should be exercised) and a
**category** (what kind of failure it tests for). The runner filters by
either or both.

Modes
-----

* `synthetic` — deterministic, zero-credit. The case carries its own
  HTML inline (or references a small synthetic page) and a canned
  `ResolvedProperty`. Great for regression testing the parser and every
  fallback tier.
* `fixture` — replays a recorded Zillow HTML file from disk. Stub
  resolver feeds a canned `ResolvedProperty`. Tests the parser against
  *real* Zillow markup without paying credits.
* `live` — actually hits ScraperAPI → Zillow → (optionally) Rentcast.
  Costs ~25 credits per call. Defaults to `--no-crosscheck` to protect
  the Rentcast monthly cap.

Categories (aligned with the task spec's "edge cases"):
    sfh                 — single-family home, happy path
    condo               — multi-unit / apartment
    luxury              — very high value ($5M+)
    rural               — low density, sparse Zillow coverage
    new_construction    — recently listed, may lack history
    no_zestimate        — property exists but Zillow gives no Zestimate
    messy_input         — typos, abbreviations, missing zip, etc.
    not_found           — address should fail to resolve to any property
    ambiguous           — address should trip the ambiguity detector
    parser_fallback     — exercises one of the 4 parser fallback tiers
    blocked             — synthetic block-page (validates error handling)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from zestimate_agent.models import ZestimateStatus

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class EvalMode(StrEnum):
    SYNTHETIC = "synthetic"
    FIXTURE = "fixture"
    LIVE = "live"


class EvalCategory(StrEnum):
    SFH = "sfh"
    CONDO = "condo"
    LUXURY = "luxury"
    RURAL = "rural"
    NEW_CONSTRUCTION = "new_construction"
    NO_ZESTIMATE = "no_zestimate"
    MESSY_INPUT = "messy_input"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    PARSER_FALLBACK = "parser_fallback"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class EvalCase:
    """One row in the eval dataset."""

    id: str
    address: str
    category: EvalCategory
    mode: EvalMode

    expected_status: ZestimateStatus = ZestimateStatus.OK
    expected_value: int | None = None  # None = "we only check status, not value"
    expected_zpid: str | None = None
    tolerance_pct: float = 0.0  # 0 = exact match required

    # ─── Synthetic / fixture plumbing ───────────────────────────
    # For SYNTHETIC mode: inline HTML served by a stub fetcher.
    synthetic_html: str | None = None
    # For FIXTURE mode: HTML file relative to FIXTURES_DIR.
    fixture_html_file: str | None = None
    # For synthetic/fixture: canned resolver output (else runner builds one).
    canned_zpid: str | None = None
    canned_url: str | None = None

    notes: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)

    # ─── Helpers ────────────────────────────────────────────────

    def load_html(self) -> str | None:
        if self.mode == EvalMode.SYNTHETIC:
            return self.synthetic_html
        if self.mode == EvalMode.FIXTURE:
            if self.fixture_html_file is None:
                return None
            return (FIXTURES_DIR / self.fixture_html_file).read_text()
        return None


# ─── Synthetic HTML builders ────────────────────────────────────


def _next_data_html(prop: dict[str, object]) -> str:
    """Minimal Zillow page with a parseable gdpClientCache."""
    gdp = {
        'ForSalePriorityQuery{"zpid":999}': {"property": prop},
    }
    nd = {
        "props": {
            "pageProps": {
                "componentProps": {"gdpClientCache": json.dumps(gdp)},
            }
        }
    }
    pad = "<!-- " + ("x" * 800) + " -->"
    return (
        f"<html><body>{pad}"
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script>'
        "</body></html>"
    )


def _html_regex_fallback(value: int) -> str:
    """HTML with no __NEXT_DATA__ — forces tier 3 (DOM regex)."""
    return (
        "<html><body>" + ("x" * 1000) +
        f"<div>${value:,}<!-- -->Zestimate<sup>&reg;</sup></div>"
        "</body></html>"
    )


def _json_regex_fallback(value: int) -> str:
    """No __NEXT_DATA__, no rendered `Zestimate` label — forces tier 4 (JSON regex)."""
    return (
        "<html><body>" + ("x" * 1000) +
        f'<script>window.__INITIAL={{"zpid":1,"zestimate":{value}}}</script>'
        f'<script>window.__MIRROR={{"zestimate":{value}}}</script>'
        "</body></html>"
    )


def _no_zestimate_html() -> str:
    return _next_data_html(
        {
            "zpid": 999,
            "zestimate": None,
            "streetAddress": "1 Test Ln",
            "address": {
                "streetAddress": "1 Test Ln",
                "city": "Testville",
                "state": "CA",
                "zipcode": "94000",
            },
        }
    )


def _happy_synthetic(
    value: int,
    street: str = "1 Test Ln",
    *,
    home_type: str = "SINGLE_FAMILY",
    bedrooms: int | None = None,
    bathrooms: float | None = None,
    living_area: int | None = None,
    year_built: int | None = None,
    home_status: str | None = None,
    rent_zestimate: int | None = None,
) -> str:
    prop: dict[str, object] = {
        "zpid": 999,
        "zestimate": value,
        "streetAddress": street,
        "homeType": home_type,
        "address": {
            "streetAddress": street,
            "city": "Testville",
            "state": "CA",
            "zipcode": "94000",
        },
    }
    if bedrooms is not None:
        prop["bedrooms"] = bedrooms
    if bathrooms is not None:
        prop["bathrooms"] = bathrooms
    if living_area is not None:
        prop["livingArea"] = living_area
    if year_built is not None:
        prop["yearBuilt"] = year_built
    if home_status is not None:
        prop["homeStatus"] = home_status
    if rent_zestimate is not None:
        prop["rentZestimate"] = rent_zestimate
    return _next_data_html(prop)


# ─── The dataset ────────────────────────────────────────────────

DEFAULT_DATASET: tuple[EvalCase, ...] = (
    # ═══════════════════════════════════════════════════════════════
    # SYNTHETIC — deterministic, zero-credit
    # ═══════════════════════════════════════════════════════════════

    # ─── SFH variants ──────────────────────────────────────────
    EvalCase(
        id="syn-sfh-happy",
        address="42 Oak Street, Testville, CA 94000",
        category=EvalCategory.SFH,
        mode=EvalMode.SYNTHETIC,
        expected_value=850_000,
        expected_zpid="999",
        synthetic_html=_happy_synthetic(
            850_000, "42 Oak Street", bedrooms=4, bathrooms=2.5,
            living_area=2400, year_built=1995,
        ),
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="baseline SFH through primary gdpClientCache path",
    ),
    EvalCase(
        id="syn-sfh-starter",
        address="18 Elm Dr, Testville, CA 94000",
        category=EvalCategory.SFH,
        mode=EvalMode.SYNTHETIC,
        expected_value=285_000,
        synthetic_html=_happy_synthetic(
            285_000, "18 Elm Dr", bedrooms=2, bathrooms=1.0,
            living_area=1100, year_built=1962,
        ),
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="low-end SFH near sanity floor boundary",
    ),
    EvalCase(
        id="syn-sfh-mcmansion",
        address="1 Estate Cir, Testville, CA 94000",
        category=EvalCategory.SFH,
        mode=EvalMode.SYNTHETIC,
        expected_value=2_400_000,
        synthetic_html=_happy_synthetic(
            2_400_000, "1 Estate Cir", bedrooms=6, bathrooms=5.5,
            living_area=6800, year_built=2019,
        ),
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="upper-mid SFH with large lot",
    ),

    # ─── Condo variants ───────────────────────────────────────
    EvalCase(
        id="syn-condo-happy",
        address="200 Market St #42, Testville, CA 94000",
        category=EvalCategory.CONDO,
        mode=EvalMode.SYNTHETIC,
        expected_value=620_000,
        expected_zpid="999",
        synthetic_html=_happy_synthetic(
            620_000, "200 Market St #42", home_type="CONDO",
            bedrooms=2, bathrooms=1.0, living_area=990,
        ),
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
    ),
    EvalCase(
        id="syn-condo-studio",
        address="55 1st St #101, Testville, CA 94000",
        category=EvalCategory.CONDO,
        mode=EvalMode.SYNTHETIC,
        expected_value=340_000,
        synthetic_html=_happy_synthetic(
            340_000, "55 1st St #101", home_type="CONDO",
            bedrooms=0, bathrooms=1.0, living_area=480,
        ),
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="studio condo — 0 bedrooms is valid",
    ),
    EvalCase(
        id="syn-condo-penthouse",
        address="100 Skyline Blvd PH1, Testville, CA 94000",
        category=EvalCategory.CONDO,
        mode=EvalMode.SYNTHETIC,
        expected_value=4_200_000,
        synthetic_html=_happy_synthetic(
            4_200_000, "100 Skyline Blvd PH1", home_type="CONDO",
            bedrooms=3, bathrooms=3.0, living_area=3200, year_built=2022,
        ),
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="luxury penthouse condo",
    ),

    # ─── Townhouse ─────────────────────────────────────────────
    EvalCase(
        id="syn-townhouse",
        address="12 Rowhome Way, Testville, CA 94000",
        category=EvalCategory.SFH,
        mode=EvalMode.SYNTHETIC,
        expected_value=475_000,
        synthetic_html=_happy_synthetic(
            475_000, "12 Rowhome Way", home_type="TOWNHOUSE",
            bedrooms=3, bathrooms=2.5, living_area=1800,
        ),
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="townhouse — different home_type, same happy path",
    ),

    # ─── Multi-family ──────────────────────────────────────────
    EvalCase(
        id="syn-multifamily",
        address="88 Duplex Ln, Testville, CA 94000",
        category=EvalCategory.SFH,
        mode=EvalMode.SYNTHETIC,
        expected_value=780_000,
        synthetic_html=_happy_synthetic(
            780_000, "88 Duplex Ln", home_type="MULTI_FAMILY",
            bedrooms=4, bathrooms=2.0,
        ),
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="multi-family property (duplex)",
    ),

    # ─── Luxury variants ──────────────────────────────────────
    EvalCase(
        id="syn-luxury-happy",
        address="1 Billionaire Row, Testville, CA 94000",
        category=EvalCategory.LUXURY,
        mode=EvalMode.SYNTHETIC,
        expected_value=45_000_000,
        synthetic_html=_happy_synthetic(45_000_000, "1 Billionaire Row"),
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="validates sanity ceiling doesn't over-reject",
    ),
    EvalCase(
        id="syn-luxury-5m",
        address="9 Pacific Heights Ter, Testville, CA 94000",
        category=EvalCategory.LUXURY,
        mode=EvalMode.SYNTHETIC,
        expected_value=5_250_000,
        synthetic_html=_happy_synthetic(
            5_250_000, "9 Pacific Heights Ter", bedrooms=5, bathrooms=4.0,
            living_area=5400, year_built=2010,
        ),
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="$5M+ threshold luxury property",
    ),
    EvalCase(
        id="syn-luxury-near-ceiling",
        address="1 Trophy Estate Dr, Testville, CA 94000",
        category=EvalCategory.LUXURY,
        mode=EvalMode.SYNTHETIC,
        expected_value=250_000_000,
        synthetic_html=_happy_synthetic(250_000_000, "1 Trophy Estate Dr"),
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="near the $500M sanity ceiling — should still pass",
    ),

    # ─── Rural / low-value ────────────────────────────────────
    EvalCase(
        id="syn-rural-low",
        address="RR 1 Box 44, Testville, CA 94000",
        category=EvalCategory.RURAL,
        mode=EvalMode.SYNTHETIC,
        expected_value=95_000,
        synthetic_html=_happy_synthetic(
            95_000, "RR 1 Box 44", bedrooms=2, bathrooms=1.0,
            living_area=800, year_built=1940,
        ),
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="rural low-value — above $10k sanity floor",
    ),
    EvalCase(
        id="syn-rural-acreage",
        address="4400 Country Rd, Testville, CA 94000",
        category=EvalCategory.RURAL,
        mode=EvalMode.SYNTHETIC,
        expected_value=320_000,
        synthetic_html=_happy_synthetic(
            320_000, "4400 Country Rd", bedrooms=3, bathrooms=2.0,
            living_area=1600, year_built=1978,
        ),
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="rural property with acreage",
    ),

    # ─── New construction ─────────────────────────────────────
    EvalCase(
        id="syn-new-construction",
        address="1 Model Home Way, Testville, CA 94000",
        category=EvalCategory.NEW_CONSTRUCTION,
        mode=EvalMode.SYNTHETIC,
        expected_value=725_000,
        synthetic_html=_happy_synthetic(
            725_000, "1 Model Home Way", home_type="SINGLE_FAMILY",
            bedrooms=4, bathrooms=3.0, living_area=2800, year_built=2026,
            home_status="FOR_SALE",
        ),
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="new construction — future year_built, FOR_SALE status",
    ),
    EvalCase(
        id="syn-new-construction-no-history",
        address="2 Builder Ct, Testville, CA 94000",
        category=EvalCategory.NEW_CONSTRUCTION,
        mode=EvalMode.SYNTHETIC,
        expected_value=550_000,
        synthetic_html=_happy_synthetic(
            550_000, "2 Builder Ct", year_built=2025, home_status="FOR_SALE",
        ),
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="new build with minimal metadata",
    ),

    # ─── Rental with rent_zestimate ───────────────────────────
    EvalCase(
        id="syn-rental-zestimate",
        address="300 Rental Ave #8, Testville, CA 94000",
        category=EvalCategory.SFH,
        mode=EvalMode.SYNTHETIC,
        expected_value=410_000,
        synthetic_html=_happy_synthetic(
            410_000, "300 Rental Ave #8", home_type="CONDO",
            rent_zestimate=2100, bedrooms=1, bathrooms=1.0,
        ),
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="property with rent_zestimate — validates property_details extraction",
    ),

    # ─── Sanity check boundary cases ──────────────────────────
    EvalCase(
        id="syn-sanity-floor-pass",
        address="1 Floor Pass Ln, Testville, CA 94000",
        category=EvalCategory.SFH,
        mode=EvalMode.SYNTHETIC,
        expected_value=10_001,
        synthetic_html=_happy_synthetic(10_001, "1 Floor Pass Ln"),
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="just above the $10k sanity floor — should pass",
    ),
    EvalCase(
        id="syn-sanity-floor-fail",
        address="1 Floor Fail Ln, Testville, CA 94000",
        category=EvalCategory.SFH,
        mode=EvalMode.SYNTHETIC,
        expected_status=ZestimateStatus.ERROR,
        expected_value=None,
        synthetic_html=_happy_synthetic(5_000, "1 Floor Fail Ln"),
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="below $10k sanity floor — should be flagged as ERROR",
    ),
    EvalCase(
        id="syn-sanity-ceiling-fail",
        address="1 Ceiling Fail Ln, Testville, CA 94000",
        category=EvalCategory.SFH,
        mode=EvalMode.SYNTHETIC,
        expected_status=ZestimateStatus.ERROR,
        expected_value=None,
        synthetic_html=_happy_synthetic(999_000_000, "1 Ceiling Fail Ln"),
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="above $500M sanity ceiling — should be flagged as ERROR",
    ),

    # ─── Parser fallback tiers ────────────────────────────────
    EvalCase(
        id="syn-fallback-html-regex",
        address="77 Regex Ave, Testville, CA 94000",
        category=EvalCategory.PARSER_FALLBACK,
        mode=EvalMode.SYNTHETIC,
        expected_value=1_500_000,
        tolerance_pct=0.0,
        synthetic_html=_html_regex_fallback(1_500_000),
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="exercises tier 3 (DOM regex) — no __NEXT_DATA__ present",
    ),
    EvalCase(
        id="syn-fallback-json-regex",
        address="99 JSON Blvd, Testville, CA 94000",
        category=EvalCategory.PARSER_FALLBACK,
        mode=EvalMode.SYNTHETIC,
        expected_value=777_000,
        synthetic_html=_json_regex_fallback(777_000),
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="exercises tier 4 (raw JSON regex) — last-resort schema drift path",
    ),
    EvalCase(
        id="syn-fallback-html-regex-low",
        address="33 Regex Ct, Testville, CA 94000",
        category=EvalCategory.PARSER_FALLBACK,
        mode=EvalMode.SYNTHETIC,
        expected_value=125_000,
        synthetic_html=_html_regex_fallback(125_000),
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="tier 3 with lower value — tests number formatting",
    ),
    EvalCase(
        id="syn-fallback-json-regex-high",
        address="44 JSON Ct, Testville, CA 94000",
        category=EvalCategory.PARSER_FALLBACK,
        mode=EvalMode.SYNTHETIC,
        expected_value=3_200_000,
        synthetic_html=_json_regex_fallback(3_200_000),
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="tier 4 with higher value",
    ),

    # ─── No-Zestimate variants ────────────────────────────────
    EvalCase(
        id="syn-no-zestimate",
        address="1 Rental Ln, Testville, CA 94000",
        category=EvalCategory.NO_ZESTIMATE,
        mode=EvalMode.SYNTHETIC,
        expected_status=ZestimateStatus.NO_ZESTIMATE,
        expected_value=None,
        synthetic_html=_no_zestimate_html(),
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="property exists, Zestimate is null — should map to NO_ZESTIMATE status",
    ),
    EvalCase(
        id="syn-no-zestimate-zero",
        address="2 Vacant Lot Rd, Testville, CA 94000",
        category=EvalCategory.NO_ZESTIMATE,
        mode=EvalMode.SYNTHETIC,
        expected_status=ZestimateStatus.ERROR,
        expected_value=None,
        synthetic_html=_next_data_html({
            "zpid": 999,
            "zestimate": 0,
            "streetAddress": "2 Vacant Lot Rd",
            "address": {
                "streetAddress": "2 Vacant Lot Rd",
                "city": "Testville",
                "state": "CA",
                "zipcode": "94000",
            },
        }),
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="Zestimate is 0 — parser treats as valid, sanity check catches as below floor",
    ),

    # ─── Blocked page variants ────────────────────────────────
    EvalCase(
        id="syn-blocked-captcha",
        address="0 Captcha Ct, Testville, CA 94000",
        category=EvalCategory.BLOCKED,
        mode=EvalMode.SYNTHETIC,
        expected_status=ZestimateStatus.ERROR,
        expected_value=None,
        synthetic_html="<html><body>"
        + ("x" * 1000)
        + "Press & Hold to confirm you are human"
        + "</body></html>",
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="block-page detection — parser should raise, agent should map to ERROR",
    ),
    EvalCase(
        id="syn-blocked-access-denied",
        address="0 Denied Dr, Testville, CA 94000",
        category=EvalCategory.BLOCKED,
        mode=EvalMode.SYNTHETIC,
        expected_status=ZestimateStatus.ERROR,
        expected_value=None,
        synthetic_html="<html><body>"
        + ("x" * 1000)
        + "Access Denied"
        + "</body></html>",
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="access denied variant of block page",
    ),
    EvalCase(
        id="syn-blocked-empty-page",
        address="0 Empty Ct, Testville, CA 94000",
        category=EvalCategory.BLOCKED,
        mode=EvalMode.SYNTHETIC,
        expected_status=ZestimateStatus.ERROR,
        expected_value=None,
        synthetic_html="<html><body></body></html>",
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="nearly-empty page — parser should fail gracefully",
    ),

    # ─── Property type diversity ──────────────────────────────
    EvalCase(
        id="syn-manufactured",
        address="Lot 7 Mobile Estates, Testville, CA 94000",
        category=EvalCategory.SFH,
        mode=EvalMode.SYNTHETIC,
        expected_value=135_000,
        synthetic_html=_happy_synthetic(
            135_000, "Lot 7 Mobile Estates", home_type="MANUFACTURED",
            bedrooms=3, bathrooms=2.0, living_area=1200, year_built=2005,
        ),
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="manufactured/mobile home type",
    ),
    EvalCase(
        id="syn-coop",
        address="500 Park Ave #12B, Testville, CA 94000",
        category=EvalCategory.CONDO,
        mode=EvalMode.SYNTHETIC,
        expected_value=1_850_000,
        synthetic_html=_happy_synthetic(
            1_850_000, "500 Park Ave #12B", home_type="COOPERATIVE",
            bedrooms=2, bathrooms=2.0, living_area=1400,
        ),
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="co-op apartment type",
    ),

    # ─── Recently sold ────────────────────────────────────────
    EvalCase(
        id="syn-recently-sold",
        address="22 Sold St, Testville, CA 94000",
        category=EvalCategory.SFH,
        mode=EvalMode.SYNTHETIC,
        expected_value=510_000,
        synthetic_html=_happy_synthetic(
            510_000, "22 Sold St", home_status="RECENTLY_SOLD",
            bedrooms=3, bathrooms=2.0, living_area=1700,
        ),
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="recently sold property — still has Zestimate",
    ),

    # ─── Off-market ───────────────────────────────────────────
    EvalCase(
        id="syn-off-market",
        address="9 Quiet Ln, Testville, CA 94000",
        category=EvalCategory.SFH,
        mode=EvalMode.SYNTHETIC,
        expected_value=395_000,
        synthetic_html=_happy_synthetic(
            395_000, "9 Quiet Ln", home_status="OFF_MARKET",
        ),
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="off-market property with Zestimate",
    ),

    # ═══════════════════════════════════════════════════════════════
    # FIXTURE — real Zillow HTML replays (zero credit cost)
    # ═══════════════════════════════════════════════════════════════
    EvalCase(
        id="fix-seattle-condo",
        address="500 5th Ave W #705, Seattle, WA 98119",
        category=EvalCategory.CONDO,
        mode=EvalMode.FIXTURE,
        expected_status=ZestimateStatus.OK,
        expected_value=636_500,
        expected_zpid="82362438",
        fixture_html_file="zillow_82362438.html",
        canned_zpid="82362438",
        canned_url="https://www.zillow.com/homedetails/82362438_zpid/",
        notes="THE oracle — a real Zillow HTML capture we own end-to-end",
    ),

    # ═══════════════════════════════════════════════════════════════
    # LIVE — costs ScraperAPI credits, run with --mode live
    # ═══════════════════════════════════════════════════════════════
    EvalCase(
        id="live-seattle-condo",
        address="500 5th Ave W #705, Seattle, WA 98119",
        category=EvalCategory.CONDO,
        mode=EvalMode.LIVE,
        expected_status=ZestimateStatus.OK,
        expected_value=636_500,
        tolerance_pct=1.0,
        expected_zpid="82362438",
        notes="live canary — same property as the fixture but against real Zillow",
    ),
    EvalCase(
        id="live-empire-state",
        address="350 5th Ave, New York, NY 10118",
        category=EvalCategory.LUXURY,
        mode=EvalMode.LIVE,
        expected_status=ZestimateStatus.NO_ZESTIMATE,
        notes="Empire State Building resolves on Zillow but has no residential Zestimate",
    ),
    EvalCase(
        id="live-white-house",
        address="1600 Pennsylvania Ave NW, Washington, DC 20500",
        category=EvalCategory.LUXURY,
        mode=EvalMode.LIVE,
        expected_status=ZestimateStatus.NO_ZESTIMATE,
        notes="White House — resolves, no Zestimate (non-marketable)",
    ),
    EvalCase(
        id="live-wall-st",
        address="11 Wall St, New York, NY 10005",
        category=EvalCategory.LUXURY,
        mode=EvalMode.LIVE,
        expected_status=ZestimateStatus.NO_ZESTIMATE,
        notes="NYSE building — commercial, no residential Zestimate",
    ),
    EvalCase(
        id="live-messy-input",
        address="500  5TH  avenue w  705,seattle wa",
        category=EvalCategory.MESSY_INPUT,
        mode=EvalMode.LIVE,
        expected_status=ZestimateStatus.OK,
        expected_value=636_500,
        tolerance_pct=1.0,
        expected_zpid="82362438",
        notes="same property, but adversarially dirty input — tests normalize + geocode path",
    ),
    EvalCase(
        id="live-not-found",
        address="123456 Nonexistent Street, Nowhere, XY 00000",
        category=EvalCategory.NOT_FOUND,
        mode=EvalMode.LIVE,
        expected_status=ZestimateStatus.NOT_FOUND,
        notes="plausible-looking but non-existent address — should NOT_FOUND gracefully",
    ),
)


# ─── Filters ────────────────────────────────────────────────────


def by_mode(mode: EvalMode, dataset: tuple[EvalCase, ...] = DEFAULT_DATASET) -> tuple[EvalCase, ...]:
    return tuple(c for c in dataset if c.mode == mode)


def by_category(
    category: EvalCategory, dataset: tuple[EvalCase, ...] = DEFAULT_DATASET
) -> tuple[EvalCase, ...]:
    return tuple(c for c in dataset if c.category == category)
