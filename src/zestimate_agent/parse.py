"""Zillow page parser — extracts the Zestimate from a fetched HTML page.

Zillow embeds property data in a double-encoded JSON blob:

    <script id="__NEXT_DATA__">
      { ..., "props": { "pageProps": { "componentProps": {
          "gdpClientCache": "{\"ForSalePriorityQuery{...}\": {
            \"property\": {
              \"zestimate\": 636500,
              \"zpid\": 82362438,
              \"address\": {\"streetAddress\": ..., \"city\": ..., ...},
              ...
            }
          }}"
      }}}}
    </script>

`gdpClientCache` is a **JSON string inside a JSON object**, keyed by GraphQL
query name + variables. Different query names appear for different listing
states (`ForSalePriorityQuery`, `OffMarketPriorityQuery`, `RentalPriorityQuery`,
etc.), so we iterate all top-level entries and pick the first one that has a
`.property` with address or zpid.

A rendered-HTML regex fallback is used as a safety net if `gdpClientCache`
isn't found (schema drift or A/B test variants).
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from zestimate_agent.errors import NoZestimateError, ParseError
from zestimate_agent.logging import get_logger
from zestimate_agent.models import FetchResult, ZestimateResult, ZestimateStatus

log = get_logger(__name__)

# ─── Regexes ────────────────────────────────────────────────────

_NEXT_DATA_RE = re.compile(
    r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.DOTALL,
)

# Rendered-DOM fallback: "$1,234,567<!-- -->Zestimate" — Zillow renders
# the value with a React HTML comment between it and the label.
_HTML_ZESTIMATE_RE = re.compile(
    r'\$([0-9][0-9,]*)\s*(?:<!--[^>]*-->)?\s*Zestimate',
    re.IGNORECASE,
)

# JSON-embedded fallback: matches `"zestimate":1234567` anywhere in the HTML,
# regardless of how deeply nested or whether our structured walker handles
# that particular schema variant.
_JSON_ZESTIMATE_RE = re.compile(
    r'\\?"zestimate\\?"\s*:\s*(\d{4,9})',
)

# Block / captcha detection — these strings appear *in the visible body* when
# PerimeterX intercepts. We deliberately exclude generic substrings like
# "perimeterx" because the PerimeterX client script is loaded on every
# legitimate page too.
_BLOCK_MARKERS = (
    "Press & Hold to confirm you are",
    "Access to this page has been denied",
    "Please verify you are a human",
    '<div id="px-captcha"',
    "Pardon Our Interruption",
)


# ─── Public API ─────────────────────────────────────────────────


def parse(fetch_result: FetchResult) -> ZestimateResult:
    """Extract a ZestimateResult from a fetched HTML page.

    Raises:
        ParseError: if the page was fetched but we can't find the Zestimate.
        NoZestimateError: if the page exists but Zillow has no Zestimate
            for that property (returned as status=NO_ZESTIMATE by orchestrator).
    """
    html = fetch_result.html
    trace_id = str(uuid.uuid4())

    if _looks_blocked(html):
        raise ParseError("page appears blocked by anti-bot (captcha / WAF)")

    # ─── Primary path: __NEXT_DATA__ → gdpClientCache ───
    next_data = _extract_next_data(html)
    if next_data is not None:
        prop = _find_property(next_data)
        if prop is not None:
            return _build_result(prop, fetch_result, trace_id)

    # ─── Fallback 1: deep walk __NEXT_DATA__ for any dict with zestimate+zpid ───
    if next_data is not None:
        log.debug("primary path failed — trying __NEXT_DATA__ deep walk")
        prop = _deep_walk_property(next_data)
        if prop is not None:
            return _build_result(prop, fetch_result, trace_id)

    # ─── Fallback 2: rendered HTML regex ───
    log.debug("deep walk failed — trying HTML regex fallback")
    html_value = _parse_from_html(html)
    if html_value is not None:
        return ZestimateResult(
            status=ZestimateStatus.OK,
            value=html_value,
            fetcher=fetch_result.fetcher,
            fetched_at=fetch_result.fetched_at,
            zillow_url=fetch_result.final_url,
            confidence=0.7,
            trace_id=trace_id,
        )

    # ─── Fallback 3: raw JSON regex anywhere in HTML ───
    log.debug("HTML regex failed — trying raw JSON regex")
    json_value = _parse_from_json_regex(html)
    if json_value is not None:
        return ZestimateResult(
            status=ZestimateStatus.OK,
            value=json_value,
            fetcher=fetch_result.fetcher,
            fetched_at=fetch_result.fetched_at,
            zillow_url=fetch_result.final_url,
            confidence=0.6,  # lowest — no metadata at all
            trace_id=trace_id,
        )

    raise ParseError("no zestimate found in page (all fallbacks exhausted)")


# ─── Internal helpers ───────────────────────────────────────────


def _looks_blocked(html: str) -> bool:
    if len(html) < 500:
        return True
    lowered = html.lower()
    return any(marker.lower() in lowered for marker in _BLOCK_MARKERS)


def _extract_next_data(html: str) -> dict[str, Any] | None:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))  # type: ignore[no-any-return]
    except json.JSONDecodeError as e:
        log.warning("__NEXT_DATA__ JSON decode failed", error=str(e))
        return None


def _find_property(next_data: dict[str, Any]) -> dict[str, Any] | None:
    """Walk __NEXT_DATA__ → gdpClientCache → first query with .property."""
    try:
        component_props = next_data["props"]["pageProps"]["componentProps"]
    except (KeyError, TypeError):
        return None

    gdp_raw = component_props.get("gdpClientCache")
    if not isinstance(gdp_raw, str):
        return None

    try:
        gdp = json.loads(gdp_raw)
    except json.JSONDecodeError as e:
        log.warning("gdpClientCache JSON decode failed", error=str(e))
        return None

    if not isinstance(gdp, dict):
        return None

    # Iterate all query results; first one with a .property wins.
    # We prefer a property that has a zestimate over one that doesn't,
    # in case there's both a for-sale and historical variant.
    best: dict[str, Any] | None = None
    for _query_key, query_val in gdp.items():
        if not isinstance(query_val, dict):
            continue
        prop = query_val.get("property")
        if not isinstance(prop, dict):
            continue
        if "zestimate" in prop and prop["zestimate"] is not None:
            return prop  # prefer a property that actually has a zestimate
        if best is None:
            best = prop
    return best


def _build_result(
    prop: dict[str, Any],
    fetch_result: FetchResult,
    trace_id: str,
) -> ZestimateResult:
    zestimate = prop.get("zestimate")
    zpid = prop.get("zpid")
    zpid_str = str(zpid) if zpid is not None else None

    address = prop.get("address") or {}
    matched = _format_address(address, prop)

    zillow_url = fetch_result.final_url
    if zpid_str and "/homedetails/" not in zillow_url:
        zillow_url = f"https://www.zillow.com/homedetails/{zpid_str}_zpid/"

    if zestimate is None:
        raise NoZestimateError(
            f"property {zpid_str or '?'} has no Zestimate field on zillow.com"
        )

    try:
        value = int(zestimate)
    except (TypeError, ValueError) as e:
        raise ParseError(f"zestimate is not an integer: {zestimate!r}") from e

    return ZestimateResult(
        status=ZestimateStatus.OK,
        value=value,
        zpid=zpid_str,
        matched_address=matched,
        zillow_url=zillow_url,
        fetcher=fetch_result.fetcher,
        fetched_at=fetch_result.fetched_at,
        confidence=1.0,
        trace_id=trace_id,
    )


def _format_address(address: dict[str, Any], prop: dict[str, Any]) -> str | None:
    street = address.get("streetAddress") or prop.get("streetAddress")
    city = address.get("city") or prop.get("city")
    state = address.get("state") or prop.get("state")
    zipcode = address.get("zipcode") or prop.get("zipcode")
    if not (street and city and state):
        return None
    base = f"{street}, {city}, {state}"
    return f"{base} {zipcode}" if zipcode else base


def _parse_from_html(html: str) -> int | None:
    m = _HTML_ZESTIMATE_RE.search(html)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _parse_from_json_regex(html: str) -> int | None:
    """Find any `"zestimate": 1234567` pattern embedded in the HTML.

    This is the most schema-drift-resistant fallback — it works even if
    Zillow completely rearranges __NEXT_DATA__, as long as the Zestimate
    is JSON-encoded somewhere on the page. We pick the most common value
    (mode) because multiple copies of the same field appear in adTargets,
    property, valueChange, etc.
    """
    matches = _JSON_ZESTIMATE_RE.findall(html)
    if not matches:
        return None
    counts: dict[int, int] = {}
    for raw in matches:
        try:
            v = int(raw)
        except ValueError:
            continue
        counts[v] = counts.get(v, 0) + 1
    if not counts:
        return None
    # Return the most frequent match (ties: highest value for safety).
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _deep_walk_property(obj: Any) -> dict[str, Any] | None:
    """Recursively find any dict that has both `zestimate` (int) and `zpid`.

    Last-resort structured fallback when the known gdpClientCache path
    breaks due to schema drift. We prefer dicts that look like property
    records (have both a zpid and a zestimate).
    """
    stack: list[Any] = [obj]
    candidates: list[dict[str, Any]] = []
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if (
                "zestimate" in cur
                and isinstance(cur.get("zestimate"), int)
                and "zpid" in cur
            ):
                candidates.append(cur)
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    if not candidates:
        return None
    # Prefer the one with the most property-like keys
    candidates.sort(
        key=lambda d: sum(k in d for k in ("address", "streetAddress", "price", "livingAreaValue")),
        reverse=True,
    )
    return candidates[0]
