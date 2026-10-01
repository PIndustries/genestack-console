// pages/environment_observe.js — Grafana-ish Observe tab for one environment.
// Plane stats, SVG area charts, and "now" pills. Auto-refresh ~20s.
// GET /api/v1/environments/{id}/observe?hours= ; on 404 compose from live APIs.
import { api, esc, fmtAge } from "../api.js";
import { applyLive, bindLiveEnv, live } from "./environment_live_state.js?v=ls5";

const REFRESH_MS = 20000;
const HOURS_OPTS = [1, 6, 24, 168];
const EMPTY_SERIES = "No samples yet — collector will fill this";
export const SERIES_META = [
  { name: "node.cpu.cores", title: "Node CPU", unit: "cores" },
  { name: "node.memory.bytes", title: "Node memory", unit: "bytes" },
  { name: "pod.cpu.cores", title: "Pod CPU", unit: "cores" },
  { name: "cluster.nodes.ready", title: "Nodes ready", unit: "" },
  { name: "cloud.servers.active", title: "Servers active", unit: "" },
];

let envIdGetter = null;
let onGoto = null;
let activeEnvId = "";
let hours = 24;
let refreshTimer = null;
let inflight = null;
let cache = null;

function observeActive() {
  const panel = document.querySelector('.tab-panel[data-panel="observe"]');
  return !!(panel && panel.classList.contains("active"));
}

function stopRefresh() {
  if (refreshTimer) {
    clearInterval(refreshTimer);
    refreshTimer = null;
  }
}

function startRefresh() {
  stopRefresh();
  refreshTimer = setInterval(() => {
    if (document.hidden || !observeActive()) return;
    const id = (envIdGetter && envIdGetter()) || activeEnvId;
    if (id) loadObserveCard(id, { silent: true });
  }, REFRESH_MS);
}

export function sparkline(el, points, opts = {}) {
  if (!el) return;
  const rows = Array.isArray(points) ? points : [];
  const vals = rows
    .map((p) => (typeof p === "number" ? p : Number(p && (p.v != null ? p.v : p.avg))))
    .filter((v) => Number.isFinite(v));
  if (!vals.length) {
    el.innerHTML = `<div class="ob-empty">${esc(EMPTY_SERIES)}</div>`;
    return;
  }
  const w = opts.width || 320;
  const h = opts.height || 72;
  const color = opts.color || "var(--accent)";
  let min = Math.min(...vals);
  let max = Math.max(...vals);
  if (min === max) {
    min = min > 0 ? min * 0.9 : min - 1;
    max = max + (max === 0 ? 1 : Math.abs(max) * 0.1);
  }
  const span = max - min || 1;
  const coords = vals.map((v, i) => {
    const x = vals.length === 1 ? w / 2 : (i / (vals.length - 1)) * w;
    const y = h - ((v - min) / span) * (h - 6) - 3;
    return [x.toFixed(2), y.toFixed(2)];
  });
  const line = coords.map((c, i) => `${i ? "L" : "M"}${c[0]},${c[1]}`).join(" ");
  const area =
    `M0,${h} ` + coords.map((c) => `L${c[0]},${c[1]}`).join(" ") + ` L${w},${h} Z`;
  const gid = `obg-${Math.random().toString(36).slice(2, 8)}`;
  el.innerHTML = `<svg class="ob-spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img">
    <defs>
      <linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="${color}" stop-opacity="0.35"/>
        <stop offset="100%" stop-color="${color}" stop-opacity="0.02"/>
      </linearGradient>
    </defs>
    <path d="${area}" fill="url(#${gid})"/>
    <path d="${line}" fill="none" stroke="${color}" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>
  </svg>`;
}

export function formatMetric(name, value) {
  if (value == null || value === "") return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  if (String(name).endsWith(".bytes")) {
    const gi = n / 1024 ** 3;
    if (gi >= 100) return `${gi.toFixed(0)} GiB`;
    if (gi >= 1) return `${gi.toFixed(1)} GiB`;
    return `${(n / 1024 ** 2).toFixed(0)} MiB`;
  }
  if (String(name).endsWith(".cores")) return n.toFixed(2);
  if (Math.abs(n - Math.round(n)) < 0.05) return String(Math.round(n));
  return n.toFixed(1);
}

