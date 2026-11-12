"""A single-file operations console.

No build step, no CDN, no framework: the page is served as one string and talks
to the same JSON API documented at ``/docs``. That keeps the repository runnable
from a clone, which for a demo is worth more than a front-end toolchain.

Three views, switched by the URL fragment so links and the back button work:

* ``#/`` - headline counters and the incident list;
* ``#/incidents/<id>`` - one incident: the diagnosis and its reasoning, the
  evidence, a chart of the anomalous metric against its history, every action
  with a preview of its effect, and the timeline;
* ``#/evals`` - the last saved eval report per scenario and engine, side by side.

Everything that comes from the API is escaped before it reaches the DOM. Row
values, ticket bodies and model reasoning are untrusted text.
"""

from __future__ import annotations

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sentinel - incident console</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #0f1115; --panel: #171a21; --line: #262b36; --text: #e6e8ee;
    --muted: #99a0b0; --accent: #6aa9ff;
    --critical: #ff6b6b; --warning: #ffc857; --ok: #4ade80; --pending: #c792ea;
    --band: rgba(106, 169, 255, 0.12);
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: #f6f7f9; --panel: #ffffff; --line: #e2e5ea; --text: #14161a;
      --muted: #5b6472; --accent: #1f6feb;
      --critical: #d1242f; --warning: #9a6700; --ok: #1a7f37; --pending: #8250df;
      --band: rgba(31, 111, 235, 0.10);
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  header {
    padding: 18px 28px; border-bottom: 1px solid var(--line);
    display: flex; align-items: center; gap: 18px; flex-wrap: wrap;
  }
  h1 { font-size: 19px; margin: 0; letter-spacing: -0.01em; }
  nav { display: flex; gap: 4px; }
  nav a {
    color: var(--muted); text-decoration: none; padding: 5px 11px; border-radius: 7px;
    font-size: 14px;
  }
  nav a.active { color: var(--text); background: var(--panel); border: 1px solid var(--line); }
  .sub { color: var(--muted); font-size: 13px; }
  main { padding: 24px 28px; max-width: 1180px; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 14px; }
  .tile, .panel, .incident {
    background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  }
  .tile { padding: 14px 16px; }
  .tile .label, .label {
    color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em;
  }
  .tile .value { font-size: 26px; font-weight: 600; margin-top: 4px; }
  h2 { font-size: 15px; margin: 28px 0 12px; letter-spacing: 0.02em; }
  h3 { font-size: 14px; margin: 0 0 10px; }
  .panel { padding: 16px 18px; margin-bottom: 14px; }
  .grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 14px; }
  .incident {
    display: block; color: inherit; text-decoration: none;
    padding: 14px 16px; margin-bottom: 12px;
  }
  a.incident:hover { border-color: var(--accent); }
  .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .pill {
    font-size: 11px; padding: 2px 9px; border-radius: 999px;
    border: 1px solid var(--line); color: var(--muted); white-space: nowrap;
  }
  .pill.critical, .pill.failed, .pill.harmful { color: var(--critical); border-color: var(--critical); }
  .pill.warning, .pill.remediated { color: var(--warning); border-color: var(--warning); }
  .pill.resolved, .pill.executed, .pill.ok { color: var(--ok); border-color: var(--ok); }
  .pill.awaiting_approval { color: var(--pending); border-color: var(--pending); }
  .mono, .id { font-family: ui-monospace, "SFMono-Regular", Consolas, monospace; font-size: 12.5px; }
  .id { color: var(--muted); }
  .title { font-weight: 600; margin: 8px 0 2px; }
  .summary, .muted { color: var(--muted); font-size: 14px; }
  .reasoning { white-space: pre-wrap; font-size: 14px; margin: 8px 0 0; }
  .actions { margin-top: 10px; display: grid; gap: 8px; }
  .action { font-size: 13px; padding: 9px 11px; border: 1px solid var(--line); border-radius: 8px; }
  .action .name { font-family: ui-monospace, Consolas, monospace; font-weight: 600; }
  .action .result { color: var(--muted); }
  .action .buttons { margin-left: auto; display: flex; gap: 6px; }
  .preview { margin-top: 8px; padding: 9px 11px; border-radius: 7px; background: var(--band); }
  .preview ul { margin: 4px 0 0; padding-left: 18px; }
  button {
    font: inherit; font-size: 13px; padding: 4px 12px; border-radius: 7px;
    border: 1px solid var(--accent); background: transparent; color: var(--accent);
    cursor: pointer;
  }
  button.quiet { border-color: var(--line); color: var(--muted); }
  button:hover { background: color-mix(in srgb, var(--accent) 12%, transparent); }
  select {
    font: inherit; font-size: 13px; padding: 3px 8px; border-radius: 7px;
    border: 1px solid var(--line); background: var(--panel); color: var(--text);
  }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
  th { color: var(--muted); font-weight: 500; font-size: 12px; }
  td.nowrap { white-space: nowrap; }
  .scroll { overflow-x: auto; }
  .timeline { list-style: none; margin: 0; padding: 0; font-size: 13px; }
  .timeline li { display: grid; grid-template-columns: 74px 130px 1fr; gap: 10px; padding: 5px 0;
    border-bottom: 1px solid var(--line); }
  .timeline li:last-child { border-bottom: 0; }
  svg.chart { width: 100%; height: auto; display: block; }
  svg.chart .axis { stroke: var(--line); }
  svg.chart text { fill: var(--muted); font-size: 11px; }
  svg.chart .line { fill: none; stroke: var(--accent); stroke-width: 2; }
  svg.chart .expected { stroke: var(--muted); stroke-dasharray: 4 4; }
  svg.chart .band { fill: var(--band); }
  svg.chart .dot { fill: var(--accent); }
  svg.chart .hit { fill: var(--critical); }
  svg.chart .fixed { fill: var(--ok); }
  .legend { display: flex; gap: 14px; flex-wrap: wrap; font-size: 12px; color: var(--muted); margin-top: 6px; }
  .swatch { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 5px; }
  .metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 10px; }
  .metric .value { font-size: 20px; font-weight: 600; }
  .yes { color: var(--ok); } .no { color: var(--critical); }
  .empty { color: var(--muted); }
  footer { padding: 20px 28px; color: var(--muted); font-size: 13px; }
  a { color: var(--accent); }
  @media (max-width: 640px) {
    header, main, footer { padding-left: 16px; padding-right: 16px; }
    .timeline li { grid-template-columns: 64px 1fr; }
    .timeline li .detail { grid-column: 1 / -1; }
    .grid2 { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<header>
  <h1>Sentinel</h1>
  <nav>
    <a href="#/" data-view="overview">Incidents</a>
    <a href="#/evals" data-view="evals">Evals</a>
  </nav>
  <span class="sub" style="margin-left:auto"><a href="/docs">API docs</a></span>
</header>
<main id="view"><p class="empty">Loading&hellip;</p></main>
<footer id="footer"></footer>

<script>
const TOKEN_KEY = "sentinel.approverToken";
let refreshTimer = null;

// -- helpers ---------------------------------------------------------------

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[char]));
}

