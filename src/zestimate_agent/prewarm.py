"""Zillow sitemap parser — extracts property URLs for cache pre-warming.

Zillow publishes gzipped XML sitemaps that list every property page. The
pre-warmer fetches the sitemap index, collects property URLs matching a
target region, and feeds them through the agent pipeline to populate the
cache ahead of user requests.

This is designed for nightly cron usage::

    zestimate prewarm --sitemap-url https://www.zillow.com/sitemap/... \
        --concurrency 5 --limit 100

Or with a file of addresses::

    zestimate prewarm --file addresses.txt --concurrency 5
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

import httpx

from zestimate_agent.agent import ZestimateAgent
from zestimate_agent.logging import get_logger
from zestimate_agent.models import ZestimateResult

log = get_logger(__name__)

# Matches Zillow property detail URLs in sitemap XML.
_ZPID_RE = re.compile(r"https://www\.zillow\.com/homedetails/[^<]+?(\d+)_zpid/")


@dataclass
class PrewarmStats:
    total: int = 0
    ok: int = 0
    cached: int = 0
    errors: int = 0
    addresses: list[str] = field(default_factory=list)


async def fetch_sitemap_urls(
    sitemap_url: str,
    *,
    limit: int = 100,
    timeout: float = 30.0,
) -> list[str]:
    """Fetch a Zillow sitemap XML and extract property detail URLs.

    Returns up to ``limit`` unique URLs.
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(sitemap_url)
        resp.raise_for_status()

    urls = _ZPID_RE.findall(resp.text)
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for zpid in urls:
        url = f"https://www.zillow.com/homedetails/{zpid}_zpid/"
        if url not in seen:
            seen.add(url)
            unique.append(url)
        if len(unique) >= limit:
            break
    return unique


async def prewarm_from_addresses(
    addresses: list[str],
    agent: ZestimateAgent,
    *,
    concurrency: int = 3,
    use_cache: bool = True,
) -> PrewarmStats:
    """Look up each address through the agent to populate the cache.

    Uses bounded concurrency to avoid overwhelming the fetcher.
    """
    stats = PrewarmStats(total=len(addresses))
    sem = asyncio.Semaphore(concurrency)

    async def _do(addr: str) -> ZestimateResult:
        async with sem:
            return await agent.aget(addr, skip_crosscheck=True, use_cache=use_cache)

    results = await asyncio.gather(*[_do(a) for a in addresses])

    for addr, result in zip(addresses, results, strict=True):
        if result.cached:
            stats.cached += 1
        elif result.ok:
            stats.ok += 1
        else:
            stats.errors += 1
        stats.addresses.append(addr)
        log.debug(
            "prewarm result",
            address=addr,
            status=result.status.value,
            cached=result.cached,
        )

    return stats