function hoursLabel(h) {
  return h === 168 ? "7d" : `${h}h`;
}

function ratio(a, b) {
  if (a == null && (b == null || b === 0)) return "—";
  if (a == null) return `— / ${b}`;
  return `${a} / ${b ?? 0}`;
}

function pointsFromSeries(payload) {
  if (Array.isArray(payload)) {
    return payload
      .map((r) => {
        if (typeof r === "number") return { v: r };
        return { t: r && (r.t || r.bucket_start_iso), v: r && (r.v != null ? r.v : r.avg) };
      })
      .filter((p) => p.v != null && Number.isFinite(Number(p.v)));
  }
  return [];
}

function lastPoint(points) {
  if (!Array.isArray(points) || !points.length) return null;
  const p = points[points.length - 1];
  return typeof p === "number" ? p : p && (p.v != null ? p.v : p.avg);
}

function normalizeObserve(raw, envId, h) {
  const data = raw && typeof raw === "object" ? raw : {};
  const live = data.live && typeof data.live === "object" ? data.live : {};
  const talos = live.talos || {};
  const k8s = live.kubernetes || {};
  const os = live.openstack || {};
  const jobs = live.jobs || {};
  const alerts = live.alerts || {};
  const plane =
    data.plane && typeof data.plane === "object"
      ? data.plane
      : {
          talos: { reachable: talos.reachable, machines: talos.machines || 0 },
          kubernetes: { ready: k8s.ready || 0, nodes: k8s.nodes || 0 },
          openstack: { instances: os.servers ?? os.instances ?? 0 },
          jobs: { running: jobs.running || 0 },
          alerts: { firing: alerts.firing || 0 },
        };
  const series = {};
  const src = data.series && typeof data.series === "object" ? data.series : {};
  Object.keys(src).forEach((name) => {
    series[name] = pointsFromSeries(src[name]);
  });
  SERIES_META.forEach((m) => {
    if (!series[m.name]) series[m.name] = [];
  });
  const now =
    data.now && typeof data.now === "object"
      ? data.now
      : { problem_pods: 0, helm_failed: 0, volume_errors: 0, problems: [] };
  return {
    ...data,
    environment_id: data.environment_id || envId,
    hours: data.hours || h,
    plane,
    series,
    now,
  };
}

export async function fetchEnvObserve(envId, hoursArg) {
  const h = hoursArg || hours || 24;
  try {
    const raw = await api(
      `/api/v1/environments/${encodeURIComponent(envId)}/observe?hours=${encodeURIComponent(h)}`
    );
    return normalizeObserve(raw, envId, h);
  } catch (e) {
    if (!e || e.status !== 404) throw e;
    return normalizeObserve(await composeEnvObserve(envId, h), envId, h);
  }
}

