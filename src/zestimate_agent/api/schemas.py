"""Request/response schemas for the HTTP API.

We deliberately **do not** reuse `ZestimateResult` as the wire type, for two
reasons:

1. The internal model carries fields (e.g. `trace_id`, `fetcher`) that are
   fine to expose but we want a stable external contract we can evolve
   independently of the internal type.
2. We want clean JSON-friendly types (ISO strings, not datetimes) and
   explicit `Optional`s rather than pydantic-defaulted `None`.

`LookupResponse.from_result()` does the translation in one place.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from zestimate_agent.models import ZestimateResult, ZestimateStatus


class LookupRequest(BaseModel):
    """Body for `POST /lookup`."""

    model_config = ConfigDict(extra="forbid")

    address: str = Field(..., min_length=3, max_length=500, description="US property address.")
    skip_crosscheck: bool = Field(
        default=False,
        description="If true, do not call the Rentcast cross-check.",
    )
    force_crosscheck: bool = Field(
        default=False,
        description="If true, run the cross-check even if the monthly cap is reached.",
    )
    use_cache: bool = Field(
        default=True,
        description="If false, bypass the result cache on both read and write.",
    )


class PropertyDetailsOut(BaseModel):
    """Wire form of property metadata (mirrors `models.PropertyDetails`)."""

    bedrooms: int | None = None
    bathrooms: float | None = None
    living_area_sqft: int | None = None
    lot_size_sqft: int | None = None
    home_type: str | None = None
    year_built: int | None = None
    zestimate_range_low: int | None = None
    zestimate_range_high: int | None = None
    rent_zestimate: int | None = None
    tax_assessed_value: int | None = None
    tax_assessed_year: int | None = None
    monthly_hoa_fee: int | None = None
    home_status: str | None = None
    price: int | None = None
    days_on_zillow: int | None = None
    latitude: float | None = None
    longitude: float | None = None
    county: str | None = None
    last_sold_price: int | None = None
    last_sold_date: str | None = None


class CrossCheckOut(BaseModel):
    """Wire form of a cross-check (mirrors `models.CrossCheck`)."""

    provider: str
    estimate: int | None = None
    range_low: int | None = None
    range_high: int | None = None
    delta_pct: float | None = None
    within_tolerance: bool | None = None
    skipped: bool = False
    skipped_reason: str | None = None


class LookupResponse(BaseModel):
    """Body returned by `POST /lookup`."""

    status: ZestimateStatus
    ok: bool
    value: int | None = None
    currency: str = "USD"
    zpid: str | None = None
    matched_address: str | None = None
    zillow_url: str | None = None
    confidence: float
    fetcher: str | None = None
    cached: bool = False
    crosscheck: CrossCheckOut | None = None
    property_details: PropertyDetailsOut | None = None
    alternates: list[dict[str, Any]] = Field(default_factory=list)
    as_of: str | None = None
    fetched_at: str
    elapsed_ms: int | None = Field(
        default=None,
        description="Wall-clock latency from request entry to response, in milliseconds.",
    )
    trace_id: str | None = None
    error: str | None = None

    @classmethod
    def from_result(
        cls,
        result: ZestimateResult,
        *,
        elapsed_ms: int | None = None,
    ) -> LookupResponse:
        cc: CrossCheckOut | None = None
        if result.crosscheck is not None:
            cc = CrossCheckOut(
                provider=result.crosscheck.provider,
                estimate=result.crosscheck.estimate,
                range_low=result.crosscheck.range_low,
                range_high=result.crosscheck.range_high,
                delta_pct=result.crosscheck.delta_pct,
                within_tolerance=result.crosscheck.within_tolerance,
                skipped=result.crosscheck.skipped,
                skipped_reason=result.crosscheck.skipped_reason,
            )
        pd: PropertyDetailsOut | None = None
        if result.property_details is not None:
            pd = PropertyDetailsOut(**result.property_details.model_dump())

        return cls(
            status=result.status,
            ok=result.ok,
            value=result.value,
            currency=result.currency,
            zpid=result.zpid,
            matched_address=result.matched_address,
            zillow_url=result.zillow_url,
            confidence=result.confidence,
            fetcher=result.fetcher,
            cached=result.cached,
            crosscheck=cc,
            property_details=pd,
            alternates=result.alternates,
            as_of=result.as_of.isoformat() if result.as_of else None,
            fetched_at=result.fetched_at.isoformat(),
            elapsed_ms=elapsed_ms,
            trace_id=result.trace_id,
            error=result.error,
        )


class HealthResponse(BaseModel):
    """Body for `GET /healthz` / `GET /readyz`."""

    status: str
    checks: dict[str, Any] = Field(default_factory=dict)


class VersionResponse(BaseModel):
    name: str
    version: str


class ErrorResponse(BaseModel):
    """RFC7807-ish error envelope."""

    error: str
    detail: str | None = None
