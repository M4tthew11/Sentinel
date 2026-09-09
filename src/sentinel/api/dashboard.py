"""A single-file operations dashboard.

No build step, no CDN, no framework: the page is served as one string and talks
to the same JSON API documented at ``/docs``. That keeps the repository runnable
from a clone, which for a demo is worth more than a front-end toolchain.
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
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: #f6f7f9; --panel: #ffffff; --line: #e2e5ea; --text: #14161a;
      --muted: #5b6472; --accent: #1f6feb;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  header {
    padding: 22px 28px; border-bottom: 1px solid var(--line);
    display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap;
  }
  h1 { font-size: 19px; margin: 0; letter-spacing: -0.01em; }
  .sub { color: var(--muted); font-size: 13px; }
  main { padding: 24px 28px; max-width: 1180px; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 14px; }
  .tile {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 10px; padding: 14px 16px;
  }
  .tile .label { color: var(--muted); font-size: 12px; text-transform: uppercase;
    letter-spacing: 0.06em; }
  .tile .value { font-size: 26px; font-weight: 600; margin-top: 4px; }
  h2 { font-size: 15px; margin: 30px 0 12px; letter-spacing: 0.02em; }
  .incident {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 10px; padding: 14px 16px; margin-bottom: 12px;
  }
  .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .pill {
    font-size: 11px; padding: 2px 9px; border-radius: 999px;
    border: 1px solid var(--line); color: var(--muted);
  }
  .pill.critical { color: var(--critical); border-color: var(--critical); }
  .pill.warning { color: var(--warning); border-color: var(--warning); }
  .pill.resolved { color: var(--ok); border-color: var(--ok); }
  .pill.awaiting_approval { color: var(--pending); border-color: var(--pending); }
  .id { font-family: ui-monospace, "SFMono-Regular", Consolas, monospace; font-size: 12px;
    color: var(--muted); }
  .title { font-weight: 600; margin: 8px 0 2px; }
  .summary { color: var(--muted); font-size: 14px; }
  .actions { margin-top: 10px; display: grid; gap: 6px; }
  .action {
    display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
    font-size: 13px; padding: 7px 10px; border: 1px solid var(--line); border-radius: 8px;
  }
  .action .name { font-family: ui-monospace, Consolas, monospace; }
  .action .result { color: var(--muted); }
  button {
    font: inherit; font-size: 13px; padding: 4px 12px; border-radius: 7px;
    border: 1px solid var(--accent); background: transparent; color: var(--accent);
    cursor: pointer;
  }
  button.reject { border-color: var(--muted); color: var(--muted); }
  button:hover { background: color-mix(in srgb, var(--accent) 12%, transparent); }
  .empty { color: var(--muted); }
  footer { padding: 20px 28px; color: var(--muted); font-size: 13px; }
  a { color: var(--accent); }
</style>
</head>
<body>
<header>
  <h1>Sentinel</h1>
  <span class="sub">self-healing data platform &mdash; incident console</span>
  <span class="sub" style="margin-left:auto"><a href="/docs">API docs</a></span>
</header>
<main>
  <div class="tiles" id="tiles"></div>
  <h2>Incidents</h2>
  <div id="incidents"><p class="empty">Loading&hellip;</p></div>
</main>
<footer id="footer"></footer>

<script>
const STATUS_CLASS = {
  resolved: "resolved", awaiting_approval: "awaiting_approval",
  critical: "critical", warning: "warning"
};

async function json(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error((await response.json()).detail || response.statusText);
  return response.json();
}

function tile(label, value) {
  return `<div class="tile"><div class="label">${label}</div>
          <div class="value">${value}</div></div>`;
}

function renderTiles(summary) {
  const byStatus = summary.incidents_by_status || {};
  document.getElementById("tiles").innerHTML = [
    tile("incidents", summary.incidents_total),
    tile("resolved", byStatus.resolved || 0),
    tile("awaiting approval", byStatus.awaiting_approval || 0),
    tile("dates held", (summary.active_holds || []).length),
    tile("upstream tickets", summary.tickets)
  ].join("");
  const run = summary.latest_run;
  document.getElementById("footer").textContent = run
    ? `Latest pipeline run ${run.run_id} for ${run.business_date} (${run.status}).`
    : "No pipeline runs recorded yet.";
}

function renderAction(incidentId, action) {
  const pending = action.status === "awaiting_approval";
  const buttons = pending
    ? `<button onclick="decide('${incidentId}', ${action.index}, 'approve')">Approve</button>
       <button class="reject"
               onclick="decide('${incidentId}', ${action.index}, 'reject')">Reject</button>`
    : "";
  return `<div class="action">
      <span class="name">${action.name}</span>
      <span class="pill ${STATUS_CLASS[action.status] || ""}">${action.status}</span>
      <span class="pill">risk: ${action.risk}</span>
      <span class="result">${action.result || action.rationale || ""}</span>
      <span style="margin-left:auto">${buttons}</span>
    </div>`;
}

function renderIncident(incident) {
  const confidence = incident.confidence == null
    ? "" : ` &middot; confidence ${Math.round(incident.confidence * 100)}%`;
  const diagnosis = incident.root_cause
    ? `<div class="summary"><strong>${incident.root_cause}</strong>${confidence}
       &middot; ${incident.engine}<br>${incident.summary}</div>`
    : `<div class="summary">Not diagnosed yet.</div>`;
  return `<div class="incident">
      <div class="row">
        <span class="id">${incident.incident_id}</span>
        <span class="pill">${incident.business_date}</span>
        <span class="pill">${incident.dataset}</span>
        <span class="pill ${STATUS_CLASS[incident.severity] || ""}">${incident.severity}</span>
        <span class="pill ${STATUS_CLASS[incident.status] || ""}">${incident.status}</span>
      </div>
      <div class="title">${incident.title}</div>
      ${diagnosis}
      <div class="actions">
        ${incident.actions.map((a) => renderAction(incident.incident_id, a)).join("")}
      </div>
    </div>`;
}

async function decide(incidentId, index, verb) {
  try {
    await json(`/api/incidents/${incidentId}/actions/${index}/${verb}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ approver: "dashboard-operator" })
    });
    await refresh();
  } catch (error) {
    alert(error.message);
  }
}

async function refresh() {
  try {
    const [summary, incidents] = await Promise.all([
      json("/api/summary"), json("/api/incidents?limit=100")
    ]);
    renderTiles(summary);
    document.getElementById("incidents").innerHTML = incidents.length
      ? incidents.map(renderIncident).join("")
      : '<p class="empty">No incidents on record.</p>';
  } catch (error) {
    document.getElementById("incidents").innerHTML =
      `<p class="empty">${error.message}</p>`;
  }
}

refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>
"""