function pill(text, kind) {
  return `<span class="pill ${esc(kind || "")}">${esc(text)}</span>`;
}

function pct(value) {
  return value == null ? "-" : `${Math.round(value * 100)}%`;
}

function num(value) {
  if (value == null || Number.isNaN(value)) return "-";
  const abs = Math.abs(value);
  return value.toLocaleString(undefined, { maximumFractionDigits: abs >= 100 ? 0 : 3 });
}

function time(iso) {
  return iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
}

async function json(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch (error) { /* not JSON */ }
    throw new Error(detail);
  }
  return response.json();
}

function storedToken() {
  try { return sessionStorage.getItem(TOKEN_KEY); } catch (error) { return null; }
}

function rememberToken(token) {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch (error) { /* storage unavailable: the token lasts for this call only */ }
}

// -- overview --------------------------------------------------------------

function tile(label, value) {
  return `<div class="tile"><div class="label">${esc(label)}</div>
          <div class="value">${esc(value)}</div></div>`;
}

function incidentCard(incident) {
  const diagnosis = incident.root_cause
    ? `<div class="summary"><strong>${esc(incident.root_cause)}</strong>
       &middot; confidence ${pct(incident.confidence)} &middot; ${esc(incident.engine)}<br>
       ${esc(incident.summary)}</div>`
    : `<div class="summary">Not diagnosed yet.</div>`;
  const pending = incident.actions.filter((a) => a.status === "awaiting_approval").length;
  return `<a class="incident" href="#/incidents/${encodeURIComponent(incident.incident_id)}">
      <div class="row">
        <span class="id">${esc(incident.incident_id)}</span>
        ${pill(incident.business_date)} ${pill(incident.dataset)}
        ${pill(incident.severity, incident.severity)} ${pill(incident.status, incident.status)}
        ${pending ? pill(`${pending} awaiting approval`, "awaiting_approval") : ""}
      </div>
      <div class="title">${esc(incident.title)}</div>
      ${diagnosis}
    </a>`;
}

