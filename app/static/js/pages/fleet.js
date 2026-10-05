// pages/fleet.js — fleet status board: every environment, its lifecycle
// position, and the single next action. Landing page after login. Overlays a
// live health pill per environment from /api/v1/fleet/live, kept current via
// the "fleet" SSE topic; the 15s board refresh stays as fallback when the
// stream is down. The dashboard stats render inline as a compact top bar.
import { api, esc, fmtAge, toast } from "../api.js";
import { store, canAdmin, gate } from "../store.js";
import { connect } from "../stream.js";
import { applyTenantFilter, currentTenantId } from "./tenant.js";
import { setBreadcrumbs } from "../components/breadcrumbs.js";

export const title = "Environments";

const REFRESH_MS = 15000;
const STALE_MS = 5 * 60 * 1000; // telemetry older than 5min is flagged stale
const STEPS = ["connect", "inventory", "config", "push", "deploy", "operate"];

function detailHref(id, step) {
  const q = new URLSearchParams();
  if (step === "inventory") {
    q.set("tab", "platform");
    q.set("ptab", "machines");
  } else if (step === "config") {
    q.set("tab", "settings");
  }
  const qs = q.toString();
  return `#/environment_detail/${encodeURIComponent(id)}${qs ? "?" + qs : ""}`;
}

let refreshTimer = null;
let onTenantChange = null;
let liveMap = new Map(); // environment_id -> /api/v1/fleet/live entry
let streamHandle = null;
let sseLive = false;
let statsCache = null; // cached stats from dashboard module for the top bar

export function destroy() {
  if (refreshTimer) {
    clearInterval(refreshTimer);
    refreshTimer = null;
  }
  if (onTenantChange) {
    window.removeEventListener("tenantchange", onTenantChange);
    onTenantChange = null;
  }
  if (streamHandle) {
    streamHandle.close();
    streamHandle = null;
  }
  sseLive = false;
  statsCache = null;
}

// Fetch dashboard stats once and cache them for the compact top bar. The
// dashboard module is still available for the full view if needed later.
async function loadStatsBar() {
  const bar = document.getElementById("fl-stats-bar");
  if (!bar) return;

  try {
    const [envs, svcStatus] = await Promise.all([
      api("/api/v1/fleet").catch(() => null),
      api("/api/v1/genestack/services/status").catch(() => null),
    ]);

    const envList = Array.isArray(envs && envs.environments) ? envs.environments : [];
    const total = envList.length;

    let healthy = 0;
    let deployed = 0;
    let alert = 0;

    envList.forEach((env) => {
      const live = liveMap.get(env.id);
      const h = live && live.health ? String(live.health).toLowerCase() : "";
      if (h === "healthy") healthy++;
      else if (h === "degraded" || h === "down") alert++;
    });

    if (svcStatus && svcStatus.reachable === true) {
      const rels = Array.isArray(svcStatus.releases) ? svcStatus.releases : [];
      deployed = rels.filter((r) => String(r.status || "").toLowerCase() === "deployed").length;
    }

    statsCache = { total, healthy, deployed, alert };
    renderStatsBar(statsCache);
  } catch {
    renderStatsBar(null);
  }
}

function renderStatsBar(stats) {
  const bar = document.getElementById("fl-stats-bar");
  if (!bar) return;

  if (!stats) {
    bar.innerHTML = `<span class="muted">Fleet stats unavailable</span>`;
    return;
  }

  bar.innerHTML = `
    <span>Fleet: <strong>${stats.total}</strong> envs</span>
    <span>Healthy: <strong class="ok">${stats.healthy}</strong></span>
    <span>Deployed: <strong>${stats.deployed}</strong></span>
    <span>Alert: <strong class="bad">${stats.alert}</strong></span>`;
}