async function composeEnvObserve(envId, h) {
  const base = `/api/v1/environments/${encodeURIComponent(envId)}`;
  const bucket = h <= 1 ? 5 : h <= 6 ? 15 : h <= 24 ? 30 : 180;
  const seriesReqs = SERIES_META.map((m) =>
    api(
      `${base}/metrics/series?name=${encodeURIComponent(m.name)}&hours=${h}&bucket_minutes=${bucket}`
    ).catch(() => ({ name: m.name, series: [] }))
  );
  const [platform, cluster, cloud, jobs, alerts, ...seriesRows] = await Promise.all([
    api(`${base}/platform`, { timeout: 25000 }).catch(() => null),
    api(`${base}/cluster`).catch(() => null),
    api(`${base}/cloud`).catch(() => null),
    api(`/api/v1/jobs?environment_id=${encodeURIComponent(envId)}&limit=100`).catch(() => []),
    api("/api/v1/alerts/summary").catch(() => null),
    ...seriesReqs,
  ]);
  const plane = {
    talos: { reachable: null, machines: 0 },
    kubernetes: { ready: 0, nodes: 0 },
    openstack: { instances: 0 },
    jobs: { running: 0 },
    alerts: { firing: 0 },
  };
  const clus = platform && typeof platform.cluster === "object" ? platform.cluster : {};
  const pNodes = Array.isArray(platform && platform.nodes) ? platform.nodes : [];
  plane.talos.machines = clus.machines != null ? clus.machines : pNodes.length;
  if (clus.talos_reachable != null) plane.talos.reachable = clus.talos_reachable;
  else plane.talos.reachable = pNodes.filter((n) => n && n.talos && n.talos.reachable).length;
  const kNodes = Array.isArray(cluster && cluster.nodes) ? cluster.nodes : [];
  if (kNodes.length) {
    plane.kubernetes.nodes = kNodes.length;
    plane.kubernetes.ready = kNodes.filter(
      (n) => String((n && n.status) || "").toLowerCase().replace(/\s+/g, "") === "ready"
    ).length;
  } else if (clus.ready != null) {
    plane.kubernetes.ready = clus.ready;
    plane.kubernetes.nodes = clus.machines || 0;
  }
  const servers = Array.isArray(cloud && cloud.servers) ? cloud.servers : [];
  plane.openstack.instances = servers.length;
  const jobList = Array.isArray(jobs) ? jobs : [];
  plane.jobs.running = jobList.filter(
    (j) => j && (j.status === "running" || j.status === "queued")
  ).length;
  if (alerts && alerts.by_environment && alerts.by_environment[envId] != null) {
    plane.alerts.firing = alerts.by_environment[envId];
  }
  const problems =
    cluster && cluster.pods && Array.isArray(cluster.pods.problems) ? cluster.pods.problems : [];
  const releases = Array.isArray(cluster && cluster.releases) ? cluster.releases : [];
  const volumes = Array.isArray(cloud && cloud.volumes) ? cloud.volumes : [];
  const series = {};
  SERIES_META.forEach((m, i) => {
    series[m.name] = pointsFromSeries(seriesRows[i] && seriesRows[i].series);
  });
  return {
    environment_id: envId,
    hours: h,
    generated_at: new Date().toISOString(),
    composed: true,
    plane,
    series,
    now: {
      problem_pods: problems.length,
      helm_failed: releases.filter(
        (r) => r && String(r.status || "").toLowerCase() !== "deployed"
      ).length,
      volume_errors: volumes.filter((v) => /error/i.test(String((v && v.status) || ""))).length,
      problems,
    },
  };
}

function statTile({ kicker, value, sub, kind, goto: dest }) {
  const cls = kind ? ` ob-stat-${kind}` : "";
  const go = dest ? ` data-ob-goto="${esc(dest)}"` : "";
  const tag = dest ? "button" : "div";
  const type = dest ? ' type="button"' : "";
  return `<${tag} class="ob-stat${cls}"${type}${go}>
    <div class="ob-stat-k">${esc(kicker)}</div>
    <div class="ob-stat-v">${esc(value)}</div>
    ${sub ? `<div class="ob-stat-s">${esc(sub)}</div>` : ""}
  </${tag}>`;
}

function planeHtml(data) {
  const p = (data && data.plane) || {};
  const talos = p.talos || {};
  const k8s = p.kubernetes || {};
  const os = p.openstack || {};
  const jobs = p.jobs || {};
  const alerts = p.alerts || {};
  const machines = talos.machines || 0;
  const reach = talos.reachable;
  const talosOk = reach != null && machines > 0 && reach === machines;
  const talosWarn = reach != null && reach > 0 && reach < machines;
  const kReady = k8s.ready || 0;
  const kNodes = k8s.nodes || 0;
  const kOk = kNodes > 0 && kReady === kNodes;
  const kWarn = kReady > 0 && kReady < kNodes;
  const firing = alerts.firing || 0;
  const running = jobs.running || 0;
  return `
    <div class="ob-row-title">Plane</div>
    <div class="ob-plane">
      ${statTile({
        kicker: "Talos",
        value: ratio(reach, machines),
        sub: "reachable / machines",
        kind: talosOk ? "ok" : talosWarn ? "warn" : machines ? "danger" : "",
        goto: "machines",
      })}
      ${statTile({
        kicker: "Kubernetes",
        value: ratio(kReady, kNodes),
        sub: "ready / nodes",
        kind: kOk ? "ok" : kWarn ? "warn" : kNodes ? "danger" : "",
        goto: "kubernetes",
      })}
      ${statTile({
        kicker: "OpenStack",
        value: String(os.instances ?? 0),
        sub: "instances",
        goto: "openstack",
      })}
      ${statTile({
        kicker: "Jobs",
        value: String(running),
        sub: "running",
        kind: running ? "warn" : "ok",
      })}
      ${statTile({
        kicker: "Alerts",
        value: String(firing),
        sub: "firing",
        kind: firing ? "danger" : "ok",
      })}
    </div>`;
}