async function renderOverview(view) {
  const [summary, incidents] = await Promise.all([
    json("/api/summary"), json("/api/incidents?limit=200")
  ]);
  const byStatus = summary.incidents_by_status || {};
  const run = summary.latest_run;
  document.getElementById("footer").textContent = run
    ? `Latest pipeline run ${run.run_id} for ${run.business_date} (${run.status}).`
    : "No pipeline runs recorded yet.";
  view.innerHTML = `
    <div class="tiles">
      ${tile("incidents", summary.incidents_total)}
      ${tile("resolved", byStatus.resolved || 0)}
      ${tile("awaiting approval", byStatus.awaiting_approval || 0)}
      ${tile("dates held", (summary.active_holds || []).length)}
      ${tile("upstream tickets", summary.tickets)}
    </div>
    <h2>Incidents</h2>
    ${incidents.length ? incidents.map(incidentCard).join("")
                       : '<p class="empty">No incidents on record.</p>'}`;
}

// -- incident detail -------------------------------------------------------

function chartSvg(points, focusDate, signal) {
  const expected = signal ? signal.expected : null;
  const detected = signal ? signal.observed : null;
  const values = points.map((p) => p.value).filter((v) => v != null);
  if (expected != null) values.push(expected);
  if (detected != null) values.push(detected);
  if (points.length < 2 || !values.length) return '<p class="empty">Not enough history to chart.</p>';
  const W = 720, H = 220, L = 58, R = 12, T = 12, B = 28;
  let lo = Math.min(...values), hi = Math.max(...values);
  if (hi === lo) { hi += 1; lo -= 1; }
  const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
  const x = (i) => L + (i * (W - L - R)) / (points.length - 1);
  const y = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo));
  const path = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(i === points.length - 1 && points[i].business_date === focusDate && detected != null ? detected : p.value).toFixed(1)}`).join("");
  const ticks = [lo + pad, (lo + hi) / 2, hi - pad];
  const focus = points.findIndex((p) => p.business_date === focusDate);
  let marks = "";
  if (focus >= 0) {
    const now = points[focus].value;
    const before = detected ?? now;
    const changed = detected != null && Math.abs(detected - now) > 1e-6 * Math.max(1, Math.abs(now));
    marks += `<circle class="hit" cx="${x(focus)}" cy="${y(before)}" r="5"><title>as detected: ${esc(num(before))}</title></circle>`;
    if (changed) {
      marks += `<line class="expected" x1="${x(focus)}" x2="${x(focus)}" y1="${y(before)}" y2="${y(now)}"/>`;
      marks += `<circle class="fixed" cx="${x(focus)}" cy="${y(now)}" r="5"><title>now, after remediation: ${esc(num(now))}</title></circle>`;
    }
  }
  const labelEvery = Math.max(1, Math.ceil(points.length / 6));
  const xlabels = points.map((p, i) => (i % labelEvery === 0 || i === points.length - 1)
    ? `<text x="${x(i)}" y="${H - 8}" text-anchor="${i === points.length - 1 ? "end" : i === 0 ? "start" : "middle"}">${esc(p.business_date.slice(5))}</text>` : "").join("");
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="metric history">
      <line class="axis" x1="${L}" y1="${H - B}" x2="${W - R}" y2="${H - B}"/>
      ${ticks.map((t) => `<text x="${L - 8}" y="${y(t) + 4}" text-anchor="end">${esc(num(t))}</text>`).join("")}
      ${expected != null ? `<line class="expected" x1="${L}" x2="${W - R}" y1="${y(expected)}" y2="${y(expected)}"/>` : ""}
      <path class="line" d="${path}"/>
      ${points.map((p, i) => i === focus ? "" : `<circle class="dot" cx="${x(i)}" cy="${y(p.value)}" r="2.5"><title>${esc(p.business_date)}: ${esc(num(p.value))}</title></circle>`).join("")}
      ${marks}
      ${xlabels}
    </svg>
    <div class="legend">
      <span><span class="swatch" style="background:var(--critical)"></span>this date, as detected</span>
      <span><span class="swatch" style="background:var(--ok)"></span>after remediation</span>
      ${expected != null ? '<span>- - expected for this weekday</span>' : ""}
    </div>`;
}