export async function render(root) {
  destroy();
  setBreadcrumbs([{ label: "Environments" }]);

  root.innerHTML = `
  <div class="card" id="fl-stats-bar">
    <span class="muted">Loading fleet stats…</span>
  </div>
  <div class="card">
    <div class="toolbar">
      <h2>Fleet</h2>
      <input id="fl-search" type="search" placeholder="search envs…" style="max-width:12rem" />
      <span class="pill" id="fl-tenant">All tenants</span>
      <button class="btn-sm" id="btn-fl-new-env" type="button">+ New environment</button>
      <a class="secondary btn-sm" href="#/observe">Observe</a>
      <button class="secondary btn-sm" id="btn-fl-refresh" type="button">Refresh</button>
      <span class="muted" id="fl-updated" style="font-size:.78rem"></span>
    </div>
    <div id="fl-err"></div>
    <div id="fl-list" class="grid"></div>
  </div>`;

  document.getElementById("btn-fl-new-env").addEventListener("click", () => {
    location.hash = "#/setup";
  });
  document.getElementById("btn-fl-refresh").addEventListener("click", () => loadFleet());
  document.getElementById("fl-list").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-fl-delete]");
    if (!btn || btn.disabled) return;
    e.preventDefault();
    e.stopPropagation();
    deleteFleetEnv(btn.getAttribute("data-fl-delete") || "", btn.getAttribute("data-fl-name") || "");
  });

  // Client-side search filter
  const searchInput = document.getElementById("fl-search");
  searchInput.addEventListener("input", () => {
    const q = searchInput.value.trim().toLowerCase();
    const cards = document.querySelectorAll(".fl-env-card");
    cards.forEach((card) => {
      const text = card.textContent.toLowerCase();
      card.style.display = (!q || text.includes(q)) ? "" : "none";
    });
  });

  skeleton();
  await loadFleet();
  await loadStatsBar();

  // Live health pills over SSE; the interval below is the fallback for when
  // EventSource is missing or the stream keeps failing.
  streamHandle = connect(["fleet"], {
    fleet: onFleetEvent,
    onState: (state) => {
      sseLive = state === "open";
    },
  });

  // Auto-refresh while the page is mounted; skip ticks while the tab is hidden
  // or the SSE stream is healthy.
  refreshTimer = setInterval(() => {
    if (!document.hidden && !sseLive) loadFleet({ background: true });
  }, REFRESH_MS);

  onTenantChange = () => {
    loadFleet();
    loadStatsBar();
  };
  window.addEventListener("tenantchange", onTenantChange);
}

function skeleton() {
  const list = document.getElementById("fl-list");
  if (!list) return;
  list.innerHTML = Array.from({ length: 2 })
    .map(
      () => `<div class="card span-6 fl-env-card fl-skeleton">
        <div class="toolbar">
          <div class="fl-sk" style="width:8rem"></div>
          <div class="fl-sk" style="width:6rem"></div>
        </div>
        <div style="display:flex;gap:1rem;margin:.3rem 0;font-size:.8rem">
          <div class="fl-sk" style="width:7rem"></div>
          <div class="fl-sk" style="width:6rem"></div>
        </div>
        <div class="fl-sk" style="width:100%"></div>
      </div>`
    )
    .join("");
}

async function loadFleet({ background = false } = {}) {
  const list = document.getElementById("fl-list");
  const err = document.getElementById("fl-err");
  if (!list || !err) return; // page was unloaded mid-flight
  if (!background) err.innerHTML = "";

  // Live telemetry rides along but never blocks the board.
  const livePromise = api("/api/v1/fleet/live").catch(() => null);

  let data;
  try {
    data = await api("/api/v1/fleet");
  } catch (e) {
    if (background && list.querySelector(".fl-env-card:not(.fl-skeleton)")) {
      let msg = e.message;
      if (e.isNetwork || e.isTimeout) msg = "Server unreachable. Check your connection.";
      err.innerHTML = `<div class="error">Refresh failed: ${esc(msg)}</div>`;
      return;
    }
    skeleton();
    let msg = e.message;
    if (e.isNetwork || e.isTimeout) msg = "Console server unreachable. Make sure the backend is running.";
    err.innerHTML = `<div class="error">Fleet status unavailable: ${esc(msg)}
      <button class="secondary btn-sm" id="btn-fl-retry" type="button" style="margin-left:.5rem">Retry</button></div>`;
    document.getElementById("btn-fl-retry").addEventListener("click", () => loadFleet());
    return;
  }

  const live = await livePromise;
  liveMap = new Map(
    (Array.isArray(live) ? live : [])
      .filter((e) => e && e.environment_id)
      .map((e) => [e.environment_id, e])
  );

  const envs = applyTenantFilter(Array.isArray(data && data.environments) ? data.environments : []);
  renderHeader(data, envs.length);
  err.innerHTML = "";

  if (!envs.length) {
    if (currentTenantId()) {
      list.innerHTML = `<div class="fl-empty muted" style="grid-column:span 12">No environments in the selected tenant.</div>`;
    } else {
      list.innerHTML = `<div class="fl-empty fl-empty-cta" style="grid-column:span 12">
        <h3>No environments yet</h3>
        <p class="muted">Guided setup: add machines by hostname and IP. A Talos ISO, or Ubuntu that is already installed, is enough. A management port is optional.</p>
        <a class="fl-next fl-cta" href="#/setup">Guided setup →</a>
      </div>`;
    }
  } else {
    const showTenant = !currentTenantId();
    const consoleLogs = await consoleDefaultLogs();
    // Render cards immediately without waiting for workflow step fetches.
    // Fire-and-forget the workflow calls so the page renders fast, then update
    // individual CTA labels when each response arrives.
    const cardPromises = envs.map((env) => renderFleetCard(env, showTenant, consoleLogs));
    const list = document.getElementById("fl-list");
    if (!list) return;
    list.innerHTML = (await Promise.all(cardPromises)).join("");
    // Update CTA labels from the current_step already present on each /fleet row.
    envs.forEach((env) => {
      updateActionLabel(env, list);
    });
  }

  const updated = document.getElementById("fl-updated");
  if (updated) updated.textContent = "updated " + new Date().toLocaleTimeString();

  // Refresh stats bar once fleet data is loaded (liveMap is now populated)
  loadStatsBar();
}

