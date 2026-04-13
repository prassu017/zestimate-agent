"""Vercel Python function entrypoint.

Vercel's Python runtime auto-discovers any module under `/api/` that exposes
an ASGI `app` symbol and hosts it as a serverless function. This file is a
thin adapter over the real app factory.

Serverless caveats (vs. a real container deployment):
    * No persistent disk — we force `CACHE_BACKEND=memory` so diskcache
      doesn't try to open a SQLite file on a read-only filesystem.
    * No long-lived process — cold start per invocation, so the in-memory
      cache resets constantly. The synthetic/healthz/version/metrics
      endpoints still work perfectly. Live /lookup calls may exceed Vercel's
      10s hobby execution limit because real Zillow scraping takes 15-30s.
    * No Rentcast counter persistence — we also disable cross-check on
      Vercel so the ephemeral counter file never matters.

Env vars to set in the Vercel dashboard (Settings → Environment Variables):
    ZESTIMATE_API_KEY=<shared secret>         (optional, enables auth)
    UNBLOCKER_API_KEY=<scraperapi key>        (required for live lookups)
    CROSSCHECK_PROVIDER=none                  (already forced below)

For production, deploy the Dockerfile instead — see README.md §Docker.
"""

from __future__ import annotations

import os

# ─── Force serverless-safe defaults before importing the agent ──
# These MUST be set before `zestimate_agent.config` is first imported,
# because get_settings() is @lru_cache'd and locks in whatever it sees.
#
# Vercel's function filesystem layout:
#   /var/task      — read-only (code + deps)
#   /tmp           — writable, ~512MB, wiped on cold start
#
# Any path that the agent writes to MUST live under /tmp, otherwise
# the first write raises `OSError: [Errno 30] Read-only file system`.
# Use SQLite on /tmp so the cache survives across warm invocations
# (same container). Memory cache resets every request in serverless.
os.environ.setdefault("CACHE_BACKEND", "sqlite")
os.environ.setdefault("CACHE_PATH", "/tmp/zestimate.db")
os.environ.setdefault("RENTCAST_USAGE_PATH", "/tmp/rentcast_usage.json")
os.environ.setdefault("CROSSCHECK_PROVIDER", "none")
os.environ.setdefault("LOG_FORMAT", "json")
os.environ.setdefault("LOG_LEVEL", "INFO")

# Serverless-optimized: 1 attempt (no retry) so we don't burn 90s
# on a blocked address. If ScraperAPI fails once, fail fast.
os.environ.setdefault("HTTP_MAX_RETRIES", "1")

# ScraperAPI premium proxies consistently need 25-35s for Zillow pages.
# The default 30s timeout cuts off right at the boundary. Bump to 45s
# so requests that take 30-40s still succeed.
os.environ.setdefault("HTTP_TIMEOUT_SECONDS", "45")

# Public demo mode: the landing page at GET / calls POST /lookup via
# same-origin fetch() without an API key header, so we force-clear any
# ZESTIMATE_API_KEY that may be set in the Vercel dashboard. This is an
# intentional "anyone can try it" trade-off for the hosted demo; real
# production (Docker/Fly) keeps auth enabled via the same env var.
os.environ.pop("ZESTIMATE_API_KEY", None)

from zestimate_agent.api import create_app

app = create_app()