async function drawChart(incident, metric) {
  const target = document.getElementById("chart");
  if (!target) return;
  const signal = (incident.anomalies || []).find((a) => a.metric === metric);
  try {
    const series = await json(`/api/metrics?dataset=${encodeURIComponent(incident.dataset)}`
      + `&metric=${encodeURIComponent(metric)}&days=28&until=${incident.business_date}`);
    target.innerHTML = chartSvg(series.points, incident.business_date, signal);
  } catch (error) {
    target.innerHTML = `<p class="empty">${esc(error.message)}</p>`;
  }
}

function actionRow(incident, action, index) {
  const pending = action.status === "awaiting_approval";
  const buttons = `<button class="quiet" onclick="togglePreview('${esc(incident.incident_id)}', ${index})">What changes?</button>`
    + (pending ? `<button onclick="decide('${esc(incident.incident_id)}', ${index}, 'approve')">Approve</button>
                  <button class="quiet" onclick="decide('${esc(incident.incident_id)}', ${index}, 'reject')">Reject</button>` : "");
  const args = Object.entries(action.arguments || {})
    .map(([k, v]) => `${esc(k)}=${esc(typeof v === "string" ? v : JSON.stringify(v))}`).join(", ");
  return `<div class="action">
      <div class="row">
        <span class="name">${esc(action.name)}</span>
        ${pill(action.status, action.status)} ${pill(`risk: ${action.risk}`)}
        <span class="buttons">${buttons}</span>
      </div>
      ${args ? `<div class="mono muted" style="margin-top:4px">${args}</div>` : ""}
      <div class="result" style="margin-top:4px">${esc(action.result || action.rationale || "")}</div>
      <div id="preview-${index}"></div>
    </div>`;
}

async function togglePreview(incidentId, index) {
  const target = document.getElementById(`preview-${index}`);
  if (target.innerHTML) { target.innerHTML = ""; return; }
  target.innerHTML = '<div class="preview muted">Working it out&hellip;</div>';
  try {
    const p = await json(`/api/incidents/${encodeURIComponent(incidentId)}/actions/${index}/preview`);
    const effects = p.effects.length ? p.effects : ["No measurable effect on the data."];
    target.innerHTML = `<div class="preview">
        <div>${esc(p.description)}</div>
        <ul>${effects.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>
        ${p.rebuilds_silver ? '<div class="muted">Silver is rebuilt and re-checked afterwards.</div>' : ""}
      </div>`;
  } catch (error) {
    target.innerHTML = `<div class="preview">${esc(error.message)}</div>`;
  }
}