function renderHeader(data, count) {
  const pill = document.getElementById("fl-tenant");
  if (pill) {
    const tid = currentTenantId();
    let name = "All tenants";
    if (tid) {
      const fromFleet = data && Array.isArray(data.tenants) ? data.tenants.find((t) => t && t.id === tid) : null;
      const fromStore = Array.isArray(store.tenants) ? store.tenants.find((t) => t && t.id === tid) : null;
      name = (fromFleet && fromFleet.name) || (fromStore && fromStore.name) || "Selected tenant";
    }
    pill.textContent = name;
  }
}

// Inherited dry_run (null) follows the console default. One /health per board
// render when the topbar has not stored it; a missing payload counts as logging.
async function consoleDefaultLogs() {
  let health = store.health;
  if (!health) {
    try {
      health = await api("/health");
      store.health = health;
    } catch {
      health = null;
    }
  }
  return health ? !!health.dry_run : true;
}

function dryRunPillHtml(env, consoleLogs) {
  const own = env ? env.dry_run : null;
  const logs = own == null ? consoleLogs : own === true;
  return logs
    ? `<span class="pill warn">dry-run</span>`
    : `<span class="pill ok">applies</span>`;
}

function renderFleetCard(env, showTenant, consoleLogs) {
  const id = env && env.id ? env.id : "";
  const name = (env && env.name) || id || "(unnamed)";
  const steps = env && env.steps && typeof env.steps === "object" ? env.steps : {};
  const cur = env && STEPS.includes(env.current_step) ? env.current_step : null;

  const healthPill = healthPillHtml(liveMap.get(id), cur);
  const stepLabel = cur || "operate";

  let chips = "";
  if (showTenant && env.tenant_name) chips += `<span class="chip">${esc(env.tenant_name)}</span>`;

  const complete = !cur;
  const lastAction = getLastActionLabel(env);

  const deploy = env && env.deploy && typeof env.deploy === "object" ? env.deploy : null;
  const status = deploy ? String(deploy.status || "").toLowerCase() : "";
  let actionHtml_str;
  if (status === "queued" || status === "running") {
    const c = deploy.stages_completed;
    const t = deploy.stages_total;
    const hint = c != null && t != null ? `<span class="fl-next-hint muted">stage ${c}/${t}</span>` : "";
    actionHtml_str = `<a class="btn-sm" href="${detailHref(id)}">Watch →</a>${hint}`;
  } else {
    actionHtml_str = `<a class="btn-sm fl-action" data-env-id="${esc(id)}" href="${detailHref(id, cur)}">Open →</a>`;
  }

  return `<div class="card span-6 fl-env-card${complete ? " fl-row-complete" : ""}" data-env="${esc(id)}">
    <div class="toolbar">
      <a href="${detailHref(id)}" style="color:inherit;text-decoration:none;font-weight:600">${esc(name)}</a>${agentDotHtml(env)}
      <span class="pill ok">${esc(stepLabel)}</span>
      ${chips}
    </div>
    <div style="display:flex; gap:1rem; margin:.3rem 0; font-size:.8rem">
      ${env.region ? `<span class="muted">Region: <code>${esc(env.region)}</code></span>` : ""}
      ${env.tier ? `<span class="muted">Tier: <code>${esc(env.tier)}</code></span>` : ""}
      ${dryRunPillHtml(env, consoleLogs)}
    </div>
    <div style="display:flex; gap:.5rem; align-items:center; flex-wrap:wrap">
      ${healthPill}
      <span class="muted" style="font-size:.78rem">${lastAction}</span>
      <span style="flex:1"></span>
      <button class="danger btn-sm" type="button" data-fl-delete="${esc(id)}" data-fl-name="${esc(name)}" ${gate(canAdmin(), "admin")}>Delete</button>
      ${actionHtml_str}
    </div>
  </div>`;
}

// Update the CTA label from the /fleet row's current_step (derived server-side
// in app/services/fleet.py; the per-env workflow payload has no such field).
function updateActionLabel(env, list) {
  const id = env && env.id ? env.id : "";
  if (!id) return;
  const currentStep = env.current_step || null;
  const labels = {
    connect: "Set up connection →",
    inventory: "Add servers →",
    config: "Edit config →",
    push: "Push files →",
    deploy: "Deploy →",
    operate: "View cluster →",
  };
  const label = (currentStep && labels[currentStep]) || "Open →";
  const btn = list.querySelector(`.fl-action[data-env-id="${id}"]`);
  if (btn) {
    btn.textContent = label;
    btn.href = detailHref(id, currentStep);
  }
}

