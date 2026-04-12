# Zestimate Agent — Specification v0.1

## 1. Problem statement
Build a production-grade Python agent that, given a US property address string, returns the **current Zestimate displayed on zillow.com** for that property, with ≥99% exact-match accuracy.

## 2. Success criteria

| Dimension | Target |
|---|---|
| Exact-match accuracy vs. live zillow.com | ≥99% on a 100-address eval set |
| P50 / P95 latency (warm) | ≤4s / ≤10s per address |
| Cold-start latency | ≤15s |
| Handles messy address input | Yes (normalization layer) |
| Handles no-Zestimate case | Returns `ZestimateResult(status="no_zestimate")`, never crashes |
| Handles ambiguous address | Returns top candidate + confidence + alternates |
| Reproducibility | Dockerized, single command to run |
| Observability | Structured logs, per-request trace, metrics counters |
| Tests | Unit + contract + live, ≥80% coverage on core modules |

## 3. Non-goals
- Historical Zestimate time series
- Rent Zestimate (unless free with primary Zestimate)
- Batch ingestion at scale
- UI / web front-end
- Auth, multi-tenancy, billing

## 4. Approach: why scraping zillow.com (via unblocker) is the only way

Zillow has no public Zestimate API. Options considered:

| # | Approach | Accuracy | Cost | Risk | Decision |
|---|---|---|---|---|---|
| A | Raw `requests` + rotating proxies | Low | Low | High | ❌ |
| B | Playwright + stealth + residential proxies | High | Med | Med | ✅ Fallback |
| C | Managed unblocker API → zillow.com → parse `__NEXT_DATA__` | High | Med-High | Low | ✅ **Primary** |
| D | Third-party Zillow-data API (Rentcast/ATTOM) | Medium (lags live) | Med | Low | ❌ as primary, ✅ as sanity check |

Zillow property pages embed the Zestimate in the `__NEXT_DATA__` hydration JSON blob. That's the stable extraction target — parsing rendered DOM is fragile.

## 5. Architecture

```
address → Normalizer → Resolver → Fetcher → Parser → Validator → Cache → ZestimateResult
           usaddress   Zillow      Unblocker __NEXT_  sanity +    sqlite
           (+Google)   autocomplete or PW    DATA__   Rentcast
```

## 6. Component contracts
See `src/zestimate_agent/models.py` for the authoritative pydantic schemas.

- `NormalizedAddress`: raw, street, city, state, zip, canonical, lat/lon, parse_confidence
- `ResolvedProperty`: zpid, url, matched_address, match_confidence, alternates
- `FetchResult`: html, status, final_url, fetched_at, fetcher, elapsed_ms
- `ZestimateResult`: status, value, zpid, matched_address, zillow_url, as_of, confidence, alternates, error

## 7. Interfaces
- **Python**: `ZestimateAgent.from_env().get(address)` → `ZestimateResult`
- **CLI**: `zestimate lookup "address"`, `--json`, `--batch file.csv`, `--eval eval_set.csv`
- **HTTP**: `POST /zestimate`, `GET /health`, `GET /metrics`

## 8. Configuration
All env-driven via `pydantic-settings`. See `.env.example`.

## 9. Observability
- `structlog` — JSON in prod, pretty in dev
- Per-request `trace_id`
- Counters: `fetch_success`, `fetch_blocked`, `parse_fail`, `cache_hit`, `crosscheck_mismatch`
- Histograms: latency per pipeline stage

## 10. Testing strategy

| Layer | Tool | What |
|---|---|---|
| Unit | pytest | Normalizer, Parser (fixtures), Validator |
| Contract | pytest + vcrpy | Resolver + Fetcher, recorded cassettes |
| Integration | pytest `@live` | Real zillow.com, gated by `RUN_LIVE_TESTS=1` |
| Eval | custom harness | 100-address benchmark |

**Eval set composition (100 addresses):**
- 40 single-family homes across 20 states
- 15 condos / townhomes
- 10 luxury ($5M+)
- 10 rural / low-value
- 10 new construction
- 10 no-Zestimate (rentals, off-market)
- 5 deliberately messy input strings

## 11. Accuracy strategy
1. Normalize hard before resolving
2. Trust Zillow's own autocomplete for zpid resolution
3. Parse `__NEXT_DATA__`, not DOM
4. Retry on block with fetcher swap
5. Fail loud on ambiguity; count ambiguous-but-correct-top-1 as pass
6. Canary job on 5 stable addresses daily

## 12. Risk register

| Risk | Lik. | Impact | Mitigation |
|---|---|---|---|
| `__NEXT_DATA__` schema change | Med | High | Multi-path parser, regex fallback, canary |
| Unblocker blocked on Zillow | Med | High | Multi-provider, Playwright fallback |
| ToS / legal | — | — | Documented; non-redistributive |
| Address ambiguity | Med | Med | Autocomplete + component match + confidence |
| Cost spike from paid APIs | Low | Low | Cache + rate caps |

## 13. Locked decisions (v0.1)
- **Unblocker**: ZenRows primary (ScraperAPI + Bright Data adapters supported)
- **Cross-check**: Rentcast free tier
- **FastAPI**: shipped
- **Eval set size**: 100
- **Python**: 3.11+