async function renderIncident(view, incidentId) {
  const incident = await json(`/api/incidents/${encodeURIComponent(incidentId)}`);
  const d = incident.diagnosis;
  const causes = d ? [d.root_cause, ...(d.contributing_causes || [])] : [];
  const metricsFromSignals = (incident.anomalies || []).map((a) => a.metric);
  const metricNames = [...new Set([...metricsFromSignals,
    ...Object.keys((incident.evidence || {}).metrics || {})
      .filter((m) => m === "row_count" || m.startsWith("measure_"))])];
  const firstMetric = metricNames[0] || "row_count";

  const checks = (incident.failed_checks || []).map((c) => `<tr>
      <td class="mono">${esc(c.check)}</td><td>${pill(c.severity, c.severity)}</td>
      <td>${esc(num(c.observed))}</td><td>${esc(num(c.threshold))}</td><td>${esc(c.details)}</td></tr>`).join("");
  const signals = (incident.anomalies || []).map((a) => `<tr>
      <td class="mono">${esc(a.metric)}</td><td>${esc(num(a.observed))}</td>
      <td>${esc(num(a.expected))}</td><td>${esc(num(a.ratio))}x</td><td>${esc(num(a.zscore))}</td></tr>`).join("");
  const timeline = (incident.timeline || []).map((e) => `<li>
      <span class="muted">${esc(time(e.at))}</span><span class="mono">${esc(e.actor)}</span>
      <span class="detail"><strong>${esc(e.event)}</strong> ${esc(e.detail)}</span></li>`).join("");

  document.getElementById("footer").textContent = `Run ${incident.run_id}`;
  view.innerHTML = `
    <p><a href="#/">&larr; all incidents</a></p>
    <div class="row">
      <span class="id">${esc(incident.incident_id)}</span>
      ${pill(incident.business_date)} ${pill(incident.dataset)}
      ${pill(incident.severity, incident.severity)} ${pill(incident.status, incident.status)}
    </div>
    <h2 style="margin-top:10px">${esc(incident.title)}</h2>

    <div class="panel">
      <h3>Diagnosis</h3>
      ${d ? `<div class="row">${causes.map((c, i) => pill(i ? `also: ${c}` : c, i ? "" : "ok")).join(" ")}
               ${pill(`confidence ${pct(d.confidence)}`)} ${pill(d.engine)}
               ${pill(`${d.tool_calls} tool call(s)`)} ${pill(`${d.latency_ms} ms`)}</div>
             <p><strong>${esc(d.summary)}</strong></p>
             <p class="reasoning">${esc(d.reasoning)}</p>`
          : '<p class="empty">Not diagnosed yet.</p>'}
    </div>

    <div class="panel">
      <div class="row" style="margin-bottom:8px">
        <h3 style="margin:0">Metric history</h3>
        <select id="metric" style="margin-left:auto" aria-label="metric">
          ${metricNames.map((m) => `<option ${m === firstMetric ? "selected" : ""}>${esc(m)}</option>`).join("")}
        </select>
      </div>
      <div id="chart"><p class="empty">Loading&hellip;</p></div>
    </div>

    <div class="grid2">
      <div class="panel scroll"><h3>Failed checks</h3>
        ${checks ? `<table><tr><th>check</th><th>severity</th><th>observed</th><th>threshold</th><th>details</th></tr>${checks}</table>`
                 : '<p class="empty">None. Every deterministic check passed.</p>'}</div>
      <div class="panel scroll"><h3>Anomalies vs. history</h3>
        ${signals ? `<table><tr><th>metric</th><th>observed</th><th>expected</th><th>ratio</th><th>z</th></tr>${signals}</table>`
                  : '<p class="empty">None.</p>'}</div>
    </div>

    <div class="panel"><h3>Actions</h3>
      <div class="actions">${incident.actions.length
        ? incident.actions.map((a, i) => actionRow(incident, a, i)).join("")
        : '<p class="empty">No actions proposed.</p>'}</div>
    </div>

    <div class="panel"><h3>Timeline</h3><ul class="timeline">${timeline}</ul></div>`;

  document.getElementById("metric").addEventListener("change", (event) => {
    drawChart(incident, event.target.value);
  });
  drawChart(incident, firstMetric);
}

// -- evals -----------------------------------------------------------------

function metricTile(label, value, invert) {
  const good = value == null ? "" : (invert ? value === 0 : value === 1) ? "yes" : "";
  return `<div class="metric"><div class="label">${esc(label)}</div>
          <div class="value ${good}">${pct(value)}</div></div>`;
}