function chartPanelsHtml() {
  return SERIES_META.map(
    (m) => `<div class="ob-panel" data-ob-chart="${esc(m.name)}">
      <div class="ob-panel-h">
        <span>${esc(m.title)}</span>
        <span class="ob-panel-val muted" data-ob-last="${esc(m.name)}">—</span>
      </div>
      <div class="ob-spark-host" data-ob-series="${esc(m.name)}">
        <div class="ob-empty">${esc(EMPTY_SERIES)}</div>
      </div>
    </div>`
  ).join("");
}

function paintSeries(data) {
  const series = (data && data.series) || {};
  const known = new Set(SERIES_META.map((m) => m.name));
  SERIES_META.forEach((m) => {
    const pts = pointsFromSeries(series[m.name] || []);
    sparkline(document.querySelector(`[data-ob-series="${m.name}"]`), pts);
    const last = document.querySelector(`[data-ob-last="${m.name}"]`);
    if (last) last.textContent = formatMetric(m.name, lastPoint(pts));
  });
  Object.keys(series).forEach((name) => {
    if (known.has(name)) return;
    const host = document.querySelector(`[data-ob-series="${CSS.escape(name)}"]`);
    if (host) sparkline(host, pointsFromSeries(series[name] || []));
  });
}

function nowHtml(data) {
  const n = (data && data.now) || {};
  const pods = n.problem_pods || 0;
  const helm = n.helm_failed || 0;
  const vols = n.volume_errors || 0;
  const problems = Array.isArray(n.problems) ? n.problems.slice(0, 8) : [];
  const pill = (count, label, dest, kind) => {
    const cls = count ? `ob-now-pill ${kind}` : "ob-now-pill";
    return `<button type="button" class="${cls}" data-ob-goto="${esc(dest)}">
      <strong>${esc(count)}</strong> ${esc(label)}
    </button>`;
  };
  const rows = problems
    .map((p) => {
      const ns = p && p.namespace ? `${p.namespace}/` : "";
      const name = (p && p.name) || "—";
      const reason = (p && (p.reason || p.status)) || "";
      return `<tr><td><code>${esc(ns + name)}</code></td><td class="muted">${esc(reason)}</td></tr>`;
    })
    .join("");
  return `
    <div class="ob-row-title">Now</div>
    <div class="ob-now">
      ${pill(pods, pods === 1 ? "problem pod" : "problem pods", "kubernetes", "warn")}
      ${pill(helm, "helm failed", "kubernetes", "danger")}
      ${pill(vols, vols === 1 ? "volume error" : "volume errors", "openstack", "danger")}
    </div>
    ${
      rows
        ? `<table class="ob-now-table"><thead><tr><th>Pod</th><th>Reason</th></tr></thead><tbody>${rows}</tbody></table>`
        : `<div class="muted ob-now-ok">No problem pods reported.</div>`
    }`;
}

function renderDash(data) {
  const plane = document.getElementById("ob-plane");
  const now = document.getElementById("ob-now");
  const msg = document.getElementById("ob-msg");
  if (plane) plane.innerHTML = planeHtml(data);
  if (now) now.innerHTML = nowHtml(data);
  paintSeries(data);
  if (msg) {
    const age = data && data.generated_at ? fmtAge(data.generated_at) : "";
    msg.textContent = age ? `updated ${age}` : "";
  }
  document.querySelectorAll("[data-ob-hours]").forEach((btn) => {
    btn.classList.toggle("active", Number(btn.dataset.obHours) === hours);
  });
}