async function deleteFleetEnv(id, name) {
  if (!id) return;
  const label = name || id;
  if (!window.confirm(`Delete environment '${label}'?\n\nThis cannot be undone.`)) return;
  try {
    await api(`/api/v1/environments/${encodeURIComponent(id)}`, { method: "DELETE" });
    toast(`Environment '${label}' deleted`, "ok");
    await loadFleet();
    await loadStatsBar();
  } catch (e) {
    toast(`Delete failed: ${e.message}`, "bad");
  }
}

function getLastActionLabel(env) {
  const deploy = env && env.deploy && typeof env.deploy === "object" ? env.deploy : null;
  if (deploy) {
    const status = String(deploy.status || "").toLowerCase();
    if (status === "queued" || status === "running") return `deploy ${status}`;
    if (status === "success" || status === "ok") return "deployed";
    if (status === "failed" || status === "error") return "deploy failed";
  }
  const cur = env && STEPS.includes(env.current_step) ? env.current_step : null;
  if (cur) return `step: ${cur}`;
  return "complete";
}

// The card's action link — always opens the environment detail page.
// (renderFleetCard now returns a generic "Open →" link and updateActionLabel
//  patches the label after the workflow fetch returns.)

// ---------- live health overlay ----------

function agentDotHtml(env) {
  if (!env || typeof env !== "object") return "";
  let enrolled = null;
  let connected = null;
  const a = env.agent && typeof env.agent === "object" ? env.agent : null;
  if (a) {
    if (typeof a.enrolled === "boolean") enrolled = a.enrolled;
    if (typeof a.connected === "boolean") connected = a.connected;
  }
  // Legacy shape: a boolean agent_connected with no enrollment state.
  if (connected === null && typeof env.agent_connected === "boolean") {
    connected = env.agent_connected;
    enrolled = enrolled ?? true;
  }
  if (enrolled === null || connected === null) return "";
  // green: connected · amber: enrolled but offline · grey: not enrolled
  const cls = connected ? "ok" : enrolled ? "warn" : "off";
  const tip = [
    connected ? "agent connected" : enrolled ? "agent enrolled — offline" : "no agent enrolled",
  ];
  return `<span class="fl-agent-dot ${cls}" title="${esc(tip.join(" · "))}"></span>`;
}

function isStale(takenAt) {
  if (!takenAt) return false;
  const t = String(takenAt);
  const ts = Date.parse(/Z$|[+-]\d{2}:?\d{2}$/.test(t) ? t : t + "Z");
  return !Number.isNaN(ts) && Date.now() - ts > STALE_MS;
}

function healthPillHtml(live, curStep) {
  if (!live || !live.health) {
    if (curStep === "connect") {
      return '<span class="pill">not deployed</span>';
    }
    return '<span class="pill fl-health" title="no telemetry yet">health: unknown</span>';
  }
  const health = String(live.health || "unknown").toLowerCase();
  const cls = health === "healthy" ? "ok" : health === "degraded" ? "warn" : health === "down" ? "bad" : "";
  const s = live.summary && typeof live.summary === "object" ? live.summary : {};
  const crash = Array.isArray(s.crashlooping) ? s.crashlooping.length : 0;
  const tip = [
    `nodes ${s.nodes_ready ?? "?"}/${s.nodes_total ?? "?"} ready`,
    `pods ${s.pods_running ?? 0}/${s.pods_pending ?? 0}/${s.pods_failed ?? 0}`,
    `crashlooping: ${crash}`,
  ];
  const age = fmtAge(live.taken_at);
  if (age) tip.push(`probed ${age}`);
  const stale = isStale(live.taken_at);
  if (stale) tip.push("(stale)");
  if (live.drifted === true) tip.push("drift");
  if (live.probe_ok === false && live.error) tip.push(String(live.error));
  return `<span class="pill ${cls} fl-health" title="${esc(tip.join(" · "))}">health: ${esc(health)}${stale ? " ⚠" : ""}</span>`;
}

// SSE fleet events: merge into liveMap and re-render the pill on the card.
function onFleetEvent(payload) {
  if (!payload || !payload.environment_id) return;
  const id = String(payload.environment_id);
  const next = Object.assign({}, liveMap.get(id) || {}, payload);
  liveMap.set(id, next);
  const card = document.querySelector(`.fl-env-card[data-env="${CSS.escape(id)}"]`);
  if (!card) return;
  const pill = card.querySelector(".fl-health");
  if (pill) pill.outerHTML = healthPillHtml(next);
  // Also update stats bar counts reactively
  if (statsCache) loadStatsBar();
}