function evalBlock(report) {
  const s = report.summary;
  const rows = report.cases.map((c) => {
    const observed = [c.observed_root_cause, ...c.observed_contributing].join(" + ");
    const expected = c.expected_causes.join(" + ") || "none";
    return `<tr>
      <td class="nowrap">${esc(c.business_date)}</td><td>${esc(c.dataset)}</td>
      <td class="mono">${esc(expected)}</td>
      <td class="mono ${c.expected_causes.length ? (c.root_cause_correct ? "yes" : "no") : ""}">${esc(observed)}</td>
      <td class="mono">${esc(c.proposed_actions.join(", ") || "-")}</td>
      <td>${c.harmful ? pill("harmful", "harmful") : c.actions_correct ? pill("right fix", "ok") : pill("incomplete", "warning")}</td>
      <td>${pill(c.final_status, c.final_status)}</td></tr>`;
  }).join("");
  return `<div class="panel">
      <div class="row" style="margin-bottom:10px">
        <h3 style="margin:0">${esc(report.engine)}</h3>
        <span class="muted">saved ${esc(new Date(report.saved_at).toLocaleString())} &middot; ${esc(report.wall_seconds)}s</span>
      </div>
      <div class="metrics">
        ${metricTile("detection", s.detection_rate)}
        ${metricTile("root cause", s.root_cause_accuracy)}
        ${metricTile("causes found", s.cause_coverage)}
        ${metricTile("right actions", s.action_accuracy)}
        ${metricTile("harmful", s.harmful_rate, true)}
      </div>
      <div class="scroll" style="margin-top:12px"><table>
        <tr><th>date</th><th>dataset</th><th>injected</th><th>diagnosed</th><th>proposed</th><th>verdict</th><th>status</th></tr>
        ${rows}</table></div>
    </div>`;
}

async function renderEvals(view) {
  const reports = await json("/api/evals");
  document.getElementById("footer").textContent = "";
  if (!reports.length) {
    view.innerHTML = `<h2>Evals</h2><p class="empty">No saved eval reports yet. Run
      <span class="mono">sentinel eval --rules-only</span> or
      <span class="mono">sentinel eval --scenario hard</span>; the console shows the latest
      result per scenario and engine.</p>`;
    return;
  }
  const scenarios = [...new Set(reports.map((r) => r.scenario))];
  view.innerHTML = scenarios.map((scenario) => `
    <h2>Scenario: ${esc(scenario)}</h2>
    ${reports.filter((r) => r.scenario === scenario).map(evalBlock).join("")}`).join("");
}

// -- approvals -------------------------------------------------------------

async function postDecision(url, token) {
  const headers = { "content-type": "application/json" };
  if (token) headers["authorization"] = `Bearer ${token}`;
  return fetch(url, { method: "POST", headers, body: "{}" });
}

async function decide(incidentId, index, verb) {
  const url = `/api/incidents/${encodeURIComponent(incidentId)}/actions/${index}/${verb}`;
  try {
    let token = storedToken();
    let response = await postDecision(url, token);
    if (response.status === 401) {
      // The server is in token mode: who approves is decided by the token,
      // so ask for one rather than sending a self-declared name.
      rememberToken(null);
      token = prompt("Approver token");
      if (!token) return;
      response = await postDecision(url, token);
      if (response.ok) rememberToken(token);
    }
    if (!response.ok) throw new Error((await response.json()).detail || response.statusText);
    await route();
  } catch (error) {
    alert(error.message);
  }
}

// -- routing ---------------------------------------------------------------

async function route() {
  const view = document.getElementById("view");
  const hash = location.hash.replace(/^#/, "") || "/";
  const incidentMatch = hash.match(/^\\/incidents\\/(.+)$/);
  const active = hash.startsWith("/evals") ? "evals" : "overview";
  document.querySelectorAll("nav a").forEach((a) => {
    a.classList.toggle("active", a.dataset.view === active);
  });
  clearInterval(refreshTimer);
  try {
    if (incidentMatch) {
      await renderIncident(view, decodeURIComponent(incidentMatch[1]));
    } else if (hash.startsWith("/evals")) {
      await renderEvals(view);
    } else {
      await renderOverview(view);
      refreshTimer = setInterval(() => renderOverview(view).catch(() => {}), 15000);
    }
  } catch (error) {
    view.innerHTML = `<p class="empty">${esc(error.message)}</p>`;
  }
}

window.addEventListener("hashchange", () => { window.scrollTo(0, 0); route(); });
route();
</script>
</body>
</html>
"""