export function observeCardHtml() {
  const range = HOURS_OPTS.map(
    (h) =>
      `<button type="button" class="ob-range${h === hours ? " active" : ""}" data-ob-hours="${h}">${hoursLabel(h)}</button>`
  ).join("");
  return `
  <div class="ob-dash" id="ob-card">
    <div class="ob-toolbar">
      <div>
        <div class="ob-kicker">Observe</div>
        <h2 class="ob-title">Environment reports</h2>
      </div>
      <div class="ob-range-bar" role="group" aria-label="Time range">${range}</div>
      <span class="muted" id="ob-msg"></span>
      <button class="secondary btn-sm" id="ob-refresh" type="button">Refresh</button>
    </div>
    <div id="ob-plane">
      <div class="ob-row-title">Plane</div>
      <div class="ob-plane">${statTile({ kicker: "…", value: "—", sub: "loading" })}</div>
    </div>
    <div class="ob-row-title">Time series</div>
    <div class="ob-charts" id="ob-charts">${chartPanelsHtml()}</div>
    <div id="ob-now">
      <div class="ob-row-title">Now</div>
      <div class="muted">Loading live counts…</div>
    </div>
  </div>`;
}

export function wireObserveCard(getEnvId, opts = {}) {
  envIdGetter = typeof getEnvId === "function" ? getEnvId : null;
  onGoto = opts && typeof opts.onGoto === "function" ? opts.onGoto : null;
  const card = document.getElementById("ob-card");
  if (card && !card.dataset.wired) {
    card.dataset.wired = "1";
    card.addEventListener("click", (e) => {
      const rangeBtn = e.target.closest("[data-ob-hours]");
      if (rangeBtn) {
        const next = Number(rangeBtn.dataset.obHours);
        if (HOURS_OPTS.includes(next) && next !== hours) {
          hours = next;
          const id = (envIdGetter && envIdGetter()) || activeEnvId;
          if (id) loadObserveCard(id);
        }
        return;
      }
      const go = e.target.closest("[data-ob-goto]");
      if (go && onGoto) onGoto(go.dataset.obGoto);
    });
  }
  const refresh = document.getElementById("ob-refresh");
  if (refresh && !refresh.dataset.wired) {
    refresh.dataset.wired = "1";
    refresh.addEventListener("click", () => {
      const id = (envIdGetter && envIdGetter()) || activeEnvId;
      if (id) loadObserveCard(id);
    });
  }
  startRefresh();
}

export async function loadObserveCard(envId, { silent = false } = {}) {
  activeEnvId = envId || "";
  const msg = document.getElementById("ob-msg");
  const plane = document.getElementById("ob-plane");
  if (!plane) return;
  if (!activeEnvId) {
    plane.innerHTML = '<div class="muted">Select an environment.</div>';
    const now = document.getElementById("ob-now");
    if (now) now.innerHTML = "";
    return;
  }
  if (!silent && msg) msg.textContent = "Loading…";
  if (inflight) {
    try {
      await inflight;
    } catch {
      /* continue */
    }
  }
  inflight = fetchEnvObserve(activeEnvId, hours);
  let data;
  try {
    data = await inflight;
  } catch (e) {
    if (activeEnvId !== envId) return;
    if (live.observe && live.envId === envId) {
      cache = live.observe;
      if (msg) msg.textContent = "";
      renderDash(cache);
      return;
    }
    if (msg) msg.textContent = "";
    plane.innerHTML = `<div class="error">${esc(e.message || "Observe unavailable")}</div>`;
    return;
  } finally {
    inflight = null;
  }
  if (activeEnvId !== envId) return;
  cache = data && typeof data === "object" ? data : {};
  bindLiveEnv(activeEnvId);
  applyLive({ observe: cache });
  renderDash(cache);
}

export function destroyObserveCard() {
  stopRefresh();
  envIdGetter = null;
  onGoto = null;
  activeEnvId = "";
  inflight = null;
  cache = null;
}
