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


def _happy_synthetic(value: int, street: str = "1 Test Ln") -> str:
    return _next_data_html(
        {
            "zpid": 999,
            "zestimate": value,
            "streetAddress": street,
            "address": {
                "streetAddress": street,
                "city": "Testville",
                "state": "CA",
                "zipcode": "94000",
            },
        }
    )


# ─── The dataset ────────────────────────────────────────────────

DEFAULT_DATASET: tuple[EvalCase, ...] = (
    # ─── Synthetic: happy path, one per category ────────────────
    EvalCase(
        id="syn-sfh-happy",
        address="42 Oak Street, Testville, CA 94000",
        category=EvalCategory.SFH,
        mode=EvalMode.SYNTHETIC,
        expected_value=850_000,
        expected_zpid="999",
        synthetic_html=_happy_synthetic(850_000, "42 Oak Street"),
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="baseline SFH through primary gdpClientCache path",
    ),
    EvalCase(
        id="syn-condo-happy",
        address="200 Market St #42, Testville, CA 94000",
        category=EvalCategory.CONDO,
        mode=EvalMode.SYNTHETIC,
        expected_value=620_000,
        expected_zpid="999",
        synthetic_html=_happy_synthetic(620_000, "200 Market St #42"),
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
    ),
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

    # ─── Synthetic: parser fallback tiers ──────────────────────
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

    # ─── Synthetic: no-Zestimate ───────────────────────────────
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

    # ─── Synthetic: blocked page ───────────────────────────────
    EvalCase(
        id="syn-blocked",
        address="0 Captcha Ct, Testville, CA 94000",
        category=EvalCategory.BLOCKED,
        mode=EvalMode.SYNTHETIC,
        expected_status=ZestimateStatus.ERROR,  # parse → blocked → ERROR via ParseError
        expected_value=None,
        synthetic_html="<html><body>"
        + ("x" * 1000)
        + "Press & Hold to confirm you are human"
        + "</body></html>",
        canned_zpid="999",
        canned_url="https://www.zillow.com/homedetails/999_zpid/",
        notes="block-page detection — parser should raise, agent should map to ERROR",
    ),

    # ─── Fixture: real captured Zillow page ────────────────────
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

    # ─── Live: reserved for --mode live runs (cost credits) ───
    EvalCase(
        id="live-seattle-condo",
        address="500 5th Ave W #705, Seattle, WA 98119",
        category=EvalCategory.CONDO,
        mode=EvalMode.LIVE,
        expected_status=ZestimateStatus.OK,
        expected_value=636_500,
        tolerance_pct=1.0,  # Zillow drifts up to ~1% day-to-day
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
