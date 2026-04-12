"""Zestimate Agent — production-grade Zillow Zestimate lookup."""

from zestimate_agent.models import (
    NormalizedAddress,
    ResolvedProperty,
    ZestimateResult,
    ZestimateStatus,
)

__version__ = "0.1.0"

__all__ = [
    "NormalizedAddress",
    "ResolvedProperty",
    "ZestimateResult",
    "ZestimateStatus",
    "__version__",
]
