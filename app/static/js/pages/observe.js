// pages/observe.js — fleet Observe: one Grafana-ish card per environment.
// GET /api/v1/fleet/observe ; on 404 compose from /fleet, /fleet/live, jobs, alerts.
import { api, esc, fmtAge } from "../api.js";
import { applyTenantFilter } from "./tenant.js";
import { setBreadcrumbs } from "../components/breadcrumbs.js";

export const title = "Observe";

const REFRESH_MS = 20000;

let refreshTimer = null;
let onTenantChange = null;

export function destroy() {
  if (refreshTimer) {
    clearInterval(refreshTimer);
    refreshTimer = null;
  }
  if (onTenantChange) {
    window.removeEventListener("tenantchange", onTenantChange);
    onTenantChange = null;
  }
}

function envHref(id) {
  return `#/environment_detail/${encodeURIComponent(id)}?tab=observe`;
}

function ratio(a, b) {
  if (a == null && (b == null || b === 0)) return "—";
  if (a == null) return `— / ${b}`;
  return `${a} / ${b ?? 0}`;
}

function healthClass(health) {
  const h = String(health || "").toLowerCase();
  if (h === "healthy") return "ok";
  if (h === "degraded") return "warn";
  if (h === "down") return "bad";
  return "";
}

function pill(count, label, kind) {
  const cls = count ? kind : "";
  return `<span class="pill ${cls}">${esc(count)} ${esc(label)}</span>`;
}

function cardHtml(row) {
  const id = (row && (row.environment_id || row.id)) || "";
  const name = (row && row.name) || id || "(unnamed)";
  const live = (row && row.live) || {};
  const p = (row && row.plane) || {
    talos: live.talos || {},
    kubernetes: live.kubernetes || {},
    openstack: { instances: (live.openstack && live.openstack.servers) || 0 },
    jobs: live.jobs || {},
    alerts: live.alerts || {},
  };
  const talos = p.talos || {};
  const k8s = p.kubernetes || {};
  const os = p.openstack || {};
  const jobs = p.jobs || {};
  const alerts = p.alerts || {};
  const now = (row && row.now) || {};
  const health = (row && row.health) || (live.kubernetes && live.kubernetes.health) || "unknown";
  const chips = [];
  if (row && row.region) chips.push(`<span class="chip">${esc(row.region)}</span>`);
  if (row && row.tier) chips.push(`<span class="chip">${esc(row.tier)}</span>`);
  if (row && row.tenant_name) chips.push(`<span class="chip">${esc(row.tenant_name)}</span>`);
  return `<a class="ob-fleet-card" href="${envHref(id)}">
    <div class="ob-fleet-head">
      <div>
        <div class="ob-kicker">Environment</div>
        <div class="ob-fleet-name">${esc(name)}</div>
      </div>
      <span class="pill ${healthClass(health)}">${esc(health)}</span>
    </div>
    <div class="ob-fleet-chips">${chips.join("")}</div>
    <div class="ob-fleet-stats">
      <div><span class="ob-stat-k">Talos</span><strong>${esc(ratio(talos.reachable, talos.machines))}</strong></div>
      <div><span class="ob-stat-k">K8s</span><strong>${esc(ratio(k8s.ready, k8s.nodes))}</strong></div>
      <div><span class="ob-stat-k">Instances</span><strong>${esc(os.instances ?? 0)}</strong></div>
      <div><span class="ob-stat-k">Jobs</span><strong>${esc(jobs.running ?? 0)}</strong></div>
      <div><span class="ob-stat-k">Alerts</span><strong>${esc(alerts.firing ?? 0)}</strong></div>
    </div>
    <div class="ob-fleet-now">
      ${pill(now.problem_pods || 0, "problem pods", "warn")}
      ${pill(now.helm_failed || 0, "helm failed", "bad")}
      ${pill(now.volume_errors || 0, "volume errors", "bad")}
    </div>
  </a>`;
}

async function fetchFleetObserve() {
  try {
    return await api("/api/v1/fleet/observe");
  } catch (e) {
    if (!e || e.status !== 404) throw e;
    return composeFleetObserve();
  }
}

