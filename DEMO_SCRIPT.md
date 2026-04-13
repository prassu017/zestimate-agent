# Zestimate Agent — Demo Script (4-5 minutes)

> Condensed walkthrough for presenting the take-home to the Nexhelm AI team.

---

## Setup (before the call)

- Have the Vercel live demo open: `https://zestimate-agent.vercel.app`
- Have the technical page open: `https://zestimate-agent.vercel.app/technical`
- Have the GitHub repo open: `https://github.com/prassu017/zestimate-agent`
- Pre-warm one address so you have a cached result ready (do a lookup 5 min before)

---

## Minute 0:00 — Opening (30s)

> "I built a production-grade Python agent that fetches Zillow Zestimates given any US address. It handles the full lifecycle — address normalization, Zillow resolution, anti-bot fetching, parsing, cross-validation, and caching — with a zero-exception guarantee. Let me show you it working live."

---

## Minute 0:30 — Live Demo (90s)

### Cached lookup (instant)

1. Go to the Vercel landing page
2. Type the **pre-warmed address** (e.g. "500 5th Ave W #705, Seattle, WA 98119")
3. Hit submit — result appears in **< 1 second**
4. Point out:
   - Zestimate value, property details (bed/bath/sqft/type)
   - Confidence score + breakdown ("high confidence: all pipeline stages agreed, cross-check: rentcast agrees")
   - Cached: true
   - Elapsed: ~50ms

### Cold lookup (real-time)

5. Type a **new address** (e.g. "350 5th Ave, New York, NY 10118" — Empire State Building)
6. "This one isn't cached, so you'll see the full pipeline run — ScraperAPI's premium proxies need about 25-30 seconds to render Zillow's page"
7. While waiting, explain: "In production, you'd pre-warm the cache for your address list. The pre-warmer tool already exists — it takes a CSV or sitemap and warms the cache with bounded concurrency."
8. When result arrives, point out the response fields

---

## Minute 2:00 — Architecture (90s)

1. Click **"Technical Architecture"** link on the landing page (or navigate to `/technical`)
2. Walk through the **Pipeline Overview** — 7 stages shown as boxes
3. Scroll to **System Architecture Diagram** — show the full flow
4. Key points to call out:
   - **"Never raises" contract** — `aget()` always returns a typed result. Every failure is a status enum, not an exception. Callers never need try/except.
   - **Pluggable fetchers** — Protocol-based. Swap ScraperAPI for ZenRows or Playwright without touching the orchestrator. FetcherChain does automatic failover.
   - **4-tier parser fallback** — Zillow changes their schema often. Parser tries structured JSON, deep walk, HTML regex, raw JSON regex. Handles schema drift gracefully.
   - **Circuit breaker** — After 5 consecutive fetch failures, fail-fast for 30s instead of burning retries.
5. Click **"Limitations"** tab — "I want to be transparent about what this can and can't do with the current tools"
   - ScraperAPI latency (25-35s) is the real bottleneck, not the hosting
   - Rentcast free tier caps cross-checks at 40/month
   - US addresses only

---

## Minute 3:30 — Engineering Quality (45s)

Switch to GitHub repo briefly:

- **269 tests**, 100% eval accuracy across 33 synthetic + fixture cases
- **mypy strict**, ruff clean
- **Eval harness** with three modes: synthetic (zero credits), fixture (replay), live (budget-gated)
- **Observability**: Prometheus metrics at `/metrics`, OpenTelemetry spans, structured JSON logging with trace IDs
- **CI pipeline**: GitHub Actions runs lint + test + coverage + eval + Docker build on every push

---

## Minute 4:15 — What I'd Build Next (30s)

> "If I had another week, here's what I'd prioritize:"

1. **Async job queue** — return immediately, webhook the result when ready. Eliminates the 30s wait entirely.
2. **Second fetcher provider** — ZenRows or BrightData as failover. The FetcherChain is already built, just needs a second implementation.
3. **Historical tracking** — store daily Zestimate snapshots, expose a trend endpoint (7d/30d/90d delta).

> "The architecture is designed for all of these — they're config changes and new modules, not rewrites."

---

## Minute 4:45 — Close (15s)

> "Happy to dive deeper into any part of the system — the parser fallback chain, the confidence scoring algorithm, the eval harness design, or anything else. What questions do you have?"

---

## Backup talking points (if asked)

- **Why ScraperAPI over Playwright?** ScraperAPI handles proxy rotation and browser fingerprinting. Playwright needs your own proxy infra. ScraperAPI is also 10x faster when the non-render path works.
- **Why not Zillow's official API?** Deprecated in 2021. No public alternative exists.
- **How do you handle rate limiting?** Token bucket per IP (configurable), API key auth, Rentcast hard cap with optimistic counter.
- **How confident are you in the parser?** 33 synthetic eval cases covering every property type, edge case, and failure mode. 100% pass rate. The 4-tier fallback means even if Zillow changes their schema, we degrade gracefully instead of breaking.
- **Cost per lookup?** ~$0.01-0.05 (10-25 ScraperAPI credits). Cached lookups are free. At scale with pre-warming, effective cost approaches zero for repeat addresses.
