"""Technical architecture page for the Zestimate Agent.

Inlined HTML — same pattern as landing.py. Serves at GET /technical
with detailed architecture diagrams, PRD, schema documentation,
and current limitations.
"""

from __future__ import annotations

TECHNICAL_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Zestimate Agent — Technical Architecture</title>
<meta name="description" content="Architecture, PRD, schema, and limitations of the Zestimate Agent pipeline.">
<style>
  :root {
    --bg: #0b0d12; --panel: #11141b; --panel-hi: #161a22;
    --border: #222836; --fg: #e6e9ef; --muted: #8a93a5;
    --accent: #7cc4ff; --accent-hi: #a7d8ff;
    --good: #4ade80; --warn: #fbbf24; --bad: #f87171;
    --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    --sans: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", Roboto, sans-serif;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: var(--bg); color: var(--fg); font-family: var(--sans); font-size: 15px; line-height: 1.6; }
  a { color: var(--accent); text-decoration: none; }
  a:hover { color: var(--accent-hi); text-decoration: underline; }
  code, pre, .mono { font-family: var(--mono); font-size: 13px; }
  main { max-width: 940px; margin: 0 auto; padding: 48px 24px 96px; }

  /* Nav bar */
  .topnav { display: flex; align-items: center; gap: 16px; margin-bottom: 36px; flex-wrap: wrap; }
  .topnav a.back { font-size: 13px; padding: 4px 12px; border: 1px solid var(--border); border-radius: 6px; color: var(--muted); }
  .topnav a.back:hover { border-color: var(--accent); color: var(--accent); text-decoration: none; }
  .topnav .title { font-size: 28px; font-weight: 700; letter-spacing: -0.02em; }
  .topnav .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; background: #1a2332; color: var(--accent); font-family: var(--mono); font-size: 11px; }

  /* Tabs */
  .tabs { display: flex; gap: 0; border-bottom: 1px solid var(--border); margin-bottom: 28px; }
  .tabs button { background: none; border: none; color: var(--muted); padding: 10px 18px; font-size: 14px; font-family: var(--sans); cursor: pointer; border-bottom: 2px solid transparent; transition: all .15s; }
  .tabs button:hover { color: var(--fg); }
  .tabs button.active { color: var(--accent); border-bottom-color: var(--accent); }
  .tab-content { display: none; }
  .tab-content.active { display: block; }

  /* Cards */
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 24px; margin-bottom: 20px; }
  .card h2 { margin: 0 0 14px; font-size: 18px; font-weight: 600; }
  .card h3 { margin: 18px 0 10px; font-size: 15px; font-weight: 600; color: var(--accent); }
  .card p { margin: 0 0 12px; color: var(--fg); }
  .card ul { margin: 0 0 12px; padding-left: 20px; }
  .card li { margin-bottom: 6px; }
  .card .muted { color: var(--muted); font-size: 13px; }

  /* Diagram */
  .diagram { background: #060810; border: 1px solid var(--border); border-radius: 8px; padding: 20px; overflow-x: auto; margin: 16px 0; }
  .diagram pre { margin: 0; color: #c3c9d5; font-size: 12.5px; line-height: 1.5; white-space: pre; }

  /* Schema table */
  table { width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 13px; }
  th { text-align: left; padding: 8px 10px; border-bottom: 2px solid var(--border); color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .08em; font-weight: 500; }
  td { padding: 8px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
  td code { background: var(--panel-hi); padding: 1px 5px; border-radius: 3px; font-size: 12px; }
  tr:hover td { background: rgba(124,196,255,0.03); }

  /* Status badges */
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-family: var(--mono); font-size: 11px; font-weight: 500; }
  .badge-ok { background: #0d2818; color: var(--good); }
  .badge-warn { background: #2a2008; color: var(--warn); }
  .badge-err { background: #2a0d0d; color: var(--bad); }
  .badge-info { background: #1a2332; color: var(--accent); }

  /* Stage boxes */
  .pipeline { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin: 16px 0; }
  .stage { background: var(--panel-hi); border: 1px solid var(--border); border-radius: 8px; padding: 14px; text-align: center; position: relative; }
  .stage .snum { font-family: var(--mono); font-size: 11px; color: var(--accent); margin-bottom: 4px; }
  .stage .sname { font-weight: 600; font-size: 13px; margin-bottom: 4px; }
  .stage .sdesc { font-size: 11px; color: var(--muted); line-height: 1.4; }
  .stage .arrow { position: absolute; right: -10px; top: 50%; transform: translateY(-50%); color: var(--border); font-size: 16px; }

  /* Limitation cards */
  .lim-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .lim { background: var(--panel-hi); border: 1px solid var(--border); border-radius: 8px; padding: 14px; }
  .lim .lim-title { font-weight: 600; font-size: 13px; margin-bottom: 4px; }
  .lim .lim-body { font-size: 12px; color: var(--muted); line-height: 1.45; }

  footer { margin-top: 36px; text-align: center; color: var(--muted); font-size: 12px; }
  @media (max-width: 700px) {
    .topnav .title { font-size: 22px; }
    .lim-grid { grid-template-columns: 1fr; }
    .pipeline { grid-template-columns: 1fr 1fr; }
    .tabs button { padding: 8px 12px; font-size: 13px; }
  }
</style>
</head>
<body>
<main>

<div class="topnav">
  <a href="/" class="back">&larr; Demo</a>
  <span class="title">Technical Architecture</span>
  <span class="pill">v1.0</span>
</div>

<div class="tabs">
  <button class="active" onclick="showTab('arch')">Architecture</button>
  <button onclick="showTab('prd')">PRD</button>
  <button onclick="showTab('schema')">Schema</button>
  <button onclick="showTab('limits')">Limitations</button>
</div>

<!-- ═══════════════════════════════════════════════════════════ -->
<!-- ARCHITECTURE TAB                                            -->
<!-- ═══════════════════════════════════════════════════════════ -->
<div id="tab-arch" class="tab-content active">

<div class="card">
  <h2>Pipeline Overview</h2>
  <p>The Zestimate Agent is a <strong>7-stage async pipeline</strong> that transforms a raw US property address into a validated Zestimate with confidence scoring. Each stage is independently testable, traceable via OpenTelemetry spans, and observable via Prometheus histograms.</p>

  <div class="pipeline">
    <div class="stage"><div class="snum">1</div><div class="sname">Normalize</div><div class="sdesc">Parse &amp; canonicalize address via usaddress + optional geocoder</div><div class="arrow">&rarr;</div></div>
    <div class="stage"><div class="snum">2</div><div class="sname">Cache</div><div class="sdesc">Check SQLite / Redis for cached result (6h TTL, daily key)</div><div class="arrow">&rarr;</div></div>
    <div class="stage"><div class="snum">3</div><div class="sname">Resolve</div><div class="sdesc">Hit Zillow autocomplete to get zpid + property URL</div><div class="arrow">&rarr;</div></div>
    <div class="stage"><div class="snum">4</div><div class="sname">Fetch</div><div class="sdesc">ScraperAPI premium proxy renders Zillow JS page</div><div class="arrow">&rarr;</div></div>
    <div class="stage"><div class="snum">5</div><div class="sname">Parse</div><div class="sdesc">Extract Zestimate + property details from __NEXT_DATA__</div><div class="arrow">&rarr;</div></div>
    <div class="stage"><div class="snum">6</div><div class="sname">Validate</div><div class="sdesc">Sanity bounds + Rentcast cross-check (advisory)</div><div class="arrow">&rarr;</div></div>
    <div class="stage"><div class="snum">7</div><div class="sname">Return</div><div class="sdesc">Cache result &amp; return typed ZestimateResult</div></div>
  </div>
</div>

<div class="card">
  <h2>System Architecture Diagram</h2>
  <div class="diagram"><pre>
  Client (CLI / HTTP / Python)
     |
     v
+------------------------------------------------------------------+
|                     ZestimateAgent  (orchestrator)                |
|  Contract: aget() NEVER raises. All failures -> ZestimateStatus  |
|  Observability: per-stage histograms, OTel spans, trace_id       |
+------------------------------------------------------------------+
     |           |           |          |           |          |
     v           v           v          v           v          v
+---------+ +--------+ +--------+ +--------+ +---------+ +-------+
|Normalize| | Cache  | |Resolve | | Fetch  | |  Parse  | |Validate|
|         | |        | |        | |        | |         | |        |
|usaddress| |SQLite/ | |Zillow  | |Scraper | |__NEXT_  | |Sanity  |
|+ Google | |Redis/  | |Auto-   | |API     | |DATA__   | |bounds  |
|Geocoder | |Memory  | |complete| |Premium | |+ 3 fall | |+ Rent- |
|         | |        | |API     | |Proxies | |back tiers|cast AVM|
+---------+ +--------+ +--------+ +--------+ +---------+ +-------+
     |           |           |          |           |          |
     v           v           v          v           v          v
+------------------------------------------------------------------+
|                      ZestimateResult                             |
|  {status, value, zpid, confidence, property_details, crosscheck, |
|   cached, trace_id, error, confidence_breakdown}                 |
+------------------------------------------------------------------+
     |
     v
+------------------------------------------------------------------+
|                    Delivery Layer                                 |
|  CLI (Rich tables)  |  HTTP API (FastAPI)  |  Python SDK          |
|  Batch CSV import   |  /lookup, /batch     |  agent.aget(addr)    |
|                     |  /zestimate alias    |                      |
+------------------------------------------------------------------+
  </pre></div>
</div>

<div class="card">
  <h2>Data Flow</h2>
  <div class="diagram"><pre>
str (raw address)
  |
  v
NormalizedAddress { street, city, state, zip, canonical, confidence }
  |
  v
ResolvedProperty { zpid, url, matched_address, match_confidence, alternates[] }
  |
  v
FetchResult { html, status_code, final_url, fetcher, elapsed_ms }
  |
  v
ZestimateResult { status, value, zpid, confidence, property_details }
  |   ^
  |   | confidence halved if cross-check disagrees
  v   |
ZestimateResult (validated) { + crosscheck, confidence_breakdown }
  </pre></div>
  <p class="muted">All models are frozen (immutable). Confidence multiplies across stages: final = parse_conf x resolve_conf x normalize_conf. Cross-check can only <em>halve</em> confidence, never increase it.</p>
</div>

<div class="card">
  <h2>Key Design Decisions</h2>
  <h3>Never-Raise Contract</h3>
  <p><code>agent.aget()</code> catches every exception and maps it to a <code>ZestimateStatus</code>. Callers inspect <code>result.status</code> — they never need try/except. This makes the API idempotent and testable.</p>

  <h3>Pluggable Fetchers via Protocol</h3>
  <p>The <code>Fetcher</code> protocol defines <code>name</code>, <code>fetch(url)</code>, and <code>aclose()</code>. Swap ScraperAPI for ZenRows, BrightData, or Playwright without touching the orchestrator. A <code>FetcherChain</code> enables automatic failover.</p>

  <h3>Cache-First, Result-Level</h3>
  <p>Caching happens at the <code>ZestimateResult</code> level (not raw HTML). This skips both fetcher credits and Rentcast API calls on cache hits. Key format: <code>v1:{canonical}:{YYYY-MM-DD}</code> forces daily refresh since Zestimates update overnight.</p>

  <h3>Cross-Check is Advisory</h3>
  <p>Rentcast cross-check never overwrites Zillow's value. It only adjusts confidence. Skipped when monthly cap (40/mo) is hit. Non-blocking: failure never breaks the main pipeline.</p>

  <h3>4-Tier Parser Fallback</h3>
  <p>Zillow changes their schema frequently. The parser tries four extraction strategies in order:</p>
  <ol>
    <li><strong>__NEXT_DATA__ structured</strong> — primary, highest confidence (1.0)</li>
    <li><strong>Deep JSON walk</strong> — traverses nested objects looking for zestimate+zpid</li>
    <li><strong>HTML regex</strong> — matches rendered "$X Zestimate" text (confidence 0.7)</li>
    <li><strong>Raw JSON regex</strong> — searches for "zestimate": digits anywhere (confidence 0.6)</li>
  </ol>

  <h3>Circuit Breaker</h3>
  <p>Three-state breaker (CLOSED &rarr; OPEN &rarr; HALF_OPEN) on the fetcher. After 5 consecutive failures, the circuit opens for 30s — fail-fast instead of burning 30s per retry on a downed upstream.</p>
</div>

<div class="card">
  <h2>Observability Stack</h2>
  <table>
    <thead><tr><th>Layer</th><th>Tool</th><th>What it tracks</th></tr></thead>
    <tbody>
      <tr><td>Metrics</td><td>Prometheus</td><td>Lookup count by status, per-stage latency histograms, Rentcast usage gauge, circuit breaker state</td></tr>
      <tr><td>Tracing</td><td>OpenTelemetry</td><td>Per-stage spans with attributes (address, zpid, confidence, fetcher, status). No-op fallback if SDK absent.</td></tr>
      <tr><td>Logging</td><td>structlog (JSON)</td><td>Structured logs with trace_id binding via contextvars. Every stage logs entry + exit.</td></tr>
      <tr><td>Health</td><td>FastAPI</td><td><code>/healthz</code> (liveness), <code>/readyz</code> (agent + cache + Rentcast cap + circuit breaker state)</td></tr>
    </tbody>
  </table>
</div>

</div>

<!-- ═══════════════════════════════════════════════════════════ -->
<!-- PRD TAB                                                     -->
<!-- ═══════════════════════════════════════════════════════════ -->
<div id="tab-prd" class="tab-content">

<div class="card">
  <h2>Product Requirements Document</h2>
  <p class="muted">Zestimate Agent v1.0 — Founding Engineer Take-Home for Nexhelm AI</p>

  <h3>Problem Statement</h3>
  <p>Real estate professionals and data teams need programmatic access to Zillow Zestimates. Zillow's official API was deprecated. The remaining path is web scraping, which is fragile (anti-bot, schema drift) and expensive (proxy credits). There is no production-grade open-source solution that handles the full lifecycle: address normalization, Zillow resolution, anti-bot fetching, robust parsing, independent cross-validation, and result caching.</p>

  <h3>Goals</h3>
  <ul>
    <li><strong>Accuracy:</strong> Return the exact Zestimate value Zillow displays, with sub-dollar precision</li>
    <li><strong>Reliability:</strong> Never raise exceptions to callers. Every failure mode is a typed status.</li>
    <li><strong>Cost efficiency:</strong> Cache results to minimize proxy credits ($0.01-0.05/lookup). Budget-gate Rentcast (40/month hard cap).</li>
    <li><strong>Observability:</strong> Full pipeline tracing, per-stage latency metrics, structured logging</li>
    <li><strong>Extensibility:</strong> Protocol-based architecture. Swap any pipeline stage without touching others.</li>
  </ul>

  <h3>User Personas</h3>
  <table>
    <thead><tr><th>Persona</th><th>Interface</th><th>Use Case</th></tr></thead>
    <tbody>
      <tr><td>Ops engineer</td><td>CLI</td><td>Ad-hoc lookups, debugging, eval runs</td></tr>
      <tr><td>Data team</td><td>Batch CLI / CSV</td><td>Bulk address lookups (up to 50/batch)</td></tr>
      <tr><td>Backend service</td><td>Python SDK</td><td>Embedding in another application</td></tr>
      <tr><td>Frontend / partner</td><td>HTTP API</td><td>Production microservice with auth + rate limiting</td></tr>
    </tbody>
  </table>

  <h3>Functional Requirements</h3>
  <table>
    <thead><tr><th>ID</th><th>Requirement</th><th>Status</th></tr></thead>
    <tbody>
      <tr><td>FR-01</td><td>Accept raw US address, return Zestimate value</td><td><span class="badge badge-ok">Done</span></td></tr>
      <tr><td>FR-02</td><td>Normalize address (parse, canonicalize, geocode fallback)</td><td><span class="badge badge-ok">Done</span></td></tr>
      <tr><td>FR-03</td><td>Resolve address to Zillow property (zpid)</td><td><span class="badge badge-ok">Done</span></td></tr>
      <tr><td>FR-04</td><td>Fetch Zillow page through anti-bot proxy</td><td><span class="badge badge-ok">Done</span></td></tr>
      <tr><td>FR-05</td><td>Parse Zestimate + property details with fallback chain</td><td><span class="badge badge-ok">Done</span></td></tr>
      <tr><td>FR-06</td><td>Cross-validate with independent provider (Rentcast AVM)</td><td><span class="badge badge-ok">Done</span></td></tr>
      <tr><td>FR-07</td><td>Cache results (SQLite / Redis / memory)</td><td><span class="badge badge-ok">Done</span></td></tr>
      <tr><td>FR-08</td><td>Confidence scoring across pipeline stages</td><td><span class="badge badge-ok">Done</span></td></tr>
      <tr><td>FR-09</td><td>CLI with single + batch + eval modes</td><td><span class="badge badge-ok">Done</span></td></tr>
      <tr><td>FR-10</td><td>HTTP API with auth, rate limiting, CORS</td><td><span class="badge badge-ok">Done</span></td></tr>
      <tr><td>FR-11</td><td>Batch HTTP endpoint (up to 50 addresses)</td><td><span class="badge badge-ok">Done</span></td></tr>
      <tr><td>FR-12</td><td>Rich property details (bed/bath/sqft/type/year/tax/HOA)</td><td><span class="badge badge-ok">Done</span></td></tr>
      <tr><td>FR-13</td><td>Circuit breaker on fetcher (fail-fast on downed upstream)</td><td><span class="badge badge-ok">Done</span></td></tr>
      <tr><td>FR-14</td><td>Signed URL support for CDN-cached endpoints</td><td><span class="badge badge-ok">Done</span></td></tr>
      <tr><td>FR-15</td><td>Cache pre-warmer (sitemap or address list)</td><td><span class="badge badge-ok">Done</span></td></tr>
    </tbody>
  </table>

  <h3>Non-Functional Requirements</h3>
  <table>
    <thead><tr><th>ID</th><th>Requirement</th><th>Target</th></tr></thead>
    <tbody>
      <tr><td>NFR-01</td><td>Cold lookup latency</td><td>&lt; 35s (ScraperAPI premium proxy)</td></tr>
      <tr><td>NFR-02</td><td>Cache-hit latency</td><td>&lt; 100ms</td></tr>
      <tr><td>NFR-03</td><td>Test coverage</td><td>269 tests, 100% eval accuracy</td></tr>
      <tr><td>NFR-04</td><td>Type safety</td><td>mypy strict, zero errors</td></tr>
      <tr><td>NFR-05</td><td>Zero-exception guarantee</td><td>aget() never raises</td></tr>
      <tr><td>NFR-06</td><td>Rentcast budget</td><td>Hard cap: 40 calls/month</td></tr>
    </tbody>
  </table>

  <h3>Workflow Diagram</h3>
  <div class="diagram"><pre>
User Request
     |
     v
[1. Normalize Address]
     | canonicalized: "Street, City, ST ZIP"
     v
[2. Cache Lookup] -----> HIT? --> Return cached ZestimateResult
     | MISS                         (skips all downstream stages)
     v
[3. Resolve on Zillow]
     | zpid + property URL
     v
[4. Fetch via ScraperAPI]
     | raw HTML (~1MB)
     v
[5. Parse __NEXT_DATA__]
     | value, property_details
     | fallback: deep walk -> HTML regex -> JSON regex
     v
[6. Validate]
     |--- Sanity: $10K < value < $500M
     |--- Cross-check: Rentcast AVM (if budget allows)
     |        |
     |        +--> within 10%? confidence unchanged
     |        +--> outside 10%? confidence halved
     v
[7. Cache Write + Return]
     | ZestimateResult with full metadata
     v
Response (CLI table / JSON / Python object)
  </pre></div>
</div>

</div>

<!-- ═══════════════════════════════════════════════════════════ -->
<!-- SCHEMA TAB                                                  -->
<!-- ═══════════════════════════════════════════════════════════ -->
<div id="tab-schema" class="tab-content">

<div class="card">
  <h2>API Endpoints</h2>
  <table>
    <thead><tr><th>Method</th><th>Path</th><th>Auth</th><th>Description</th></tr></thead>
    <tbody>
      <tr><td><span class="badge badge-ok">POST</span></td><td><code>/lookup</code></td><td>API key</td><td>Look up a Zestimate for a single address</td></tr>
      <tr><td><span class="badge badge-ok">GET</span></td><td><code>/lookup?address=</code></td><td>API key</td><td>Browser-friendly GET alias</td></tr>
      <tr><td><span class="badge badge-info">POST</span></td><td><code>/zestimate</code></td><td>API key</td><td>Spec-compliant alias for /lookup</td></tr>
      <tr><td><span class="badge badge-info">POST</span></td><td><code>/batch</code></td><td>API key</td><td>Batch lookup (up to 50 addresses)</td></tr>
      <tr><td><span class="badge badge-warn">GET</span></td><td><code>/healthz</code></td><td>None</td><td>Liveness probe (always 200)</td></tr>
      <tr><td><span class="badge badge-warn">GET</span></td><td><code>/readyz</code></td><td>None</td><td>Readiness probe (checks agent, cache, Rentcast)</td></tr>
      <tr><td><span class="badge badge-warn">GET</span></td><td><code>/version</code></td><td>None</td><td>Package name + version</td></tr>
      <tr><td><span class="badge badge-warn">GET</span></td><td><code>/metrics</code></td><td>None</td><td>Prometheus exposition format</td></tr>
    </tbody>
  </table>
</div>

<div class="card">
  <h2>LookupRequest Schema</h2>
  <table>
    <thead><tr><th>Field</th><th>Type</th><th>Default</th><th>Description</th></tr></thead>
    <tbody>
      <tr><td><code>address</code></td><td>string</td><td><em>required</em></td><td>US property address (3-500 chars)</td></tr>
      <tr><td><code>skip_crosscheck</code></td><td>bool</td><td><code>false</code></td><td>Skip Rentcast cross-check</td></tr>
      <tr><td><code>force_crosscheck</code></td><td>bool</td><td><code>false</code></td><td>Run cross-check even if monthly cap hit</td></tr>
      <tr><td><code>use_cache</code></td><td>bool</td><td><code>true</code></td><td>Use result cache (read + write)</td></tr>
    </tbody>
  </table>
</div>

<div class="card">
  <h2>LookupResponse Schema</h2>
  <table>
    <thead><tr><th>Field</th><th>Type</th><th>Description</th></tr></thead>
    <tbody>
      <tr><td><code>status</code></td><td>enum</td><td><span class="badge badge-ok">ok</span> <span class="badge badge-warn">no_zestimate</span> <span class="badge badge-warn">not_found</span> <span class="badge badge-warn">ambiguous</span> <span class="badge badge-err">blocked</span> <span class="badge badge-err">error</span></td></tr>
      <tr><td><code>ok</code></td><td>bool</td><td>True iff status is "ok" and value is non-null</td></tr>
      <tr><td><code>value</code></td><td>int | null</td><td>Zestimate in USD</td></tr>
      <tr><td><code>currency</code></td><td>string</td><td>Always "USD"</td></tr>
      <tr><td><code>zpid</code></td><td>string | null</td><td>Zillow property ID</td></tr>
      <tr><td><code>matched_address</code></td><td>string | null</td><td>Canonical address matched on Zillow</td></tr>
      <tr><td><code>zillow_url</code></td><td>string | null</td><td>Direct URL to Zillow property page</td></tr>
      <tr><td><code>confidence</code></td><td>float</td><td>Composite score 0-1. Halved on cross-check disagreement.</td></tr>
      <tr><td><code>confidence_breakdown</code></td><td>string[] | null</td><td>Human-readable explanation of confidence factors</td></tr>
      <tr><td><code>fetcher</code></td><td>string | null</td><td>Provider used (e.g. "scraperapi")</td></tr>
      <tr><td><code>cached</code></td><td>bool</td><td>True if served from cache</td></tr>
      <tr><td><code>crosscheck</code></td><td>object | null</td><td>CrossCheck result (see below)</td></tr>
      <tr><td><code>property_details</code></td><td>object | null</td><td>PropertyDetails (see below)</td></tr>
      <tr><td><code>alternates</code></td><td>array</td><td>Other candidate addresses from resolver</td></tr>
      <tr><td><code>fetched_at</code></td><td>string</td><td>ISO-8601 timestamp</td></tr>
      <tr><td><code>elapsed_ms</code></td><td>int | null</td><td>Wall-clock latency in milliseconds</td></tr>
      <tr><td><code>trace_id</code></td><td>string | null</td><td>Unique request trace ID (also in X-Request-ID header)</td></tr>
      <tr><td><code>error</code></td><td>string | null</td><td>Human-readable error when status != "ok"</td></tr>
    </tbody>
  </table>
</div>

<div class="card">
  <h2>CrossCheck Schema</h2>
  <table>
    <thead><tr><th>Field</th><th>Type</th><th>Description</th></tr></thead>
    <tbody>
      <tr><td><code>provider</code></td><td>string</td><td>Provider name (e.g. "rentcast")</td></tr>
      <tr><td><code>estimate</code></td><td>int | null</td><td>Independent valuation in USD</td></tr>
      <tr><td><code>range_low</code></td><td>int | null</td><td>Lower bound of valuation range</td></tr>
      <tr><td><code>range_high</code></td><td>int | null</td><td>Upper bound of valuation range</td></tr>
      <tr><td><code>delta_pct</code></td><td>float | null</td><td>(crosscheck - zillow) / zillow * 100</td></tr>
      <tr><td><code>within_tolerance</code></td><td>bool | null</td><td>True if |delta_pct| &le; 10%</td></tr>
      <tr><td><code>skipped</code></td><td>bool</td><td>True if cross-check was skipped</td></tr>
      <tr><td><code>skipped_reason</code></td><td>string | null</td><td>Why it was skipped (e.g. "monthly_cap_reached")</td></tr>
    </tbody>
  </table>
</div>

<div class="card">
  <h2>PropertyDetails Schema</h2>
  <table>
    <thead><tr><th>Field</th><th>Type</th><th>Description</th></tr></thead>
    <tbody>
      <tr><td><code>bedrooms</code></td><td>int | null</td><td>Number of bedrooms</td></tr>
      <tr><td><code>bathrooms</code></td><td>float | null</td><td>Number of bathrooms (0.5 = half bath)</td></tr>
      <tr><td><code>living_area_sqft</code></td><td>int | null</td><td>Living area in square feet</td></tr>
      <tr><td><code>lot_size_sqft</code></td><td>int | null</td><td>Lot size in square feet</td></tr>
      <tr><td><code>home_type</code></td><td>string | null</td><td>SINGLE_FAMILY, CONDO, TOWNHOUSE, etc.</td></tr>
      <tr><td><code>year_built</code></td><td>int | null</td><td>Year the property was built</td></tr>
      <tr><td><code>zestimate_range_low</code></td><td>int | null</td><td>Zillow's low confidence bound</td></tr>
      <tr><td><code>zestimate_range_high</code></td><td>int | null</td><td>Zillow's high confidence bound</td></tr>
      <tr><td><code>rent_zestimate</code></td><td>int | null</td><td>Estimated monthly rent</td></tr>
      <tr><td><code>tax_assessed_value</code></td><td>int | null</td><td>Tax-assessed property value</td></tr>
      <tr><td><code>monthly_hoa_fee</code></td><td>int | null</td><td>Monthly HOA fee</td></tr>
      <tr><td><code>home_status</code></td><td>string | null</td><td>FOR_SALE, RECENTLY_SOLD, etc.</td></tr>
      <tr><td><code>latitude</code></td><td>float | null</td><td>Property latitude</td></tr>
      <tr><td><code>longitude</code></td><td>float | null</td><td>Property longitude</td></tr>
      <tr><td><code>last_sold_price</code></td><td>int | null</td><td>Last sale price in USD</td></tr>
      <tr><td><code>last_sold_date</code></td><td>string | null</td><td>Last sale date (ISO format)</td></tr>
    </tbody>
  </table>
</div>

<div class="card">
  <h2>HTTP Status Code Mapping</h2>
  <table>
    <thead><tr><th>ZestimateStatus</th><th>HTTP Code</th><th>Meaning</th></tr></thead>
    <tbody>
      <tr><td><span class="badge badge-ok">ok</span></td><td>200</td><td>Zestimate found and returned</td></tr>
      <tr><td><span class="badge badge-warn">no_zestimate</span></td><td>200</td><td>Property exists but Zillow has no Zestimate</td></tr>
      <tr><td><span class="badge badge-warn">not_found</span></td><td>404</td><td>Address did not resolve to a Zillow property</td></tr>
      <tr><td><span class="badge badge-warn">ambiguous</span></td><td>409</td><td>Address resolved to multiple candidates</td></tr>
      <tr><td><span class="badge badge-err">blocked</span></td><td>502</td><td>Fetcher was blocked by Zillow anti-bot</td></tr>
      <tr><td><span class="badge badge-err">error</span></td><td>502</td><td>Unexpected pipeline error</td></tr>
    </tbody>
  </table>
</div>

<div class="card">
  <h2>BatchRequest / BatchResponse</h2>
  <h3>Request</h3>
  <table>
    <thead><tr><th>Field</th><th>Type</th><th>Default</th><th>Description</th></tr></thead>
    <tbody>
      <tr><td><code>addresses</code></td><td>string[]</td><td><em>required</em></td><td>1-50 US property addresses</td></tr>
      <tr><td><code>skip_crosscheck</code></td><td>bool</td><td><code>true</code></td><td>Skip Rentcast (saves budget)</td></tr>
      <tr><td><code>use_cache</code></td><td>bool</td><td><code>true</code></td><td>Use result cache</td></tr>
    </tbody>
  </table>
  <h3>Response</h3>
  <table>
    <thead><tr><th>Field</th><th>Type</th><th>Description</th></tr></thead>
    <tbody>
      <tr><td><code>total</code></td><td>int</td><td>Number of addresses in request</td></tr>
      <tr><td><code>ok_count</code></td><td>int</td><td>Number of successful lookups</td></tr>
      <tr><td><code>results</code></td><td>BatchResultItem[]</td><td>Results in same order as input</td></tr>
      <tr><td><code>elapsed_ms</code></td><td>int</td><td>Total wall-clock time</td></tr>
    </tbody>
  </table>
</div>

</div>

<!-- ═══════════════════════════════════════════════════════════ -->
<!-- LIMITATIONS TAB                                             -->
<!-- ═══════════════════════════════════════════════════════════ -->
<div id="tab-limits" class="tab-content">

<div class="card">
  <h2>Current Limitations</h2>
  <p class="muted">Honest assessment of what this system can and cannot do, with the tools and budget available.</p>

  <div class="lim-grid">
    <div class="lim">
      <div class="lim-title">ScraperAPI Latency (25-35s)</div>
      <div class="lim-body">Premium residential proxies + Zillow's JS rendering take 25-35 seconds per cold lookup. This is inherent to the proxy approach — Zillow requires full browser rendering. Cached lookups return in &lt;100ms. Mitigation: cache pre-warming for known address lists.</div>
    </div>
    <div class="lim">
      <div class="lim-title">Zillow Schema Drift</div>
      <div class="lim-body">Zillow's <code>__NEXT_DATA__</code> structure changes without notice (new query names, moved fields). The 4-tier parser fallback chain handles this gracefully, but a major restructure could temporarily break extraction. Mitigation: eval harness catches regressions quickly.</div>
    </div>
    <div class="lim">
      <div class="lim-title">Rentcast Budget (40 calls/month)</div>
      <div class="lim-body">Free Rentcast tier limits cross-checks to 40/month. After that, cross-check is silently skipped (confidence is still computed, just without second opinion). The hard cap prevents accidental overage. Mitigation: <code>force_crosscheck=true</code> for critical lookups.</div>
    </div>
    <div class="lim">
      <div class="lim-title">ScraperAPI Credits ($)</div>
      <div class="lim-body">Each premium Zillow fetch costs 10-25 ScraperAPI credits (~$0.01-0.05). High-volume usage requires budget planning. Mitigation: aggressive caching (6h TTL, daily key rotation), batch endpoint skips cross-check by default.</div>
    </div>
    <div class="lim">
      <div class="lim-title">US Addresses Only</div>
      <div class="lim-body">The normalizer uses <code>usaddress</code> (US-specific parser), and Zillow only covers US properties. International addresses will fail at the normalization stage with a clear error.</div>
    </div>
    <div class="lim">
      <div class="lim-title">Vercel Serverless Constraints</div>
      <div class="lim-body">Vercel's serverless environment has no persistent disk (memory-only cache), ephemeral process (cold starts reset cache), and function timeout limits. The Vercel deployment is a demo — production should use Docker on Railway/Fly.io for persistent SQLite/Redis cache.</div>
    </div>
    <div class="lim">
      <div class="lim-title">Anti-Bot Detection</div>
      <div class="lim-body">Zillow actively blocks scrapers. ScraperAPI's premium proxies handle this ~95% of the time, but blocks still happen. The circuit breaker fails fast (30s cooldown) instead of burning retries. <code>FetchBlockedError</code> is not retried — same params will get blocked again.</div>
    </div>
    <div class="lim">
      <div class="lim-title">Single Provider Dependency</div>
      <div class="lim-body">Currently depends on ScraperAPI as the primary fetcher. The <code>Fetcher</code> protocol supports ZenRows, BrightData, and Playwright as alternatives, but only ScraperAPI is fully integrated. Adding a second provider would improve resilience.</div>
    </div>
  </div>
</div>

<div class="card">
  <h2>Tools &amp; Dependencies</h2>
  <table>
    <thead><tr><th>Tool</th><th>Purpose</th><th>Limitation</th></tr></thead>
    <tbody>
      <tr><td><strong>ScraperAPI</strong></td><td>Anti-bot proxy for Zillow fetching</td><td>25-35s latency, credit-based pricing, occasional blocks</td></tr>
      <tr><td><strong>Rentcast API</strong></td><td>Independent AVM for cross-validation</td><td>40 calls/month free tier, address format sensitivity</td></tr>
      <tr><td><strong>usaddress</strong></td><td>US address parsing/normalization</td><td>US-only, struggles with unusual formats</td></tr>
      <tr><td><strong>Zillow Autocomplete</strong></td><td>Address &rarr; zpid resolution</td><td>Public API, no SLA, may change without notice</td></tr>
      <tr><td><strong>SQLite (diskcache)</strong></td><td>Default cache backend</td><td>Single-node only, no read replicas</td></tr>
      <tr><td><strong>Redis</strong></td><td>Optional distributed cache</td><td>Requires infrastructure, adds latency vs. local SQLite</td></tr>
      <tr><td><strong>FastAPI</strong></td><td>HTTP API framework</td><td>Python GIL limits true parallelism (mitigated by async I/O)</td></tr>
      <tr><td><strong>Vercel</strong></td><td>Serverless hosting for demo</td><td>No persistent disk, cold starts, 60s max timeout</td></tr>
    </tbody>
  </table>
</div>

<div class="card">
  <h2>What I'd Build Next</h2>
  <table>
    <thead><tr><th>#</th><th>Feature</th><th>Why</th></tr></thead>
    <tbody>
      <tr><td>1</td><td>Second fetcher provider (ZenRows or BrightData)</td><td>Eliminates single-provider dependency; FetcherChain auto-failover already built</td></tr>
      <tr><td>2</td><td>Webhook / async job queue</td><td>Return immediately, POST result to callback URL when ready — eliminates 30s wait</td></tr>
      <tr><td>3</td><td>Per-API-key rate limiting &amp; usage tracking</td><td>Multi-tenant deployment with per-customer quotas</td></tr>
      <tr><td>4</td><td>Historical Zestimate tracking</td><td>Store daily snapshots, expose trend endpoint (7d/30d/90d delta)</td></tr>
      <tr><td>5</td><td>Persistent deployment (Fly.io + Redis)</td><td>Eliminates cold start, enables distributed cache across regions</td></tr>
    </tbody>
  </table>
</div>

</div>

<footer>
  Zestimate Agent &mdash; Built by Prasanna for Nexhelm AI &mdash;
  <a href="/">Demo</a> &middot;
  <a href="/docs">API Docs</a> &middot;
  <a href="/metrics">Metrics</a>
</footer>

</main>

<script>
function showTab(name) {
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tabs button').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  event.target.classList.add('active');
}
</script>
</body>
</html>
"""