async function composeFleetObserve() {
  const [fleet, live, jobs, alerts] = await Promise.all([
    api("/api/v1/fleet").catch(() => ({ environments: [] })),
    api("/api/v1/fleet/live").catch(() => []),
    api("/api/v1/jobs?limit=200").catch(() => []),
    api("/api/v1/alerts/summary").catch(() => ({ firing: 0, by_environment: {} })),
  ]);
  const liveMap = new Map(
    (Array.isArray(live) ? live : [])
      .filter((e) => e && e.environment_id)
      .map((e) => [e.environment_id, e])
  );
  const jobList = Array.isArray(jobs) ? jobs : [];
  const jobsByEnv = {};
  jobList.forEach((j) => {
    if (!j || !j.environment_id) return;
    if (j.status !== "running" && j.status !== "queued") return;
    jobsByEnv[j.environment_id] = (jobsByEnv[j.environment_id] || 0) + 1;
  });
  const firing = (alerts && alerts.by_environment) || {};
  const envs = Array.isArray(fleet && fleet.environments) ? fleet.environments : [];
  return {
    generated_at: new Date().toISOString(),
    composed: true,
    environments: envs.map((env) => {
      const id = env && env.id ? env.id : "";
      const row = liveMap.get(id) || {};
      const summary = row.summary && typeof row.summary === "object" ? row.summary : {};
      const ready = summary.nodes_ready || 0;
      const total = summary.nodes_total || 0;
      const crash = Array.isArray(summary.crashlooping) ? summary.crashlooping.length : 0;
      const failed = summary.pods_failed || 0;
      return {
        environment_id: id,
        id,
        name: env.name,
        region: env.region,
        tier: env.tier,
        tenant_id: env.tenant_id,
        tenant_name: env.tenant_name,
        health: row.health || "unknown",
        plane: {
          talos: { reachable: null, machines: total },
          kubernetes: { ready, nodes: total },
          openstack: { instances: 0 },
          jobs: { running: jobsByEnv[id] || 0 },
          alerts: { firing: firing[id] || 0 },
        },
        now: {
          problem_pods: crash + failed,
          helm_failed: 0,
          volume_errors: 0,
        },
      };
    }),
  };
}

function renderList(data) {
  const list = document.getElementById("ob-fleet-list");
  const msg = document.getElementById("ob-fleet-msg");
  if (!list) return;
  const rows = applyTenantFilter(
    (Array.isArray(data && data.environments) ? data.environments : []).map((r) => ({
      ...r,
      id: r.environment_id || r.id,
      tenant_id: r.tenant_id,
    }))
  );
  if (!rows.length) {
    list.innerHTML = `<div class="ob-empty-cta">
      <h3>No environments yet</h3>
      <p class="muted">Guided setup: metal, Talos, Kubernetes, OpenStack — then Observe fills from the collector.</p>
      <a class="btn-sm" href="#/setup">Guided setup →</a>
    </div>`;
  } else {
    list.innerHTML = rows.map(cardHtml).join("");
  }
  if (msg) {
    const age = data && data.generated_at ? fmtAge(data.generated_at) : "";
    msg.textContent = age ? `updated ${age}` : "";
  }
}

async function loadBoard({ background = false } = {}) {
  const list = document.getElementById("ob-fleet-list");
  const err = document.getElementById("ob-fleet-err");
  if (!list || !err) return;
  if (!background) err.innerHTML = "";
  let data;
  try {
    data = await fetchFleetObserve();
  } catch (e) {
    let note = e.message || "unavailable";
    if (e.isNetwork || e.isTimeout) note = "Console server unreachable.";
    err.innerHTML = `<div class="error">Observe unavailable: ${esc(note)}</div>`;
    if (!background) list.innerHTML = `<div class="muted">Could not load fleet Observe.</div>`;
    return;
  }
  err.innerHTML = "";
  renderList(data);
}

export async function render(root) {
  destroy();
  setBreadcrumbs([{ label: "Observe" }]);
  root.innerHTML = `
  <div class="ob-dash ob-fleet">
    <div class="ob-toolbar">
      <div>
        <div class="ob-kicker">Fleet</div>
        <h2 class="ob-title">Observe</h2>
      </div>
      <span class="muted" id="ob-fleet-msg"></span>
      <button class="secondary btn-sm" id="ob-fleet-refresh" type="button">Refresh</button>
    </div>
    <p class="muted ob-lead">Live plane across every environment. Open a card for time series and problem pods.</p>
    <div id="ob-fleet-err"></div>
    <div id="ob-fleet-list" class="ob-fleet-grid"><div class="muted">Loading…</div></div>
  </div>`;
  document.getElementById("ob-fleet-refresh").addEventListener("click", () => loadBoard());
  await loadBoard();
  refreshTimer = setInterval(() => {
    if (!document.hidden) loadBoard({ background: true });
  }, REFRESH_MS);
  onTenantChange = () => loadBoard();
  window.addEventListener("tenantchange", onTenantChange);
}
