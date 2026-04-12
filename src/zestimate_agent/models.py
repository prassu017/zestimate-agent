"""Core pydantic models — the contracts between every layer of the agent.

These types flow through the pipeline:

    str  ──normalize──►  NormalizedAddress
         ──resolve────►  ResolvedProperty
         ──fetch──────►  FetchResult
         ──parse──────►  ZestimateResult
         ──validate──►  ZestimateResult (with confidence adjusted)

Keeping them in one file (rather than split per module) makes the full data
flow easy to read end-to-end, which matters more than separation here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ─── Enums ──────────────────────────────────────────────────────


class ZestimateStatus(StrEnum):
    """Terminal status for a Zestimate lookup."""

    OK = "ok"
    NO_ZESTIMATE = "no_zestimate"  # property exists on Zillow but has no Zestimate
    NOT_FOUND = "not_found"  # address did not resolve to a Zillow property
    AMBIGUOUS = "ambiguous"  # multiple candidates, no clear winner
    BLOCKED = "blocked"  # fetcher was blocked (captcha, 403, rate limit)
    ERROR = "error"  # unexpected failure


# ─── Normalized address ─────────────────────────────────────────


class NormalizedAddress(BaseModel):
    """A US address parsed into canonical components.

    `canonical` is the single-line form used for cache keys and Zillow
    autocomplete queries. `street`/`city`/`state`/`zip` are used for
    disambiguation when the resolver returns multiple candidates.
    """

    model_config = ConfigDict(frozen=True)

    raw: str = Field(..., description="Original input string, untouched.")
    street: str
    city: str
    state: str = Field(..., min_length=2, max_length=2, description="USPS 2-letter state code.")
    zip: str = Field(..., pattern=r"^\d{5}(-\d{4})?$", description="5 or 9 digit ZIP.")
    canonical: str = Field(..., description="'Street, City, ST ZIP' form.")
    lat: float | None = None
    lon: float | None = None
    parse_confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("state")
    @classmethod
    def _upper_state(cls, v: str) -> str:
        return v.upper()

    @field_validator("zip")
    @classmethod
    def _zip5(cls, v: str) -> str:
        # Keep only the 5-digit portion for canonical comparison;
        # the +4 is preserved in `raw` if needed.
        return v[:5]


# ─── Resolved property ──────────────────────────────────────────


class ResolvedProperty(BaseModel):
    """A Zillow property identified for a given address.

    Produced by the Resolver after calling Zillow's autocomplete/search
    endpoint. `match_confidence` reflects how closely the resolved address
    matches the input after component comparison.
    """

    model_config = ConfigDict(frozen=True)

    zpid: str
    url: str
    matched_address: str
    match_confidence: float = Field(ge=0.0, le=1.0)
    alternates: list[dict[str, Any]] = Field(default_factory=list)


# ─── Raw fetch result ───────────────────────────────────────────


class FetchResult(BaseModel):
    """Output of a Fetcher — raw HTML plus metadata."""

    html: str
    status: int
    final_url: str
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    fetcher: str  # "zenrows" | "scraperapi" | "playwright" | ...
    elapsed_ms: int | None = None


# ─── Final result ───────────────────────────────────────────────


class CrossCheck(BaseModel):
    """Independent valuation from a second data provider (e.g. Rentcast).

    Used to cross-validate the Zillow Zestimate. We never overwrite Zillow's
    number — we only *adjust confidence* based on agreement. If the cross-check
    provider disagrees by more than the configured tolerance, confidence is
    halved and the disagreement is surfaced in the result.

    A cross-check may be skipped (not performed) for several reasons — the
    monthly request cap being the most common — in which case `skipped_reason`
    is set and `estimate` is None.
    """

    provider: str  # "rentcast" | "attom" | ...
    estimate: int | None = None
    range_low: int | None = None
    range_high: int | None = None
    delta_pct: float | None = Field(
        default=None,
        description="(crosscheck - zillow) / zillow * 100, signed.",
    )
    within_tolerance: bool | None = None
    skipped: bool = False
    skipped_reason: str | None = None
    fetched_at: datetime | None = None


class ZestimateResult(BaseModel):
    """Top-level output of the agent. Always returned — never raises."""

    status: ZestimateStatus
    value: int | None = Field(default=None, description="Zestimate in USD, integer dollars.")
    currency: str = "USD"

    zpid: str | None = None
    matched_address: str | None = None
    zillow_url: str | None = None

    as_of: datetime | None = Field(
        default=None, description="Zestimate last-updated timestamp, if known."
    )
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    alternates: list[dict[str, Any]] = Field(default_factory=list)
    crosscheck: CrossCheck | None = None

    raw_source: str = "zillow.com"
    fetcher: str | None = None
    trace_id: str | None = None
    cached: bool = Field(
        default=False,
        description="True if this result was served from the local cache.",
    )

    error: str | None = None

    # ─── Convenience ────────────────────────────────────────────

    @property
    def ok(self) -> bool:
        return self.status == ZestimateStatus.OK and self.value is not None

    def to_display(self) -> str:
        """Human-readable single-line form for CLI output."""
        if self.ok:
            assert self.value is not None
            return f"${self.value:,}  —  {self.matched_address}  (zpid={self.zpid})"
        return f"[{self.status.value}] {self.error or self.matched_address or ''}".strip()
