"""Landing page HTML for the public demo deployment.

Inlined as a Python string (rather than a static file) so it ships in the
same serverless function bundle with zero extra packaging work — no
`StaticFiles`, no `importlib.resources`, no MANIFEST.in. One module import
and the FastAPI route returns it as `HTMLResponse`.

Design goals
------------
* Single file, no build step, no framework.
* Works identically on Vercel, Docker, and `uvicorn` local dev.
* Vanilla `fetch()` against the same-origin `POST /lookup` endpoint.
* Gracefully degrades if JavaScript is disabled (shows the curl example).
* Dark theme, monospace accents — reads as "infra demo" not "marketing site".
"""

from __future__ import annotations

# Keep the HTML as a single top-level constant so it's cheap to import and
# easy to eyeball. Roughly ~180 lines — small enough to inline, large enough
# to tell a story (problem → architecture → live demo → links).
LANDING_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Zestimate Agent — live demo</title>
<meta name="description" content="Production-grade Python agent that fetches Zillow Zestimates with crosscheck validation and confidence scoring.">
<style>
  :root {
    --bg: #0b0d12;
    --panel: #11141b;
    --panel-hi: #161a22;
    --border: #222836;
    --fg: #e6e9ef;
    --muted: #8a93a5;
    --accent: #7cc4ff;
    --accent-hi: #a7d8ff;
    --good: #4ade80;
    --warn: #fbbf24;
    --bad: #f87171;
    --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    --sans: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", Roboto, sans-serif;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: var(--bg); color: var(--fg); font-family: var(--sans); font-size: 15px; line-height: 1.55; }
  a { color: var(--accent); text-decoration: none; }
  a:hover { color: var(--accent-hi); text-decoration: underline; }
  code, pre, .mono { font-family: var(--mono); font-size: 13px; }
  main { max-width: 860px; margin: 0 auto; padding: 48px 24px 96px; }
  header.hero { margin-bottom: 40px; }
  header.hero h1 { font-size: 34px; font-weight: 700; margin: 0 0 8px; letter-spacing: -0.02em; }
  header.hero .tag { color: var(--muted); font-size: 16px; margin: 0; }
  header.hero .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; background: #1a2332; color: var(--accent); font-family: var(--mono); font-size: 11px; margin-right: 8px; vertical-align: middle; }
  section.card { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 24px; margin-bottom: 20px; }
  section.card h2 { margin: 0 0 16px; font-size: 18px; font-weight: 600; color: var(--fg); }
  section.card h2 .small { font-size: 12px; font-weight: 400; color: var(--muted); margin-left: 8px; }

  form .row { display: flex; gap: 10px; }
  input[type=text] {
    flex: 1; background: var(--panel-hi); border: 1px solid var(--border); color: var(--fg);
    padding: 12px 14px; border-radius: 8px; font-family: var(--sans); font-size: 15px;
    outline: none; transition: border-color .12s;
  }
  input[type=text]:focus { border-color: var(--accent); }
  input[type=text]::placeholder { color: var(--muted); }
  button {
    background: var(--accent); color: #0b0d12; border: 0; padding: 12px 20px; border-radius: 8px;
    font-weight: 600; font-size: 15px; cursor: pointer; transition: background .12s, transform .08s;
  }
  button:hover { background: var(--accent-hi); }
  button:active { transform: translateY(1px); }
  button[disabled] { background: var(--border); color: var(--muted); cursor: not-allowed; }
  .examples { margin-top: 10px; font-size: 13px; color: var(--muted); }
  .examples a { margin-right: 12px; }

  /* Result card */
  #result { display: none; margin-top: 18px; }
  #result.show { display: block; }
  #result.err { border-color: #4b1d1d; background: #1a1010; }
  .spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin .7s linear infinite; vertical-align: middle; margin-right: 8px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .val { font-size: 44px; font-weight: 700; letter-spacing: -0.02em; color: var(--fg); margin: 4px 0 2px; }
  .addr { color: var(--muted); font-size: 14px; margin: 0 0 18px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 14px 24px; }
  .grid .k { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .08em; margin-bottom: 2px; }
  .grid .v { font-family: var(--mono); font-size: 13px; color: var(--fg); word-break: break-all; }
  .conf-ok { color: var(--good); }
  .conf-mid { color: var(--warn); }
  .conf-lo { color: var(--bad); }
  details { margin-top: 14px; }
  details summary { cursor: pointer; color: var(--muted); font-size: 12px; user-select: none; }
  pre.raw { background: #060810; border: 1px solid var(--border); border-radius: 6px; padding: 12px; margin-top: 8px; overflow-x: auto; color: #c3c9d5; font-size: 12px; }

  /* Arch / links block */
  .arch { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-top: 6px; }
  .arch .box { background: var(--panel-hi); border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; }
  .arch .box .title { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .08em; margin-bottom: 2px; }
  .arch .box .body { font-size: 13px; color: var(--fg); }
  .links { display: flex; flex-wrap: wrap; gap: 8px 18px; margin-top: 12px; font-size: 13px; }

  footer { margin-top: 36px; text-align: center; color: var(--muted); font-size: 12px; }
  @media (max-width: 600px) {
    header.hero h1 { font-size: 26px; }
    .val { font-size: 34px; }
    form .row { flex-direction: column; }
    .arch { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<main>
  <header class="hero">
    <h1><span class="pill">DEMO</span>Zestimate Agent</h1>
    <p class="tag">Production-grade Python agent that fetches the current Zillow Zestimate for any US address &mdash; with independent cross-check and confidence scoring.</p>
  </header>

  <section class="card">
    <h2>Try it live <span class="small">public demo &middot; no auth</span></h2>
    <form id="lookup-form">
      <div class="row">
        <input id="addr" type="text" autocomplete="off"
               placeholder="e.g. 1600 Pennsylvania Ave NW, Washington, DC 20500"
               value="1600 Amphitheatre Parkway, Mountain View, CA 94043" required>
        <button id="go" type="submit">Get Zestimate</button>
      </div>
      <div class="examples">
        try:
        <a href="#" data-addr="350 5th Ave, New York, NY 10118">Empire State</a>
        <a href="#" data-addr="2 Lincoln Memorial Cir NW, Washington, DC 20002">Lincoln Memorial</a>
        <a href="#" data-addr="1 Infinite Loop, Cupertino, CA 95014">Infinite Loop</a>
      </div>
    </form>
    <section class="card" id="result"></section>
  </section>

  <section class="card">
    <h2>How it works</h2>
    <div class="arch">
      <div class="box"><div class="title">1 &mdash; Resolve</div><div class="body">Zillow autocomplete API &rarr; <code>zpid</code> + canonical address</div></div>
      <div class="box"><div class="title">2 &mdash; Fetch</div><div class="body">ScraperAPI (residential proxies) &rarr; Zillow property page HTML</div></div>
      <div class="box"><div class="title">3 &mdash; Parse</div><div class="body">3-tier: <code>__NEXT_DATA__</code> &rarr; inline JSON &rarr; HTML heuristic</div></div>
      <div class="box"><div class="title">4 &mdash; Cross-check</div><div class="body">Rentcast AVM (budget-capped at 40/mo) &rarr; confidence halved on disagreement</div></div>
    </div>
    <div class="links">
      <a href="/docs">OpenAPI / Swagger UI &rarr;</a>
      <a href="/healthz">/healthz</a>
      <a href="/version">/version</a>
      <a href="/metrics">/metrics</a>
      <a href="https://github.com/prassu017/zestimate-agent" target="_blank" rel="noopener">GitHub source &rarr;</a>
    </div>
  </section>

  <section class="card">
    <h2>Raw API <span class="small">same endpoint the form above calls</span></h2>
    <pre class="mono" style="background:#060810;border:1px solid var(--border);border-radius:6px;padding:14px;overflow-x:auto;color:#c3c9d5;">curl -X POST https://zestimate-agent.vercel.app/lookup \\
  -H "Content-Type: application/json" \\
  -d '{"address": "1600 Pennsylvania Ave NW, Washington, DC 20500"}'</pre>
  </section>

  <footer>Built for <a href="https://nexthelm.ai" target="_blank" rel="noopener">Nexthelm</a> &middot; <span class="mono">FastAPI + httpx + selectolax + ScraperAPI</span></footer>
</main>

<script>
(function() {
  var form = document.getElementById('lookup-form');
  var addrInput = document.getElementById('addr');
  var btn = document.getElementById('go');
  var resultEl = document.getElementById('result');

  function fmtMoney(n) {
    if (n == null) return '—';
    return '$' + Number(n).toLocaleString('en-US');
  }
  function confClass(c) {
    if (c == null) return '';
    if (c >= 0.75) return 'conf-ok';
    if (c >= 0.4) return 'conf-mid';
    return 'conf-lo';
  }
  function h(tag, attrs, children) {
    var el = document.createElement(tag);
    if (attrs) for (var k in attrs) {
      if (k === 'class') el.className = attrs[k];
      else if (k === 'text') el.textContent = attrs[k];
      else if (k === 'html') el.innerHTML = attrs[k];
      else el.setAttribute(k, attrs[k]);
    }
    if (children) children.forEach(function(c) { if (c) el.appendChild(c); });
    return el;
  }

  function renderLoading() {
    resultEl.className = 'card show';
    resultEl.innerHTML = '';
    resultEl.appendChild(h('div', {}, [
      h('span', { class: 'spinner' }),
      h('span', { text: 'Contacting Zillow via ScraperAPI… (usually 2-5s warm; cached hits <50ms)' })
    ]));
  }
  function renderError(msg, raw) {
    resultEl.className = 'card show err';
    resultEl.innerHTML = '';
    resultEl.appendChild(h('div', { class: 'val', text: 'Lookup failed' }));
    resultEl.appendChild(h('p', { class: 'addr', text: msg }));
    if (raw) {
      var det = h('details');
      det.appendChild(h('summary', { text: 'raw response' }));
      det.appendChild(h('pre', { class: 'raw', text: JSON.stringify(raw, null, 2) }));
      resultEl.appendChild(det);
    }
  }
  function renderResult(r) {
    resultEl.className = 'card show';
    resultEl.innerHTML = '';

    // Status badge + value
    var headline = h('div');
    if (r.status === 'ok' && r.value) {
      headline.appendChild(h('div', { class: 'val', text: fmtMoney(r.value) }));
    } else {
      headline.appendChild(h('div', { class: 'val', text: 'No Zestimate' }));
    }
    headline.appendChild(h('p', { class: 'addr', text: r.matched_address || addrInput.value }));
    resultEl.appendChild(headline);

    // Grid of metadata
    var grid = h('div', { class: 'grid' });
    function cell(k, v, klass) {
      var div = h('div');
      div.appendChild(h('div', { class: 'k', text: k }));
      var vel = h('div', { class: 'v' + (klass ? ' ' + klass : '') });
      vel.textContent = v;
      div.appendChild(vel);
      return div;
    }
    grid.appendChild(cell('status', r.status || 'unknown'));
    if (r.confidence != null) grid.appendChild(cell('confidence', r.confidence.toFixed(3), confClass(r.confidence)));
    if (r.fetcher) grid.appendChild(cell('fetcher', r.fetcher));
    if (r.zpid) grid.appendChild(cell('zpid', r.zpid));
    if (r.cached != null) grid.appendChild(cell('cached', r.cached ? 'yes' : 'no'));
    if (r.elapsed_ms != null) grid.appendChild(cell('lookup took', (r.elapsed_ms / 1000).toFixed(2) + ' s'));
    if (r.crosscheck && r.crosscheck.estimate != null) {
      grid.appendChild(cell('rentcast', fmtMoney(r.crosscheck.estimate)));
      grid.appendChild(cell('delta %', r.crosscheck.delta_pct != null ? r.crosscheck.delta_pct.toFixed(2) + '%' : '—'));
      if (r.crosscheck.range_low != null && r.crosscheck.range_high != null) {
        grid.appendChild(cell('rentcast range', fmtMoney(r.crosscheck.range_low) + ' - ' + fmtMoney(r.crosscheck.range_high)));
      }
      if (r.crosscheck.within_tolerance != null) {
        grid.appendChild(cell('agreement', r.crosscheck.within_tolerance ? 'within tolerance' : 'disagrees'));
      }
    } else if (r.crosscheck && r.crosscheck.skipped) {
      grid.appendChild(cell('rentcast', 'skipped'));
      if (r.crosscheck.skipped_reason) grid.appendChild(cell('skip reason', r.crosscheck.skipped_reason));
    }
    if (r.trace_id) grid.appendChild(cell('trace id', r.trace_id));
    resultEl.appendChild(grid);

    // Alternates (only when the resolver returned ambiguous candidates).
    if (r.alternates && r.alternates.length > 0) {
      var altBox = h('div', { class: 'addr', html: '<strong>Other candidates:</strong>' });
      altBox.style.marginTop = '12px';
      var ul = document.createElement('ul');
      ul.style.margin = '6px 0 0 18px';
      ul.style.padding = '0';
      ul.style.fontSize = '12px';
      r.alternates.forEach(function(a) {
        var li = document.createElement('li');
        li.textContent = (a.display || 'unknown') + (a.zpid ? ' (zpid ' + a.zpid + ')' : '') + (a.score != null ? ' — score ' + a.score : '');
        ul.appendChild(li);
      });
      altBox.appendChild(ul);
      resultEl.appendChild(altBox);
    }

    // Surface error text if present (e.g. blocked / error status).
    if (r.error) {
      var errP = h('p', { class: 'addr' });
      errP.style.color = 'var(--bad)';
      errP.textContent = 'error: ' + r.error;
      resultEl.appendChild(errP);
    }

    // Link to Zillow + raw
    if (r.zillow_url) {
      var p = h('p', { class: 'addr', html: '<a href="' + r.zillow_url + '" target="_blank" rel="noopener">View on Zillow &rarr;</a>' });
      resultEl.appendChild(p);
    }
    var det = h('details');
    det.appendChild(h('summary', { text: 'raw JSON response' }));
    det.appendChild(h('pre', { class: 'raw', text: JSON.stringify(r, null, 2) }));
    resultEl.appendChild(det);
  }

  async function doLookup(addr) {
    btn.disabled = true;
    btn.textContent = 'Looking up…';
    renderLoading();
    try {
      var res = await fetch('/lookup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ address: addr })
      });
      var body = null;
      try { body = await res.json(); } catch (e) { body = { error: 'non-JSON response', raw: await res.text() }; }
      if (!res.ok && res.status >= 500) {
        renderError('Server returned HTTP ' + res.status, body);
      } else if (body && body.error) {
        renderError(body.detail || body.error, body);
      } else {
        renderResult(body);
      }
    } catch (e) {
      renderError('Network error: ' + (e && e.message ? e.message : String(e)));
    } finally {
      btn.disabled = false;
      btn.textContent = 'Get Zestimate';
    }
  }

  form.addEventListener('submit', function(ev) {
    ev.preventDefault();
    var addr = (addrInput.value || '').trim();
    if (!addr) return;
    doLookup(addr);
  });
  document.querySelectorAll('.examples a[data-addr]').forEach(function(a) {
    a.addEventListener('click', function(ev) {
      ev.preventDefault();
      addrInput.value = a.getAttribute('data-addr');
      doLookup(addrInput.value);
    });
  });
})();
</script>
</body>
</html>
"""
