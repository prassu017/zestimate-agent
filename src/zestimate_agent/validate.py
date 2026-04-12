"""Validator — sanity checks + optional cross-check against Rentcast.

This runs **after** `parse.parse()` and before `ZestimateAgent` returns the
result to the caller. It has two jobs:

1. **Sanity checks** — catch obviously-broken Zestimates (e.g. $3, $999B) and
   flip them to `ERROR` status. These rules are conservative; they should only
   fire on parser bugs, not on legitimately unusual properties.

2. **Cross-check** — ask a second data provider (Rentcast) for an independent
   AVM for the same address. If it agrees within tolerance, confidence stays
   put. If it disagrees, confidence is halved and the disagreement is surfaced
   in `result.crosscheck`. The cross-check is **advisory only** — it never
   overwrites Zillow's value or changes `status` from OK to anything else.

Cross-check is optional and skippable:
- disabled via `CROSSCHECK_ENABLED=0`
- skipped when Rentcast monthly cap is reached (see `crosscheck.UsageCounter`)
- skipped per-call via `agent.aget(..., no_crosscheck=True)`
"""

from __future__ import annotations

from zestimate_agent.crosscheck import RentcastClient
from zestimate_agent.logging import get_logger
from zestimate_agent.models import (
    CrossCheck,
    NormalizedAddress,
    ZestimateResult,
    ZestimateStatus,
)

log = get_logger(__name__)

# Absolute sanity bounds — no legit US residential Zestimate will fall
# outside these. Tightened later as we collect more eval data.
_MIN_VALUE = 10_000  # $10k — below this, it's almost certainly a parse bug
_MAX_VALUE = 500_000_000  # $500M — above this, ditto (even Bel-Air mansions top out ~$250M)


# ─── Sanity ─────────────────────────────────────────────────────


def sanity_check(result: ZestimateResult) -> ZestimateResult:
    """Flag obviously-wrong Zestimates. Returns a (possibly updated) result."""
    if result.status != ZestimateStatus.OK or result.value is None:
        return result

    if result.value < _MIN_VALUE:
        log.warning("sanity: value below floor", value=result.value, zpid=result.zpid)
        return result.model_copy(
            update={
                "status": ZestimateStatus.ERROR,
                "error": f"sanity check: value ${result.value:,} below ${_MIN_VALUE:,} floor",
            }
        )

    if result.value > _MAX_VALUE:
        log.warning("sanity: value above ceiling", value=result.value, zpid=result.zpid)
        return result.model_copy(
            update={
                "status": ZestimateStatus.ERROR,
                "error": f"sanity check: value ${result.value:,} above ${_MAX_VALUE:,} ceiling",
            }
        )

    return result


# ─── Cross-check ────────────────────────────────────────────────


async def cross_check(
    result: ZestimateResult,
    *,
    client: RentcastClient | None,
    address: NormalizedAddress | None,
    force: bool = False,
) -> ZestimateResult:
    """Run Rentcast cross-check and return an updated result.

    Never raises. If the client is None or preconditions aren't met, returns
    the result unchanged.
    """
    if client is None:
        return result
    if result.status != ZestimateStatus.OK or result.value is None:
        return result
    if address is None:
        # Without a canonical address we can't query Rentcast.
        return result.model_copy(
            update={
                "crosscheck": CrossCheck(
                    provider="rentcast",
                    skipped=True,
                    skipped_reason="no normalized address available",
                )
            }
        )

    cc = await client.cross_check(
        address=address,
        zillow_value=result.value,
        force=force,
    )

    # Confidence adjustment: if the cross-check ran *and* disagrees, halve
    # confidence. Agreement or a skipped cross-check leaves confidence alone.
    new_confidence = result.confidence
    if not cc.skipped and cc.within_tolerance is False:
        new_confidence = result.confidence * 0.5
        log.warning(
            "crosscheck disagreement — halving confidence",
            zillow=result.value,
            rentcast=cc.estimate,
            delta_pct=cc.delta_pct,
            confidence_before=result.confidence,
            confidence_after=new_confidence,
        )
    elif not cc.skipped and cc.within_tolerance is True:
        log.info(
            "crosscheck agreement",
            zillow=result.value,
            rentcast=cc.estimate,
            delta_pct=cc.delta_pct,
        )

    return result.model_copy(
        update={
            "crosscheck": cc,
            "confidence": round(new_confidence, 4),
        }
    )


# ─── Convenience pipeline ───────────────────────────────────────


async def validate(
    result: ZestimateResult,
    *,
    client: RentcastClient | None = None,
    address: NormalizedAddress | None = None,
    force_crosscheck: bool = False,
    skip_crosscheck: bool = False,
) -> ZestimateResult:
    """Run sanity + (optional) cross-check. Convenience wrapper."""
    result = sanity_check(result)
    if skip_crosscheck:
        return result
    return await cross_check(
        result,
        client=client,
        address=address,
        force=force_crosscheck,
    )
