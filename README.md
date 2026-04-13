# Zestimate Agent

> Production-grade Python agent that, given a US property address, fetches the **current Zillow Zestimate** and returns it as a typed, validated result — with sub-dollar accuracy on the happy path.

Built as a Founding Engineer take-home for **Nexhelm AI**.

```
$ zestimate lookup "1600 Amphitheatre Pkwy, Mountain View, CA 94043"

╭────────────── Zestimate result ──────────────╮
│ Zestimate   $2,634,900                       │
│ Address     1600 Amphitheatre Pkwy, …        │
│ zpid        22001234                         │
│ Confidence  0.96                             │
│ Fetcher     unblocker                        │
│ Cross-check rentcast: $2,611,000 (-0.9%)     │
│ Trace       f1d2…                            │
╰──────────────────────────────────────────────╯
```

---

## Table of contents

1. [What it is](#what-it-is)
2. [Results](#results)
3. [Quick start](#quick-start)
4. [Architecture](#architecture)
5. [Design decisions & trade-offs](#design-decisions--trade-offs)
6. [Observability](#observability)
7. [Running the eval harness](#running-the-eval-harness)
8. [Docker](#docker)
9. [Cost model](#cost-model)
10. [What I'd do next](#what-id-do-next)
11. [Project layout](#project-layout)

---

## What it is

A single Python package that ships **four interfaces** to the same core pipeline:

| Interface | Entry point | Use case |
|---|---|---|
| **CLI** | `zestimate lookup "<addr>"` | Ad-hoc lookups, ops debugging |
| **Batch CLI** | `zestimate batch addresses.csv` | Bulk lookups from CSV |
| **Python** | `ZestimateAgent.from_env().aget(addr)` | Embedding in another service |
| **HTTP** | `POST /lookup` (FastAPI) | Production microservice, containerized |

The pipeline is **pluggable at every layer** via Protocol types, so you can swap fetchers (ZenRows / ScraperAPI / BrightData / Playwright), cross-check providers (Rentcast / ATTOM), and cache backends (SQLite / memory / null) without touching the orchestrator.

**Correctness contract:** `agent.aget()` **never raises**. Every failure mode — blocked fetch, ambiguous address, Zillow has no Zestimate, parse error — is mapped to a `ZestimateStatus` and returned in a `ZestimateResult`. Callers inspect `result.status`; they never have to wrap in try/except.

---

## Results

| Metric | Value |
|---|---|
| **Eval accuracy (synthetic + fixture, 33 cases)** | **100%** (33/33 exact match) |
| **Live accuracy (Seattle condo, real Zillow)** | **exact** ($636,500, zpid 82362438) |
| **Unit + integration tests passing** | **209 / 209** (+ 4 live integration skipped) |
| **Mypy strict** | **clean** (29 source files) |
| **Ruff (`E F I N UP B SIM RUF`)** | **clean** |
| **Lines of production code** | ~3,800 |
| **Cold lookup latency (ScraperAPI)** | **~2-5 s** (render=false fast path) |
| **Cache-hit latency (real server)** | **~16 ms** |
| **CI** | GitHub Actions: lint + test (py3.11/3.12) + coverage + eval + Docker |

The eval harness has **three modes**:

- **synthetic** — inline HTML exercises the parser over hand-authored edge cases. Zero credits. 32 cases: SFH (starter/mid/mcmansion) / condo (studio/penthouse/co-op) / townhouse / multi-family / manufactured / luxury ($5M/$45M/$250M) / rural / new construction / rental / recently sold / off-market / sanity boundary (floor pass/fail, ceiling fail) / parser fallback tiers (HTML regex, JSON regex) / no-Zestimate (null + zero) / blocked page variants (captcha/access-denied/empty).
- **fixture** — replays a pre-recorded Zillow page against the real parser. Zero credits. 1 case (Seattle condo).
- **live** — hits real Zillow + Rentcast. Budget-gated: refuses `--mode live` without `--limit`, refuses `--limit > 3` without `--force`. 6 cases covering live canary, commercial/institutional properties, messy input, and not-found.

---

## Quick start

### Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[api,dev]"
```

### Configure

```bash
cp .env.example .env
# Set at minimum: UNBLOCKER_API_KEY (your ScraperAPI / ZenRows key)
# Optional: CROSSCHECK_PROVIDER=rentcast + CROSSCHECK_API_KEY=... for validation
```

### Run

```bash
# CLI — single lookup
zestimate lookup "123 Main St, Seattle, WA 98101"
zestimate lookup --json "123 Main St, Seattle, WA 98101"

# CLI — batch from CSV
zestimate batch addresses.csv --out results.csv
zestimate batch addresses.csv --json --concurrency 5

# HTTP server
zestimate serve --port 8000
curl -X POST localhost:8000/lookup \
     -H 'Content-Type: application/json' \
     -d '{"address":"123 Main St, Seattle, WA 98101"}'

# Python
python -c "
from zestimate_agent import ZestimateAgent
import asyncio
agent = ZestimateAgent.from_env()
result = asyncio.run(agent.aget('123 Main St, Seattle, WA 98101'))
print(result.to_display())
asyncio.run(agent.aclose())
"

# Eval harness
zestimate eval --mode synthetic    # zero credits, runs every CI build
zestimate eval --mode fixture      # zero credits, parser regression guard
zestimate eval --mode all --json   # machine-readable for dashboards
```

---

## Architecture

```
                          ┌─────────────────────────────────────┐
  raw address string      │                                     │
          │               │   ZestimateAgent (orchestrator)     │
          ▼               │                                     │
   ┌────────────┐         │   never raises — always returns     │
   │  Normalize │◄──┐     │   ZestimateResult w/ status         │
   │ (usaddress)│   │     └──────────────┬──────────────────────┘
   └──────┬─────┘   │                    │
          │         │                    │
          ▼         │         ┌──────────▼──────────┐
   ┌────────────┐   └─────────┤   Result cache      │ daily-partitioned
   │   Cache    │             │ v1:{canonical}:{date}│ SQLite via diskcache
   │ (diskcache)│             └──────────┬──────────┘  (hit: ~1ms, skips resolve+fetch+parse)
   └──────┬─────┘                        │
          │ miss                         │
          ▼                              │
   ┌────────────┐                        │
   │  Resolve   │   Zillow autocomplete/search → zpid + canonical URL
   │ (httpx)    │   Handles 0-match (NOT_FOUND) and ≥2-match (AMBIGUOUS)
   └──────┬─────┘
          │
          ▼
   ┌────────────┐   Primary: WAF-bypass unblocker (ScraperAPI / ZenRows / BrightData)
   │   Fetch    │   Fallback: headless Playwright (optional)
   │ (Protocol) │   Circuit breaker: fail-fast after N consecutive failures
   └──────┬─────┘   Retry w/ tenacity, exponential backoff
          │ HTML
          ▼
   ┌────────────┐   Three-tier parser (in order):
   │   Parse    │     1. __NEXT_DATA__ JSON (stable hydration blob)
   │(selectolax)│     2. Regex over inline JSON (fallback)
   │            │     3. HTML span heuristic (last resort)
   └──────┬─────┘   Detects "no Zestimate" state; raises ParseError on blocks
          │
          ▼
   ┌────────────┐   Sanity: value in [$1k, $100M], zpid present
   │  Validate  │   Cross-check (optional): Rentcast AVM, advisory only
   │            │   Disagreement halves confidence, never blocks the answer
   └──────┬─────┘
          │
          ▼
   ZestimateResult { status, value, zpid, confidence, property_details, crosscheck, trace_id, … }
```

Each layer is a `Protocol` the orchestrator depends on — tests inject fakes, real code uses the default implementations.

---

## Design decisions & trade-offs

### Parse the JSON hydration blob, not the DOM

Zillow is a Next.js app. Every property page ships a `<script id="__NEXT_DATA__">` with the full page state as JSON. Parsing that is:

- **Stable** across Zillow's frequent A/B tests (the DOM churns weekly; the hydration shape is schema-stable for months).
- **Exact** — we get the integer Zestimate, not a rendered `"$1.23M"` string that rounds away $1,234.
- **Fast** — one `json.loads()` on ~500KB vs traversing selectolax selectors.

Two fallback tiers (regex over inline JSON; then HTML span heuristic) exist for the ~1% of pages where the hydration blob is missing or truncated.

### Rich property details, zero extra API calls

The same `__NEXT_DATA__` blob that carries the Zestimate also contains **bedrooms, bathrooms, sqft, lot size, year built, property type, rent Zestimate, Zestimate confidence range, tax assessment, HOA, listing status, last sale price/date, and lat/lon**. We parse all of these into a `PropertyDetails` model and surface them in both the API response and the interactive landing page UI. This costs zero extra credits and transforms the demo from "scraper that returns a number" into "property intelligence platform."

### Render-free fast path (10x faster, 60% cheaper)

Zillow's Next.js app server-side-renders the `__NEXT_DATA__` blob into the initial HTML response. ScraperAPI's `render=true` mode spins up a headless browser, runs JavaScript, and waits for the page to settle -- unnecessary for SSR'd content. We default to `premium=true` only (~10 credits, ~2-3s) and auto-upgrade to `render=true` (~25 credits, ~15-25s) only if the fetched HTML lacks the hydration blob. Same sticky-upgrade pattern as our existing `ultra_premium` escalation.

### Pluggable fetchers, protocol-typed

The hard problem with Zillow isn't extracting the Zestimate — it's getting past Cloudflare / PerimeterX. The **Fetcher** is a `Protocol` so the orchestrator doesn't know or care whether HTML came from ScraperAPI, ZenRows, BrightData, or Playwright. Swapping providers is a config change; failing over from one to another is a `try/except FetchBlockedError`.

A **Playwright fallback fetcher** (`FETCHER_PRIMARY=playwright`) is also implemented for the pathological 1% the unblocker can't get through. It launches headless Chromium with stealth patches, reuses a persistent browser context across calls (connection pooling equivalent), and integrates the circuit breaker. Requires the optional `playwright` dependency group (`pip install -e ".[playwright]" && playwright install chromium`).

### Result cache, not HTML cache

The cache sits **between Normalize and Resolve** — every hit short-circuits resolve + fetch + parse + cross-check in one shot, saving ~15 seconds and ~25 credits per call.

Cache key: `v1:{canonical_address_lowercase}:{YYYY-MM-DD}`

- `v1:` — schema version prefix. Bump it to mass-invalidate after a breaking model change.
- `{canonical}` — deterministic from the Normalizer, so `"500 5th Ave"` and `"500 5TH AVE"` collide.
- `{YYYY-MM-DD}` — daily partition. Zillow updates Zestimates overnight; the date suffix forces a refresh at midnight UTC regardless of TTL.

**Only `OK`, `NO_ZESTIMATE`, and `NOT_FOUND` are cached** — those are stable terminal states. `BLOCKED` / `ERROR` / `AMBIGUOUS` are never cached because they're retry-worthy.

On a cache hit the stored result's `trace_id` is **refreshed** so every request is still uniquely traceable — only the payload is reused.

### Circuit breaker for upstream fetchers

Each fetcher instance carries its own `CircuitBreaker` (closed/open/half_open). After N consecutive failures (default 5), the breaker opens and immediately rejects new requests with `CircuitOpenError` — no retries, no timeout wait. After a configurable recovery timeout (default 30s), one probe request is allowed through; if it succeeds, the circuit closes. This prevents cascading latency when ScraperAPI is down and saves credits that would otherwise be burned on doomed retries. The current state is exposed via a Prometheus gauge (`zestimate_circuit_breaker_state`) and the `/readyz` endpoint.

### Budget-aware cross-check

Rentcast's free tier is 50 req/month. I cap at **40** to leave headroom for debugging + eval. The counter is:

- Persisted to `.cache/rentcast_usage.json`, keyed by `YYYY-MM`
- Incremented **before** the HTTP call (under-counting would silently blow the budget on flaky network)
- **Survives process restarts** and is shared across CLI / API server / eval runs
- Never raises. At cap, we return a `CrossCheck` with `skipped=True` and the lookup continues

Cross-check is **advisory**, not blocking. If Rentcast disagrees by > tolerance, we *halve confidence* and surface the disagreement — we never overwrite Zillow's number. A missing cross-check must never deny a Zillow answer.

### Never-raise orchestrator

`ZestimateAgent.aget()` catches every typed error (`NormalizationError`, `PropertyNotFoundError`, `AmbiguousAddressError`, `FetchBlockedError`, `FetchError`, `ParseError`, `NoZestimateError`, `ResolverError`) and maps it to a `ZestimateStatus`. Unknown exceptions fall through to `ERROR`. The contract is:

```python
result = await agent.aget(addr)  # never raises
if result.ok:                    # status == OK and value is not None
    ...
```

This matters for the HTTP layer: the FastAPI handler is a **six-line adapter** with no try/except, because the agent guarantees all errors are structured. HTTP status codes are derived from `ZestimateStatus` via a static map (OK → 200, NOT_FOUND → 404, BLOCKED → 502, …).

### Eval-driven development

The eval harness (`src/zestimate_agent/eval/`) is the answer to "how do we know we're at ≥99%?". Every commit runs `zestimate eval --mode all` in CI. Three modes let us:

- Measure **parser correctness** without hitting the network (synthetic)
- Measure **end-to-end correctness** against real Zillow HTML without live fetches (fixture)
- Spot-check **live accuracy** on a budget-gated handful of real lookups (live)

Reports emit as rich tables, JSON (for dashboards), or CSV (for spreadsheets). Per-category breakdown makes it trivial to spot "SFH is 100% but condo dropped to 94%".

### Why SQLite for cache, not Redis

Single-node simplicity. The target deployment is one container; a second process in the same pod for Redis adds operational surface area for zero benefit at this scale. The `ResultCache` is a Protocol, so swapping to Redis when we outgrow a single node is a 60-line class, not a refactor.

### Test infrastructure

- **Runtime-checkable Protocols** + dataclass fakes for every dependency. No mocking framework needed.
- **VCR-style cassettes** — recorded Zillow autocomplete and fetcher responses replayed in CI. Catches schema drift without burning credits. Tests in `test_resolve_cassettes.py` and `test_fetch_cassettes.py`.
- **respx** for httpx-level stubs of Zillow/Rentcast (see `test_resolve.py`, `test_crosscheck.py`).
- **Real HTML fixtures** in `src/zestimate_agent/eval/fixtures/` for parser regression tests.
- **Dependency injection** all the way down — `ZestimateAgent(settings, normalizer=..., resolver=..., fetcher=..., cache=..., crosschecker=...)` lets every test construct exactly the agent shape it needs.

---

## Observability

Structured logging via **structlog** with per-request `trace_id` propagated through `structlog.contextvars`. The `trace_id` is bound once at the start of each lookup and automatically appears in every log line from every pipeline stage (normalizer, resolver, fetcher, parser, validator) -- no explicit passing required:

```
{"event": "lookup done", "trace_id": "f1d2…", "value": 636500,
 "confidence": 0.95, "fetcher": "unblocker", "crosscheck": {...}}
```

The `trace_id` is also returned as an `X-Request-ID` response header for correlation with load balancers and observability tooling.

Set `LOG_FORMAT=json` for machine-parseable output (Docker image default), `LOG_FORMAT=pretty` for dev.

### Prometheus metrics (at `/metrics`)

| Metric | Type | Labels | Purpose |
|---|---|---|---|
| `zestimate_lookups_total` | counter | `status` | Lookups by terminal status |
| `zestimate_lookup_duration_seconds` | histogram | -- | End-to-end latency |
| `zestimate_stage_duration_seconds` | histogram | `stage` | Per-stage latency (normalize, resolve, fetch, parse, validate) |
| `zestimate_cache_events_total` | counter | `event` | Hit / miss / write |
| `zestimate_crosscheck_total` | counter | `outcome` | ok / skipped / error |
| `zestimate_rentcast_usage` | gauge | — | Current-month Rentcast calls used |
| `zestimate_rentcast_cap` | gauge | — | Configured cap (40) |
| `zestimate_circuit_breaker_state` | gauge | `provider` | 0=closed, 1=open, 2=half_open |
| `zestimate_http_requests_total` | counter | `path,method,code` | Raw HTTP counter |
| `zestimate_http_request_duration_seconds` | histogram | `path,method` | HTTP latency |

Histogram buckets are tuned for "fast when cached (< 100ms), slow when live (1s – 30s)". Label cardinality is bounded to declared route templates so the metric doesn't explode on URL variation.

### Health endpoints

- `GET /healthz` — liveness, always 200 if the process is alive
- `GET /readyz` — readiness, reports `agent`, `cache_backend`, `circuit_breaker` state, and Rentcast cap in `checks{}`
- `GET /version` — package version

---

## Running the eval harness

```bash
# Zero-credit sanity check — runs on every CI build
zestimate eval --mode synthetic
# ┌─ Eval summary ─────────────────────┐
# │ Cases              7               │
# │ Correct            7/7  (100.0%)   │
# │ ≥99% target        HIT             │
# │ Exact value match  7/7             │
# │ Latency p50/p95    0ms / 3ms       │
# └────────────────────────────────────┘

# Parser regression test against a real Zillow page
zestimate eval --mode fixture

# Budget-gated live probe (ScraperAPI credits!)
zestimate eval --mode live --limit 1

# Filter by category, emit JSON for a dashboard
zestimate eval --mode all --categories sfh,condo --json

# CI-friendly: exit 0 if ≥99%, 1 otherwise
zestimate eval --mode all && echo "ship it"
```

---

## Docker

Multi-stage build: `python:3.12-slim-bookworm` builder → slim runtime with non-root `app` user, `tini` as PID 1, and a `HEALTHCHECK` hitting `/healthz`.

```bash
docker build -t zestimate-agent .

docker run --rm -p 8000:8000 \
    -e UNBLOCKER_API_KEY=sk_xxx \
    -e CROSSCHECK_PROVIDER=rentcast \
    -e CROSSCHECK_API_KEY=rc_xxx \
    -e ZESTIMATE_API_KEY=my-shared-secret \
    zestimate-agent

# CLI inside the container:
docker run --rm -e UNBLOCKER_API_KEY=sk_xxx zestimate-agent \
    zestimate lookup "123 Main St, Seattle, WA 98101"
```

Final image is ~150 MB, runs as non-root, ships only the runtime venv (no build toolchain).

---

## Cost model

Per lookup at steady-state (with render-free fast path):

| Path | ScraperAPI credits | Rentcast reqs | Wall time |
|---|---|---|---|
| Cache hit | **0** | 0 | **~16 ms** |
| Cache miss, fast path (no render) | **~10** | 0 | **~2-5 s** |
| Cache miss, render fallback | ~25 | 0 | ~15-25 s |
| Cache miss, with cross-check | ~10 | 1 | ~3-6 s |

The render-free fast path works for >99% of Zillow property pages (they server-side-render `__NEXT_DATA__`). The ~25-credit render fallback only fires for rare pages that don't include the hydration blob.

At ScraperAPI hobby pricing (~$0.002/credit) and a 70% cache hit rate, a service doing 10k lookups/day costs roughly **$0.60/day** on scraping (down from $1.50 with the render-free optimization), **plus a flat $0-$15/month for infra** (one small VPS, or a $5/mo Fly.io nano). Rentcast stays free by construction (40-req/mo cap).

The cache is where the unit economics live. At 0% hit rate you're paying credits on every call; at 70% hit rate the same service is **3x cheaper**.

---

## What I'd do next (concrete, not hand-wavy)

| # | Upgrade | Effort | Why |
|---|---|---|---|
| 1 | **Redis cache backend** | ~60 lines | Required for > 1 pod; `ResultCache` Protocol is ready |
| 2 | **Pydantic-based config for the eval dataset** | 1 day | So non-engineers can add cases by editing a YAML file |
| 3 | **OpenTelemetry traces** | 2 days | Span per pipeline stage. We already emit per-stage Prometheus histograms and `trace_id` in contextvars, so this is mostly glue code |
| 4 | **CDN in front of `/lookup`** with signed URLs | 3 days | Free extra cache layer; moves 90% of duplicate traffic off the origin |
| 5 | **A `ZillowSitemap` pre-warmer** | 3 days | Nightly crawl of the top-N ZIPs to pre-populate the cache |
| 6 | **Fetcher chain with automatic failover** | 1 day | Try unblocker first, fall back to Playwright on `FetchBlockedError`. The circuit breaker and Protocol are already in place |

What *is* built is the substrate they'd all sit on — every item above is a one-file change against existing Protocols and abstractions.

---

## Project layout

```
.github/workflows/
└── ci.yml              # lint + test (py3.11/3.12) + coverage + eval + Docker

src/zestimate_agent/
├── __init__.py
├── agent.py            # orchestrator (never-raises, per-stage timers, trace_id propagation)
├── api/                # FastAPI layer
│   ├── app.py          # factory + lifespan + middleware
│   ├── routes.py       # /lookup, /zestimate, /healthz, /readyz, /version, /metrics
│   ├── deps.py         # dependency injection + X-API-Key auth + rate limiter
│   ├── landing.py      # interactive demo UI (single-file, zero-build)
│   ├── schemas.py      # wire types (PropertyDetailsOut, CrossCheckOut, LookupResponse)
│   └── metrics.py      # Prometheus counters/histograms/gauges (incl. per-stage)
├── cache.py            # daily-partitioned, TTL-bounded diskcache backend
├── cli.py              # typer CLI (lookup, batch, eval, serve, cache-*, rentcast-status)
├── config.py           # pydantic-settings, all env vars
├── crosscheck.py       # Rentcast client + persistent monthly counter
├── errors.py           # typed error taxonomy
├── eval/               # correctness measurement
│   ├── dataset.py      # 39 hand-curated cases across 11 categories
│   ├── runner.py       # synthetic/fixture/live modes, bounded concurrency
│   ├── report.py       # stats + JSON + CSV + rich tables
│   └── fixtures/       # recorded Zillow HTML for regression tests
├── fetch/              # pluggable fetchers (connection-pooled, circuit-breaker-protected)
│   ├── circuit_breaker.py  # 3-state breaker (closed/open/half_open) + Prometheus gauge
│   ├── playwright.py   # headless Chromium fallback (stealth patches, lazy browser)
│   └── unblocker.py    # ScraperAPI / ZenRows / BrightData (render-free fast path)
├── logging.py          # structlog config (contextvars-based trace_id)
├── models.py           # pydantic contracts (PropertyDetails, CrossCheck, ZestimateResult)
├── normalize.py        # usaddress-backed address parser
├── parse.py            # three-tier parser + property details extraction
├── resolve.py          # Zillow autocomplete -> zpid resolver (connection-pooled)
└── validate.py         # sanity + cross-check

tests/
├── fixtures/
│   └── cassettes/          # VCR-style recorded API responses (resolver + fetcher)
├── unit/               # 209 tests, 29 source files, mypy strict, ruff clean
│   ├── test_agent_cache.py
│   ├── test_api.py
│   ├── test_cache.py
│   ├── test_circuit_breaker.py  # state machine + Prometheus + fetcher integration
│   ├── test_crosscheck.py
│   ├── test_eval.py
│   ├── test_fetch_cassettes.py  # VCR fetch→parse pipeline tests
│   ├── test_fetch_playwright.py # Playwright fetcher unit tests
│   ├── test_resolve_cassettes.py # VCR resolver replay tests
│   └── ...
└── integration/
    └── test_resolver_live.py  # opt-in, needs RUN_LIVE_TESTS=1
```

---

## License

MIT

---

## Acknowledgements

Built over one intense session with [Claude Code](https://claude.com/claude-code). The architecture, decisions, and code review are my own; Claude was the pair programmer. Every design decision in this README is defensible in a whiteboard conversation — ask me.
