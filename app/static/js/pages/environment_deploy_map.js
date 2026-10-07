// Overview as an xyflow-style canvas: HTML card nodes, bezier edges,
// dotted grid, pan/zoom, controls, minimap. Not Cytoscape.
import { api, esc, loadingHtml, skeletonHtml, toast } from "../api.js";
import { canAdmin, canRun, envName as envNameById, gate } from "../store.js";
import { connect } from "../stream.js";
import {
  mountSpace3d,
  setSpace3dEnabled,
  isSpace3dEnabled,
  pushPacket,
  resizeSpace3d,
  destroySpace3d,
  fitSpace3d,
  zoomSpace3d,
} from "./environment_space3d.js?v=ls16";
import {
  mountHoneycomb,
  setHoneycombEnabled,
  isHoneycombEnabled,
  resizeHoneycomb,
  destroyHoneycomb,
  honeycombReset,
  stageHoneycomb,
  clearHoneycombStage,
} from "./environment_honeycomb.js?v=ls16";
import { mergeSnapshot, applyLive, bindLiveEnv } from "./environment_live_state.js?v=ls7";

const POLL_MS = 8000;
const LIVE_POLL_MS = 3500;
const IDLE_MS = 20000;
const SNAP_TIMEOUT = 8000;
const PODS_TIMEOUT = 12000;
const PODS_REFRESH_MS = 20000;
const LIVE_PODS_MS = 4000;
const ACTIVE = new Set(["queued", "running"]);
const COL = 300;
const GAP_Y = 14;
const ROW_GAP = 48;
const SIZE = {
  env: [268, 148],
  group: [280, 160],
  registry: [280, 148],
  regcache: [260, 56],
  tests: [280, 148],
  testsuite: [260, 72],
  machine: [276, 156],
  k8s: [260, 132],
  ns: [248, 128],
  pod: [260, 132],
  vm: [232, 64],
  overlay: [280, 148],
  vpc: [248, 96],
  net: [248, 96],
  subnet: [232, 64],
  router: [232, 72],
  tenant: [248, 96],
  edge: [280, 148],
  ingress: [248, 96],
  fip: [220, 56],
  lb: [232, 72],
  svc: [232, 64],
  gw: [248, 96],
  route: [248, 96],
  min: [200, 36],
};
const METRICS_MS = 1000;
const HIST_MAX = 60;
const LIVE_SMOOTH_S = 0.32;
const FEED_MAX = 80;
const FEED_DEDUP_MS = 12000;

const KIND_LABEL = {
  env: "Environment",
  group: "Infrastructure",
  registry: "Image registry",
  regcache: "Mirror",
  tests: "Validation",
  testsuite: "Test suite",
  machine: "Machine",
  k8s: "Kubernetes node",
  ns: "Namespace",
  pod: "Container",
  vm: "Instance",
  overlay: "Overlay networks",
  vpc: "VPC",
  net: "Provider net",
  subnet: "Subnet",
  router: "Router",
  tenant: "Tenant",
  edge: "Edge / ingress",
  ingress: "Ingress",
  fip: "Floating IP",
  lb: "Load balancer",
  svc: "Service",
  gw: "Gateway",
  route: "HTTPRoute",
  osrole: "OpenStack role",
  more: "More",
};

const HEALTH_WORD = {
  ok: "healthy",
  run: "in progress",
  warn: "needs attention",
  bad: "blocked",
  wait: "idle",
};

let envId = "";
let envName = "";
let catalog = [];
let pipe = null;
let job = null;
let recentJobs = [];
let snap = null;
let registryGen = 0;
let cacheCardSig = "";
let platform = null;
let inventoryServers = [];
let workloads = null;
let workloadsError = "";
let osVms = [];
let osCloud = null;
let k8sIngress = [];
let k8sServices = [];
let k8sGateways = [];
let k8sRoutes = [];
let k8sPools = [];
let bmLive = [];
let selected = "infra";
let mapFocus = "";
let nodeModal = null;
let hostEscBound = false;
let bmNodesCache = { at: 0, nodes: [] };
let nodeLogTimer = null;
let popDismissed = "";
let collapsed = new Set();
let minimized = new Set();
let ignoreFoldUntil = 0;
let autoReveal = false;
let userCollapsed = new Set();
let lastLiveRefresh = 0;
let fetchInflight = false;
let timer = null;
let stream = null;
let lastFetch = 0;
let lastInsp = "";
let lastNext = "";
let lastIds = "";
let feed = [];
let lastGraph = { nodes: [], edges: [], byId: new Map(), podsByHost: new Map() };
let view = { x: 48, y: 36, k: 0.85 };
let workloadsAt = 0;
let podsInflight = false;
let dragging = false;
let dragStart = { x: 0, y: 0, vx: 0, vy: 0 };
let minimapDrag = false;
let wiredFlow = false;
let resizeObs = null;
let liveMetrics = null;
let shownLive = null;
let liveHist = { cluster: [], nodes: {}, pods: {} };
let metricsTimer = null;
let metricsInflight = false;
let metricsMissing = false;
let liveRaf = 0;
let liveRafLast = 0;
let lastSparkAt = 0;
let space3dOn = false;
let nestOn = false;
let liveMetalHosts = new Map();
let pendingPulse = "";
let lastMetalKey = "";
let lastMetalAt = 0;

const LAYER_COPY = {
  env: "This environment. Registry is this env’s image cache on Console; Infrastructure is the metal.",
  registry: "This environment’s pull-through registry on Console. Nodes pull from here instead of the internet. Warm it before a greenfield.",
  tests: "Functional proof. Helm up is not enough — Tempest and genestack verify say whether the cloud actually works.",
};

function cacheKey(id) {
  return `gsc-overview:${id}`;
}

function readCache(id) {
  try {
    const raw = sessionStorage.getItem(cacheKey(id));
    if (!raw) return null;
    const d = JSON.parse(raw);
    if (!d || typeof d !== "object") return null;
    return d;
  } catch {
    return null;
  }
}

function writeCache() {
  if (!envId) return;
  try {
    sessionStorage.setItem(
      cacheKey(envId),
      JSON.stringify({
        ts: Date.now(),
        envName,
        snap,
        pipe,
        catalog,
        workloads,
        platform,
        inventoryServers,
        osVms,
      })
    );
  } catch {
    /* quota */
  }
}

function applyCache(d) {
  if (!d) return;
  if (d.envName) envName = d.envName;
  if (d.snap) snap = d.snap;
  if (d.pipe) {
    // Never resurrect a cached Stopped banner — job status is live.
    pipe = { ...d.pipe, failed_at: null, running: false, can_continue: false };
  }
  if (Array.isArray(d.catalog) && d.catalog.length) catalog = d.catalog;
  if (d.workloads) workloads = d.workloads;
  if (d.platform) platform = d.platform;
  if (Array.isArray(d.inventoryServers)) inventoryServers = d.inventoryServers;
  if (Array.isArray(d.osVms)) osVms = d.osVms;
  if (d.osCloud && (d.osCloud.available || (d.osCloud.networks || []).length)) osCloud = d.osCloud;
  if (Array.isArray(d.k8sIngress)) k8sIngress = d.k8sIngress;
  if (Array.isArray(d.k8sServices)) k8sServices = d.k8sServices;
  if (Array.isArray(d.k8sGateways)) k8sGateways = d.k8sGateways;
  if (Array.isArray(d.k8sRoutes)) k8sRoutes = d.k8sRoutes;
  if (Array.isArray(d.k8sPools)) k8sPools = d.k8sPools;
}

function failedAtFromJob(j) {
  if (!j || String(j.status || "") !== "failed") return "";
  const err = String(j.error || "");
  const m = /stage ['"]([^'"]+)['"](?: item ['"]([^'"]+)['"])?/i.exec(err);
  if (m) return m[2] ? `${m[1]}/${m[2]}` : m[1];
  return err.slice(0, 80);
}

function isDeployOp(j) {
  return /genestack\.(deploy|greenfield|tempest|verify)|registry\.mirror/i.test(
    String((j && j.operation) || "")
  );
}

function applyRecentJobs(rows) {
  const list = Array.isArray(rows)
    ? rows
    : Array.isArray(rows && rows.jobs)
      ? rows.jobs
      : Array.isArray(rows && rows.items)
        ? rows.items
        : [];
  if (!list.length) return;
  recentJobs = list;
  const deploy =
    list.find((j) => isDeployOp(j) && ACTIVE.has(String(j.status || ""))) ||
    list.find((j) => isDeployOp(j)) ||
    list[0] ||
    null;
  if (!deploy) return;
  const keepLog = job && job.id === deploy.id ? job.log_text || "" : "";
  job = {
    id: deploy.id,
    status: deploy.status,
    log_text: keepLog,
    operation: deploy.operation,
    error: deploy.error,
  };
  if (!pipe) pipe = {};
  const st = String(deploy.status || "");
  const op = String(deploy.operation || "");
  pipe.running = ACTIVE.has(st);
  if (pipe.running) {
    pipe.failed_at = null;
    pipe.can_continue = false;
  } else if (st === "failed") {
    pipe.failed_at = failedAtFromJob(deploy) || pipe.failed_at || null;
    pipe.can_continue = true;
    if (/greenfield/i.test(op) && metalFailedAt(deploy)) {
      pipe.next_stage = "hosts";
    }
  } else {
    pipe.failed_at = null;
  }
}

function metalFailedAt(j) {
  const err = String((j && (j.error || j.failed_at)) || "");
  const at = String((pipe && pipe.failed_at) || "");
  return /maintenance|PXE|iso|greenfield\/(pxe|maintenance)|timed out waiting/i.test(`${err} ${at}`);
}

function applyWorkloads(wl) {
  if (!wl || wl.ok === false) {
    if (!workloads) {
      workloads = { pods: [] };
      workloadsError = (wl && (wl.error || wl.message)) || "containers unavailable";
    }
    return;
  }
  const incoming = Array.isArray(wl.pods) ? wl.pods : [];
  const prev = (workloads && Array.isArray(workloads.pods) && workloads.pods) || [];
  if (!incoming.length && prev.length) return;
  workloads = wl;
  workloadsError = wl.error ? String(wl.error) : "";
}

function setText(el, text) {
  const next = String(text || "");
  if (el && el.textContent !== next) el.textContent = next;
}

function stripLogPrefix(line) {
  return String(line || "").replace(/^\[\d{4}-\d{2}-\d{2}T[^\]]+\]\s*/, "");
}

function prettyApiLine(payload) {
  const method = String(payload.method || "");
  const status = payload.status;
  let path = String(payload.path || "").replace(/^\/api\/v1/, "");
  const ops = /\/ops\//.test(String(payload.path || ""));
  const who = ops ? "agent" : "console";
  let what = `${method} ${path}`;
  if (method === "POST" && /\/jobs$/.test(path)) what = "queued a job";
  else if (method === "POST" && /\/deploy$/.test(path)) what = "deploy OpenStack";
  else if (method === "POST" && /\/jobs\/[^/]+\/cancel/.test(path)) what = "cancel job";
  else if (/ensure-longhorn-labels/.test(path)) what = "repair Longhorn labels";
  else if (/gc-stale-pods/.test(path)) what = "gc stale pods";
  else if (method === "DELETE" && /\/k8s\/pods\//.test(path)) what = "delete pod";
  else if (method === "POST" && /\/k8s\/nodes\/[^/]+\/(cordon|uncordon|drain)/.test(path)) {
    const a = path.match(/\/(cordon|uncordon|drain)/);
    what = a ? a[1] + " node" : what;
  }
  return `${who} ${what} → ${status}`;
}

function classifyLiveLine(text) {
  const s = String(text || "");
  if (/old OS|k8s Ready|did not stick/i.test(s)) return "iso";
  if (/virtual CD|iso_boot|iLO disc|inserting http|remounting iLO/i.test(s)) return "iso";
  if (/DHCP|TFTP|PXE|pxe_boot|not coming through|iPXE|network boot/i.test(s)) return "pxe";
  if (/Talos maintenance|talos-ready|maintenance API ready/i.test(s)) return "boot";
  if (/ForceOff|ForceOn|POST /i.test(s)) return "boot";
  return "";
}

function packetAnchor(id) {
  if (id && lastGraph.byId && lastGraph.byId.has(id)) return id;
  if (lastGraph.byId && lastGraph.byId.has("registry")) return "registry";
  if (lastGraph.byId && lastGraph.byId.has("infra")) return "infra";
  return lastGraph.byId && lastGraph.byId.has("env") ? "env" : "";
}

function machineIdFor(name) {
  const raw = String(name || "").trim();
  if (!raw || raw === "-") return "";
  if (lastGraph.byId && lastGraph.byId.has(raw)) return raw;
  const mid = raw.startsWith("m:") ? raw : `m:${raw}`;
  if (lastGraph.byId && lastGraph.byId.has(mid)) return mid;
  const want = raw.toLowerCase();
  const short = shortName(raw).toLowerCase();
  for (const n of lastGraph.nodes || []) {
    if (n.kind !== "machine") continue;
    const full = String(n.machineName || n.title || "").toLowerCase();
    if (full === want || shortName(full) === short) return n.id;
  }
  for (const m of machineRows()) {
    if (!m || !m.name) continue;
    const full = String(m.name).toLowerCase();
    if (full === want || shortName(full) === short) return `m:${m.name}`;
  }
  return "";
}

function hostFromLine(text, explicit) {
  const given = String(explicit || "").trim();
  if (given && given !== "-" && !/^\d{1,3}(?:\.\d{1,3}){3}$/.test(given)) {
    const id = machineIdFor(given);
    return { name: (id && lastGraph.byId.get(id) && lastGraph.byId.get(id).machineName) || given, id: id || `m:${given}` };
  }
  const s = String(text || "");
  const tagged =
    s.match(/(?:iso_boot|pxe_boot)\s+node=([A-Za-z0-9._-]+)/i) ||
    s.match(/\bnode=([A-Za-z0-9._-]+)/i) ||
    s.match(/\[greenfield\]\s+([A-Za-z0-9._-]+)/) ||
    s.match(/\bDHCP\s+([A-Za-z0-9._-]{3,})/i) ||
    s.match(/^([A-Za-z0-9._-]+):\s+(?:PXE|inserting|old OS|Talos)/i) ||
    s.match(/^([A-Za-z0-9._-]+)\s+(?:in Talos|old OS|booted the old OS)/i);
  if (tagged) {
    const name = tagged[1];
    if (!/^(DHCP|TFTP|HTTP|iLO|PXE|ISO|node)$/i.test(name)) {
      const id = machineIdFor(name);
      return { name, id: id || `m:${name}` };
    }
  }
  for (const n of lastGraph.nodes || []) {
    if (n.kind !== "machine" || !n.machineName) continue;
    const full = String(n.machineName);
    const short = shortName(full);
    if (full && s.includes(full)) return { name: full, id: n.id };
    if (short && short.length > 3 && s.includes(short)) return { name: full, id: n.id };
  }
  const serial = s.match(/\b(\d{6,}-[A-Za-z0-9._-]+)\b/);
  if (serial) {
    const name = serial[1];
    return { name, id: machineIdFor(name) || `m:${name}` };
  }
  return null;
}

function livePacketFor(text, kind, host) {
  if (kind !== "iso" && kind !== "pxe" && kind !== "boot" && kind !== "net") return;
  const hit = hostFromLine(text, host);
  const known = hit && hit.id && lastGraph.byId && lastGraph.byId.has(hit.id) ? hit.id : "";
  const fromReg = packetAnchor("registry");
  const infra = packetAnchor("infra");
  const dest = packetAnchor(known) || infra || fromReg;
  const src = fromReg || infra || packetAnchor("env");
  if (!src || !dest) return;
  if (kind === "iso" || kind === "pxe") {
    pushPacket(src, dest === src ? infra || dest : dest, kind);
  } else if (kind === "boot") {
    pushPacket(infra || src, known || fromReg || dest, "boot");
  } else {
    pushPacket(known || src, known ? src : dest, kind || "net");
  }
}

function noteLiveHost(name, kind, message) {
  const hit = hostFromLine(message, name);
  const host = (hit && hit.name) || String(name || "").trim();
  if (!host) return "";
  let bm = "booting";
  if (kind === "boot" || /Talos maintenance|talos-ready|maintenance API/i.test(String(message || ""))) {
    bm = "talos-ready";
  }
  liveMetalHosts.set(host, { kind, bm_state: bm, at: Date.now(), message: String(message || "") });
  const idx = bmLive.findIndex((n) => n && n.name === host);
  if (idx >= 0) bmLive[idx] = { ...bmLive[idx], state: bm };
  else bmLive = bmLive.concat([{ name: host, state: bm }]);
  return hit && hit.id ? hit.id : machineIdFor(host);
}

function pulseHost(idOrName) {
  const id = String(idOrName || "").startsWith("m:") ? idOrName : machineIdFor(idOrName);
  if (!id || !lastGraph.byId || !lastGraph.byId.has(id)) return;
  if (selected !== id) {
    selected = id;
    lastInsp = "";
    document.querySelectorAll(".sf-card.sel").forEach((el) => el.classList.remove("sel"));
    const card = document.querySelector(`[data-sf-id="${CSS.escape(id)}"]`);
    if (card) card.classList.add("sel");
    renderInspector();
    renderMinimap();
  }
  const card = document.querySelector(`[data-sf-id="${CSS.escape(id)}"]`);
  if (!card) return;
  card.classList.remove("pulse");
  void card.offsetWidth;
  card.classList.add("pulse");
  window.setTimeout(() => card.classList.remove("pulse"), 1600);
}

function resetFeed() {
  feed = [];
  const el = document.getElementById("dm-feed");
  if (!el) return;
  el.innerHTML = `<div class="dm-feed-row api dm-feed-wait">waiting for DHCP, iLO, jobs, and API…</div>`;
}

function pushFeed(kind, line, host) {
  const text = String(line || "").trim();
  if (!text) return "";
  const typed = kind === "job" || kind === "api" ? classifyLiveLine(text) || kind : kind;
  const now = Date.now();
  if (feed.some((f) => f.kind === typed && f.line === text && now - f.t < FEED_DEDUP_MS)) return "";
  feed.push({ t: now, kind: typed, line: text });
  if (feed.length > FEED_MAX) feed = feed.slice(-FEED_MAX);
  const el = document.getElementById("dm-feed");
  if (el) {
    const wait = el.querySelector(".dm-feed-wait");
    if (wait) wait.remove();
    const row = document.createElement("div");
    row.className = `dm-feed-row ${typed}`;
    const ts = new Date(now).toLocaleTimeString();
    row.innerHTML = `<span class="dm-feed-ts">${esc(ts)}</span> ${esc(text)}`;
    el.appendChild(row);
    while (el.childElementCount > FEED_MAX) el.removeChild(el.firstChild);
    el.scrollTop = el.scrollHeight;
  }
  livePacketFor(text, typed, host);
  const hit = hostFromLine(text, host);
  if (typed === "pxe" || typed === "iso" || typed === "boot") {
    if (hit || host) {
      const mid = noteLiveHost(host || (hit && hit.name), typed, text);
      if (mid) pendingPulse = mid;
      return mid || (hit && hit.id) || "";
    }
    return "";
  }
  if (hit && hit.id && lastGraph.byId && lastGraph.byId.has(hit.id)) pendingPulse = hit.id;
  return (hit && hit.id) || "";
}

function ingestJobLines(text) {
  const lines = String(text || "").split("\n");
  const classified = [];
  let lastPlain = "";
  for (const raw of lines) {
    const line = stripLogPrefix(raw).trim();
    if (!line) continue;
    lastPlain = line;
    if (classifyLiveLine(line)) classified.push(line);
  }
  const out = classified.length ? classified.slice(-12) : lastPlain ? [lastPlain] : [];
  let lastHost = "";
  for (const line of out) lastHost = pushFeed("job", line.slice(0, 180)) || lastHost;
  return lastHost;
}

function forThisEnv(payload) {
  if (!payload) return false;
  if (!payload.environment_id) return true;
  return payload.environment_id === envId;
}

function overlayOn() {
  return space3dOn || nestOn;
}

function applyOverlayChrome() {
  const world = document.getElementById("dm-world");
  if (world) {
    world.hidden = overlayOn();
    world.style.visibility = overlayOn() ? "hidden" : "";
    world.style.pointerEvents = overlayOn() ? "none" : "";
  }
  const mini = document.getElementById("dm-minimap");
  if (mini) mini.hidden = overlayOn();
  const flow = document.getElementById("dm-flow");
  if (flow) {
    flow.classList.toggle("space3d", space3dOn);
    flow.classList.toggle("nest", nestOn);
  }
  const hud3 = document.querySelector("#dm-flow .sf-space3d-hud");
  if (hud3) hud3.hidden = !space3dOn;
  const hudN = document.querySelector("#dm-flow .sf-nest-hud");
  if (hudN) hudN.hidden = !nestOn;
  const b3 = document.getElementById("dm-space3d-toggle");
  if (b3) b3.classList.toggle("active", space3dOn);
  const bn = document.getElementById("dm-nest-toggle");
  if (bn) bn.classList.toggle("active", nestOn);
  if (!nestOn) applyNestConsoleStage("");
  else positionNestConsole();
}

function applySpace3dView(on) {
  const turningOn = !!on && !space3dOn;
  if (on && nestOn) {
    nestOn = false;
    setHoneycombEnabled(false);
  }
  space3dOn = !!on;
  applyOverlayChrome();
  if (turningOn) renderAll();
  setSpace3dEnabled(space3dOn);
  if (space3dOn) resizeSpace3d();
}

function applyNestView(on) {
  const turningOn = !!on && !nestOn;
  if (on && space3dOn) {
    space3dOn = false;
    setSpace3dEnabled(false);
  }
  nestOn = !!on;
  applyOverlayChrome();
  if (turningOn) {
    honeycombReset();
    renderAll();
  }
  setHoneycombEnabled(nestOn);
  if (nestOn) {
    resizeHoneycomb();
    if (nodeModal) applyNestConsoleStage(selected);
    else positionNestConsole();
  }
}

function flushLiveUi() {
  const pulse = pendingPulse;
  pendingPulse = "";
  renderAll();
  if (pulse) pulseHost(pulse);
}

function roleList(roles) {
  if (Array.isArray(roles)) return roles.map((r) => String(r).toLowerCase());
  if (typeof roles === "string") {
    return roles.split(/[,\s]+/).map((r) => r.toLowerCase()).filter(Boolean);
  }
  return [];
}

function shortName(name) {
  const s = String(name || "");
  const m = s.match(/^[0-9]+-(.+)$/);
  return m ? m[1] : s;
}

function stageState(id) {
  const stages = pipe && Array.isArray(pipe.stages) ? pipe.stages : [];
  const st = stages.find((s) => s && s.id === id);
  return String((st && st.state) || "pending");
}

function stageSpec(id) {
  return (catalog || []).find((s) => s && s.id === id) || {};
}

function mapStage(st) {
  if (st === "done") return "ok";
  if (st === "failed") return "bad";
  if (st === "running") return "run";
  return "wait";
}

function osInstalled() {
  return stageState("core") === "done" && stageState("compute-network") === "done";
}

function normHost(v) {
  return String(v || "").trim().toLowerCase();
}

function hostIp(n) {
  return String((n && (n.private_ip || n.ip || n.public_ip)) || "").trim();
}

function bootSaysUbuntu(node) {
  const next = String((node && node.next_boot) || "");
  const stage = String((node && node.boot_stage) || "");
  return next === "ubuntu" || stage === "ubuntu";
}

function mergeFleetRows() {
  const byName = new Map();
  const ipToName = new Map();
  const remember = (row) => {
    const key = normHost(row && row.name);
    if (!key) return;
    const prev = byName.get(key);
    if (!prev) byName.set(key, { ...row, name: row.name });
    else Object.assign(prev, row, { name: prev.name });
    const ip = hostIp(byName.get(key));
    if (ip) ipToName.set(ip, key);
  };
  const findKey = (n) => {
    const key = normHost(n && n.name);
    if (byName.has(key)) return key;
    const ip = hostIp(n);
    if (ip && ipToName.has(ip)) return ipToName.get(ip);
    return "";
  };
  for (const s of inventoryServers) {
    if (!s || s.assigned === false) continue;
    const name = String(s.hostname || "").trim();
    if (!name) continue;
    remember({
      name,
      private_ip: s.private_ip || s.ip || "",
      public_ip: s.public_ip || "",
      roles: Array.isArray(s.roles) ? s.roles : [],
      os: "",
      source: s.source || "",
    });
  }
  for (const n of bmLive) {
    if (!n || !n.name) continue;
    const key = findKey(n);
    if (!key) continue;
    const prev = byName.get(key);
    if (bootSaysUbuntu(n)) prev.os = "ubuntu";
    if (n.state && !prev.bm_state) prev.bm_state = n.state;
  }
  const overlay = (n) => {
    if (!n || !n.name) return;
    const key = findKey(n);
    if (!key) {
      remember({ ...n });
      return;
    }
    const prev = byName.get(key);
    const patch = { ...n };
    delete patch.name;
    if (!roleList(n.roles).length) delete patch.roles;
    if (!patch.os) delete patch.os;
    if (!patch.private_ip) delete patch.private_ip;
    if (!patch.public_ip) delete patch.public_ip;
    Object.assign(prev, patch);
    const ip = hostIp(prev);
    if (ip) ipToName.set(ip, key);
  };
  const fromSnap = (snap && Array.isArray(snap.nodes) ? snap.nodes : []) || [];
  const fromPlat = platform && Array.isArray(platform.nodes) ? platform.nodes : [];
  for (const n of fromSnap) overlay(n);
  for (const n of fromPlat) overlay(n);
  let rows = [...byName.values()];
  if (!rows.length) {
    rows = (fromPlat.length ? fromPlat : fromSnap).map((n) => ({ ...n }));
  }
  for (const row of rows) {
    if (row && !row.os) row.os = "talos";
  }
  return rows;
}

function machineRows() {
  const fromSnap = (snap && Array.isArray(snap.nodes) ? snap.nodes : []) || [];
  const pxeBy = new Map();
  const bmBy = new Map();
  for (const n of fromSnap) {
    if (n && n.name && n.pxe) pxeBy.set(n.name, n.pxe);
    if (n && n.name && n.bm_state) bmBy.set(n.name, n.bm_state);
  }
  for (const h of ((snap && snap.pxe && snap.pxe.hosts) || [])) {
    if (h && h.name && !pxeBy.has(h.name)) pxeBy.set(h.name, h);
  }
  for (const n of bmLive) {
    if (!n || !n.name) continue;
    if (n.state) bmBy.set(n.name, n.state);
  }
  const now = Date.now();
  for (const [name, row] of liveMetalHosts) {
    if (!name || !row) continue;
    if (now - (row.at || 0) > 90000) {
      liveMetalHosts.delete(name);
      continue;
    }
    if (!bmBy.has(name) || String(bmBy.get(name)).toLowerCase() === "talos-ready") {
      bmBy.set(name, row.bm_state || "booting");
    }
  }
  const logBoot = bootingFromLog();
  for (const name of logBoot) {
    if (!bmBy.has(name) || String(bmBy.get(name)).toLowerCase() === "talos-ready") bmBy.set(name, "booting");
  }
  const rows = mergeFleetRows();
  if (!pxeBy.size && !bmBy.size) return rows;
  return rows.map((n) => {
    if (!n) return n;
    const extra = {};
    if (pxeBy.has(n.name)) extra.pxe = pxeBy.get(n.name);
    const bm = bmBy.get(n.name);
    if (bm && (!n.bm_state || String(n.bm_state).toLowerCase() === "talos-ready")) extra.bm_state = bm;
    else if (!n.bm_state && bm) extra.bm_state = bm;
    return Object.keys(extra).length ? { ...n, ...extra } : n;
  });
}

function machineState(m) {
  const roles = roleList(m.roles);
  const ip = m.private_ip || m.public_ip;
  const tal = m.talos && typeof m.talos === "object" ? m.talos : {};
  const kn = m.kubernetes && typeof m.kubernetes === "object" ? m.kubernetes : {};
  const k8sStatus = kn.status || m.kubernetes;
  const k8sReady = String(k8sStatus || "").toLowerCase() === "ready";
  const talosOk = tal.reachable === true || !!m.talos_reachable;
  const configured = !!(ip && roles.length);
  const osRoles = roles.filter((r) =>
    ["control", "compute", "network", "storage", "storage-ceph", "storage-cinder"].includes(r)
  );
  const coreDone = stageState("core") === "done";
  const computeDone = stageState("compute-network") === "done";
  let osState = "wait";
  if (!k8sReady) osState = "wait";
  else if (osRoles.includes("compute") && !computeDone) osState = coreDone ? "run" : "wait";
  else if (osRoles.length && !coreDone) osState = "wait";
  else if (osRoles.length && coreDone) osState = "ok";
  else osState = coreDone ? "ok" : "wait";
  const bits = [
    configured ? "ok" : "wait",
    talosOk ? "ok" : "wait",
    k8sReady ? "ok" : talosOk ? "run" : "wait",
    osState,
  ];
  let roll = "wait";
  if (bits.every((b) => b === "ok")) roll = "ok";
  else if (bits.includes("bad")) roll = "bad";
  else if (bits.includes("run") || bits.includes("ok")) roll = "run";
  return {
    configured,
    talosOk,
    talosVer: tal.version || m.talos_version || "",
    k8sReady,
    k8sVer: kn.version || m.kubernetes_version || "",
    k8sRoles: kn.roles || "",
    k8sName: kn.name || "",
    osRoles,
    osState,
    roll,
    ip: ip || "",
    roles,
    cpu: kn.cpu_capacity || m.cpu_capacity || "",
    memGi: kn.mem_gi != null ? kn.mem_gi : m.mem_gi,
  };
}

function osWord(m) {
  return String((m && m.os) || "").toLowerCase() === "ubuntu" ? "Ubuntu" : "Talos";
}

function fleetMembership(m, st) {
  const osName = osWord(m);
  const roles = st && Array.isArray(st.roles) ? st.roles : [];
  if (st && st.k8sReady) return { label: `${osName} · Ready`, state: "ok" };
  if (roles.length) return { label: `${osName} · Not joined`, state: "warn" };
  return { label: `${osName} · Recorded`, state: "wait" };
}

function rememberEnvName() {
  if (snap && snap.name) {
    envName = snap.name;
    return;
  }
  const named = envNameById(envId);
  if (named && named !== envId) envName = named;
}

function applyFleetSources(platRes, srvRes) {
  if (platRes && Array.isArray(platRes.nodes)) platform = platRes;
  if (srvRes && Array.isArray(srvRes.servers)) inventoryServers = srvRes.servers;
  rememberEnvName();
}

function fleetLine(machines) {
  const list = Array.isArray(machines) ? machines : [];
  let ready = 0;
  let notJoined = 0;
  for (const m of list) {
    const st = machineState(m);
    if (st.k8sReady) ready += 1;
    else if ((st.roles || []).length) notJoined += 1;
  }
  const noun = list.length === 1 ? "machine" : "machines";
  return `${list.length} ${noun} · ${ready} ready · ${notJoined} not joined`;
}

function parseActivity(logText) {
  const text = String(logText || "");
  const lines = text.split("\n");
  let stage = "";
  let item = "";
  let service = "";
  let chart = "";
  const RE_STAGE = /===\s*stage\s+\d+\s*\/\s*\d+\s*:\s*([\w-]+)/;
  const RE_BASH = /\$\s+bash\s+(bin\/\S+)/;
  const RE_FAIL = /FAILED at\s+([\w-]+)\/(\S+)/;
  const RE_SVC = /SERVICE_NAME=([A-Za-z0-9_-]+)/;
  const RE_CHART = /HELM_CHART_PATH=(\S+)/;
  const RE_HELM = /Release "([A-Za-z0-9_-]+)"/;
  const RE_PXE = /pxe|network boot|iso boot|virtual CD|BIOS/i;
  const RE_TALOS = /talosctl|talos apply|:50000|maintenance mode/i;
  let pxe = false;
  let talos = false;
  for (const line of lines) {
    const s = RE_STAGE.exec(line);
    if (s) stage = s[1];
    const b = RE_BASH.exec(line);
    if (b) item = b[1];
    const f = RE_FAIL.exec(line);
    if (f) {
      stage = f[1];
      item = f[2];
    }
    const sv = RE_SVC.exec(line);
    if (sv) service = sv[1];
    const ch = RE_CHART.exec(line);
    if (ch) chart = ch[1];
    const hm = RE_HELM.exec(line);
    if (hm && !service) service = hm[1];
    const ti = /\[timing\]\s+stage=(\S+)/.exec(line);
    if (ti) stage = ti[1];
    if (RE_PXE.test(line)) pxe = true;
    if (RE_TALOS.test(line)) talos = true;
  }
  return { stage, item, service, chart, pxe, talos, tail: lines.filter(Boolean).slice(-16) };
}

function liveWork() {
  const act = parseActivity(job && job.log_text);
  const cur = pipe && pipe.current && typeof pipe.current === "object" ? pipe.current : {};
  const stem = (raw) =>
    String(raw || "")
      .replace(/^bin\//, "")
      .replace(/\.sh$/, "")
      .replace(/^install-/, "");
  const item = stem(cur.item || cur.service || cur.label || act.item || act.service);
  const stage = String(cur.stage || act.stage || "");
  return {
    live: isLive(),
    stage,
    item,
    service: item,
    ns: nsForService(item),
    pxe: !!(cur.pxe || act.pxe),
    talos: !!(cur.talos || act.talos),
  };
}

function workHitsPod(work, p) {
  if (!work || !work.live || !p) return false;
  const svc = String(work.item || work.service || "").toLowerCase();
  if (!svc) return false;
  const stem = svc.replace(/-operator$/, "").replace(/-replication$/, "").replace(/-sentinel$/, "");
  const name = String(p.name || "").toLowerCase();
  const ns = String(p.namespace || "");
  if (work.ns && ns !== work.ns) {
    if (!(/ovn/.test(svc) && ns === "kube-system")) return false;
  }
  if (name.includes(svc)) return true;
  if (stem.length >= 5 && name.includes(stem)) return true;
  if (/ovn/.test(svc) && /(ovn|ovs-ovn)/.test(name) && ns === "kube-system") return true;
  return false;
}

function mergeJobLog(prev, tail) {
  const a = String(prev || "");
  const b = String(tail || "");
  if (!b) return a;
  if (!a) return b;
  if (a.endsWith(b)) return a.length > 240000 ? a.slice(-200000) : a;
  const max = Math.min(a.length, b.length);
  for (let n = max; n >= 32; n--) {
    if (a.slice(-n) === b.slice(0, n)) {
      const out = a + b.slice(n);
      return out.length > 240000 ? out.slice(-200000) : out;
    }
  }
  if (a.length > b.length) return a;
  const out = `${a}\n${b}`;
  return out.length > 240000 ? out.slice(-200000) : out;
}

function isLive() {
  const op = String((job && job.operation) || "");
  const running = !!(pipe && pipe.running) || (job && ACTIVE.has(String(job.status || "")));
  if (!running) return false;
  if (!op) return true;
  return /genestack\.deploy|genestack\.greenfield|genestack\.tempest|genestack\.verify|registry\.mirror|baremetal|talos|pxe/i.test(
    op
  );
}

function isGreenfieldLive() {
  return isLive() && /greenfield/i.test(String((job && job.operation) || ""));
}

function isMetalRebuild() {
  // A remembered failed greenfield job does not keep the map in a rebuild.
  // The line is on only while a metal job is queued or running.
  if (isGreenfieldLive()) {
    const act = parseActivity(job && job.log_text);
    const cur = pipe && pipe.current && typeof pipe.current === "object" ? pipe.current : {};
    const sid = String(act.stage || cur.id || cur.stage || (pipe && pipe.next_stage) || "");
    if (sid && !/^(hosts|talos)$/i.test(sid)) return false;
    return true;
  }
  for (const j of recentJobs || []) {
    if (!j || !ACTIVE.has(String(j.status || ""))) continue;
    const op = String(j.operation || "");
    if (/greenfield|iso_boot|pxe_boot/i.test(op)) return true;
    if (/genestack\.deploy/i.test(op)) {
      const act = parseActivity((job && job.id === j.id && job.log_text) || "");
      if (!act.stage || /^(hosts|talos|infrastructure)$/i.test(act.stage)) return true;
    }
  }
  return false;
}

function readyMachineCount() {
  return machineRows().filter((m) => machineState(m).k8sReady).length;
}

function oldOsStillServing() {
  return isMetalRebuild() && readyMachineCount() > 0;
}

function bootingFromLog() {
  const text = String((job && job.log_text) || "");
  const out = new Set();
  const re = /pxe_boot node=([A-Za-z0-9._-]+)|iso_boot node=([A-Za-z0-9._-]+)/gi;
  let m;
  while ((m = re.exec(text))) {
    const name = m[1] || m[2];
    if (name) out.add(name);
  }
  return out;
}

function maintenanceFromLog() {
  const text = String((job && job.log_text) || "");
  const out = new Set();
  const re = /\[greenfield\] ([A-Za-z0-9._-]+) maintenance API ready/g;
  let m;
  while ((m = re.exec(text))) {
    if (m[1]) out.add(m[1]);
  }
  return out;
}

function nsForService(raw) {
  const s = String(raw || "").toLowerCase().replace(/^bin\//, "").replace(/\.sh$/, "").replace(/^install-/, "");
  if (!s) return "";
  if (/longhorn/.test(s)) return "longhorn-system";
  if (/cert-manager/.test(s)) return "cert-manager";
  if (/kube-ovn|ovn/.test(s)) return "kube-system";
  if (/metallb/.test(s)) return "metallb-system";
  if (/prometheus|grafana|loki|tempo|fluent/.test(s)) return "prometheus";
  if (
    /mariadb|rabbit|memcached|keystone|glance|nova|neutron|placement|horizon|cinder|heat|octavia|barbican|libvirt|manila|magnum|designate|skyline/.test(
      s
    )
  ) {
    return "openstack";
  }
  if (/sealed|redis|postgres-operator|topolvm|envoy/.test(s)) return "kube-system";
  return "";
}

function pxeHot(pxe) {
  const phase = String((pxe && pxe.phase) || "");
  if (!phase) return false;
  if (pxe && pxe.in_progress) return true;
  return /download|iPXE|DHCP lease|bootloader|ISO/i.test(phase);
}

function machinePhase(m, st) {
  const bm = String(m.bm_state || m.state || "").toLowerCase();
  const act = parseActivity(job && job.log_text);
  const live = isLive();
  const metal = isMetalRebuild();
  const pxe = m && m.pxe;
  const name = m && m.name;
  if (bm === "failed" || st.roll === "bad") return { label: "failed", state: "bad" };
  if (metal) {
    if (maintenanceFromLog().has(name) || (st.talosOk && bm === "talos-ready")) {
      return { label: "Talos maintenance", state: "run" };
    }
    if (st.k8sReady) {
      return { label: st.k8sRoles ? `K8s still up · ${st.k8sRoles}` : "K8s still up", state: "run" };
    }
    const phase = pxe && pxe.phase ? String(pxe.phase) : "";
    if (phase && !/^waiting for DHCP$/i.test(phase)) return { label: phase, state: "run" };
    if (bm === "booting") return { label: "PXE / BIOS", state: "run" };
    return { label: "queued", state: "run" };
  }
  if (!live) return fleetMembership(m, st);
  if (pxe && pxe.phase && (pxeHot(pxe) || live || !st.k8sReady)) {
    return { label: pxe.phase, state: "run" };
  }
  if (st.k8sReady) return { label: st.k8sRoles ? `Ready · ${st.k8sRoles}` : "Ready", state: "ok" };
  if (st.talosOk) return { label: live ? "joining Kubernetes" : `Talos ${st.talosVer || "up"}`, state: live ? "run" : "ok" };
  if (bm === "booting" || act.pxe) return { label: "PXE / BIOS", state: "run" };
  if (live && (stageState("hosts") === "running" || act.talos || bm === "registered")) {
    return { label: act.pxe ? "PXE / BIOS" : "installing Talos", state: "run" };
  }
  if (!st.configured) return { label: "waiting", state: "wait" };
  return { label: st.ip || "no ip", state: "wait" };
}

function applyLiveReveal(machines, podsByHost) {
  if (!autoReveal) return;
  const metal = isMetalRebuild();
  if (metal && !oldOsStillServing()) {
    collapsed.add("tests");
    const maint = maintenanceFromLog();
    for (const m of machines) {
      const mid = `m:${m.name}`;
      const kid = `${mid}:k8s`;
      const bm = String(m.bm_state || m.state || "").toLowerCase();
      const hot = pxeHot(m.pxe) || bm === "booting" || maint.has(m.name);
      if (hot && !userCollapsed.has(mid)) collapsed.delete(mid);
      collapsed.add(kid);
    }
    return;
  }
  const act = parseActivity(job && job.log_text);
  const workNow = liveWork();
  const hot = workNow.ns || nsForService(act.service || act.item);
  const live = isLive();
  for (const m of machines) {
    const st = machineState(m);
    const mid = `m:${m.name}`;
    const kid = `${mid}:k8s`;
    const hostPods = podsByHost.get(m.name) || [];
    if (!userCollapsed.has(mid) && (st.talosOk || st.k8sReady || (live && hostPods.length))) {
      collapsed.delete(mid);
    }
    if (!userCollapsed.has(kid) && (st.k8sReady || hostPods.length)) {
      collapsed.delete(kid);
    }
    if (hot && !userCollapsed.has(`${kid}:ns:${hot}`)) collapsed.delete(`${kid}:ns:${hot}`);
  }
}

function beginLive({ collapse } = {}) {
  autoReveal = true;
  userCollapsed = new Set();
  minimized = new Set();
  workloadsAt = 0;
  lastLiveRefresh = 0;
  if (collapse && !space3dOn && !nestOn) {
    for (const n of lastGraph.nodes) {
      if ((n.childCount || 0) > 0 && n.id !== "env" && n.id !== "infra") collapsed.add(n.id);
    }
  }
  persistFold();
  view._userMoved = false;
}

function prettyService(raw) {
  let s = String(raw || "").trim();
  s = s.replace(/^bin\//, "").replace(/\.sh$/, "").replace(/^install-/, "");
  if (!s) return "";
  return s
    .split(/[-_/]/)
    .filter(Boolean)
    .map((p) => p.charAt(0).toUpperCase() + p.slice(1))
    .join(" ");
}

function osRoleLabel(st) {
  const roles = (st && st.osRoles) || [];
  if (!roles.length) return "no OpenStack role";
  const map = {
    control: "control plane",
    compute: "compute",
    network: "network",
    storage: "storage",
    "storage-ceph": "ceph",
    "storage-cinder": "cinder",
  };
  return roles.map((r) => map[r] || r).join(" · ");
}

function stageNode(id) {
  const spec = stageSpec(id);
  const st = stageState(id);
  return { title: spec.name || id, hint: st, state: mapStage(st), stageId: id };
}

function podIsLive(p) {
  if (!p || !p.name) return false;
  if (p.stale) return false;
  const phase = String(p.phase || "");
  const reason = String(p.reason || "");
  if (reason.includes("ContainerStatusUnknown") || phase.includes("Unknown")) return false;
  if (phase === "Succeeded") return false;
  return true;
}

function podList() {
  const rows = workloads && Array.isArray(workloads.pods) ? workloads.pods : [];
  return rows.filter(podIsLive);
}

function podBlocked(p) {
  const reason = String((p && p.reason) || "").toLowerCase();
  const phase = String((p && p.phase) || "").toLowerCase();
  if (phase === "failed") return true;
  return /crashloop|oomkilled|evicted|errimage|imagepullbackoff|failedmount|failedattach|multi-attach/.test(
    reason
  );
}

function podReady(p) {
  const ready = p && p.ready;
  if (ready === true) return true;
  if (typeof ready === "string" && ready.includes("/")) {
    const [a, b] = ready.split("/");
    return !!(a && b && a === b && a !== "0");
  }
  return false;
}

function podState(p) {
  const phase = String((p && p.phase) || "").toLowerCase();
  if (podBlocked(p)) return "bad";
  if (phase === "succeeded") return "ok";
  if (phase === "running") return podReady(p) ? "ok" : "run";
  if (phase === "pending" || phase === "unknown" || !phase) {
    if (isLive()) return "run";
    return "wait";
  }
  return "wait";
}

function machineKeys(m) {
  const keys = new Set();
  const add = (v) => {
    const s = String(v || "").trim();
    if (!s) return;
    keys.add(s.toLowerCase());
    keys.add(shortName(s).toLowerCase());
  };
  add(m.name);
  const kn = m.kubernetes && typeof m.kubernetes === "object" ? m.kubernetes : {};
  add(kn.name);
  add(machineState(m).k8sName);
  return keys;
}

function assignPods(machines, pods) {
  const byHost = new Map();
  for (const m of machines) byHost.set(m.name, []);
  const index = machines.map((m) => ({ m, keys: machineKeys(m) }));
  const used = new Set();
  const sorted = pods.slice().sort((a, b) => {
    const ka = `${a.namespace || ""}/${a.name || ""}`;
    const kb = `${b.namespace || ""}/${b.name || ""}`;
    return ka.localeCompare(kb);
  });
  for (const p of sorted) {
    const node = String(p.node || "").trim().toLowerCase();
    if (!node) continue;
    const short = shortName(node).toLowerCase();
    const hit = index.find(({ keys }) => keys.has(node) || keys.has(short));
    if (!hit) continue;
    const key = `${p.namespace || ""}/${p.name}`;
    if (used.has(key)) continue;
    used.add(key);
    byHost.get(hit.m.name).push(p);
  }
  return byHost;
}

function osGroupState() {
  if (!osInstalled()) return "wait";
  const ids = ["core", "compute-network", "platform-extras", "testing"];
  if (ids.some((id) => stageState(id) === "failed")) return "bad";
  if (ids.some((id) => stageState(id) === "running")) return "run";
  return "ok";
}

function nsKindLabel(ns) {
  const n = String(ns || "");
  if (n === "openstack" || n.startsWith("openstack-")) return "OpenStack";
  if (n === "kube-system" || n === "kube-public" || n === "kube-node-lease") return "cluster";
  if (n === "longhorn-system") return "storage";
  if (n === "kube-ovn" || n === "ovn-kubernetes") return "network";
  if (n === "cert-manager") return "certs";
  if (n === "prometheus" || n === "monitoring") return "observe";
  return "namespace";
}

function nsRank(ns) {
  const n = String(ns || "");
  if (n === "openstack" || n.startsWith("openstack")) return 0;
  if (n === "kube-system") return 1;
  if (n === "longhorn-system") return 2;
  return 3;
}

function groupPodsByNs(hostPods) {
  const map = new Map();
  for (const p of hostPods) {
    const ns = String(p.namespace || "default");
    if (!map.has(ns)) map.set(ns, []);
    map.get(ns).push(p);
  }
  return [...map.keys()]
    .sort((a, b) => {
      const d = nsRank(a) - nsRank(b);
      return d || a.localeCompare(b);
    })
    .map((ns) => ({ ns, pods: map.get(ns) }));
}

function isNovaComputePod(p) {
  return /nova-compute/i.test(String((p && p.name) || ""));
}

function vmNetworks(v) {
  const addr = v && v.addresses;
  if (!addr || typeof addr !== "object" || Array.isArray(addr)) return [];
  return Object.keys(addr).filter(Boolean);
}

function vmState(v) {
  const s = String((v && v.status) || "").toUpperCase();
  if (s === "ERROR" || s === "UNKNOWN") return "bad";
  if (s === "ACTIVE" || s === "RESCUED") return "ok";
  if (
    s === "BUILD" ||
    s === "BUILDING" ||
    s === "SPAWNING" ||
    s === "REBOOT" ||
    s === "HARD_REBOOT" ||
    s === "MIGRATING" ||
    s === "RESIZE" ||
    s === "VERIFY_RESIZE"
  ) {
    return "run";
  }
  if (s === "SHUTOFF" || s === "PAUSED" || s === "SUSPENDED" || s === "SHELVED" || s === "SHELVED_OFFLOADED") {
    return "wait";
  }
  return "run";
}

function cloudRows(key) {
  const c = osCloud;
  if (!c) return [];
  const rows = c[key];
  return Array.isArray(rows) ? rows.filter((x) => x && (x.id || x.name || x.ip)) : [];
}

function netKind(n) {
  return n && (n.external || n.router_external) ? "net" : "vpc";
}

function netHealth(n) {
  const s = String((n && n.status) || "").toUpperCase();
  if (s === "ERROR" || s === "DOWN") return "bad";
  if (s === "ACTIVE" || s === "UP" || s === "ENABLED") return "ok";
  return "wait";
}

function vmHasIp(v, ip) {
  if (!v || !ip) return false;
  return JSON.stringify(v.addresses || {}).includes(String(ip));
}

function fipsForVm(v, fips) {
  return (fips || []).filter((f) => vmHasIp(v, f.ip) || vmHasIp(v, f.fixed_ip));
}

function isRouterOwner(owner) {
  const o = String(owner || "");
  return (
    o.includes("router_interface") ||
    o.includes("router_gateway") ||
    o.includes("ha_router") ||
    o.includes("router_centralized_snat")
  );
}

function vmForFip(f, servers, portById) {
  const pid = String((f && (f.port || f.port_id)) || "");
  const port = pid && portById ? portById.get(pid) : null;
  if (port && port.device_id) {
    const hit = (servers || []).find((s) => String(s.id) === String(port.device_id));
    if (hit) return hit;
  }
  return (servers || []).find((v) => vmHasIp(v, f && f.ip) || vmHasIp(v, f && f.fixed_ip)) || null;
}

function projectLabel(p) {
  return String((p && (p.name || p.id)) || "").trim();
}

function attachCloudStack(add, link, byId) {
  const projects = cloudRows("projects");
  const networks = cloudRows("networks");
  const subnets = cloudRows("subnets");
  const routers = cloudRows("routers");
  const fips = cloudRows("floating_ips");
  const lbs = cloudRows("load_balancers");
  const ports = cloudRows("ports");
  const servers = osVms && osVms.length ? osVms : cloudRows("servers");
  const ingresses = Array.isArray(k8sIngress) ? k8sIngress : [];
  const services = Array.isArray(k8sServices) ? k8sServices : [];
  const gateways = Array.isArray(k8sGateways) ? k8sGateways : [];
  const routes = Array.isArray(k8sRoutes) ? k8sRoutes : [];
  const pools = Array.isArray(k8sPools) ? k8sPools : [];
  const portById = new Map();
  for (const p of ports) {
    if (p && p.id) portById.set(String(p.id), p);
  }
  const overlayKids = [];
  const tenantKids = [];
  const edgeKids = [];

  const overlayState =
    networks.length || routers.length ? (networks.some((n) => netHealth(n) === "bad") ? "warn" : "ok") : "wait";
  add({
    id: "overlay",
    kind: "overlay",
    state: overlayState,
    title: "Overlay / VPC",
    subtitle: `${networks.length} nets · ${subnets.length} subnets · ${routers.length} routers`,
    childCount: networks.length + routers.length,
  });
  link("env", "overlay");

  const netById = new Map();
  const netByName = new Map();
  for (const n of networks) {
    const nid = `net:${n.id || n.name}`;
    const kind = netKind(n);
    const node = add({
      id: nid,
      kind,
      state: netHealth(n),
      title: n.name || n.id,
      subtitle: n.external ? "provider" : "vpc",
      nestParent: "overlay",
      netId: n.id,
      project_id: n.project_id || "",
      external: !!n.external,
      shared: !!n.shared,
      status: n.status || "",
      subnets: n.subnets || [],
      childCount: 0,
    });
    link("overlay", nid);
    overlayKids.push(node);
    if (n.id) netById.set(String(n.id), nid);
    if (n.name) netByName.set(String(n.name), nid);
  }

  for (const s of subnets) {
    const sid = `subnet:${s.id || s.name}`;
    const parent = netById.get(String(s.network || s.network_id || "")) || "overlay";
    add({
      id: sid,
      kind: "subnet",
      state: "ok",
      title: s.name || s.cidr || s.id,
      subtitle: s.cidr || "subnet",
      nestParent: parent,
      cidr: s.cidr || "",
      netId: s.network || s.network_id || "",
      project_id: s.project_id || "",
    });
    link(parent, sid);
    const p = byId.get(parent);
    if (p) p.childCount = (p.childCount || 0) + 1;
  }

  for (const r of routers) {
    const rid = `router:${r.id || r.name}`;
    const gw = r.external_gateway;
    const gwNet =
      gw && typeof gw === "object"
        ? netById.get(String(gw.network_id || gw.network || ""))
        : "";
    const parent = gwNet || "overlay";
    add({
      id: rid,
      kind: "router",
      state: netHealth(r),
      title: r.name || r.id,
      subtitle: gwNet ? "gateway" : "router",
      nestParent: parent,
      project_id: r.project_id || "",
      status: r.status || "",
      gateway: gw,
    });
    link(parent, rid);
    if (parent === "overlay") overlayKids.push(byId.get(rid));
  }

  for (const p of ports) {
    if (!isRouterOwner(p.device_owner) || !p.device_id) continue;
    const rid = `router:${p.device_id}`;
    if (!byId.get(rid)) continue;
    const nid = netById.get(String(p.network_id || ""));
    if (nid) link(rid, nid);
    for (const fi of p.fixed_ips || []) {
      const sid = fi && fi.subnet_id ? `subnet:${fi.subnet_id}` : "";
      if (sid && byId.get(sid)) link(rid, sid);
    }
  }

  const tenantState = projects.length || servers.length ? "ok" : "wait";
  add({
    id: "tenants",
    kind: "tenant",
    state: tenantState,
    title: "Tenants",
    subtitle: `${projects.length} projects · ${servers.length} instances`,
    childCount: Math.max(projects.length, servers.length ? 1 : 0),
  });
  link("env", "tenants");

  const tenantByProject = new Map();
  const ensureTenant = (projectId, name) => {
    const key = String(projectId || name || "").trim();
    if (!key) return "";
    if (tenantByProject.has(key)) return tenantByProject.get(key);
    const tid = `tenant:${key}`;
    const node = add({
      id: tid,
      kind: "tenant",
      state: "ok",
      title: name || key,
      subtitle: "project",
      nestParent: "tenants",
      project_id: projectId || key,
      childCount: 0,
    });
    link("tenants", tid);
    tenantKids.push(node);
    tenantByProject.set(key, tid);
    if (name && name !== key) tenantByProject.set(name, tid);
    return tid;
  };
  for (const p of projects) ensureTenant(p.id, projectLabel(p));

  for (const v of servers) {
    if (!v || !v.id) continue;
    const vid = `vm:${v.id}`;
    const nets = vmNetworks(v);
    const hits = (fips || []).filter((f) => {
      const viaPort = vmForFip(f, [v], portById);
      return viaPort || vmHasIp(v, f.ip) || vmHasIp(v, f.fixed_ip);
    });
    const tenantName = v.project_name || v.tenant || "";
    const tid = ensureTenant(v.project_id || tenantName, tenantName || v.project_id);
    const host = v.host || "";
    const path = [
      host ? `metal ${shortName(host)}` : "",
      "nova",
      tenantName || (tid ? tid.replace(/^tenant:/, "") : ""),
      ...nets.map((n) => `net ${n}`),
      ...hits.map((f) => `fip ${f.ip}`),
    ].filter(Boolean);
    const node = add({
      id: vid,
      kind: "vm",
      state: vmState(v),
      title: v.name || v.id,
      subtitle: [v.status, v.flavor].filter(Boolean).join(" · "),
      nestParent: tid || "tenants",
      machineName: host,
      vmId: v.id,
      status: v.status,
      flavor: v.flavor,
      image: v.image,
      host,
      addresses: v.addresses,
      network: nets[0] || "",
      networks: nets,
      project_id: v.project_id || "",
      project_name: tenantName,
      tenant: tenantName || v.project_id || "",
      fips: hits.map((f) => f.ip).filter(Boolean),
      path,
    });
    if (tid) {
      link(tid, vid);
      const t = byId.get(tid);
      if (t) t.childCount = (t.childCount || 0) + 1;
    } else {
      link("tenants", vid);
    }
    for (const name of nets) {
      const nid = netByName.get(name) || netById.get(name);
      if (nid) link(nid, vid);
    }
    for (const f of hits) {
      const fid = `fip:${f.id || f.ip}`;
      link(vid, fid);
    }
    if (!tid) tenantKids.push(node);
  }

  for (const p of ports) {
    if (!p.device_id || !byId.get(`vm:${p.device_id}`)) continue;
    const vid = `vm:${p.device_id}`;
    const nid = netById.get(String(p.network_id || ""));
    if (nid) link(nid, vid);
    for (const fi of p.fixed_ips || []) {
      const sid = fi && fi.subnet_id ? `subnet:${fi.subnet_id}` : "";
      if (sid && byId.get(sid)) link(sid, vid);
    }
  }

  const edgeNets = fips.length + ingresses.length + lbs.length + gateways.length + routes.length + pools.length;
  add({
    id: "edge",
    kind: "edge",
    state: edgeNets ? "ok" : "wait",
    title: "Edge / Ingress",
    subtitle: `${gateways.length} gw · ${routes.length} routes · ${ingresses.length} ingress · ${fips.length} FIP`,
    childCount: edgeNets,
  });
  link("env", "edge");

  for (const ing of ingresses) {
    const iid = `ing:${ing.namespace || "default"}/${ing.name}`;
    const hosts = Array.isArray(ing.hosts) ? ing.hosts.filter(Boolean) : [];
    const url =
      (Array.isArray(ing.urls) && ing.urls[0]) ||
      (hosts[0] ? `${ing.tls ? "https" : "http"}://${hosts[0]}` : "");
    const node = add({
      id: iid,
      kind: "ingress",
      state: ing.address ? "ok" : "wait",
      title: ing.name,
      subtitle: [ing.class, hosts[0] || ing.address || "no host"].filter(Boolean).join(" · "),
      nestParent: "edge",
      namespace: ing.namespace || "default",
      name: ing.name,
      hosts,
      address: ing.address || "",
      className: ing.class || "",
      tls: !!ing.tls,
      url,
      backends: ing.backends || [],
      path: ["edge", ing.class || "ingress", hosts[0] || ing.name].filter(Boolean),
    });
    link("edge", iid);
    edgeKids.push(node);
    for (const b of ing.backends || []) {
      if (!b || !b.name) continue;
      const sid = `svc:${b.namespace || ing.namespace || "default"}/${b.name}`;
      link(iid, sid);
    }
  }

  for (const f of fips) {
    const fid = `fip:${f.id || f.ip}`;
    const associated = vmForFip(f, servers, portById);
    const extNet = netById.get(String(f.floating_network_id || ""));
    const routerId = f.router_id ? `router:${f.router_id}` : "";
    const node = add({
      id: fid,
      kind: "fip",
      state: f.status && String(f.status).toUpperCase() === "DOWN" ? "wait" : associated ? "ok" : "run",
      title: f.ip || f.id,
      subtitle: associated ? associated.name || "associated" : "pool",
      nestParent: "edge",
      ip: f.ip || "",
      fixed_ip: f.fixed_ip || "",
      port: f.port || f.port_id || "",
      project_id: f.project_id || "",
      vmId: associated && associated.id,
      router_id: f.router_id || "",
      floating_network_id: f.floating_network_id || "",
      path: ["edge", "floating-ip", f.ip, associated ? `vm ${associated.name}` : ""].filter(Boolean),
    });
    link("edge", fid);
    if (associated) link(`vm:${associated.id}`, fid);
    if (extNet) link(extNet, fid);
    if (routerId && byId.get(routerId)) link(routerId, fid);
    edgeKids.push(node);
  }

  for (const lb of lbs) {
    const lid = `lb:${lb.id || lb.name}`;
    const node = add({
      id: lid,
      kind: "lb",
      state: netHealth(lb),
      title: lb.name || lb.id,
      subtitle: lb.vip_address || lb.provider || "octavia",
      nestParent: "edge",
      vip: lb.vip_address || "",
      provider: lb.provider || "",
      status: lb.status || lb.provisioning_status || "",
    });
    link("edge", lid);
    edgeKids.push(node);
  }

  for (const s of services) {
    const typ = String(s.type || "");
    if (typ !== "LoadBalancer" && typ !== "NodePort" && !(s.external_ips || []).length && !(s.load_balancer || []).length) {
      continue;
    }
    const sid = `svc:${s.namespace || "default"}/${s.name}`;
    const node = add({
      id: sid,
      kind: "svc",
      state: (s.load_balancer || []).length || (s.external_ips || []).length ? "ok" : "run",
      title: s.name,
      subtitle: [typ, (s.load_balancer || [])[0] || (s.external_ips || [])[0] || s.cluster_ip].filter(Boolean).join(" · "),
      nestParent: "edge",
      namespace: s.namespace || "default",
      name: s.name,
      svcType: typ,
      cluster_ip: s.cluster_ip || "",
      ports: s.ports || [],
      load_balancer: s.load_balancer || [],
    });
    link("edge", sid);
    edgeKids.push(node);
  }

  for (const g of gateways) {
    const gid = `gw:${g.namespace || "default"}/${g.name}`;
    const addrs = Array.isArray(g.addresses) ? g.addresses.filter(Boolean) : [];
    const addr = g.address || addrs[0] || "";
    const url = addr ? (String(addr).includes("://") ? addr : `https://${addr}`) : "";
    const node = add({
      id: gid,
      kind: "gw",
      state: addr ? "ok" : "wait",
      title: g.name,
      subtitle: [g.class, addr || "gateway"].filter(Boolean).join(" · "),
      nestParent: "edge",
      namespace: g.namespace || "default",
      name: g.name,
      className: g.class || "",
      address: addr,
      addresses: addrs,
      listeners: g.listeners || [],
      url,
      path: ["edge", "gateway", g.name, addr].filter(Boolean),
    });
    link("edge", gid);
    edgeKids.push(node);
  }

  for (const r of routes) {
    const rid = `route:${r.namespace || "default"}/${r.name}`;
    const hosts = Array.isArray(r.hosts) ? r.hosts.filter(Boolean) : [];
    const url = (Array.isArray(r.urls) && r.urls[0]) || (hosts[0] ? `https://${hosts[0]}` : "");
    const prefs = Array.isArray(r.parent_refs) ? r.parent_refs : [];
    const parentGw =
      prefs[0] && prefs[0].name
        ? `gw:${prefs[0].namespace || r.namespace || "default"}/${prefs[0].name}`
        : "edge";
    const node = add({
      id: rid,
      kind: "route",
      state: hosts.length ? "ok" : "wait",
      title: r.name,
      subtitle: hosts[0] || "httproute",
      nestParent: byId.get(parentGw) ? parentGw : "edge",
      namespace: r.namespace || "default",
      name: r.name,
      hosts,
      url,
      backends: r.backends || [],
      parent_refs: prefs,
      path: ["edge", "httproute", hosts[0] || r.name].filter(Boolean),
    });
    link(byId.get(parentGw) ? parentGw : "edge", rid);
    edgeKids.push(node);
    for (const b of r.backends || []) {
      if (!b || !b.name) continue;
      const sid = `svc:${b.namespace || r.namespace || "default"}/${b.name}`;
      link(rid, sid);
    }
  }

  for (const p of pools) {
    const pid = `pool:${p.name}`;
    const addrs = Array.isArray(p.addresses) ? p.addresses.filter(Boolean) : [];
    const node = add({
      id: pid,
      kind: "lb",
      state: addrs.length ? "ok" : "wait",
      title: p.name,
      subtitle: addrs[0] || "metallb",
      nestParent: "edge",
      provider: "metallb",
      vip: addrs[0] || "",
      addresses: addrs,
      path: ["edge", "metallb", p.name].filter(Boolean),
    });
    link("edge", pid);
    edgeKids.push(node);
    for (const g of gateways) {
      const gaddrs = [].concat(g.addresses || [], g.address || []);
      if (gaddrs.some((a) => a && addrs.some((pool) => String(a).includes(String(pool).split("/")[0])))) {
        link(`gw:${g.namespace || "default"}/${g.name}`, pid);
      }
    }
  }

  const env = byId.get("env");
  if (env) env.childCount = (env.childCount || 0) + 3;
  return { overlayKids, tenantKids, edgeKids };
}

function assignVms(machines, vms) {
  const byHost = new Map();
  for (const m of machines) byHost.set(m.name, []);
  const index = machines.map((m) => ({ m, keys: machineKeys(m) }));
  for (const v of vms || []) {
    if (!v || !v.id) continue;
    const host = String(v.host || "").trim().toLowerCase();
    if (!host) continue;
    const short = shortName(host).toLowerCase();
    const hit = index.find(({ keys }) => keys.has(host) || keys.has(short));
    if (!hit) continue;
    byHost.get(hit.m.name).push(v);
  }
  return byHost;
}

function nsRollup(pods) {
  const bits = (pods || []).map(podState);
  if (!bits.length) return "wait";
  if (bits.includes("bad")) return "warn";
  if (bits.includes("run")) return "run";
  if (bits.includes("ok")) return "ok";
  return "wait";
}

function paintAncestors(graph) {
  const kids = new Map();
  for (const e of graph.edges || []) {
    if (!kids.has(e.source)) kids.set(e.source, []);
    kids.get(e.source).push(e.target);
  }
  const rank = { wait: 0, ok: 1, run: 2, warn: 3, bad: 4 };
  const isLeaf = (n) => n.kind === "vm" || n.kind === "pod" || n.kind === "more";
  const walk = (id, seen) => {
    const n = graph.byId.get(id);
    if (!n) return "wait";
    const childIds = kids.get(id) || [];
    if (isLeaf(n)) {
      let worst = n.state || "wait";
      for (const cid of childIds) {
        if (seen.has(cid)) continue;
        seen.add(cid);
        const cs = walk(cid, seen);
        if ((rank[cs] || 0) > (rank[worst] || 0)) worst = cs;
      }
      if (n.kind === "pod" && (worst === "bad" || worst === "warn") && n.state !== "bad") {
        n.state = "warn";
      }
      return n.state || "wait";
    }
    if (!childIds.length) return n.state || "wait";
    let worst = "wait";
    for (const cid of childIds) {
      if (seen.has(cid)) continue;
      seen.add(cid);
      const cs = walk(cid, seen);
      if ((rank[cs] || 0) > (rank[worst] || 0)) worst = cs;
    }
    const prior = n.state || "wait";
    // This host/namespace only. Green children → green parent. Orange only
    // when something on this branch is blocked; yellow while a child is
    // actually in progress — not because a deploy is running elsewhere.
    if (prior === "bad") n.state = "bad";
    else if (worst === "bad") n.state = "warn";
    else if ((rank[worst] || 0) > (rank[prior] || 0) && worst !== "ok") n.state = worst;
    return n.state;
  };
  for (const n of graph.nodes) {
    if (isLeaf(n)) continue;
    walk(n.id, new Set([n.id]));
  }
}

function workItems() {
  const act = parseActivity(job && job.log_text);
  const nxt = nextAction();
  const rows = [];
  if (nxt.kind === "run" || nxt.kind === "bad") {
    rows.push({
      kind: "job",
      state: nxt.kind === "bad" ? "bad" : "run",
      title: nxt.text,
      detail: prettyService(act.service || act.item) || act.stage || "",
    });
  }
  if (!isMetalRebuild()) {
    const pods = lastGraph.nodes.filter((n) => n.kind === "pod" || n.kind === "vm");
    for (const n of pods) {
      if (n.state === "ok") continue;
      rows.push({
        kind: "pod",
        state: n.state,
        title: n.title,
        detail: [n.namespace, n.phase, n.ready, n.reason].filter(Boolean).join(" · "),
        id: n.id,
      });
    }
  }
  const order = { bad: 0, run: 1, warn: 2, wait: 3 };
  rows.sort((a, b) => (order[a.state] ?? 9) - (order[b.state] ?? 9) || String(a.title).localeCompare(b.title));
  return rows;
}

function dim(kind, id) {
  if (id && minimized.has(id)) return SIZE.min;
  return SIZE[kind] || SIZE.pod;
}

function validationInfo() {
  const v = (snap && snap.validation) || {};
  const status = String(v.status || "never");
  let state = "wait";
  if (status === "passed") state = "ok";
  else if (status === "failed") state = "bad";
  else if (status === "running") state = "run";
  else if (status === "never") state = "warn";
  return {
    status,
    state,
    summary: v.summary || "not validated",
    chart: !!v.chart_installed,
    liveOk: !!v.live_ok,
    checks: Array.isArray(v.checks) ? v.checks : [],
    tempest: v.tempest || { status: "never", passed: 0, failed: 0, ran_count: 0, failures: [] },
    verify: v.verify || { status: "never", passed: 0, failed: 0, ran_count: 0, failures: [] },
  };
}

function registryInfo() {
  const r = (snap && snap.registry) || {};
  const caches = Array.isArray(r.caches) ? r.caches : [];
  const ready = Number(r.ready_count != null ? r.ready_count : caches.filter((c) => c && c.running).length);
  const total = Number(r.cache_count != null ? r.cache_count : caches.length);
  let state = "wait";
  if (total && ready === total) state = "ok";
  else if (ready) state = "run";
  else if (total) state = "bad";
  return {
    bind: r.bind || "",
    hostSource: r.host_source || "",
    configuredHost: r.configured_host || "",
    loaded: !!(snap && snap.registry),
    ready,
    total,
    state,
    caches,
    images: Number(r.image_count || 0),
    envName: r.environment_name || envName || "",
    last: r.last_mirror || null,
  };
}

function stack(items, x, y0) {
  let y = y0;
  for (const n of items) {
    if (!n) continue;
    const [w, h] = dim(n.kind, n.id);
    n.x = x;
    n.y = y;
    n.w = w;
    n.h = h;
    y += h + GAP_Y;
  }
  return y;
}

function edgeKidsMap(edges) {
  const m = new Map();
  for (const e of edges || []) {
    if (!e || !e.source || !e.target) continue;
    if (!m.has(e.source)) m.set(e.source, []);
    m.get(e.source).push(e.target);
  }
  return m;
}

function edgeParentsMap(edges) {
  const m = new Map();
  for (const e of edges || []) {
    if (!e || !e.source || !e.target) continue;
    if (!m.has(e.target)) m.set(e.target, []);
    m.get(e.target).push(e.source);
  }
  return m;
}

function walkIds(start, adj, out) {
  const st = [start];
  while (st.length) {
    const id = st.pop();
    if (!id || out.has(id)) continue;
    out.add(id);
    const next = adj.get(id);
    if (next) for (let i = 0; i < next.length; i++) st.push(next[i]);
  }
}

function branchIds(focus, edges) {
  if (!focus) return null;
  const kids = edgeKidsMap(edges);
  const parents = edgeParentsMap(edges);
  const keep = new Set();
  walkIds(focus, parents, keep);
  const desc = new Set();
  walkIds(focus, kids, desc);
  for (const id of desc) walkIds(id, parents, keep);
  keep.add("env");
  return keep;
}

function buildGraph() {
  const nodes = [];
  const edges = [];
  const byId = new Map();
  const add = (n) => {
    const prev = byId.get(n.id);
    if (prev) {
      Object.assign(prev, n);
      return prev;
    }
    nodes.push(n);
    byId.set(n.id, n);
    return n;
  };
  const linked = new Set();
  const link = (source, target) => {
    if (!source || !target || source === target) return;
    const id = `e:${source}->${target}`;
    if (linked.has(id)) return;
    linked.add(id);
    edges.push({ id, source, target });
  };

  const machines = machineRows();
  const pods = podList();
  const podsByHost = assignPods(machines, pods);
  const vmsByHost = assignVms(machines, osVms);
  applyLiveReveal(machines, podsByHost);
  const act = parseActivity(job && job.log_text);
  const work = liveWork();
  const hotNs = work.ns || nsForService(act.service || act.item);
  const metal = isMetalRebuild();
  const serving = oldOsStillServing();
  const ready = machines.filter((m) => machineState(m).k8sReady).length;
  const talosN = machines.filter((m) => machineState(m).talosOk).length;
  const hostsState = stageState("hosts");
  const infraState =
    metal && !serving
      ? isLive()
        ? "run"
        : "bad"
      : talosN === machines.length && machines.length
        ? "ok"
        : hostsState === "failed"
          ? "bad"
          : "run";
  const installed = metal && !serving ? false : osInstalled();
  const metalBooting = machines.filter(
    (m) =>
      String(m.bm_state || "").toLowerCase() === "booting" ||
      pxeHot(m.pxe) ||
      maintenanceFromLog().has(m.name)
  ).length;

  const reg = registryInfo();
  add({
    id: "env",
    kind: "env",
    state: metal
      ? serving
        ? "run"
        : isLive()
          ? "run"
          : "bad"
      : installed
        ? "ok"
        : pipe && pipe.running
          ? "run"
          : "wait",
    title: envName || "Environment",
    subtitle: metal
      ? serving
        ? `${ready} still ready, rebuild in progress`
        : `Metal rebuild · ${metalBooting} booting`
      : isLive()
        ? `deploying · ${machines.length} machines`
        : fleetLine(machines),
    childCount: 6,
  });

  const rows = [];
  const cacheNodes = [];
  const testNodes = [];
  if (isOpen("env")) {
    add({
      id: "registry",
      kind: "registry",
      state: /registry\.mirror/i.test(String((job && job.operation) || "")) && ACTIVE.has(String((job && job.status) || ""))
        ? "run"
        : reg.state,
      title: "Registry",
      subtitle: !reg.loaded
        ? "Reading address"
        : reg.bind
          ? `${reg.bind} · ${reg.ready}/${reg.total || 0} up · ${reg.images} images`
          : `${reg.ready}/${reg.total || 0} caches`,
      bind: reg.bind,
      hostSource: reg.hostSource,
      configuredHost: reg.configuredHost,
      registryLoaded: reg.loaded,
      images: reg.images,
      caches: reg.caches,
      lastMirror: reg.last,
      childCount: reg.caches.length,
    });
    link("env", "registry");
    if (isOpen("registry")) {
      for (const c of reg.caches) {
        const cid = `reg:${c.registry}`;
        add({
          id: cid,
          kind: "regcache",
          state: c.running ? (c.images ? "ok" : "run") : "bad",
          title: c.registry,
          subtitle: c.running ? `${c.images || 0} images · :${c.port}` : `down · :${c.port}`,
          bind: reg.bind,
          port: c.port,
          endpoint: c.endpoint,
          images: c.images || 0,
          repositories: c.repositories || [],
          running: !!c.running,
        });
        link("registry", cid);
        cacheNodes.push(byId.get(cid));
      }
    }
    const val = validationInfo();
    const testLive = /genestack\.(tempest|verify)/i.test(String((job && job.operation) || "")) && ACTIVE.has(String((job && job.status) || ""));
    add({
      id: "tests",
      kind: "tests",
      state: metal ? "wait" : testLive ? "run" : val.state,
      title: "Tests",
      subtitle: metal ? "paused during metal rebuild" : val.summary,
      chart: val.chart,
      checks: val.checks,
      tempest: val.tempest,
      verify: val.verify,
      childCount: 2,
    });
    link("env", "tests");
    if (isOpen("tests")) {
      for (const suite of [
        { id: "tempest", title: "Tempest", row: val.tempest },
        { id: "verify", title: "Verify", row: val.verify },
      ]) {
        const row = suite.row || {};
        const st =
          row.status === "passed" ? "ok" : row.status === "failed" ? "bad" : row.status === "running" ? "run" : "wait";
        const tid = `test:${suite.id}`;
        const counts =
          row.status === "never"
            ? "never run"
            : `${row.passed || 0} passed · ${row.failed || 0} failed`;
        add({
          id: tid,
          kind: "testsuite",
          state: st,
          title: suite.title,
          subtitle: counts,
          suite: suite.id,
          row,
        });
        link("tests", tid);
        testNodes.push(byId.get(tid));
      }
    }
    add({
      id: "infra",
      kind: "group",
      state: metal ? infraState : isLive() && talosN < machines.length ? "run" : infraState,
      title: "Infrastructure",
      subtitle: metal
        ? serving
          ? `${ready} still ready, rebuild in progress`
          : `${maintenanceFromLog().size} in Talos maintenance · ${metalBooting} booting`
        : fleetLine(machines),
      childCount: machines.length,
    });
    link("env", "infra");

    if (isOpen("infra")) {
      for (const m of machines) {
        const st = machineState(m);
        const phase = machinePhase(m, st);
        const hostPods = podsByHost.get(m.name) || [];
        const mid = `m:${m.name}`;
        const kid = `${mid}:k8s`;
        const showTree = space3dOn || nestOn || isLive();
        const showK8s = (showTree || isOpen(mid)) && (st.k8sReady || st.talosOk || hostPods.length);
        const hostWorking = work.live && hostPods.some((p) => workHitsPod(work, p));
        add({
          id: mid,
          kind: "machine",
          state: hostWorking && phase.state !== "bad" ? "run" : phase.state,
          title: shortName(m.name),
          subtitle: hostWorking ? `installing ${work.item || work.service}` : phase.label,
          hot: hostWorking,
          machineName: m.name,
          roles: st.roles,
          osRoles: st.osRoles,
          talosOk: st.talosOk,
          talosVer: st.talosVer,
          k8sReady: st.k8sReady,
          cpu: st.cpu,
          memGi: st.memGi,
          podCount: hostPods.length,
          childCount: showK8s ? 1 : 0,
        });
        link("infra", mid);
        const nsGroups = [];
        const nsList = groupPodsByNs(hostPods);
        if (showK8s) {
          add({
            id: kid,
            kind: "k8s",
            state: hostWorking ? "run" : st.k8sReady ? "ok" : st.talosOk ? "run" : "wait",
            title: "Kubernetes",
            subtitle: hostWorking
              ? `installing ${work.item || work.service}`
              : `${st.k8sReady ? "Ready" : "not Ready"} · ${nsList.length} ns · ${hostPods.length} containers`,
            hot: hostWorking,
            machineName: m.name,
            podCount: hostPods.length,
            cpu: st.cpu,
            memGi: st.memGi,
            childCount: nsList.length,
          });
          link(mid, kid);
          if (showTree || isOpen(kid)) {
            nsList.forEach(({ ns, pods: nsPods }) => {
              const nid = `${kid}:ns:${ns}`;
              const nsState = nsRollup(nsPods);
              const workingHere = work.live && nsPods.some((p) => workHitsPod(work, p));
              const counts = { ok: 0, run: 0, warn: 0, bad: 0, wait: 0 };
              nsPods.forEach((p) => {
                const ps = podState(p);
                counts[ps] = (counts[ps] || 0) + 1;
              });
              add({
                id: nid,
                kind: "ns",
                state: workingHere && nsState !== "bad" ? "run" : nsState,
                title: ns,
                subtitle: workingHere
                  ? `installing ${work.item || work.service}`
                  : `${nsPods.length} containers · ${nsKindLabel(ns)}`,
                machineName: m.name,
                namespace: ns,
                counts,
                childCount: nsPods.length,
                hot: workingHere,
              });
              link(kid, nid);
              const podNodes = [];
              if (showTree || isOpen(nid)) {
                nsPods.forEach((p) => {
                  const id = `pod:${p.namespace || ""}/${p.name}`;
                  const hostVms = isNovaComputePod(p) ? vmsByHost.get(m.name) || [] : [];
                  add({
                    id,
                    kind: "pod",
                    state: workHitsPod(work, p) && podState(p) !== "bad" ? "run" : podState(p),
                    title: p.name,
                    hot: workHitsPod(work, p),
                    subtitle: p.phase || "",
                    machineName: m.name,
                    namespace: p.namespace || "",
                    name: p.name,
                    node: p.node || "",
                    phase: p.phase || "",
                    ready: p.ready,
                    restarts: p.restarts,
                    reason: p.reason || "",
                    containers: Array.isArray(p.containers) ? p.containers : [],
                    controllers: Array.isArray(p.controllers) ? p.controllers : [],
                    childCount: hostVms.length,
                  });
                  link(nid, id);
                  const vmNodes = [];
                  if (showTree || isOpen(id)) {
                    hostVms.forEach((v) => {
                      const vid = `vm:${v.id}`;
                      const nets = vmNetworks(v);
                      add({
                        id: vid,
                        kind: "vm",
                        state: vmState(v),
                        title: v.name || v.id,
                        subtitle: [v.status, v.flavor].filter(Boolean).join(" · "),
                        machineName: m.name,
                        vmId: v.id,
                        status: v.status,
                        flavor: v.flavor,
                        image: v.image,
                        host: v.host,
                        addresses: v.addresses,
                        network: nets[0] || "",
                        networks: nets,
                        project_id: v.project_id || "",
                        project_name: v.project_name || "",
                        tenant: v.project_name || v.project_id || v.tenant || "",
                      });
                      link(id, vid);
                      vmNodes.push(byId.get(vid));
                    });
                  }
                  const node = byId.get(id);
                  if (node) node.vmNodes = vmNodes;
                  podNodes.push(node);
                });
              }
              if (isOpen(kid)) nsGroups.push({ nid, podNodes });
            });
          }
        }
        rows.push({ mid, kid, nsGroups, showK8s });
      }
    }
    attachCloudStack(add, link, byId);
  }

  const keep = branchIds(mapFocus, edges);
  const km = edgeKidsMap(edges);
  const place = (n, x, y, w, h) => {
    if (!n) return;
    if (keep && !keep.has(n.id)) return;
    n.x = x;
    n.y = y;
    n.w = w;
    n.h = h;
  };

  const [ew, eh] = dim("env", "env");
  const [gw, gh] = dim("group", "infra");
  const [rw, rh] = dim("registry", "registry");
  const [tw, th] = dim("tests", "tests");
  const cacheH = cacheNodes.reduce((a, n, i) => a + dim("regcache", n && n.id)[1] + (i ? GAP_Y : 0), 0);
  const testH = testNodes.reduce((a, n, i) => a + dim("testsuite", n && n.id)[1] + (i ? GAP_Y : 0), 0);

  const visId = (id) => !keep || keep.has(id);
  const visVm = (v) => v && visId(v.id);
  const visPod = (p) => p && visId(p.id);
  const visNs = (g) => g && (visId(g.nid) || g.podNodes.some(visPod));

  const podBlockH = (p) => {
    const [, ph] = dim("pod", p.id);
    const vms = (p.vmNodes || []).filter(visVm);
    if (!vms.length) return ph;
    const vmsH = vms.reduce((a, v, i) => a + dim("vm", v.id)[1] + (i ? GAP_Y : 0), 0);
    return Math.max(ph, vmsH);
  };
  const nsBlockH = (g) => {
    const [, nh] = dim("ns", g.nid);
    const pods = g.podNodes.filter(visPod);
    if (!pods.length) return nh;
    const podsH = pods.reduce((a, p, i) => a + podBlockH(p) + (i ? GAP_Y : 0), 0);
    return Math.max(nh, podsH);
  };
  const visRows = rows.filter((r) => visId(r.mid) || (r.showK8s && visId(r.kid)));
  const rowHeights = visRows.map((r) => {
    const [, mh] = dim("machine", r.mid);
    if (!r.showK8s) return mh;
    const [, kh] = dim("k8s", r.kid);
    const groups = r.nsGroups.filter(visNs);
    if (!groups.length) return Math.max(mh, kh);
    const nsH = groups.reduce((a, g) => a + nsBlockH(g), 0) + Math.max(0, groups.length - 1) * GAP_Y;
    return Math.max(mh, kh, nsH);
  });
  const totalH = rowHeights.reduce((a, h) => a + h + ROW_GAP, 0) || (keep ? 0 : 280);

  place(byId.get("env"), 0, 0, ew, eh);
  let yLeft = eh + ROW_GAP;
  if (byId.get("registry") && visId("registry")) {
    place(byId.get("registry"), 0, yLeft, rw, rh);
    yLeft += rh + GAP_Y;
    if (cacheNodes.length) {
      stack(cacheNodes, 0, yLeft);
      yLeft += cacheH + GAP_Y;
    }
  }
  if (byId.get("tests") && visId("tests")) {
    place(byId.get("tests"), 0, yLeft, tw, th);
    yLeft += th + GAP_Y;
    if (testNodes.length) {
      stack(testNodes, 0, yLeft);
      yLeft += testH + GAP_Y;
    }
  }
  const leftH = yLeft;
  const fabricH = totalH;
  const colH = Math.max(leftH, fabricH);
  place(byId.get("infra"), COL, Math.max(0, (colH - gh) / 2), gw, gh);

  let y = Math.max(0, (colH - fabricH) / 2);
  visRows.forEach((r, i) => {
    const rowH = rowHeights[i];
    const [mw, mh] = dim("machine", r.mid);
    const [kw, kh] = dim("k8s", r.kid);
    place(byId.get(r.mid), COL * 2, y + (rowH - mh) / 2, mw, mh);
    if (r.showK8s) place(byId.get(r.kid), COL * 3, y + (rowH - kh) / 2, kw, kh);
    const groups = r.nsGroups.filter(visNs);
    const groupsH = groups.reduce((a, g) => a + nsBlockH(g), 0) + Math.max(0, groups.length - 1) * GAP_Y;
    let nsY = y + Math.max(0, (rowH - groupsH) / 2);
    groups.forEach((g) => {
      const blockH = nsBlockH(g);
      const [nw, nh] = dim("ns", g.nid);
      place(byId.get(g.nid), COL * 4, nsY + (blockH - nh) / 2, nw, nh);
      const pods = g.podNodes.filter(visPod);
      if (pods.length) {
        const podsH = pods.reduce((a, p, i) => a + podBlockH(p) + (i ? GAP_Y : 0), 0);
        let podY = nsY + Math.max(0, (blockH - podsH) / 2);
        pods.forEach((p) => {
          const block = podBlockH(p);
          const [pw, ph] = dim("pod", p.id);
          place(p, COL * 5, podY + (block - ph) / 2, pw, ph);
          const vms = (p.vmNodes || []).filter(visVm);
          if (vms.length) {
            const vmsH = vms.reduce((a, v, i) => a + dim("vm", v.id)[1] + (i ? GAP_Y : 0), 0);
            stack(vms, COL * 6, podY + Math.max(0, (block - vmsH) / 2));
          }
          podY += block + GAP_Y;
        });
      }
      nsY += blockH + GAP_Y;
    });
    y += rowH + ROW_GAP;
  });

  const placeDown = (id, x, y0, depth) => {
    const n = byId.get(id);
    if (!n || !visId(id)) return y0;
    const [nw, nh] = dim(n.kind, id);
    if (n.x == null) place(n, x, y0, nw, nh);
    if (!isOpen(id) || depth > 8) return (n.y || y0) + (n.h || nh);
    if (!mapFocus && depth >= 1) return (n.y || y0) + (n.h || nh);
    let ky = n.y != null ? n.y : y0;
    let bottom = (n.y || y0) + (n.h || nh);
    for (const kid of km.get(id) || []) {
      const kn = byId.get(kid);
      if (!kn || kn.x != null) continue;
      if (!visId(kid)) continue;
      const b = placeDown(kid, (n.x || x) + COL, ky, depth + 1);
      ky = b + GAP_Y;
      bottom = Math.max(bottom, b);
    }
    return bottom;
  };
  const cloudY = colH + ROW_GAP * 1.6;
  const bands = [
    ["overlay", 0],
    ["tenants", COL * 3],
    ["edge", COL * 6],
  ];
  for (const [id, x] of bands) {
    const n = byId.get(id);
    if (!n || !visId(id)) continue;
    const [bw, bh] = dim(n.kind, id);
    place(n, x, cloudY, bw, bh);
    if (isOpen(id)) placeDown(id, x, cloudY, 0);
  }

  lastGraph = { nodes, edges, byId, podsByHost, live: work };
  paintAncestors(lastGraph);
  return lastGraph;
}

function bezier(x1, y1, x2, y2) {
  const c = Math.max(48, Math.abs(x2 - x1) * 0.45);
  return `M${x1},${y1} C${x1 + c},${y1} ${x2 - c},${y2} ${x2},${y2}`;
}

function pillDot(state) {
  return `<span class="sf-dot ${esc(state || "wait")}"></span>`;
}

function fmtBytes(n) {
  if (n == null || !Number.isFinite(Number(n))) return "—";
  const v = Number(n);
  const gi = v / 1024 ** 3;
  if (gi >= 100) return `${gi.toFixed(0)} GiB`;
  if (gi >= 1) return `${gi.toFixed(1)} GiB`;
  const mi = v / 1024 ** 2;
  if (mi >= 1) return `${mi.toFixed(0)} MiB`;
  return `${Math.max(0, v / 1024).toFixed(0)} KiB`;
}

function fmtCores(n) {
  if (n == null || !Number.isFinite(Number(n))) return "—";
  const v = Number(n);
  if (Math.abs(v) < 0.001) return "0";
  if (v >= 10) return v.toFixed(1);
  if (v >= 1) return v.toFixed(2);
  return `${Math.round(v * 1000)}m`;
}

function band(pct) {
  if (pct == null || !Number.isFinite(Number(pct))) return "wait";
  if (pct >= 85) return "bad";
  if (pct >= 65) return "warn";
  if (pct >= 35) return "run";
  return "ok";
}

function pushHist(bucket, key, sample) {
  if (!liveHist[bucket]) liveHist[bucket] = bucket === "cluster" ? [] : {};
  if (bucket === "cluster") {
    liveHist.cluster.push(sample);
    if (liveHist.cluster.length > HIST_MAX) liveHist.cluster.shift();
    return;
  }
  const map = liveHist[bucket];
  const arr = map[key] || (map[key] = []);
  arr.push(sample);
  if (arr.length > HIST_MAX) arr.shift();
}

function liveNodeFor(machineName) {
  const rows = (livePack() && livePack().nodes) || [];
  if (!machineName || !rows.length) return null;
  const m = findMachine(machineName) || { name: machineName };
  const keys = machineKeys(m);
  return (
    rows.find((n) => {
      const name = String(n.name || "");
      return keys.has(name.toLowerCase()) || keys.has(shortName(name).toLowerCase());
    }) || null
  );
}

function livePodFor(ns, name) {
  const rows = (livePack() && livePack().pods) || [];
  return rows.find((p) => p.ns === ns && p.name === name) || null;
}

function livePack() {
  return shownLive || liveMetrics;
}

function liveForGraphNode(n) {
  const pack = livePack();
  // A missing live-metrics route stores an error pack with an empty cluster.
  // Treat that as no series so the inspector shows the quiet sentence
  // instead of blank CPU / RAM / Disk bars.
  if (!n || !pack || pack.error) return null;
  if (n.kind === "env" || n.kind === "group") {
    return { kind: "cluster", cluster: pack.cluster, cpus: pack.cpus || [] };
  }
  if (n.kind === "machine" || n.kind === "k8s" || n.machineName) {
    const row = liveNodeFor(n.machineName || "");
    return row ? { kind: "node", node: row, cpus: row.cpus || [] } : null;
  }
  if (n.kind === "pod") {
    const row = livePodFor(n.namespace, n.name || n.title);
    return row ? { kind: "pod", pod: row } : null;
  }
  if (n.kind === "ns") {
    const host = liveNodeFor(n.machineName || "");
    const hostName = host && host.name;
    const pods = ((pack.pods) || []).filter((p) => {
      if (p.ns !== n.namespace) return false;
      if (!hostName) return true;
      return p.node === hostName || shortName(p.node) === shortName(hostName);
    });
    return {
      kind: "ns",
      cpu: pods.reduce((a, p) => a + Number(p.cpu || 0), 0),
      mem: pods.reduce((a, p) => a + Number(p.mem || 0), 0),
      pods,
    };
  }
  return null;
}

function barKind(label) {
  const l = String(label || "").toLowerCase();
  if (l === "cpu") return "cpu";
  if (l === "ram" || l === "mem") return "mem";
  if (l === "disk") return "disk";
  return l || "x";
}

function usageBar(label, pct, sub) {
  const p = pct == null || !Number.isFinite(Number(pct)) ? null : Math.max(0, Math.min(100, Number(pct)));
  const w = p == null ? 0 : p;
  return `<div class="sf-bar" data-bar="${esc(barKind(label))}" title="${esc(label)} ${p == null ? "n/a" : `${p.toFixed(0)}%`}">
    <span class="sf-bar-lab">${esc(label)}</span>
    <span class="sf-bar-track"><span class="sf-bar-fill ${band(p)}" style="width:${w.toFixed(1)}%"></span></span>
    <span class="sf-bar-val">${p == null ? "—" : `${p.toFixed(0)}%`}${sub ? ` <em>${esc(sub)}</em>` : ""}</span>
  </div>`;
}

function cpuHeat(cpus, limit) {
  const rows = Array.isArray(cpus) ? cpus : [];
  const slice = limit && rows.length > limit ? rows.slice(0, limit) : rows;
  if (!slice.length) return "";
  const cells = slice
    .map((c) => {
      const pct = Number(c.pct);
      const title = `${c.node ? shortName(c.node) + " " : ""}CPU ${c.id} · ${Number.isFinite(pct) ? pct.toFixed(0) : "—"}%`;
      return `<span class="sf-cpu ${band(pct)}" title="${esc(title)}"></span>`;
    })
    .join("");
  const more = rows.length > slice.length ? `<span class="sf-cpu-more">+${rows.length - slice.length}</span>` : "";
  return `<div class="sf-cpuheat" aria-label="per-CPU">${cells}${more}</div>`;
}

function sparklineMini(points, color) {
  const vals = (points || []).map((p) => Number(p)).filter((v) => Number.isFinite(v));
  if (vals.length < 2) return `<span class="sf-spark empty"></span>`;
  const w = 108;
  const h = 28;
  let min = Math.min(...vals);
  let max = Math.max(...vals);
  if (min === max) {
    min = Math.max(0, min - 5);
    max = Math.min(100, max + 5);
  }
  const span = max - min || 1;
  const coords = vals.map((v, i) => {
    const x = (i / (vals.length - 1)) * w;
    const y = h - ((v - min) / span) * (h - 4) - 2;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const line = coords.map((c, i) => `${i ? "L" : "M"}${c}`).join(" ");
  return `<svg class="sf-spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true"><path d="${line}" fill="none" stroke="${color || "var(--accent)"}" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
}

function liveStrip(n) {
  const live = liveForGraphNode(n);
  if (!live) return `<div class="sf-live" data-live></div>`;
  if (live.kind === "cluster") {
    const c = live.cluster || {};
    return `<div class="sf-live" data-live data-kind="cluster">
      ${usageBar("CPU", c.cpu && c.cpu.pct, `${fmtCores(c.cpu && c.cpu.used)}/${fmtCores(c.cpu && c.cpu.cap)}`)}
      ${usageBar("RAM", c.mem && c.mem.pct, fmtBytes(c.mem && c.mem.used))}
      ${usageBar("Disk", c.disk && c.disk.pct, fmtBytes(c.disk && c.disk.used))}
      ${cpuHeat(live.cpus, n.kind === "env" ? 64 : 96)}
    </div>`;
  }
  if (live.kind === "node") {
    const row = live.node;
    return `<div class="sf-live" data-live data-kind="node">
      ${usageBar("CPU", row.cpu && row.cpu.pct, `${fmtCores(row.cpu && row.cpu.used)}/${row.cores || "—"}`)}
      ${usageBar("RAM", row.mem && row.mem.pct, fmtBytes(row.mem && row.mem.used))}
      ${usageBar("Disk", row.disk && row.disk.pct, fmtBytes(row.disk && row.disk.used))}
      ${cpuHeat(live.cpus, 32)}
    </div>`;
  }
  if (live.kind === "pod") {
    const p = live.pod;
    const host = liveNodeFor(n.machineName || n.node || "");
    const cpuPct = host && host.cpu && host.cpu.cap ? Math.min(100, (Number(p.cpu) / host.cpu.cap) * 100) : Math.min(100, Number(p.cpu) * 100);
    const memPct = host && host.mem && host.mem.cap ? Math.min(100, (Number(p.mem) / host.mem.cap) * 100) : null;
    return `<div class="sf-live" data-live data-kind="pod">
      ${usageBar("CPU", Number.isFinite(cpuPct) ? cpuPct : null, fmtCores(p.cpu))}
      ${usageBar("RAM", memPct, fmtBytes(p.mem))}
    </div>`;
  }
  if (live.kind === "ns") {
    const host = liveNodeFor(n.machineName || "");
    const cpuPct = host && host.cpu && host.cpu.cap ? Math.min(100, (Number(live.cpu) / host.cpu.cap) * 100) : null;
    const memPct = host && host.mem && host.mem.cap ? Math.min(100, (Number(live.mem) / host.mem.cap) * 100) : null;
    return `<div class="sf-live" data-live data-kind="ns">
      ${usageBar("CPU", cpuPct, fmtCores(live.cpu))}
      ${usageBar("RAM", memPct, fmtBytes(live.mem))}
    </div>`;
  }
  return "";
}

function liveDetailHtml(id) {
  const n = lastGraph.byId.get(id);
  const live = liveForGraphNode(n);
  if (metricsMissing) return "";
  if (!liveMetrics) return "";
  if (liveMetrics.error && !live) return "";
  if (!live) return "";
  const histCluster = liveHist.cluster || [];
  if (live.kind === "cluster") {
    const c = live.cluster || {};
    const cpuPts = histCluster.map((s) => s.cpu);
    const memPts = histCluster.map((s) => s.mem);
    const diskPts = histCluster.map((s) => s.disk);
    return `<div class="dm-live">
      <div class="dm-live-pair">${usageBar("CPU", c.cpu && c.cpu.pct, `${fmtCores(c.cpu && c.cpu.used)} / ${fmtCores(c.cpu && c.cpu.cap)}`)}${sparklineMini(cpuPts, "var(--ok)")}</div>
      <div class="dm-live-pair">${usageBar("RAM", c.mem && c.mem.pct, `${fmtBytes(c.mem && c.mem.used)} / ${fmtBytes(c.mem && c.mem.cap)}`)}${sparklineMini(memPts, "#58a6ff")}</div>
      <div class="dm-live-pair">${usageBar("Disk", c.disk && c.disk.pct, `${fmtBytes(c.disk && c.disk.used)} / ${fmtBytes(c.disk && c.disk.cap)}`)}${sparklineMini(diskPts, "#d29922")}</div>
      <div class="dm-live-kicker">${(live.cpus || []).length} CPUs</div>
      ${cpuHeat(live.cpus)}
    </div>`;
  }
  if (live.kind === "node") {
    const row = live.node;
    const hist = (liveHist.nodes && liveHist.nodes[row.name]) || [];
    const bars = (live.cpus || [])
      .map(
        (c) =>
          `<div class="dm-cpu-row"><span>CPU ${esc(String(c.id))}</span>${usageBar("", c.pct)}</div>`
      )
      .join("");
    return `<div class="dm-live">
      <div class="dm-live-pair">${usageBar("CPU", row.cpu && row.cpu.pct, `${fmtCores(row.cpu && row.cpu.used)} / ${row.cores}`)}${sparklineMini(hist.map((s) => s.cpu), "var(--ok)")}</div>
      <div class="dm-live-pair">${usageBar("RAM", row.mem && row.mem.pct, `${fmtBytes(row.mem && row.mem.used)} / ${fmtBytes(row.mem && row.mem.cap)}`)}${sparklineMini(hist.map((s) => s.mem), "#58a6ff")}</div>
      <div class="dm-live-pair">${usageBar("Disk", row.disk && row.disk.pct, `${fmtBytes(row.disk && row.disk.used)} / ${fmtBytes(row.disk && row.disk.cap)}`)}${sparklineMini(hist.map((s) => s.disk), "#d29922")}</div>
      <div class="dm-live-kicker">${row.cores || 0} CPUs on ${esc(shortName(row.name))}</div>
      <div class="dm-cpu-list">${bars}</div>
    </div>`;
  }
  if (live.kind === "pod") {
    const p = live.pod;
    const key = `${p.ns}/${p.name}`;
    const hist = (liveHist.pods && liveHist.pods[key]) || [];
    return `<div class="dm-live">
      <div class="dm-live-pair">${usageBar("CPU", Math.min(100, Number(p.cpu || 0) * 100), fmtCores(p.cpu))}${sparklineMini(hist.map((s) => s.cpu), "var(--ok)")}</div>
      <div class="dm-live-pair">${usageBar("RAM", null, fmtBytes(p.mem))}${sparklineMini(hist.map((s) => s.memGi), "#58a6ff")}</div>
    </div>`;
  }
  if (live.kind === "ns") {
    const rows = (live.pods || [])
      .slice()
      .sort((a, b) => Number(b.cpu || 0) - Number(a.cpu || 0))
      .slice(0, 16)
      .map(
        (p) =>
          `<li><code>${esc(p.name)}</code> ${esc(fmtCores(p.cpu))} · ${esc(fmtBytes(p.mem))}</li>`
      )
      .join("");
    return `<div class="dm-live">
      ${usageBar("CPU", null, fmtCores(live.cpu))}
      ${usageBar("RAM", null, fmtBytes(live.mem))}
      <ul class="dm-items">${rows || "<li class='muted'>No pod series in this namespace.</li>"}</ul>
    </div>`;
  }
  return "";
}

function kindLabel(kind) {
  return KIND_LABEL[kind] || "Node";
}

function pathLine(node) {
  const bits = (node && node.path) || [];
  if (!bits.length) return "";
  return `<p class="muted">${bits.map((b) => esc(String(b))).join(" → ")}</p>`;
}

function kvTable(rows) {
  const body = (rows || [])
    .filter((r) => r && r[1] != null && r[1] !== "")
    .map(([k, v]) => `<tr><td>${esc(k)}</td><td colspan="2">${esc(String(v))}</td></tr>`)
    .join("");
  return body ? `<table class="dm-check"><tbody>${body}</tbody></table>` : "";
}

function healthWord(state) {
  return HEALTH_WORD[state] || "unknown";
}

function sfChip(label, state) {
  if (!label) return "";
  return `<span class="sf-chip ${esc(state || "")}">${esc(label)}</span>`;
}

function healthMetrics(n) {
  if (!n) return "";
  const chips = [];
  if (n.kind === "env") {
    const machines = machineRows();
    const metal = isMetalRebuild();
    const ready = machines.filter((m) => machineState(m).k8sReady).length;
    chips.push(
      sfChip(
        metal
          ? ready
            ? `${ready} still ready, rebuild in progress`
            : `Metal rebuild · ${machines.length} booting`
          : fleetLine(machines),
        metal ? (ready ? "run" : "bad") : machines.length && ready === machines.length ? "ok" : ready ? "run" : "wait"
      )
    );
    const blocked = (lastGraph.nodes || []).filter((x) => x.state === "bad").length;
    if (blocked) chips.push(sfChip(`${blocked} blocked`, "bad"));
    else chips.push(sfChip(healthWord(n.state), n.state));
  } else if (n.kind === "group") {
    chips.push(sfChip(healthWord(n.state), n.state));
  } else if (n.kind === "registry") {
    chips.push(sfChip(`${n.caches ? n.caches.filter((c) => c.running).length : 0}/${(n.caches || []).length} up`, n.state));
    chips.push(sfChip(`${n.images || 0} images`, n.images ? "ok" : "wait"));
    if (n.bind) chips.push(sfChip(n.bind, "ok"));
  } else if (n.kind === "regcache") {
    chips.push(sfChip(n.running ? "up" : "down", n.running ? "ok" : "bad"));
    chips.push(sfChip(`${n.images || 0} images`, n.images ? "ok" : "wait"));
    if (n.port) chips.push(sfChip(`:${n.port}`, ""));
  } else if (n.kind === "tests") {
    chips.push(sfChip(n.subtitle || healthWord(n.state), n.state));
    if (n.chart) chips.push(sfChip("chart installed", "ok"));
    else chips.push(sfChip("chart missing", "wait"));
  } else if (n.kind === "testsuite") {
    const row = n.row || {};
    chips.push(sfChip(row.status || "never", n.state));
    if (row.ran_count) chips.push(sfChip(`${row.passed || 0}/${row.ran_count}`, n.state));
  } else if (n.kind === "machine") {
    const m = findMachine(n.machineName);
    const st = m ? machineState(m) : {};
    const metal = isMetalRebuild();
    const dhcpWait = /^waiting for DHCP$/i.test(String((m && m.pxe && m.pxe.phase) || ""));
    if (metal && dhcpWait && st.k8sReady) chips.push(sfChip("still ready, rebuild in progress", "run"));
    if (n.cpu) chips.push(sfChip(`${n.cpu} CPU`, "ok"));
    if (n.memGi != null && n.memGi !== "") chips.push(sfChip(`${n.memGi} Gi`, "ok"));
    if (n.podCount) chips.push(sfChip(`${n.podCount} pods`, n.state === "bad" ? "bad" : ""));
  } else if (n.kind === "k8s") {
    chips.push(sfChip(n.state === "ok" ? "Ready" : "not Ready", n.state === "ok" ? "ok" : n.state));
    if (n.podCount != null) chips.push(sfChip(`${n.podCount} pods`, n.state === "bad" ? "bad" : ""));
    if (n.cpu) chips.push(sfChip(`${n.cpu} CPU`, "ok"));
    if (n.memGi != null && n.memGi !== "") chips.push(sfChip(`${n.memGi} Gi`, "ok"));
    const m = findMachine(n.machineName);
    const st = m ? machineState(m) : {};
    if (st.k8sVer) chips.push(sfChip(st.k8sVer, ""));
  } else if (n.kind === "ns") {
    const c = n.counts || {};
    const total = (c.ok || 0) + (c.run || 0) + (c.warn || 0) + (c.bad || 0) + (c.wait || 0);
    chips.push(sfChip(`${total} pods`, c.bad ? "bad" : c.run ? "run" : total ? "ok" : "wait"));
    if (c.bad) chips.push(sfChip(`${c.bad} blocked`, "bad"));
    else if (c.run) chips.push(sfChip(`${c.run} starting`, "run"));
  } else if (n.kind === "pod") {
    chips.push(sfChip(n.phase || healthWord(n.state), n.state));
    if (n.ready != null && n.ready !== "") chips.push(sfChip(String(n.ready), n.state === "ok" ? "ok" : n.state));
    if (n.restarts) chips.push(sfChip(`${n.restarts} rst`, Number(n.restarts) > 3 ? "bad" : "warn"));
  } else if (n.kind === "vm") {
    chips.push(sfChip(n.status || "VM", n.state));
    if (n.flavor) chips.push(sfChip(n.flavor, ""));
  } else {
    chips.push(sfChip(healthWord(n.state), n.state));
  }
  if (!chips.length) return "";
  return `<div class="sf-metrics" aria-label="health">${chips.join("")}</div>`;
}

function consoleButton(node) {
  if (!node) return "";
  if (node.kind === "vm" && node.vmId) {
    return `<button type="button" class="btn-sm" data-dm-open-console data-kind="vm" data-id="${esc(node.vmId)}">Console</button>`;
  }
  if (node.kind === "pod") {
    const ns = node.namespace || "default";
    const pn = node.name || node.title || "";
    if (!pn) return "";
    return `<button type="button" class="btn-sm" data-dm-open-console data-kind="pod" data-ns="${esc(ns)}" data-pod="${esc(pn)}">Console</button>`;
  }
  if (node.kind === "machine" || node.kind === "k8s" || node.kind === "osrole") {
    const name = node.machineName || (node.id || "").replace(/^m:/, "");
    if (!name) return "";
    return `<button type="button" class="btn-sm" data-dm-open-console data-kind="machine" data-name="${esc(name)}">Console</button>`;
  }
  return "";
}

function wrapDetail(node, html) {
  const cons = consoleButton(node);
  return `<article class="dm-detail ${esc(node.state || "wait")}">
    <div class="dm-detail-kicker">${esc(kindLabel(node.kind))} · ${esc(healthWord(node.state))}</div>
    ${healthMetrics(node)}
    ${cons ? `<div class="dm-insp-actions">${cons}</div>` : ""}
    <div id="dm-live-slot" class="dm-live-slot"></div>
    ${html}
  </article>`;
}

function nodeInner(n) {
  const sel = n.id === selected ? " sel" : "";
  const dash = n.dashed ? " dashed" : "";
  const min = minimized.has(n.id);
  const hot = n.hot ? " hot" : "";
  const inH = n.kind === "env" ? "" : `<span class="sf-handle sf-handle-in"></span>`;
  const outH = n.kind === "pod" ? "" : `<span class="sf-handle sf-handle-out"></span>`;
  const open = isOpen(n.id);
  const twist =
    (n.childCount || 0) > 0
      ? `<button type="button" class="sf-twist" data-toggle="${esc(n.id)}" aria-expanded="${
          open ? "true" : "false"
        }" title="${open ? "Collapse" : "Expand"}">${open ? "▾" : "▸"}</button>`
      : `<span class="sf-twist sf-twist-spacer"></span>`;
  const minBtn = `<button type="button" class="sf-minbtn" data-min="${esc(n.id)}" title="${
    min ? "Restore" : "Minimize"
  }">${min ? "▢" : "–"}</button>`;
  const focused = n.id === mapFocus;
  const hamBtn = `<button type="button" class="sf-hambtn${focused ? " on" : ""}" data-hammer="${esc(
    n.id
  )}" title="${focused ? "Show full fabric" : "Hammer down this branch"}">↧</button>`;
  const metrics = min ? "" : healthMetrics(n);
  const live = min ? "" : `<div class="sf-live" data-live></div>`;
  return `${inH}${outH}
    <div class="sf-card ${esc(n.kind)} ${esc(n.state || "wait")}${sel}${dash}${min ? " min" : ""}${
    focused ? " focus" : ""
  }${hot}" data-sf-id="${esc(n.id)}" data-kind="${esc(n.kind || "")}">
      <div class="sf-card-bar"></div>
      <div class="sf-card-body">
        <div class="sf-card-title">${twist}<span>${esc(n.title || "")}</span>${hamBtn}${minBtn}</div>
        ${min ? "" : `<div class="sf-card-sub">${esc(n.subtitle || "")}</div>${metrics}${live}`}
      </div>
    </div>`;
}

function applyView() {
  const world = document.getElementById("dm-world");
  if (world) world.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.k})`;
  const flow = document.getElementById("dm-flow");
  if (flow) {
    const size = 18 * view.k;
    flow.style.backgroundSize = `${size}px ${size}px`;
    flow.style.backgroundPosition = `${view.x}px ${view.y}px`;
  }
  renderMinimap();
  const pop = document.getElementById("dm-node-pop");
  if (pop && !pop.hidden) renderNodePop();
}

function worldSize(graph) {
  let maxX = 400;
  let maxY = 300;
  for (const n of graph.nodes) {
    maxX = Math.max(maxX, (n.x || 0) + (n.w || 0) + 80);
    maxY = Math.max(maxY, (n.y || 0) + (n.h || 0) + 80);
  }
  return { w: maxX, h: maxY };
}

function edgeState(target) {
  const st = String((target && target.state) || "wait");
  if (st === "ok" || st === "run" || st === "warn" || st === "bad" || st === "wait") return st;
  return "wait";
}

function drawEdges(graph) {
  const svg = document.getElementById("dm-edges");
  if (!svg) return;
  const { w, h } = worldSize(graph);
  svg.setAttribute("width", String(w));
  svg.setAttribute("height", String(h));
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  const byId = graph.byId;
  const marks = [
    ["ok", "#3fb950"],
    ["run", "#d29922"],
    ["warn", "#f0883e"],
    ["bad", "#f85149"],
    ["wait", "#6e6e6e"],
  ]
    .map(
      ([id, fill]) =>
        `<marker id="sf-arrow-${id}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="${fill}"></path></marker>`
    )
    .join("");
  const paths = graph.edges
    .map((e) => {
      const a = byId.get(e.source);
      const b = byId.get(e.target);
      if (!a || !b || a.x == null || b.x == null) return "";
      const x1 = a.x + a.w;
      const y1 = a.y + a.h / 2;
      const x2 = b.x;
      const y2 = b.y + b.h / 2;
      const st = edgeState(b);
      return `<path class="sf-edge ${st}" d="${bezier(x1, y1, x2, y2)}" />`;
    })
    .join("");
  svg.innerHTML = `<defs>${marks}</defs>${paths}`;
}

function syncNodes(graph) {
  const root = document.getElementById("dm-nodes");
  if (!root) return;
  const have = new Map();
  root.querySelectorAll("[data-node]").forEach((el) => have.set(el.getAttribute("data-node"), el));
  const keep = new Set();
  for (const n of graph.nodes) {
    if (n.x == null || n.y == null) continue;
    keep.add(n.id);
    let wrap = have.get(n.id);
    if (!wrap) {
      wrap = document.createElement("div");
      wrap.className = "sf-node";
      wrap.setAttribute("data-node", n.id);
      root.appendChild(wrap);
    }
    wrap.className = `sf-node ${n.state || "wait"}${n.id === mapFocus ? " focus" : ""}`;
    wrap.style.transform = `translate(${n.x}px, ${n.y}px)`;
    wrap.style.width = `${n.w}px`;
    wrap.style.height = `${n.h}px`;
    const html = nodeInner(n);
    if (wrap.dataset.html !== html) {
      wrap.innerHTML = html;
      wrap.dataset.html = html;
    }
    paintLiveOnWrap(wrap, n);
  }
  have.forEach((el, id) => {
    if (!keep.has(id)) el.remove();
  });
}

function minimapMetrics() {
  const canvas = document.getElementById("dm-minimap");
  const graph = lastGraph;
  if (!canvas) return null;
  let maxX = 1;
  let maxY = 1;
  for (const n of graph.nodes || []) {
    maxX = Math.max(maxX, (n.x || 0) + (n.w || 0));
    maxY = Math.max(maxY, (n.y || 0) + (n.h || 0));
  }
  const w = canvas.width;
  const h = canvas.height;
  const s = Math.min((w - 8) / maxX, (h - 8) / maxY);
  return { canvas, w, h, s, maxX, maxY };
}

function minimapToWorld(clientX, clientY) {
  const m = minimapMetrics();
  if (!m || !m.s) return { wx: 0, wy: 0 };
  const rect = m.canvas.getBoundingClientRect();
  const x = ((clientX - rect.left) / Math.max(1, rect.width)) * m.w;
  const y = ((clientY - rect.top) / Math.max(1, rect.height)) * m.h;
  return { wx: (x - 4) / m.s, wy: (y - 4) / m.s };
}

function panToWorld(wx, wy) {
  const flow = document.getElementById("dm-flow");
  if (!flow) return;
  view.x = flow.clientWidth / 2 - wx * view.k;
  view.y = flow.clientHeight / 2 - wy * view.k;
  view._userMoved = true;
  applyView();
}

function renderMinimap() {
  const m = minimapMetrics();
  if (!m || !m.canvas.getContext) return;
  const ctx = m.canvas.getContext("2d");
  ctx.clearRect(0, 0, m.w, m.h);
  ctx.fillStyle = "#070e09";
  ctx.fillRect(0, 0, m.w, m.h);
  const placed = lastGraph.nodes.filter((n) => n.x != null && n.y != null);
  if (!placed.length) return;
  const colors = { ok: "#3fb950", wait: "#6e6e6e", run: "#d29922", warn: "#f0883e", bad: "#f85149" };
  for (const n of placed) {
    ctx.fillStyle = colors[n.state] || colors.wait;
    ctx.globalAlpha = n.id === selected ? 1 : 0.7;
    ctx.fillRect(4 + n.x * m.s, 4 + n.y * m.s, Math.max(3, n.w * m.s), Math.max(2, n.h * m.s));
  }
  ctx.globalAlpha = 1;
  const flow = document.getElementById("dm-flow");
  if (!flow) return;
  const vw = flow.clientWidth / view.k;
  const vh = flow.clientHeight / view.k;
  const vx = -view.x / view.k;
  const vy = -view.y / view.k;
  ctx.strokeStyle = "#34c759";
  ctx.lineWidth = 1;
  ctx.strokeRect(4 + vx * m.s, 4 + vy * m.s, vw * m.s, vh * m.s);
}

function fitView() {
  if (mapFocus) {
    fitPlaced();
    return;
  }
  focusStart();
}

function watchShellSize() {
  const shell = document.getElementById("dm-card");
  if (!shell || typeof ResizeObserver === "undefined") return;
  if (resizeObs) {
    resizeObs.disconnect();
    resizeObs = null;
  }
  resizeObs = new ResizeObserver(() => {
    if (space3dOn) {
      resizeSpace3d();
      return;
    }
    if (nestOn) {
      resizeHoneycomb();
      positionNestConsole();
      return;
    }
    if (!view._userMoved) fitView();
    else applyView();
  });
  resizeObs.observe(shell);
}

function focusStart() {
  const flow = document.getElementById("dm-flow");
  const env = lastGraph.byId && lastGraph.byId.get("env");
  if (!flow || !env || env.x == null) return;
  const infra = lastGraph.byId.get("infra");
  const left = env.x;
  const right = (infra ? infra.x + infra.w : env.x + env.w) + COL * 0.7;
  const top = Math.min(env.y, infra && infra.y != null ? infra.y : env.y);
  const bot = Math.max(
    env.y + env.h,
    infra && infra.y != null ? infra.y + infra.h : env.y + env.h
  );
  const cx = (left + right) / 2;
  const cy = (top + bot) / 2;
  const vw = flow.clientWidth;
  const vh = flow.clientHeight;
  if (vw < 40 || vh < 40) return;
  const gw = Math.max(1, right - left);
  const gh = Math.max(1, bot - top);
  // Shrink only when the start of the graph does not fit. Leave a strip for the control bar.
  const k = Math.min(1.05, (vw - 56) / gw, (vh - 48) / gh);
  view.k = Math.max(0.2, k);
  view.x = vw / 2 - cx * view.k;
  view.y = vh / 2 - cy * view.k;
  applyView();
}

function isOpen(id) {
  return !collapsed.has(id);
}

function foldKey() {
  return envId ? `gsc-fold:v4:${envId}` : "";
}

function crumbPath(id) {
  const byId = lastGraph.byId;
  const parents = edgeParentsMap(lastGraph.edges || []);
  const path = [];
  let cur = id;
  const seen = new Set();
  while (cur && !seen.has(cur)) {
    seen.add(cur);
    const n = byId && byId.get(cur);
    if (n) path.push(n);
    if (cur === "env") break;
    const prefs = parents.get(cur) || [];
    const nest = n && n.nestParent;
    cur = nest && (prefs.includes(nest) || (byId && byId.get(nest))) ? nest : prefs[0] || "";
  }
  return path.reverse();
}

function expandFocusPath(id, edges) {
  if (!id) return;
  const parents = edgeParentsMap(edges);
  const q = [id];
  const seen = new Set();
  while (q.length) {
    const cur = q.pop();
    if (!cur || seen.has(cur)) continue;
    seen.add(cur);
    collapsed.delete(cur);
    userCollapsed.delete(cur);
    const prefs = parents.get(cur) || [];
    for (let i = 0; i < prefs.length; i++) q.push(prefs[i]);
  }
}

function fitPlaced() {
  const flow = document.getElementById("dm-flow");
  const placed = (lastGraph.nodes || []).filter((n) => n.x != null && n.y != null);
  if (!flow || !placed.length) return;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const n of placed) {
    minX = Math.min(minX, n.x);
    minY = Math.min(minY, n.y);
    maxX = Math.max(maxX, n.x + (n.w || 0));
    maxY = Math.max(maxY, n.y + (n.h || 0));
  }
  const pad = 64;
  const vw = Math.max(240, flow.clientWidth);
  const vh = Math.max(180, flow.clientHeight);
  const bw = Math.max(1, maxX - minX + pad * 2);
  const bh = Math.max(1, maxY - minY + pad * 2);
  view.k = Math.max(0.12, Math.min(1.15, Math.min(vw / bw, vh / bh)));
  view.x = vw / 2 - ((minX + maxX) / 2) * view.k;
  view.y = vh / 2 - ((minY + maxY) / 2) * view.k;
  view._userMoved = true;
  applyView();
}

function hammerDown(id) {
  const next = String(id || "");
  if (!next || next === "env" || next === mapFocus) {
    mapFocus = "";
  } else {
    mapFocus = next;
    expandFocusPath(mapFocus, lastGraph.edges || []);
    selected = mapFocus;
  }
  lastInsp = "";
  persistFold();
  renderAll();
  if (mapFocus) fitPlaced();
  else {
    view._userMoved = false;
    view._fitted = false;
    fitView();
  }
}

function hammerUp() {
  if (!mapFocus) return;
  const parents = edgeParentsMap(lastGraph.edges || []);
  const n = lastGraph.byId && lastGraph.byId.get(mapFocus);
  const prefs = parents.get(mapFocus) || [];
  const nest = n && n.nestParent;
  const up = nest && prefs.includes(nest) ? nest : prefs[0] || "";
  if (!up || up === "env") hammerDown("");
  else hammerDown(up);
}

function renderFocusHud() {
  const el = document.getElementById("dm-focus-hud");
  const flow = document.getElementById("dm-flow");
  if (!el) return;
  if (flow) flow.classList.toggle("hammered", !!mapFocus);
  if (!mapFocus) {
    el.hidden = true;
    el.innerHTML = "";
    return;
  }
  const crumbs = crumbPath(mapFocus);
  const bits = crumbs
    .map((c, i) => {
      const last = i === crumbs.length - 1;
      return `<button type="button" class="sf-crumb${last ? " here" : ""}" data-hammer="${esc(c.id)}">${esc(
        c.title || c.id
      )}</button>`;
    })
    .join(`<span class="sf-crumb-sep">/</span>`);
  el.hidden = false;
  el.innerHTML = `<button type="button" class="sf-crumb all" data-hammer="" title="Show full fabric">all</button><span class="sf-crumb-sep">·</span>${bits}<button type="button" class="sf-crumb up" data-hammer-up title="Up one level">up</button>`;
}

function persistFold() {
  const k = foldKey();
  if (!k) return;
  try {
    sessionStorage.setItem(k, JSON.stringify({ collapsed: [...collapsed], minimized: [...minimized] }));
  } catch {
    /* quota */
  }
}

function restoreFold() {
  collapsed = new Set(["registry", "tests", "overlay", "tenants", "edge"]);
  minimized = new Set();
  const k = foldKey();
  if (!k) return;
  try {
    const d = JSON.parse(sessionStorage.getItem(k) || "null");
    if (d && Array.isArray(d.collapsed)) collapsed = new Set(d.collapsed);
    if (d && Array.isArray(d.minimized)) minimized = new Set(d.minimized);
  } catch {
    /* ignore */
  }
}

function toggleCollapsed(id) {
  if (!id) return;
  if (collapsed.has(id)) {
    collapsed.delete(id);
    userCollapsed.delete(id);
  } else {
    collapsed.add(id);
    userCollapsed.add(id);
  }
  persistFold();
}

function toggleMinimized(id) {
  if (!id) return;
  if (minimized.has(id)) {
    minimized.delete(id);
    collapsed.delete(id);
    ignoreFoldUntil = Date.now() + 500;
  } else {
    minimized.add(id);
    collapsed.add(id);
  }
  persistFold();
}

function expandAll() {
  collapsed = new Set();
  minimized = new Set();
  persistFold();
  lastInsp = "";
  renderAll();
  if (mapFocus) fitPlaced();
}

function collapseAll() {
  for (const n of lastGraph.nodes) {
    if ((n.childCount || 0) > 0 && n.id !== "env" && n.id !== "infra") collapsed.add(n.id);
  }
  persistFold();
  lastInsp = "";
  renderAll();
  if (mapFocus) fitPlaced();
}

function syncGraph() {
  const graph = buildGraph();
  syncNodes(graph);
  drawEdges(graph);
  applyView();
  if (!space3dOn && !view._userMoved && !view._fitted) {
    fitView();
    view._fitted = true;
  }
}

function findMachine(name) {
  return machineRows().find((m) => m && m.name === name) || null;
}

function hostPods(name) {
  const map = lastGraph.podsByHost;
  if (map && map.has(name)) return map.get(name) || [];
  return [];
}

function continueBtn(stageId, running) {
  if (!stageId || !canAdmin()) return "";
  if (isMetalRebuild() && stageId !== "hosts") return "";
  const spec = stageSpec(stageId);
  const name = spec.name || stageId;
  const disabled = running ? " disabled" : "";
  if (stageId === "testing") {
    return `<button type="button" class="btn-sm" data-dm-validate="tempest" ${gate(true, "admin")}${disabled}>Run Tempest</button>`;
  }
  if (stageId === "hosts") {
    return `<button type="button" class="secondary btn-sm" data-dm-until="${esc(stageId)}" ${gate(
      true,
      "admin"
    )}${disabled}>Run host setup only</button>`;
  }
  return (
    `<button type="button" class="btn-sm" data-dm-continue="${esc(stageId)}" ${gate(true, "admin")}${disabled}>Continue from ${esc(
      name
    )}</button>` +
    `<button type="button" class="secondary btn-sm" data-dm-until="${esc(stageId)}" ${gate(
      true,
      "admin"
    )}${disabled}>Run ${esc(name)} only</button>`
  );
}

function nextAction() {
  const running = !!(pipe && pipe.running) || (job && ACTIVE.has(String(job.status || "")));
  const nxt = pipe && pipe.next_stage ? String(pipe.next_stage) : "";
  const failed = running ? "" : String((pipe && pipe.failed_at) || failedAtFromJob(job) || "");
  const act = parseActivity(job && job.log_text);
  const runningStage = ((pipe && pipe.stages) || []).find((s) => s && s.state === "running");
  const stageId = (runningStage && runningStage.id) || act.stage || nxt;
  const svc = prettyService(act.service || act.item);
  if (running) {
    const via = /openstack-helm/i.test(act.chart || "")
      ? "OpenStack Helm on Kubernetes"
      : "Genestack on Kubernetes";
    const remain = Array.isArray(pipe && pipe.remaining) ? pipe.remaining.length : 0;
    const done = pipe && pipe.done_count != null ? pipe.done_count : null;
    const total = pipe && pipe.total_count != null ? pipe.total_count : null;
    const counts =
      total != null ? ` · ${done || 0}/${total} done${remain ? ` · ${remain} left` : ""}` : remain ? ` · ${remain} left` : "";
    const cur = pipe && pipe.current;
    const label = (cur && cur.label) || svc;
    const pxeHosts = ((snap && snap.pxe && snap.pxe.hosts) || []).filter((h) => h && h.phase);
    const gf = /greenfield/i.test(String((job && job.operation) || ""));
    let text;
    if (pxeHosts.length) {
      const shown = pxeHosts
        .slice(0, 4)
        .map((h) => `${shortName(h.name)} ${h.phase}`)
        .join(" · ");
      const extra = pxeHosts.length > 4 ? ` +${pxeHosts.length - 4}` : "";
      text = `PXE ${pxeHosts.length} host${pxeHosts.length === 1 ? "" : "s"} · ${shown}${extra}`;
    } else if (gf && (act.pxe || !label || stageId === "hosts")) {
      text = `PXE / BIOS — waiting for nodes${counts}`;
    } else if (/registry\.mirror/i.test(String((job && job.operation) || ""))) {
      text = `Warming image cache${counts}`;
    } else if (label) {
      text = `Installing ${prettyService(label)} · ${via}${counts}`;
    } else if (act.pxe) {
      text = `PXE / BIOS — nodes coming up${counts}`;
    } else if (act.talos) {
      text = `Installing Talos${counts}`;
    } else {
      text = `Provisioning OpenStack · ${stageSpec(stageId).name || stageId || "pipeline"}${counts}`;
    }
    return { kind: "run", text, stage: stageId };
  }
  if (isMetalRebuild()) {
    const isoN = (recentJobs || []).filter((j) => /iso_boot/i.test(String((j && j.operation) || ""))).length;
    const err = String((job && job.error) || failed || "").replace(/\s+/g, " ").trim();
    const stageName = stageSpec("hosts").name || "Host Setup";
    const detail = err ? `${err.slice(0, 96)}. ` : "";
    const extra = isoN ? `ISO retry on ${isoN} host${isoN === 1 ? "" : "s"}. ` : "";
    const ready = readyMachineCount();
    const progress = ready ? `${ready} still ready, rebuild in progress. ` : "";
    return {
      kind: ready ? "warn" : "bad",
      text: `${progress}${detail}${extra}Next is ${stageName}.`,
      stage: "hosts",
    };
  }
  if (failed) {
    const resume = nxt && nxt !== "testing" ? nxt : failed.split("/")[0] || "operators";
    return {
      kind: "bad",
      text: `Stopped at ${failed}. Continue from ${resume}.`,
      stage: resume,
    };
  }
  if (nxt === "testing") {
    return { kind: "next", text: "Cloud is up. Next control point: Run Tempest.", stage: "testing" };
  }
  if (nxt && nxt !== "hosts") {
    return { kind: "next", text: `Next: ${stageSpec(nxt).name || nxt}`, stage: nxt };
  }
  if (!nxt) return { kind: "ok", text: "Pipeline complete.", stage: "" };
  return { kind: "next", text: "Deploy the cluster from hosts.", stage: "hosts" };
}

function inspectorFor(id) {
  const node = lastGraph.byId.get(id);
  const act = parseActivity(job && job.log_text);
  const running = !!(pipe && pipe.running);
  if (!node) {
    return `<p class="muted">Click a node for its detail card. Environment → Registry / Infrastructure → machine → Kubernetes → namespace → container → instance.</p>`;
  }
  if (node.kind === "tests") {
    const checks = (node.checks || [])
      .map(
        (c) =>
          `<tr><td>${esc(c.name)}</td><td><span class="pill ${
            c.ok === true ? "ok" : c.ok === false ? "bad" : ""
          }">${esc(c.ok === true ? "ok" : c.ok === false ? "not ok" : "unknown")}</span></td><td class="muted">${esc(
            c.detail || ""
          )}</td></tr>`
      )
      .join("");
    const t = node.tempest || {};
    const v = node.verify || {};
    return wrapDetail(
      node,
      `<div class="dm-insp-head"><h3>Tests · ${esc(envName || "")}</h3>
        <span class="pill ${node.state === "ok" ? "ok" : node.state === "bad" ? "bad" : "warn"}">${esc(
          node.subtitle || "not validated"
        )}</span></div>
      <p>Helm “testing” only installs the Tempest chart. These results say whether the cloud actually works.</p>
      <table class="dm-check"><tbody>
        <tr><td>Tempest</td><td><span class="pill ${t.status === "passed" ? "ok" : t.status === "failed" ? "bad" : ""}">${esc(
          t.status || "never"
        )}</span></td><td class="muted">${esc(t.message || "")}</td></tr>
        <tr><td>Verify</td><td><span class="pill ${v.status === "passed" ? "ok" : v.status === "failed" ? "bad" : ""}">${esc(
          v.status || "never"
        )}</span></td><td class="muted">${esc(v.message || "")}</td></tr>
        ${checks}
      </tbody></table>
      <div class="dm-insp-actions">
        ${canRun() ? `<button type="button" class="btn-sm" data-dm-validate="tempest">Run Tempest</button>` : ""}
        ${canRun() ? `<button type="button" class="secondary btn-sm" data-dm-validate="verify">Run verify</button>` : ""}
      </div>`
    );
  }
  if (node.kind === "testsuite") {
    const row = node.row || {};
    const fails = Array.isArray(row.failures) ? row.failures : [];
    const failHtml = fails.length
      ? `<ul class="dm-items">${fails
          .map((f) => `<li class="bad"><code>${esc(typeof f === "string" ? f : f.name || JSON.stringify(f))}</code></li>`)
          .join("")}</ul>`
      : `<p class="muted">${row.status === "never" ? "Never run. Chart up is not a test result." : "No listed failures."}</p>`;
    return wrapDetail(
      node,
      `<div class="dm-insp-head"><h3>${esc(node.title)}</h3>
        <span class="pill ${node.state === "ok" ? "ok" : node.state === "bad" ? "bad" : ""}">${esc(
          row.status || "never"
        )}</span></div>
      <p class="muted">${esc(row.message || "")} · ${esc(String(row.passed || 0))} passed · ${esc(
        String(row.failed || 0)
      )} failed · ${esc(String(row.skipped || 0))} skipped</p>
      ${failHtml}
      <div class="dm-insp-actions">${
        canRun()
          ? `<button type="button" class="btn-sm" data-dm-validate="${esc(node.suite || "tempest")}">Run ${esc(
              node.title
            )}</button>`
          : ""
      }</div>`
    );
  }
  if (node.kind === "registry") {
    const caches = node.caches || [];
    const last = node.lastMirror;
    const rows = caches
      .map(
        (c) =>
          `<tr><td>${esc(c.registry)}</td><td><span class="pill ${c.running ? "ok" : "bad"}">${
            c.running ? "up" : "down"
          }</span></td><td class="muted">${esc(c.endpoint || "")} · ${esc(String(c.images || 0))} images</td></tr>`
      )
      .join("");
    const lastLine = last
      ? `${esc(last.status || "")} ${esc(String(last.id || "").slice(0, 8))}`
      : "never warmed";
    const where =
      node.hostSource === "registry"
        ? "saved on this environment"
        : node.hostSource === "pxe"
          ? "the PXE next-server"
          : node.hostSource === "console"
            ? "this console"
            : "";
    const lead = !node.registryLoaded
      ? "Reading the address machines pull from."
      : node.bind
        ? `Machines pull container images from <code>${esc(node.bind)}</code>${
            where ? ` (${esc(where)})` : ""
          }. Configure sets that address and which registries are mirrored.`
        : "This console has no pull address yet. Configure sets the address machines use and which registries are mirrored.";
    return wrapDetail(
      node,
      `<div class="dm-insp-head"><h3>Registry · ${esc(envName || node.envName || "env")}</h3>
        <span class="pill ${node.state === "ok" ? "ok" : node.state === "run" ? "warn" : ""}">${esc(
          `${node.images || 0} images`
        )}</span></div>
      <p class="muted">${lead}</p>
      <table class="dm-check"><tbody>${rows}</tbody></table>
      <p class="muted">Last warm: ${lastLine}</p>
      <div class="dm-insp-actions">
        <button type="button" class="btn-sm" data-dm-reg-config ${gate(canRun(), "operator")}>Configure</button>
        ${
          canAdmin()
            ? `<button type="button" class="secondary btn-sm" data-dm-warm>Cache images and charts</button>`
            : ""
        }
      </div>`
    );
  }
  if (node.kind === "regcache") {
    const repos = Array.isArray(node.repositories) ? node.repositories : [];
    const list = repos.length
      ? `<ul class="dm-items">${repos.map((r) => `<li><code>${esc(r)}</code></li>`).join("")}</ul>`
      : `<p class="muted">No images cached yet. Warm this environment’s registry.</p>`;
    return wrapDetail(
      node,
      `<div class="dm-insp-head"><h3>${esc(node.title)}</h3>
        <span class="pill ${node.running ? "ok" : "bad"}">${esc(node.running ? "up" : "down")}</span></div>
      <p class="muted">${esc(node.endpoint || "")}</p>
      ${list}`
    );
  }
  if (node.kind === "overlay" || node.kind === "edge" || node.id === "tenants") {
    const kids = (lastGraph.edges || []).filter((e) => e.source === node.id).map((e) => lastGraph.byId.get(e.target)).filter(Boolean);
    const list = kids.length
      ? `<ul class="dm-items">${kids
          .slice(0, 40)
          .map((k) => `<li><code>${esc(k.title || k.id)}</code> ${esc(k.subtitle || k.kind)}</li>`)
          .join("")}</ul>`
      : `<p class="muted">Nothing live here yet. When OpenStack/K8s publish ${esc(node.title)}, they land in this nest.</p>`;
    return wrapDetail(
      node,
      `<div class="dm-insp-head"><h3>${esc(node.title)}</h3><span class="pill">${esc(String(kids.length))} in nest</span></div>
      ${pathLine(node)}
      <p class="muted">${esc(node.subtitle || "")}</p>
      ${list}`
    );
  }
  if (node.kind === "net" || node.kind === "vpc" || node.kind === "subnet" || node.kind === "router") {
    return wrapDetail(
      node,
      `<div class="dm-insp-head"><h3>${esc(node.title)}</h3><span class="pill ${node.state}">${esc(node.subtitle || kindLabel(node.kind))}</span></div>
      ${pathLine(node)}
      ${kvTable([
        ["Kind", kindLabel(node.kind)],
        ["Status", node.status],
        ["CIDR", node.cidr],
        ["Net ID", node.netId],
        ["Project", node.project_id],
        ["External", node.external ? "provider" : ""],
        ["Shared", node.shared ? "yes" : ""],
      ])}`
    );
  }
  if (node.kind === "tenant") {
    const kids = (lastGraph.edges || []).filter((e) => e.source === node.id).map((e) => lastGraph.byId.get(e.target)).filter(Boolean);
    const vms = kids.filter((k) => k.kind === "vm");
    const list = vms.length
      ? `<ul class="dm-items">${vms.map((v) => `<li><code>${esc(v.title)}</code> ${esc(v.status || v.state)}</li>`).join("")}</ul>`
      : `<p class="muted">No instances in this project yet.</p>`;
    return wrapDetail(
      node,
      `<div class="dm-insp-head"><h3>${esc(node.title)}</h3><span class="pill">${esc(String(vms.length))} VMs</span></div>
      ${kvTable([["Project", node.project_id || node.title], ["Instances", vms.length]])}
      ${list}`
    );
  }
  if (node.kind === "ingress" || node.kind === "edge" || node.kind === "gw" || node.kind === "route") {
    const open = node.url
      ? `<button type="button" class="btn-sm" data-dm-open-url="${esc(node.url)}">Open URL</button>`
      : "";
    return wrapDetail(
      node,
      `<div class="dm-insp-head"><h3>${esc(node.title)}</h3><span class="pill ${node.state}">${esc(node.className || node.subtitle || "edge")}</span></div>
      ${pathLine(node)}
      <div class="dm-insp-actions">${open}</div>
      ${kvTable([
        ["Class", node.className],
        ["Hosts", (node.hosts || []).join(", ")],
        ["Address", node.address || (node.addresses || []).join(", ")],
        ["TLS", node.tls ? "yes" : ""],
        ["URL", node.url],
        ["Namespace", node.namespace],
        ["Backends", (node.backends || []).map((b) => b.name || "").filter(Boolean).join(", ")],
        ["Parents", (node.parent_refs || []).map((p) => p.name || "").filter(Boolean).join(", ")],
      ])}`
    );
  }
  if (node.kind === "fip" || node.kind === "lb" || node.kind === "svc") {
    return wrapDetail(
      node,
      `<div class="dm-insp-head"><h3>${esc(node.title)}</h3><span class="pill ${node.state}">${esc(kindLabel(node.kind))}</span></div>
      ${pathLine(node)}
      ${kvTable([
        ["IP", node.ip || node.vip || node.cluster_ip],
        ["Fixed", node.fixed_ip],
        ["Port", node.port],
        ["Type", node.svcType || node.provider],
        ["VM", node.vmId],
        ["Namespace", node.namespace],
        ["Ports", (node.ports || []).join(", ")],
        ["LB", (node.load_balancer || []).join(", ")],
      ])}`
    );
  }
  if (node.kind === "vm") {
    return wrapDetail(
      node,
      `${vmFactsHtml(node)}`
    );
  }
  if (node.kind === "pod") {
    return wrapDetail(
      node,
      `${podFactsHtml(node)}`
    );
  }
  if (node.kind === "more") {
    return wrapDetail(node, `<p class="muted">${esc(String(node.more || 0))} more containers on this host.</p>`);
  }
  if (node.kind === "ns") {
    const pods = hostPods(node.machineName).filter((p) => String(p.namespace || "default") === node.namespace);
    const list = pods.length
      ? `<ul class="dm-items">${pods
          .slice(0, 40)
          .map((p) => `<li><code>${esc(p.name)}</code> ${esc(p.phase || "")}</li>`)
          .join("")}</ul>`
      : `<p class="muted">No live containers in this namespace on this host.</p>`;
    return wrapDetail(node, `<div class="dm-insp-head"><h3>${esc(node.namespace || node.title)}</h3>
      <span class="pill">${esc(nsKindLabel(node.namespace))}</span></div>
      <div class="dm-insp-actions">
        <button type="button" class="secondary btn-sm" data-dm-k8s-events data-ns="${esc(node.namespace || "")}">Events</button>
      </div>
      <p class="muted">${esc(shortName(node.machineName || ""))} · ${pods.length} containers</p>
      ${list}`);
  }
  if (node.kind === "k8s") {
    const m = findMachine(node.machineName);
    const st = m ? machineState(m) : {};
    const kn = k8sNodeName(m);
    const ops = canRun()
      ? `<button type="button" class="secondary btn-sm" data-dm-k8s-cordon data-name="${esc(kn)}" ${gate(true, "operator")}>Cordon</button>
         <button type="button" class="secondary btn-sm" data-dm-k8s-uncordon data-name="${esc(kn)}" ${gate(true, "operator")}>Uncordon</button>
         <button type="button" class="danger btn-sm" data-dm-k8s-drain data-name="${esc(kn)}" ${gate(true, "operator")}>Drain</button>`
      : "";
    return wrapDetail(node, `<div class="dm-insp-head"><h3>Kubernetes · ${esc(shortName(node.machineName || ""))}</h3>
      <span class="pill ${st.k8sReady ? "ok" : "warn"}">${esc(st.k8sReady ? "Ready" : "not Ready")}</span></div>
      <p class="muted">${esc(st.k8sRoles || "")} ${esc(st.k8sVer || "")}</p>
      ${st.cpu ? `<p class="muted">${esc(String(st.cpu))} CPU · ${esc(st.memGi != null ? `${st.memGi} Gi` : "")}</p>` : ""}
      <div class="dm-insp-actions">
        <button type="button" class="secondary btn-sm" data-dm-k8s-describe data-kind="nodes" data-name="${esc(kn)}">Describe</button>
        ${ops}
      </div>`);
  }
  if (node.kind === "osrole") {
    const installed = osInstalled();
    const nxt = pipe && pipe.next_stage ? String(pipe.next_stage) : "";
    const stage = nxt && nxt !== "hosts" ? nxt : "operators";
    const body = installed
      ? `<p>This box’s OpenStack role: <strong>${esc(node.subtitle || "")}</strong>. Services are Helm releases on the Kubernetes cluster (official Genestack plays).</p>`
      : `<p><strong>OpenStack is still being installed</strong> onto this Kubernetes cluster via Genestack Helm plays.</p>
         <p class="muted">This host will run: ${esc((node.osRoles || []).join(", ") || "no OpenStack role")}.</p>`;
    return wrapDetail(node, `<div class="dm-insp-head"><h3>OpenStack · ${esc(shortName(node.machineName || ""))}</h3></div>
      ${body}
      <div class="dm-insp-actions">${continueBtn(stage, running)}</div>`);
  }
  if (node.machineName || node.kind === "machine") {
    const name = node.machineName || (node.id || "").replace(/^m:/, "");
    const m = findMachine(name);
    if (!m) return wrapDetail(node, `<p>${esc(node.title)}</p>`);
    const pods = hostPods(m.name);
    let podBlock = "";
    if (workloadsError) podBlock = `<p class="muted">Containers unavailable — ${esc(workloadsError)}</p>`;
    else if (!pods.length) podBlock = `<p class="muted">No containers on this host.</p>`;
    else {
      podBlock = `<p class="muted dm-desc">${pods.length} containers</p>
        <ul class="dm-items">${pods
          .slice(0, 40)
          .map((p) => `<li><code>${esc(p.namespace || "")}/${esc(p.name)}</code> ${esc(p.phase || "")}</li>`)
          .join("")}</ul>`;
    }
    return wrapDetail(
      node,
      `${hostFactsHtml(m.name)}
      ${podBlock}`
    );
  }
  if (node.stageId) {
    const spec = stageSpec(node.stageId);
    const items = Array.isArray(spec.items) ? spec.items : [];
    const itemHtml = items.length
      ? `<ul class="dm-items">${items.map((it) => `<li><code>${esc(it.script || it.name || "")}</code></li>`).join("")}</ul>`
      : "";
    const logLines = act.tail.slice(-12);
    const logHtml = logLines.length
      ? `<pre class="dm-log">${logLines.map((l) => esc(stripLogPrefix(l))).join("\n")}</pre>`
      : "";
    return wrapDetail(node, `<div class="dm-insp-head"><h3>${esc(spec.name || node.title)}</h3><span class="pill ${
      node.state === "ok" ? "ok" : node.state === "bad" ? "bad" : node.state === "run" ? "warn" : ""
    }">${esc(stageState(node.stageId))}</span></div>
      <p class="muted dm-desc">${esc(spec.description || "")}</p>
      ${itemHtml}${logHtml}<div class="dm-insp-actions">${continueBtn(node.stageId, running)}</div>`);
  }
  if (LAYER_COPY[node.id]) {
    return wrapDetail(node, `<div class="dm-insp-head"><h3>${esc(node.title)}</h3></div>
      <p>${esc(LAYER_COPY[node.id])}</p>
      <p class="muted">${esc(node.subtitle || "")}</p>`);
  }
  return wrapDetail(node, `<p>${esc(node.title)}</p><p class="muted">${esc(node.subtitle || "")}</p>`);
}

function renderLiveSlot() {
  const html = liveDetailHtml(selected);
  const slot = document.getElementById("dm-live-slot");
  if (slot) slot.innerHTML = html;
  const popSlot = document.getElementById("dm-live-slot-pop");
  if (popSlot) popSlot.innerHTML = html;
  const modalSlot = document.getElementById("dm-live-slot-modal");
  if (modalSlot) modalSlot.innerHTML = html;
}

function renderInspector() {
  const el = document.getElementById("dm-inspector");
  if (!el) return;
  const html = inspectorFor(selected);
  if (html !== lastInsp) {
    lastInsp = html;
    el.innerHTML = html;
  }
  renderNodePop();
  renderLiveSlot();
}

function renderNodePop() {
  const world = document.getElementById("dm-world");
  if (!world) return;
  let pop = document.getElementById("dm-node-pop");
  const node = lastGraph.byId.get(selected);
  const hide =
    !node ||
    node.kind === "env" ||
    node.kind === "group" ||
    popDismissed === selected ||
    (nodeModal && document.getElementById("dm-host-modal") && !document.getElementById("dm-host-modal").hidden);
  if (!pop) {
    pop = document.createElement("div");
    pop.id = "dm-node-pop";
    pop.className = "dm-node-pop";
    pop.hidden = true;
    world.appendChild(pop);
    pop.addEventListener("pointerdown", (e) => e.stopPropagation());
    pop.addEventListener("click", (e) => {
      e.stopPropagation();
      if (e.target.closest("[data-dm-pop-close]")) {
        popDismissed = selected;
        pop.hidden = true;
        return;
      }
      handleDetailClick(e);
    });
  }
  if (hide) {
    pop.hidden = true;
    return;
  }
  const inner = inspectorFor(selected);
  const html = `<button type="button" class="dm-node-pop-close" data-dm-pop-close aria-label="Close details">×</button>${inner}`;
  if (pop.dataset.last !== html) {
    pop.dataset.last = html;
    pop.innerHTML = html;
    const slot = pop.querySelector("#dm-live-slot");
    if (slot) slot.id = "dm-live-slot-pop";
  }
  pop.hidden = false;
  pop.style.left = `${(node.x || 0) + (node.w || 260) + 14}px`;
  pop.style.top = `${node.y || 0}px`;
}

function renderNext() {
  const el = document.getElementById("dm-next");
  if (!el) return;
  const nxt = nextAction();
  const running = nxt.kind === "run";
  const queue = workItems();
  const blocked = queue.filter((r) => r.state === "bad").length;
  const active = queue.filter((r) => r.state === "run" || r.state === "wait").length;
  const btn =
    nxt.stage && nxt.stage === "testing" && canAdmin() && !running
      ? `<button type="button" class="btn-sm" data-dm-validate="tempest" ${gate(true, "admin")}>Run Tempest</button>`
      : nxt.stage && nxt.stage !== "hosts" && canAdmin() && !running
        ? `<button type="button" class="btn-sm" data-dm-continue="${esc(nxt.stage)}" ${gate(true, "admin")}>Continue from ${esc(
            stageSpec(nxt.stage).name || nxt.stage
          )}</button>`
        : "";
  const live = isLive();
  let workLabel = "";
  if (queue.length) {
    if (live && blocked) workLabel = `${active || 1} in progress · ${blocked} blocked`;
    else if (live) workLabel = `${active || queue.length} in progress`;
    else if (blocked) workLabel = `${blocked} blocked`;
    else workLabel = `${active || queue.length} in progress`;
  }
  const workBtn = workLabel
    ? `<button type="button" class="secondary btn-sm" data-dm-work>${esc(workLabel)}</button>`
    : "";
  const html = `<div class="dm-next-text">${running ? '<span class="dm-dot"></span>' : ""}${esc(
    nxt.text
  )}</div><div class="dm-insp-actions">${workBtn}${btn}</div>`;
  const key = `${nxt.kind}|${html}`;
  if (key === lastNext) return;
  lastNext = key;
  el.className = `sf-next dm-next ${blocked ? "warn" : nxt.kind}`;
  el.innerHTML = html;
}

function mapPipeItem(state) {
  const s = String(state || "pending");
  if (s === "done") return "ok";
  if (s === "running") return "run";
  if (s === "failed") return "bad";
  return "wait";
}

function fmtDuration(seconds) {
  if (seconds == null || !Number.isFinite(Number(seconds))) return "";
  const s = Math.max(0, Math.round(Number(seconds)));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h) return `${h}h${String(m).padStart(2, "0")}m`;
  if (m) return `${m}m${String(sec).padStart(2, "0")}s`;
  return `${sec}s`;
}

function renderPipe() {
  const el = document.getElementById("dm-pipe");
  if (!el) return;
  const metal = isMetalRebuild();
  const serving = oldOsStillServing();
  const stages = (pipe && Array.isArray(pipe.stages) ? pipe.stages : []).filter(Boolean);
  if (!stages.length) {
    el.hidden = true;
    el.innerHTML = "";
    return;
  }
  const hideProgress = metal && !serving;
  const done = hideProgress ? 0 : Number(pipe.done_count || 0);
  const total = Number(pipe.total_count || 0);
  const left = hideProgress ? total : Array.isArray(pipe.remaining) ? pipe.remaining.length : 0;
  const elapsed = fmtDuration(pipe.elapsed_s != null ? pipe.elapsed_s : pipe.timings && pipe.timings.total_s);
  const head = metal
    ? `<div class="dm-pipe-head">${
        serving
          ? `${readyMachineCount()} still ready, rebuild in progress`
          : "Metal rebuild · Host Setup in progress"
      }${elapsed ? ` · ${esc(elapsed)}` : ""}</div>`
    : total
      ? `<div class="dm-pipe-head">${done}/${total} complete${left ? ` · ${left} left` : ""}${
          elapsed ? ` · ${esc(elapsed)} elapsed` : ""
        } · click a stage to control it</div>`
      : elapsed
        ? `<div class="dm-pipe-head">${esc(elapsed)} elapsed · click a stage to control it</div>`
        : "";
  const cols = stages
    .map((s) => {
      let st = mapPipeItem(s.state);
      if (metal && !serving) {
        st = s.id === "hosts" ? (isLive() ? "run" : "bad") : "wait";
      } else if (metal && s.id === "hosts") {
        st = isLive() ? "run" : "bad";
      }
      const hot = hideProgress
        ? null
        : (Array.isArray(s.items) ? s.items : []).find(
            (it) => it && (it.state === "running" || it.state === "failed")
          );
      const items = Array.isArray(s.items) ? s.items : [];
      const doneN = hideProgress ? 0 : items.filter((it) => it && it.state === "done").length;
      const count = items.length ? ` ${doneN}/${items.length}` : "";
      const took = fmtDuration(s.seconds);
      const extra = hot ? ` · ${prettyService(hot.name)}` : count;
      const timeBit = took ? ` · ${took}` : "";
      const opt = s.required === false ? " optional" : "";
      return `<button type="button" class="dm-pipe-pill ${st}${opt}" data-dm-stage="${esc(s.id)}" title="${
        s.required === false ? "Optional control point" : s.control === "test" ? "Tempest control point" : "Helm control point"
      }">
        <span class="sf-dot ${st}"></span>${esc(s.name || s.id)}${
          extra || timeBit ? `<span class="muted">${esc(extra)}${esc(timeBit)}</span>` : ""
        }
      </button>`;
    })
    .join("");
  const html = `${head}<div class="dm-pipe-stages">${cols}</div>`;
  if (html === el.dataset.last) {
    el.hidden = false;
    return;
  }
  el.dataset.last = html;
  el.hidden = false;
  el.innerHTML = html;
}

function stageItemLabel(state) {
  const s = String(state || "pending");
  if (s === "done") return "Done";
  if (s === "running") return "In progress";
  if (s === "failed") return "Blocked";
  return "Waiting";
}

function openStageModal(stageId) {
  const id = String(stageId || "").trim();
  if (!id) return;
  stageModalId = id;
  const spec = stageSpec(id);
  const row = ((pipe && pipe.stages) || []).find((s) => s && s.id === id) || {};
  const items = Array.isArray(row.items) && row.items.length ? row.items : Array.isArray(spec.items) ? spec.items : [];
  const st = mapPipeItem(row.state || spec.state);
  const act = parseActivity(job && job.log_text);
  const liveHere = isLive() && (act.stage === id || row.state === "running");
  const label = { ok: "complete", run: "in progress", bad: "blocked", wait: "not started" };
  const list = items.length
    ? `<ul class="dm-work-list">${items
        .map((it) => {
          const name = prettyService(it.name || it.script || "");
          const ist = mapPipeItem(it.state);
          return `<li class="${esc(ist)}"><span class="sf-dot ${esc(ist)}"></span><div><strong>${esc(
            name
          )}</strong><div class="muted">${esc(stageItemLabel(it.state))}${
            it.script ? ` · ${it.script}` : ""
          }</div></div></li>`;
        })
        .join("")}</ul>`
    : `<p class="muted">No items listed for this stage.</p>`;
  const logLines = liveHere || row.state === "failed" ? (act.tail || []).slice(-12) : [];
  const logHtml = logLines.length
    ? `<pre class="dm-log">${logLines.map((l) => esc(stripLogPrefix(l))).join("\n")}</pre>`
    : "";
  const running = isLive();
  const actions = continueBtn(id, running && row.state === "running");
  const required = row.required !== false && spec.required !== false;
  const control = row.control || spec.control || (id === "testing" ? "test" : id === "hosts" ? "metal" : "helm");
  const took = fmtDuration(row.seconds);
  const note =
    control === "test"
      ? "Tempest is its own job. It does not run inside Restack or Greenfield."
      : control === "metal"
        ? "Host setup is Talos on boxes already in maintenance. Greenfield is the PXE / metal wipe."
        : required
          ? "Required helm control point. A failure here stops the restack."
          : "Optional helm control point. A failed chart here is a warning — the restack keeps going.";
  const html = `<p class="muted">${esc(spec.description || row.description || "")}</p>
    <p><span class="pill ${st === "ok" ? "ok" : st === "bad" ? "bad" : st === "run" ? "warn" : ""}">${esc(
      label[st] || st
    )}</span>
    <span class="pill">${required ? "required" : "optional"}</span>
    <span class="pill">${esc(control)}</span>
    ${took ? `<span class="muted">${esc(took)}</span>` : ""}</p>
    <p class="dm-control-note">${esc(note)}</p>
    ${list}${logHtml}
    <div class="dm-insp-actions">${actions}</div>`;
  const body = openToolModal(`${spec.name || id}`, html);
  if (body && !body.dataset.stageWired) {
    body.dataset.stageWired = "1";
    body.addEventListener("click", (e) => {
      const until = e.target.closest("[data-dm-until]");
      if (until) {
        runStageOnly(until.getAttribute("data-dm-until"));
        return;
      }
      const cont = e.target.closest("[data-dm-continue]");
      if (cont) continueFrom(cont.getAttribute("data-dm-continue"));
    });
  }
}

function openWorkModal() {
  const rows = workItems();
  if (!rows.length) {
    openToolModal("In progress", `<p class="muted">Nothing is pending or blocked right now.</p>`);
    return;
  }
  const label = { bad: "Blocked", run: "In progress", wait: "Waiting", warn: "Needs attention" };
  const html = `<ul class="dm-work-list">${rows
    .map(
      (r) =>
        `<li class="${esc(r.state)}"${r.id ? ` data-dm-work-node="${esc(r.id)}"` : ""}><span class="sf-dot ${esc(
          r.state
        )}"></span><div><strong>${esc(r.title)}</strong><div class="muted">${esc(r.detail || label[r.state] || r.state)}</div></div></li>`
    )
    .join("")}</ul>`;
  const body = openToolModal("What's in progress", html);
  if (body && !body.dataset.workWired) {
    body.dataset.workWired = "1";
    body.addEventListener("click", (e) => {
      const row = e.target.closest("[data-dm-work-node]");
      if (!row) return;
      const id = row.getAttribute("data-dm-work-node");
      closeToolModal();
      if (id) selectNode(id);
    });
  }
}

function renderChrome() {
  const meta = document.getElementById("dm-meta");
  if (!meta) return;
  const when = lastFetch ? new Date(lastFetch).toLocaleTimeString() : "";
  setText(meta, pipe && pipe.running ? `live · ${when}` : when);
}

function renderEnvHealth() {
  const el = document.getElementById("env-health");
  if (!el) return;
  const machines = machineRows();
  const metal = isMetalRebuild();
  const ready = machines.filter((m) => machineState(m).k8sReady).length;
  const notJoined = machines.filter((m) => {
    const st = machineState(m);
    return !st.k8sReady && (st.roles || []).length;
  }).length;
  const live = isLive() || !!(pipe && pipe.running);
  let health = "unknown";
  if (machines.length) {
    if (live || metal) health = "working";
    else if (notJoined) health = "attention";
    else health = "ok";
  }
  const noun = machines.length === 1 ? "machine" : "machines";
  const bits = [
    `<span>health: ${esc(health)}</span>`,
    `<span>${esc(envName || "Environment")}</span>`,
    `<span>${machines.length} ${noun}</span>`,
    `<span>${ready} ready</span>`,
    `<span>${notJoined} not joined</span>`,
  ];
  if (metal) {
    const booting = machines.filter(
      (m) =>
        String(m.bm_state || "").toLowerCase() === "booting" ||
        pxeHot(m.pxe) ||
        maintenanceFromLog().has(m.name)
    ).length;
    bits.push(`<span>Metal rebuild · ${booting} booting</span>`);
  }
  const reg = registryInfo();
  if (reg.total) bits.push(`<span>registry ${reg.ready}/${reg.total}${reg.images ? ` · ${reg.images} img` : ""}</span>`);
  const val = validationInfo();
  bits.push(
    `<span>tests ${esc(metal ? "paused" : val.status === "never" ? "not run" : val.status)}</span>`
  );
  const pxeInfo = (snap && snap.pxe) || {};
  const pxeHosts = (pxeInfo.hosts || []).filter((h) => h && (pxeHot(h) || (h.phase && !/^waiting for DHCP$/i.test(h.phase)))).length;
  if (pxeHosts) bits.push(`<span>PXE ${pxeHosts} host${pxeHosts === 1 ? "" : "s"}</span>`);
  el.innerHTML = bits.join('<span class="env-health-sep" aria-hidden="true">·</span>');
}

function renderAll() {
  syncGraph();
  renderFocusHud();
  renderInspector();
  renderNext();
  renderPipe();
  renderChrome();
  renderEnvHealth();
  renderCacheCard();
  if (stageModalId) {
    const modal = document.getElementById("dm-tool-modal");
    if (modal && !modal.hidden) openStageModal(stageModalId);
    else stageModalId = "";
  }
  if (nodeModal) refreshNodeModal();
  if (space3dOn) resizeSpace3d();
  if (nestOn) {
    resizeHoneycomb();
    positionNestConsole();
  }
}

function refreshPods(id) {
  if (podsInflight) return;
  const minAge = isLive() ? LIVE_PODS_MS : PODS_REFRESH_MS;
  if (workloads && Date.now() - workloadsAt < minAge) return;
  podsInflight = true;
  api(`/api/v1/environments/${encodeURIComponent(id)}/k8s/workloads?pods_only=1`, { timeout: PODS_TIMEOUT })
    .then((wl) => {
      if (envId !== id) return;
      applyWorkloads(wl);
      workloadsAt = Date.now();
      writeCache();
      renderAll();
    })
    .catch(() => {
      if (envId !== id || workloads) return;
      workloads = { pods: [] };
      workloadsError = "containers still loading";
    })
    .finally(() => {
      podsInflight = false;
    });
}

async function fetchState() {
  if (!envId || fetchInflight) return;
  fetchInflight = true;
  const id = envId;
  let released = false;
  refreshRegistry(id);
  try {
    refreshPods(id);
    const snapP = api(`/api/v1/ops/environments/${encodeURIComponent(id)}/snapshot`, { timeout: SNAP_TIMEOUT }).catch(() => null);
    const pipeP = catalog.length
      ? Promise.resolve({ stages: catalog })
      : api("/api/v1/genestack/pipeline", { timeout: 5000 }).catch(() => ({ stages: [] }));
    const vmsP = api(`/api/v1/environments/${encodeURIComponent(id)}/vms`, { timeout: 12000 }).catch(() => null);
    const jobsP = api(`/api/v1/jobs?environment_id=${encodeURIComponent(id)}&limit=16`, { timeout: 4000 }).catch(() => null);
    const bmP = api(`/api/v1/environments/${encodeURIComponent(id)}/baremetal`, { timeout: 6000 }).catch(() => null);
    const cloudP = api(`/api/v1/environments/${encodeURIComponent(id)}/cloud`, { timeout: 20000 }).catch(() => null);
    const ingP = api(`/api/v1/environments/${encodeURIComponent(id)}/k8s/ingresses`, { timeout: 10000 }).catch(() => null);
    const svcP = api(`/api/v1/environments/${encodeURIComponent(id)}/k8s/services`, { timeout: 10000 }).catch(() => null);
    const gwP = api(`/api/v1/environments/${encodeURIComponent(id)}/k8s/gateways`, { timeout: 10000 }).catch(() => null);
    const routeP = api(`/api/v1/environments/${encodeURIComponent(id)}/k8s/httproutes`, { timeout: 10000 }).catch(() => null);
    const poolP = api(`/api/v1/environments/${encodeURIComponent(id)}/k8s/metallb/pools`, { timeout: 8000 }).catch(() => null);
    const platP = api(`/api/v1/environments/${encodeURIComponent(id)}/platform`, { timeout: 20000 }).catch(() => null);
    const srvP = api(`/api/v1/environments/${encodeURIComponent(id)}/servers`, { timeout: 8000 }).catch(() => null);
    // Inventory is enough for the shared line. Cloud and VM calls can sit
    // on their full timeout, and that must not leave the line at 0 machines.
    Promise.all([platP, srvP]).then(([platEarly, srvEarly]) => {
      if (id !== envId) return;
      applyFleetSources(platEarly, srvEarly);
      renderEnvHealth();
    }).catch(() => {});
    // Cloud and VM reads can take the full timeout. Keep them off the lock
    // so switching environments does not sit on an empty count.
    const [snapRes, pipeCat, jobsRes, bmRes, ingRes, svcRes, gwRes, routeRes, poolRes, platRes, srvRes] = await Promise.all([
      snapP, pipeP, jobsP, bmP, ingP, svcP, gwP, routeP, poolP, platP, srvP,
    ]);
    let cloudRes = null;
    let vmsRes = null;
    if (id !== envId) return;
    const merged = mergeSnapshot(
      {
        snap,
        pipe,
        job,
        recentJobs,
        workloads,
        osVms,
        bmLive,
        catalog,
      },
      {
        snap: snapRes || undefined,
        pipe: snapRes && snapRes.pipeline ? snapRes.pipeline : undefined,
        workloads,
        osVms: vmsRes && Array.isArray(vmsRes.vms) ? vmsRes.vms : undefined,
        bmLive: bmRes && Array.isArray(bmRes.nodes) ? bmRes.nodes : undefined,
        catalog: pipeCat && Array.isArray(pipeCat.stages) ? pipeCat.stages : undefined,
        recentJobs: (jobsRes && (jobsRes.jobs || jobsRes)) || (snapRes && snapRes.recent_jobs) || undefined,
      }
    );
    if (merged.snap) {
      const prevN = ((snap && snap.nodes) || []).length;
      const nextN = ((merged.snap.nodes) || []).length;
      snap = nextN || !prevN ? merged.snap : { ...merged.snap, nodes: snap.nodes };
    }
    if (merged.pipe) pipe = merged.pipe;
    if (merged.osVms && merged.osVms.length) osVms = merged.osVms;
    if (merged.bmLive && merged.bmLive.length) bmLive = merged.bmLive;
    applyFleetSources(platRes, srvRes);
    if (merged.catalog && merged.catalog.length) catalog = merged.catalog;
    const cloudHas =
      cloudRes &&
      (cloudRes.available ||
        (cloudRes.servers || []).length ||
        (cloudRes.networks || []).length ||
        (cloudRes.projects || []).length ||
        (cloudRes.floating_ips || []).length);
    if (cloudHas) osCloud = cloudRes;
    if (ingRes && Array.isArray(ingRes.ingresses) && (ingRes.ingresses.length || !k8sIngress.length)) {
      k8sIngress = ingRes.ingresses;
    }
    if (svcRes && Array.isArray(svcRes.services) && (svcRes.services.length || !k8sServices.length)) {
      k8sServices = svcRes.services;
    }
    if (gwRes && Array.isArray(gwRes.gateways) && (gwRes.gateways.length || !k8sGateways.length)) {
      k8sGateways = gwRes.gateways;
    }
    if (routeRes && Array.isArray(routeRes.httproutes) && (routeRes.httproutes.length || !k8sRoutes.length)) {
      k8sRoutes = routeRes.httproutes;
    } else if (routeRes && Array.isArray(routeRes.routes) && (routeRes.routes.length || !k8sRoutes.length)) {
      k8sRoutes = routeRes.routes;
    }
    if (poolRes && Array.isArray(poolRes.pools) && (poolRes.pools.length || !k8sPools.length)) {
      k8sPools = poolRes.pools;
    }
    if ((!osVms || !osVms.length) && osCloud && Array.isArray(osCloud.servers) && osCloud.servers.length) {
      osVms = osCloud.servers;
    }
    applyRecentJobs(jobsRes || (snap && snap.recent_jobs));
    bindLiveEnv(id);
    applyLive({
      snap,
      pipe,
      job,
      recentJobs,
      workloads,
      osVms,
      bmLive,
      catalog,
      osCloud,
      k8sIngress,
      k8sServices,
      k8sGateways,
      k8sRoutes,
      k8sPools,
    });
    if (job && job.id && ACTIVE.has(String(job.status || ""))) {
      try {
        const detail = await api(`/api/v1/ops/jobs/${encodeURIComponent(job.id)}`, { timeout: 5000 });
        if (detail && detail.log_text) job.log_text = detail.log_text;
        if (detail && detail.status) job.status = detail.status;
      } catch {
        /* keep last log */
      }
    }
    if (isLive()) autoReveal = true;
    lastFetch = Date.now();
    writeCache();
    renderAll();
    fetchInflight = false;
    released = true;
    const [cloudLate, vmsLate] = await Promise.all([cloudP, vmsP]);
    if (id !== envId || fetchInflight) return;
    cloudRes = cloudLate;
    vmsRes = vmsLate;
    if (vmsRes && Array.isArray(vmsRes.vms) && (vmsRes.vms.length || vmsRes.source === "live")) osVms = vmsRes.vms;
    const cloudLateHas =
      cloudRes &&
      (cloudRes.available ||
        (cloudRes.servers || []).length ||
        (cloudRes.networks || []).length ||
        (cloudRes.projects || []).length ||
        (cloudRes.floating_ips || []).length);
    if (cloudLateHas) osCloud = cloudRes;
    if ((!osVms || !osVms.length) && osCloud && Array.isArray(osCloud.servers) && osCloud.servers.length) {
      osVms = osCloud.servers;
    }
    renderAll();
  } finally {
    if (!released) fetchInflight = false;
  }
  // A switch during this read used to wait out the idle poll before the
  // new environment got a count. Start that read now.
  if (envId && envId !== id) tick();
}

function lerpNum(a, b, t) {
  const x = Number(a);
  const y = Number(b);
  if (!Number.isFinite(y)) return Number.isFinite(x) ? x : null;
  if (!Number.isFinite(x)) return y;
  return x + (y - x) * t;
}

function lerpRes(dst, src, t) {
  if (!src) return dst || src;
  const out = dst && typeof dst === "object" ? dst : {};
  out.used = lerpNum(out.used, src.used, t);
  out.cap = src.cap;
  out.pct = lerpNum(out.pct, src.pct, t);
  return out;
}

function stepShownLive(dt) {
  const src = liveMetrics;
  if (!src) return;
  const t = 1 - Math.exp(-dt / LIVE_SMOOTH_S);
  if (!shownLive) {
    shownLive = JSON.parse(JSON.stringify(src));
    return;
  }
  shownLive.cluster = shownLive.cluster || {};
  const sc = src.cluster || {};
  shownLive.cluster.cpu = lerpRes(shownLive.cluster.cpu, sc.cpu, t);
  shownLive.cluster.mem = lerpRes(shownLive.cluster.mem, sc.mem, t);
  shownLive.cluster.disk = lerpRes(shownLive.cluster.disk, sc.disk, t);
  shownLive.cluster.cores = sc.cores;
  const prevNodes = new Map((shownLive.nodes || []).map((n) => [n.name, n]));
  shownLive.nodes = (src.nodes || []).map((n) => {
    const p = prevNodes.get(n.name) || n;
    const prevCpu = new Map((p.cpus || []).map((c) => [`${c.node}:${c.id}`, c]));
    return {
      ...n,
      cpu: lerpRes(p.cpu, n.cpu, t),
      mem: lerpRes(p.mem, n.mem, t),
      disk: lerpRes(p.disk, n.disk, t),
      cpus: (n.cpus || []).map((c) => {
        const pc = prevCpu.get(`${c.node}:${c.id}`) || c;
        return { ...c, pct: lerpNum(pc.pct, c.pct, t) };
      }),
    };
  });
  const prevCores = new Map((shownLive.cpus || []).map((c) => [`${c.node}:${c.id}`, c]));
  shownLive.cpus = (src.cpus || []).map((c) => {
    const pc = prevCores.get(`${c.node}:${c.id}`) || c;
    return { ...c, pct: lerpNum(pc.pct, c.pct, t) };
  });
  const prevPods = new Map((shownLive.pods || []).map((p) => [`${p.ns}/${p.name}`, p]));
  shownLive.pods = (src.pods || []).map((p) => {
    const o = prevPods.get(`${p.ns}/${p.name}`) || p;
    return { ...p, cpu: lerpNum(o.cpu, p.cpu, t), mem: lerpNum(o.mem, p.mem, t) };
  });
}

function setBarEl(root, key, pct, sub) {
  const el = root.querySelector(`[data-bar="${key}"]`);
  if (!el) return;
  const p = pct == null || !Number.isFinite(Number(pct)) ? null : Math.max(0, Math.min(100, Number(pct)));
  const fill = el.querySelector(".sf-bar-fill");
  const val = el.querySelector(".sf-bar-val");
  if (fill) {
    fill.style.width = `${p == null ? 0 : p.toFixed(1)}%`;
    fill.className = `sf-bar-fill ${band(p)}`;
  }
  if (val) {
    val.innerHTML = `${p == null ? "—" : `${p.toFixed(0)}%`}${sub ? ` <em>${esc(sub)}</em>` : ""}`;
  }
  el.title = `${key} ${p == null ? "n/a" : `${p.toFixed(0)}%`}`;
}

function paintHeat(root, cpus, limit) {
  const heat = root.querySelector(".sf-cpuheat");
  const rows = Array.isArray(cpus) ? cpus : [];
  if (!rows.length) {
    if (heat) heat.remove();
    return;
  }
  const html = cpuHeat(rows, limit);
  if (!heat) {
    root.insertAdjacentHTML("beforeend", html);
    return;
  }
  const slice = limit && rows.length > limit ? rows.slice(0, limit) : rows;
  const cells = heat.querySelectorAll(".sf-cpu");
  if (cells.length !== slice.length) {
    heat.outerHTML = html;
    return;
  }
  slice.forEach((c, i) => {
    const pct = Number(c.pct);
    cells[i].className = `sf-cpu ${band(pct)}`;
    cells[i].title = `${c.node ? shortName(c.node) + " " : ""}CPU ${c.id} · ${Number.isFinite(pct) ? pct.toFixed(0) : "—"}%`;
  });
}

function paintLiveOnWrap(wrap, n) {
  if (!wrap || !n || minimized.has(n.id)) return;
  let slot = wrap.querySelector("[data-live]");
  const live = liveForGraphNode(n);
  if (!slot) return;
  if (!slot.querySelector(".sf-bar")) {
    const html = liveStrip(n);
    if (html) {
      slot.outerHTML = html;
      slot = wrap.querySelector("[data-live]");
    }
    return;
  }
  if (!live) return;
  if (live.kind === "cluster") {
    const c = live.cluster || {};
    setBarEl(slot, "cpu", c.cpu && c.cpu.pct, `${fmtCores(c.cpu && c.cpu.used)}/${fmtCores(c.cpu && c.cpu.cap)}`);
    setBarEl(slot, "mem", c.mem && c.mem.pct, fmtBytes(c.mem && c.mem.used));
    setBarEl(slot, "disk", c.disk && c.disk.pct, fmtBytes(c.disk && c.disk.used));
    paintHeat(slot, live.cpus, n.kind === "env" ? 64 : 96);
  } else if (live.kind === "node") {
    const row = live.node;
    setBarEl(slot, "cpu", row.cpu && row.cpu.pct, `${fmtCores(row.cpu && row.cpu.used)}/${row.cores || "—"}`);
    setBarEl(slot, "mem", row.mem && row.mem.pct, fmtBytes(row.mem && row.mem.used));
    setBarEl(slot, "disk", row.disk && row.disk.pct, fmtBytes(row.disk && row.disk.used));
    paintHeat(slot, live.cpus, 32);
  } else if (live.kind === "pod") {
    const p = live.pod;
    const host = liveNodeFor(n.machineName || n.node || "");
    const cpuPct = host && host.cpu && host.cpu.cap ? Math.min(100, (Number(p.cpu) / host.cpu.cap) * 100) : Math.min(100, Number(p.cpu) * 100);
    const memPct = host && host.mem && host.mem.cap ? Math.min(100, (Number(p.mem) / host.mem.cap) * 100) : null;
    setBarEl(slot, "cpu", Number.isFinite(cpuPct) ? cpuPct : null, fmtCores(p.cpu));
    setBarEl(slot, "mem", memPct, fmtBytes(p.mem));
  } else if (live.kind === "ns") {
    const host = liveNodeFor(n.machineName || "");
    const cpuPct = host && host.cpu && host.cpu.cap ? Math.min(100, (Number(live.cpu) / host.cpu.cap) * 100) : null;
    const memPct = host && host.mem && host.mem.cap ? Math.min(100, (Number(live.mem) / host.mem.cap) * 100) : null;
    setBarEl(slot, "cpu", cpuPct, fmtCores(live.cpu));
    setBarEl(slot, "mem", memPct, fmtBytes(live.mem));
  }
}

function paintAllLive(opts = {}) {
  const root = document.getElementById("dm-nodes");
  if (root) {
    root.querySelectorAll("[data-node]").forEach((wrap) => {
      const n = lastGraph.byId.get(wrap.getAttribute("data-node"));
      if (n) paintLiveOnWrap(wrap, n);
    });
  }
  const slot = document.getElementById("dm-live-slot");
  const n = lastGraph.byId.get(selected);
  const live = liveForGraphNode(n);
  if (slot && live) {
    const liveRoot = slot.querySelector(".dm-live") || slot;
    if (live.kind === "cluster") {
      const c = live.cluster || {};
      setBarEl(liveRoot, "cpu", c.cpu && c.cpu.pct, `${fmtCores(c.cpu && c.cpu.used)} / ${fmtCores(c.cpu && c.cpu.cap)}`);
      setBarEl(liveRoot, "mem", c.mem && c.mem.pct, `${fmtBytes(c.mem && c.mem.used)} / ${fmtBytes(c.mem && c.mem.cap)}`);
      setBarEl(liveRoot, "disk", c.disk && c.disk.pct, `${fmtBytes(c.disk && c.disk.used)} / ${fmtBytes(c.disk && c.disk.cap)}`);
    } else if (live.kind === "node") {
      const row = live.node;
      setBarEl(liveRoot, "cpu", row.cpu && row.cpu.pct, `${fmtCores(row.cpu && row.cpu.used)} / ${row.cores}`);
      setBarEl(liveRoot, "mem", row.mem && row.mem.pct, `${fmtBytes(row.mem && row.mem.used)} / ${fmtBytes(row.mem && row.mem.cap)}`);
      setBarEl(liveRoot, "disk", row.disk && row.disk.pct, `${fmtBytes(row.disk && row.disk.used)} / ${fmtBytes(row.disk && row.disk.cap)}`);
    } else if (live.kind === "pod") {
      const p = live.pod;
      const host = liveNodeFor(n.machineName || n.node || "");
      const cpuPct = host && host.cpu && host.cpu.cap ? Math.min(100, (Number(p.cpu) / host.cpu.cap) * 100) : Math.min(100, Number(p.cpu) * 100);
      const memPct = host && host.mem && host.mem.cap ? Math.min(100, (Number(p.mem) / host.mem.cap) * 100) : null;
      setBarEl(liveRoot, "cpu", Number.isFinite(cpuPct) ? cpuPct : null, fmtCores(p.cpu));
      setBarEl(liveRoot, "mem", memPct, fmtBytes(p.mem));
    }
  }
  const now = performance.now();
  if (opts.forceSpark || now - lastSparkAt > 200) {
    lastSparkAt = now;
    const c = shownLive && shownLive.cluster;
    if (c) {
      pushHist("cluster", null, { t: Date.now(), cpu: c.cpu && c.cpu.pct, mem: c.mem && c.mem.pct, disk: c.disk && c.disk.pct });
    }
    patchDetailSparks();
    renderEnvHealth();
  }
}

function patchDetailSparks() {
  const slot = document.getElementById("dm-live-slot");
  if (!slot) return;
  const n = lastGraph.byId.get(selected);
  const live = liveForGraphNode(n);
  if (!live) return;
  let series = [];
  const colors = ["var(--ok)", "#58a6ff", "#d29922"];
  if (live.kind === "cluster") {
    series = [
      (liveHist.cluster || []).map((s) => s.cpu),
      (liveHist.cluster || []).map((s) => s.mem),
      (liveHist.cluster || []).map((s) => s.disk),
    ];
  } else if (live.kind === "node" && live.node) {
    const hist = (liveHist.nodes && liveHist.nodes[live.node.name]) || [];
    series = [hist.map((s) => s.cpu), hist.map((s) => s.mem), hist.map((s) => s.disk)];
  } else {
    return;
  }
  slot.querySelectorAll(".dm-live-pair").forEach((pair, i) => {
    const host = pair.querySelector("svg.sf-spark, span.sf-spark");
    if (!host || !series[i]) return;
    host.outerHTML = sparklineMini(series[i], colors[i]);
  });
}

function loopLiveSmooth(ts) {
  liveRaf = requestAnimationFrame(loopLiveSmooth);
  if (document.hidden || !liveMetrics) return;
  const dt = liveRafLast ? Math.min(0.05, (ts - liveRafLast) / 1000) : 0.016;
  liveRafLast = ts;
  stepShownLive(dt);
  paintAllLive();
}

function startLiveSmooth() {
  if (liveRaf) cancelAnimationFrame(liveRaf);
  liveRafLast = 0;
  liveRaf = requestAnimationFrame(loopLiveSmooth);
}

function stopLiveSmooth() {
  if (liveRaf) cancelAnimationFrame(liveRaf);
  liveRaf = 0;
}

function rememberLive(data) {
  if (!data) return;
  const c = data.cluster || {};
  pushHist("cluster", null, {
    t: Date.now(),
    cpu: c.cpu && c.cpu.pct,
    mem: c.mem && c.mem.pct,
    disk: c.disk && c.disk.pct,
  });
  for (const n of data.nodes || []) {
    pushHist("nodes", n.name, {
      t: Date.now(),
      cpu: n.cpu && n.cpu.pct,
      mem: n.mem && n.mem.pct,
      disk: n.disk && n.disk.pct,
    });
  }
  for (const p of data.pods || []) {
    pushHist("pods", `${p.ns}/${p.name}`, {
      t: Date.now(),
      cpu: Math.min(100, Number(p.cpu || 0) * 100),
      memGi: Number(p.mem || 0),
    });
  }
}

function refreshLiveMetrics() {
  if (!envId || metricsInflight || metricsMissing) return;
  if (!document.getElementById("dm-card")) return;
  metricsInflight = true;
  const id = envId;
  api(`/api/v1/ops/environments/${encodeURIComponent(id)}/live-metrics`, { timeout: 12000 })
    .then((data) => {
      if (envId !== id) return;
      liveMetrics = data;
      rememberLive(data);
      if (!shownLive) shownLive = JSON.parse(JSON.stringify(data));
      startLiveSmooth();
      paintAllLive({ forceSpark: true });
    })
    .catch((err) => {
      if (envId !== id) return;
      if (err && err.status === 404) {
        metricsMissing = true;
        if (metricsTimer) clearTimeout(metricsTimer);
        metricsTimer = null;
        return;
      }
      if (liveMetrics) return;
      liveMetrics = {
        error: "Live metrics unavailable",
        cluster: {},
        cpus: [],
        nodes: [],
        pods: [],
      };
    })
    .finally(() => {
      metricsInflight = false;
      renderLiveSlot();
    });
}

function scheduleMetrics() {
  if (metricsMissing) return;
  if (metricsTimer) clearTimeout(metricsTimer);
  metricsTimer = setTimeout(() => {
    refreshLiveMetrics();
    scheduleMetrics();
  }, METRICS_MS);
}

function schedule() {
  if (timer) clearTimeout(timer);
  const running = isLive() || !!(pipe && pipe.running) || (job && ACTIVE.has(String(job.status || "")));
  const pxeHotNow = ((snap && snap.pxe && snap.pxe.hosts) || []).some((h) => h && pxeHot(h));
  const wait = running ? (pxeHotNow ? 1500 : isLive() ? LIVE_POLL_MS : POLL_MS) : IDLE_MS;
  timer = setTimeout(() => tick(), wait);
}

async function tick() {
  if (!envId || !document.getElementById("dm-card")) return;
  try {
    await fetchState();
  } catch {
    /* keep last frame */
  }
  schedule();
}

async function repairCluster() {
  if (!envId) return;
  toast("Repairing cluster (Longhorn labels + stale pods)…", "ok");
  try {
    const labels = await api(`/api/v1/environments/${encodeURIComponent(envId)}/k8s/ensure-longhorn-labels`, {
      method: "POST",
      body: "{}",
    });
    const gc = await api(`/api/v1/environments/${encodeURIComponent(envId)}/k8s/gc-stale-pods`, {
      method: "POST",
      body: "{}",
    });
    toast(`${labels.message || "labels ok"} · ${gc.message || "gc ok"}`, "ok");
    workloadsAt = 0;
    await fetchState();
  } catch (e) {
    toast(e && e.message ? e.message : "repair failed", "error");
  }
}

async function restackOpenstack() {
  if (!envId) return;
  if (
    !confirm(
      "Restack OpenStack on the running Kubernetes cluster?\n\nTalos and kube-ovn stay up. Helm from OpenStack core. Tempest is not included. This is not a metal wipe and does not upgrade CNI."
    )
  ) {
    return;
  }
  try {
    const created = await api(`/api/v1/environments/${encodeURIComponent(envId)}/jobs`, {
      method: "POST",
      body: JSON.stringify({
        operation: "genestack.deploy",
        params: { from_stage: "core", skip_push: true, parallelism: 2 },
      }),
    });
    toast(`Restack ${String(created.id || "").slice(0, 8)}… from core (not CNI)`, "ok");
    beginLive({ collapse: true });
    await fetchState();
    schedule();
  } catch (e) {
    toast(e && e.message ? e.message : "restack failed to start", "error");
  }
}

async function runValidation(kind) {
  if (!envId) return;
  const which = kind === "verify" ? "verify" : "tempest";
  try {
    const created = await api(`/api/v1/ops/environments/${encodeURIComponent(envId)}/validate`, {
      method: "POST",
      body: JSON.stringify(which === "verify" ? { kind: "verify", level: "standard" } : { kind: "tempest", action: "run" }),
    });
    toast(`${which === "verify" ? "Verify" : "Tempest"} ${String(created.id || "").slice(0, 8)}…`, "ok");
    beginLive({ collapse: true });
    await fetchState();
    schedule();
  } catch (e) {
    toast(e && e.message ? e.message : "validation failed to start", "error");
  }
}

async function warmImageCache() {
  if (!envId) return;
  try {
    const created = await api(`/api/v1/environments/${encodeURIComponent(envId)}/jobs`, {
      method: "POST",
      body: JSON.stringify({ operation: "registry.mirror", params: {} }),
    });
    toast(`Caching images ${String(created.id || "").slice(0, 8)}…`, "ok");
    beginLive({ collapse: true });
    await fetchState();
    schedule();
  } catch (e) {
    toast(e && e.message ? e.message : "image cache warm failed to start", "error");
  }
}

async function greenfieldRedeploy() {
  if (!envId) return;
  if (
    !confirm(
      "GREENFIELD REDEPLOY\n\nThis PXE-boots every inventory server (iLO ISO if PXE fails; OVH BYOI on OVH), formats Talos install disks, rebuilds Kubernetes, then redeploys OpenStack from the current inventory.\n\nEverything running on those boxes is destroyed. BMC must be registered for each server."
    )
  ) {
    return;
  }
  const typed = window.prompt(`Type ${envName || "GREENFIELD"} to confirm the metal wipe:`);
  if (typed == null) return;
  if (String(typed).trim() !== String(envName || "GREENFIELD").trim()) {
    toast("Greenfield cancelled — name did not match", "warn");
    return;
  }
  try {
    const created = await api(`/api/v1/environments/${encodeURIComponent(envId)}/jobs`, {
      method: "POST",
      body: JSON.stringify({
        operation: "genestack.greenfield",
        params: { skip_push: true, boot: "auto", parallelism: 2 },
      }),
    });
    toast(`Greenfield ${String(created.id || "").slice(0, 8)}… PXE then hosts`, "ok");
    beginLive({ collapse: true });
    await fetchState();
    schedule();
  } catch (e) {
    toast(e && e.message ? e.message : "greenfield failed to start", "error");
  }
}

async function bmNodeFor(name) {
  if (!envId || !name) return null;
  if (Date.now() - bmNodesCache.at > 20000 || !bmNodesCache.nodes.length) {
    const bm = await api(`/api/v1/environments/${encodeURIComponent(envId)}/baremetal`);
    bmNodesCache = { at: Date.now(), nodes: (bm && bm.nodes) || [] };
  }
  const want = String(name || "").toLowerCase();
  const short = shortName(name).toLowerCase();
  return (
    bmNodesCache.nodes.find((n) => n && String(n.name || "").toLowerCase() === want) ||
    bmNodesCache.nodes.find((n) => n && shortName(n.name).toLowerCase() === short) ||
    bmNodesCache.nodes.find((n) => n && String(n.hostname || "").toLowerCase() === want) ||
    null
  );
}

async function openIlo(name) {
  return openHostModal(name);
}

function vmFactsHtml(node) {
  const addrs = node.addresses && typeof node.addresses === "object" ? JSON.stringify(node.addresses) : "";
  return `<div class="dm-insp-head"><h3>${esc(node.title || node.name || "instance")}</h3><span class="pill ${
    node.state === "ok" ? "ok" : node.state === "bad" ? "bad" : node.state === "run" ? "warn" : ""
  }">${esc(node.status || "VM")}</span></div>
    <div class="dm-insp-actions">
      <button type="button" class="secondary btn-sm" data-dm-vm-console data-id="${esc(node.vmId || "")}">VNC console</button>
    </div>
    <table class="dm-check"><tbody>
      <tr><td>ID</td><td colspan="2"><code>${esc(node.vmId || "")}</code></td></tr>
      <tr><td>Hypervisor</td><td colspan="2">${esc(node.host || node.machineName || "")}</td></tr>
      <tr><td>Tenant</td><td colspan="2">${esc(node.tenant || node.project_name || node.project_id || "")}</td></tr>
      <tr><td>Network</td><td colspan="2">${esc((node.networks || []).join(", ") || node.network || "")}</td></tr>
      <tr><td>Flavor</td><td colspan="2">${esc(node.flavor || "")}</td></tr>
      <tr><td>Image</td><td colspan="2">${esc(node.image || "")}</td></tr>
      <tr><td>Addresses</td><td colspan="2">${esc(addrs)}</td></tr>
      ${(node.fips || []).length ? `<tr><td>Floating IP</td><td colspan="2">${esc((node.fips || []).join(", "))}</td></tr>` : ""}
    </tbody></table>
    ${pathLine(node)}`;
}

function podFactsHtml(node) {
  const ns = node.namespace || "default";
  const pn = node.name || node.title || "";
  const ops = canRun()
    ? `<button type="button" class="secondary btn-sm" data-dm-pod-exec data-ns="${esc(ns)}" data-pod="${esc(pn)}">Console</button>
       <button type="button" class="secondary btn-sm" data-dm-pod-restart data-ns="${esc(ns)}" data-pod="${esc(pn)}" ${gate(true, "operator")}>Restart</button>
       <button type="button" class="danger btn-sm" data-dm-pod-delete data-ns="${esc(ns)}" data-pod="${esc(pn)}" ${gate(true, "operator")}>Delete</button>`
    : "";
  return `<div class="dm-insp-head"><h3>${esc(pn)}</h3><span class="pill ${
    node.state === "ok" ? "ok" : node.state === "bad" ? "bad" : node.state === "run" ? "warn" : ""
  }">${esc(node.phase || node.state || "")}</span></div>
    <div class="dm-insp-actions">
      <button type="button" class="secondary btn-sm" data-dm-pod-logs data-ns="${esc(ns)}" data-pod="${esc(pn)}">Logs</button>
      <button type="button" class="secondary btn-sm" data-dm-pod-describe data-ns="${esc(ns)}" data-pod="${esc(pn)}">Describe</button>
      ${ops}
    </div>
    <table class="dm-check"><tbody>
      <tr><td>Namespace</td><td colspan="2">${esc(ns)}</td></tr>
      <tr><td>Node</td><td colspan="2">${esc(node.node || node.machineName || "")}</td></tr>
      <tr><td>Ready</td><td colspan="2">${esc(node.ready == null ? "" : String(node.ready))}</td></tr>
      <tr><td>Restarts</td><td colspan="2">${esc(node.restarts == null ? "" : String(node.restarts))}</td></tr>
      <tr><td>Owner</td><td colspan="2">${esc((node.controllers || []).join(", ") || "none")}</td></tr>
    </tbody></table>`;
}

function hostFactsHtml(name) {
  const m = findMachine(name);
  if (!m) return `<p class="muted">Host ${esc(name)} is not in inventory.</p>`;
  const st = machineState(m);
  const pxe = m.pxe;
  const rows = [
    ["Config", st.configured ? "ok" : "missing", st.roles.join(", ") || "assign roles in Config"],
    [
      "PXE",
      pxe && pxe.phase ? "run" : "idle",
      pxe && pxe.phase ? `${pxe.phase}${pxe.detail ? " · " + pxe.detail : ""}` : "idle",
    ],
    ["Talos", st.talosOk ? "ok" : "down", st.talosVer || "not reachable"],
    ["Kubernetes", st.k8sReady ? "ok" : "pending", `${st.k8sRoles || "no k8s role"} ${st.k8sVer}`.trim()],
    ["CPU", st.cpu ? "ok" : "wait", st.cpu ? `${st.cpu} cores` : "unknown"],
    ["Memory", st.memGi != null ? "ok" : "wait", st.memGi != null ? `${st.memGi} Gi` : "unknown"],
    ["OpenStack", st.osState, st.osRoles.join(", ") || "no OpenStack role on this box"],
  ];
  const table = rows
    .map(
      ([k, s, d]) =>
        `<tr><td>${esc(k)}</td><td><span class="pill ${s === "ok" ? "ok" : s === "run" || s === "pending" ? "warn" : ""}">${esc(
          s
        )}</span></td><td class="muted">${esc(d)}</td></tr>`
    )
    .join("");
  const talosOps = canRun()
    ? `<button type="button" class="secondary btn-sm" data-dm-talos-reboot data-name="${esc(m.name)}" ${gate(true, "operator")}>Reboot</button>
       <button type="button" class="secondary btn-sm" data-dm-talos-shutdown data-name="${esc(m.name)}" ${gate(true, "operator")}>Shutdown</button>
       <button type="button" class="danger btn-sm" data-dm-talos-reset data-name="${esc(m.name)}" ${gate(true, "operator")}>Reset</button>`
    : "";
  return `<div class="dm-insp-head"><h3>${esc(shortName(m.name))}</h3><span class="muted">${esc(st.ip || "")}</span></div>
    <div class="dm-insp-actions">
      <button type="button" class="secondary btn-sm" data-dm-ilo="${esc(m.name)}">iLO</button>
      <button type="button" class="secondary btn-sm" data-dm-talos-dmesg data-name="${esc(m.name)}">Talos logs</button>
      <button type="button" class="secondary btn-sm" data-dm-talos-services data-name="${esc(m.name)}">Services</button>
      <button type="button" class="secondary btn-sm" data-dm-talos-health data-name="${esc(m.name)}">Health</button>
    </div>
    <div class="dm-insp-actions">${talosOps}</div>
    <table class="dm-check"><tbody>${table}</tbody></table>`;
}

function onHostKey(e) {
  if (e.key === "Escape") {
    e.preventDefault();
    closeHostModal();
  }
}

function nodeModalTabs() {
  const kind = nodeModal && nodeModal.kind;
  if (kind === "machine") return [{ id: "ilo", label: "iLO" }, { id: "talos", label: "Talos logs" }];
  if (kind === "pod") return [{ id: "exec", label: "Console" }, { id: "logs", label: "Logs" }];
  return [];
}

function renderNodeTabs() {
  const el = document.getElementById("dm-host-tabs");
  if (!el) return;
  const tabs = nodeModalTabs();
  const active = (nodeModal && nodeModal.console) || "";
  if (!tabs.length) {
    el.hidden = true;
    el.innerHTML = "";
    return;
  }
  el.hidden = false;
  el.innerHTML = tabs
    .map(
      (t) =>
        `<button type="button" class="${t.id === active ? "" : "secondary "}btn-sm" data-dm-host-tab="${esc(t.id)}">${esc(
          t.label
        )}</button>`
    )
    .join("");
}

function showIframePane(on) {
  const frame = document.getElementById("dm-host-frame");
  const term = document.getElementById("dm-host-term");
  const wait = document.getElementById("dm-host-kvm-wait");
  if (frame) frame.hidden = !on;
  if (term) term.hidden = !!on;
  if (wait && !on) wait.hidden = true;
}

function setKvmWait(msg) {
  const wait = document.getElementById("dm-host-kvm-wait");
  if (!wait) return;
  if (msg) {
    wait.hidden = false;
    wait.textContent = msg;
  } else {
    wait.hidden = true;
  }
}

function stopNodeLog() {
  if (nodeLogTimer) {
    clearInterval(nodeLogTimer);
    nodeLogTimer = null;
  }
}

let iloLiveFrame = null;

function closeIloConsole() {
  const frame = iloLiveFrame || document.getElementById("dm-host-frame");
  if (frame) frame.src = "about:blank";
  iloLiveFrame = null;
}

async function openIloConsole(currentEnv, node, opts) {
  const o = opts || {};
  const frame = o.frame || document.getElementById("dm-host-frame");
  closeIloConsole();
  iloLiveFrame = frame;
  if (!node || !node.id) {
    throw new Error("No management port is registered for this host. Add it on Machines.");
  }
  const data = await api(
    `/api/v1/environments/${encodeURIComponent(currentEnv)}/baremetal/nodes/${encodeURIComponent(node.id)}/console/session`,
    { method: "POST", timeout: 25000 }
  );
  const embed = data && data.embed_url ? String(data.embed_url) : "";
  if (!data || !data.ok || !embed.startsWith("/") || embed.startsWith("//")) {
    throw new Error((data && (data.error || data.message)) || "iLO console unavailable");
  }
  if (frame) {
    frame.removeAttribute("sandbox");
    frame.src = embed;
  }
  if (o.popEl) o.popEl.disabled = false;
  if (o.helpEl) o.helpEl.textContent = "iLO HTML5 console. BIOS / PXE / OS for this box.";
}

function stopNodeConsole() {
  stopNodeLog();
  closeIloConsole();
  closeExec();
  const frame = document.getElementById("dm-host-frame");
  if (frame) {
    frame.src = "about:blank";
    frame.removeAttribute("sandbox");
    frame.removeAttribute("hidden");
  }
  const term = document.getElementById("dm-host-term");
  if (term) {
    term.innerHTML = "";
    term.hidden = true;
  }
  const pop = document.querySelector("#dm-host-modal [data-dm-host-popout]");
  if (pop) pop.disabled = true;
}

function ensureHostModal() {
  let el = document.getElementById("dm-host-modal");
  if (el) return el;
  el = document.createElement("div");
  el.id = "dm-host-modal";
  el.className = "os-console-modal dm-host-overlay";
  el.hidden = true;
  el.innerHTML = `<div class="os-console-panel dm-host-panel" role="dialog" aria-modal="true" aria-labelledby="dm-host-title">
      <div class="os-console-head">
        <h2 id="dm-host-title">Console</h2>
        <div class="os-console-actions">
          <button type="button" class="secondary btn-sm" data-dm-host-popout disabled>Open in tab</button>
          <button type="button" class="secondary btn-sm" data-dm-host-reconnect>Reconnect</button>
          <button type="button" class="secondary btn-sm" data-dm-host-close>Close</button>
        </div>
      </div>
      <div class="dm-host-grid">
        <aside id="dm-host-facts" class="dm-host-facts"></aside>
        <section class="dm-host-kvm">
          <div id="dm-host-tabs" class="dm-host-tabs" hidden></div>
          <iframe id="dm-host-frame" title="Remote console" referrerpolicy="no-referrer" allow="clipboard-read; clipboard-write; fullscreen"></iframe>
          <div id="dm-host-kvm-wait" class="dm-host-kvm-wait" hidden>Opening iLO…</div>
          <div id="dm-host-term" class="dm-host-term" hidden></div>
          <p class="muted os-console-help" id="dm-host-help"></p>
        </section>
      </div>
    </div>`;
  document.body.appendChild(el);
  el.addEventListener("click", (e) => {
    if (e.target === el || e.target.closest("[data-dm-host-close]")) {
      closeHostModal();
      return;
    }
    const tab = e.target.closest("[data-dm-host-tab]");
    if (tab && nodeModal) {
      e.preventDefault();
      nodeModal.console = tab.getAttribute("data-dm-host-tab") || nodeModal.console;
      startNodeConsole({ reconnect: true });
      refreshNodeModal();
      return;
    }
    if (e.target.closest("[data-dm-host-reconnect]")) {
      e.preventDefault();
      startNodeConsole({ reconnect: true });
      return;
    }
    if (e.target.closest("[data-dm-ilo]")) {
      e.preventDefault();
      if (nodeModal) nodeModal.console = "ilo";
      startNodeConsole({ reconnect: true });
      refreshNodeModal();
      return;
    }
    if (e.target.closest("[data-dm-talos-dmesg]")) {
      e.preventDefault();
      if (nodeModal) nodeModal.console = "talos";
      startNodeConsole({ reconnect: true });
      refreshNodeModal();
      return;
    }
    if (e.target.closest("[data-dm-vm-console]")) {
      e.preventDefault();
      if (nodeModal) nodeModal.console = "vnc";
      startNodeConsole({ reconnect: true });
      return;
    }
    if (e.target.closest("[data-dm-pod-exec]")) {
      e.preventDefault();
      if (nodeModal) nodeModal.console = "exec";
      startNodeConsole({ reconnect: true });
      refreshNodeModal();
      return;
    }
    if (e.target.closest("[data-dm-pod-logs]")) {
      e.preventDefault();
      if (nodeModal) nodeModal.console = "logs";
      startNodeConsole({ reconnect: true });
      refreshNodeModal();
      return;
    }
    if (e.target.closest("[data-dm-host-popout]")) {
      e.preventDefault();
      const frame = document.getElementById("dm-host-frame");
      const src = frame && !frame.hidden && frame.src;
      if (src && src !== "about:blank") window.open(src, "_blank", "noopener");
      return;
    }
    handleDetailClick(e);
  });
  return el;
}

function closeHostModal() {
  nodeModal = null;
  stopNodeConsole();
  renderNodePop();
  applyNestConsoleStage("");
  const modal = document.getElementById("dm-host-modal");
  if (modal) modal.hidden = true;
  if (hostEscBound) {
    document.removeEventListener("keydown", onHostKey);
    hostEscBound = false;
  }
}

function nodeModalTitle() {
  if (!nodeModal) return "Console";
  if (nodeModal.kind === "vm") return `${nodeModal.name || "instance"} · VNC`;
  if (nodeModal.kind === "pod") return `${nodeModal.ns || "default"}/${nodeModal.name}`;
  const m = findMachine(nodeModal.name);
  const st = m ? machineState(m) : {};
  const pxe = m && m.pxe;
  const phase = pxe && pxe.phase ? pxe.phase : st.k8sReady ? "Ready" : "";
  const which = nodeModal.console === "talos" ? "Talos logs" : "iLO";
  return `${shortName(nodeModal.name)}${phase ? " · " + phase : ""} · ${which}`;
}

function nodeFactsHtml() {
  if (!nodeModal) return "";
  const graphNode = lastGraph.byId.get(nodeModal.key) || lastGraph.byId.get(selected);
  if (graphNode) return inspectorFor(graphNode.id);
  if (nodeModal.kind === "vm") {
    return vmFactsHtml({
      title: nodeModal.name,
      vmId: nodeModal.vmId,
      status: nodeModal.status,
      flavor: nodeModal.flavor,
      image: nodeModal.image,
      host: nodeModal.host,
      machineName: nodeModal.machineName,
      addresses: nodeModal.addresses,
      state: nodeModal.state,
    });
  }
  if (nodeModal.kind === "pod") {
    return podFactsHtml({
      name: nodeModal.name,
      namespace: nodeModal.ns,
      machineName: nodeModal.machineName,
    });
  }
  return hostFactsHtml(nodeModal.name);
}

function refreshNodeModal() {
  if (!nodeModal) return;
  const modal = document.getElementById("dm-host-modal");
  if (!modal || modal.hidden) return;
  const title = document.getElementById("dm-host-title");
  if (title) title.textContent = nodeModalTitle();
  renderNodeTabs();
  const facts = document.getElementById("dm-host-facts");
  if (facts) {
    const html = nodeFactsHtml() || `<p class="muted">No details for this node.</p>`;
    if (facts.dataset.last !== html) {
      facts.dataset.last = html;
      facts.innerHTML = html;
      const slot = facts.querySelector("#dm-live-slot");
      if (slot) slot.id = "dm-live-slot-modal";
    }
  }
}

async function fillTermText(loader, { follow } = {}) {
  const term = document.getElementById("dm-host-term");
  const help = document.getElementById("dm-host-help");
  if (!term) return;
  showIframePane(false);
  if (!term.querySelector("pre")) term.innerHTML = `<pre class="dm-log dm-tool-pre">Loading…</pre>`;
  const pre = term.querySelector("pre");
  const run = async () => {
    if (!nodeModal) return;
    try {
      const text = await loader();
      if (!nodeModal || !pre) return;
      if (pre.textContent !== text) pre.textContent = text;
      if (follow) requestAnimationFrame(() => scrollLogToEnd(pre));
    } catch (e) {
      if (pre && (!pre.textContent || pre.textContent === "Loading…")) {
        pre.textContent = e && e.message ? e.message : "unavailable";
      }
    }
  };
  await run();
  stopNodeLog();
  if (follow) nodeLogTimer = setInterval(run, 4000);
  if (help) help.textContent = follow ? "Following live output." : "";
}

async function startIloConsole(opts = {}) {
  const modal = ensureHostModal();
  const frame = document.getElementById("dm-host-frame");
  const help = document.getElementById("dm-host-help");
  const pop = modal.querySelector("[data-dm-host-popout]");
  const title = document.getElementById("dm-host-title");
  const term = document.getElementById("dm-host-term");
  stopNodeLog();
  closeExec();
  if (term) term.hidden = true;
  if (frame) {
    frame.hidden = false;
    frame.removeAttribute("sandbox");
    frame.setAttribute("allow", "clipboard-read; clipboard-write; fullscreen");
  }
  showIframePane(true);
  setKvmWait("Opening iLO…");
  if (help) help.textContent = "Opening iLO…";
  try {
    const node = await bmNodeFor(nodeModal && nodeModal.name);
    if (!node || !node.id) {
      const msg = "No management port is registered for this host. Add it on Machines.";
      setKvmWait(msg);
      if (help) help.textContent = msg;
      toast(msg, "warn");
      return;
    }
    await openIloConsole(envId, node, {
      reconnect: !!opts.reconnect,
      modal,
      frame,
      titleEl: title,
      helpEl: help,
      popEl: pop,
      noEsc: true,
    });
    if (help && /unavailable|failed|No BMC|session table/i.test(help.textContent || "")) {
      setKvmWait(help.textContent);
      return;
    }
    if (help && (!help.textContent || help.textContent.startsWith("iLO console") || help.textContent === "Opening iLO…")) {
      help.textContent = "iLO HTML5 console. BIOS / PXE / OS for this box.";
    }
    setTimeout(() => setKvmWait(""), 1600);
  } catch (e) {
    const msg = e && e.message ? e.message : "iLO failed";
    toast(msg, "error");
    if (help) help.textContent = msg;
    setKvmWait(msg);
  }
}

async function startVmConsole() {
  const frame = document.getElementById("dm-host-frame");
  const help = document.getElementById("dm-host-help");
  const pop = document.querySelector("#dm-host-modal [data-dm-host-popout]");
  const serverId = nodeModal && nodeModal.vmId;
  if (!serverId) {
    if (help) help.textContent = "This instance has no server id.";
    return;
  }
  stopNodeLog();
  closeIloConsole();
  closeExec();
  showIframePane(true);
  setKvmWait("Opening VNC…");
  if (frame) {
    frame.setAttribute("sandbox", "allow-scripts allow-same-origin allow-forms");
    frame.src = "about:blank";
  }
  if (help) help.textContent = "Opening OpenStack VNC…";
  try {
    const d = await api(
      `/api/v1/environments/${encodeURIComponent(envId)}/cloud/servers/${encodeURIComponent(serverId)}/console/session`,
      { method: "POST", timeout: 25000 }
    );
    if (!d || !d.ok || !d.embed_url) {
      const err = (d && (d.error || d.message)) || "VNC console unavailable";
      toast(err, "error");
      if (help) help.textContent = err;
      setKvmWait(err);
      return;
    }
    const embed = String(d.embed_url);
    if (!embed.startsWith("/") || embed.startsWith("//")) {
      toast("VNC console unavailable", "error");
      if (help) help.textContent = "VNC console unavailable";
      setKvmWait("VNC console unavailable");
      return;
    }
    if (frame) frame.src = embed;
    if (pop) pop.disabled = false;
    if (help) help.textContent = "OpenStack VNC via Console proxy (nova-novncproxy). Token stays on the server.";
    setTimeout(() => setKvmWait(""), 800);
  } catch (e) {
    const msg = e && e.message ? e.message : "VNC failed";
    toast(msg, "error");
    if (help) help.textContent = msg;
    setKvmWait(msg);
  }
}

async function startTalosLogs() {
  const name = nodeModal && nodeModal.name;
  if (!name) return;
  closeIloConsole();
  closeExec();
  const help = document.getElementById("dm-host-help");
  if (help) help.textContent = "Talos kernel log (dmesg) for this node.";
  await fillTermText(async () => {
    const d = await api(
      `/api/v1/environments/${encodeURIComponent(envId)}/platform/nodes/${encodeURIComponent(name)}/dmesg`,
      { timeout: 25000 }
    );
    return (d && (d.text || d.yaml || d.error)) || JSON.stringify(d, null, 2);
  }, { follow: true });
}

async function startPodLogsConsole() {
  const ns = (nodeModal && nodeModal.ns) || "default";
  const pod = nodeModal && nodeModal.name;
  if (!pod) return;
  closeIloConsole();
  closeExec();
  const help = document.getElementById("dm-host-help");
  if (help) help.textContent = `Logs — ${ns}/${pod}`;
  await fillTermText(() => fetchPodLogText(ns, pod), { follow: true });
}

async function startPodExecConsole() {
  const ns = (nodeModal && nodeModal.ns) || "default";
  const pod = nodeModal && nodeModal.name;
  const help = document.getElementById("dm-host-help");
  const term = document.getElementById("dm-host-term");
  if (!pod || !term) return;
  if (!canRun()) {
    if (help) help.textContent = "Container exec needs operator role — showing logs.";
    nodeModal.console = "logs";
    await startPodLogsConsole();
    return;
  }
  closeIloConsole();
  stopNodeLog();
  showIframePane(false);
  term.innerHTML = `<div class="muted">Connecting…</div>`;
  if (help) help.textContent = `Container exec — ${ns}/${pod}`;
  await openPodExec(ns, pod, { mount: term });
}

async function startNodeConsole(opts = {}) {
  if (!nodeModal) return;
  const kind = nodeModal.kind;
  const mode = nodeModal.console;
  renderNodeTabs();
  if (kind === "vm" || mode === "vnc") return startVmConsole();
  if (kind === "pod" && mode === "logs") return startPodLogsConsole();
  if (kind === "pod") return startPodExecConsole();
  if (kind === "machine" && mode === "talos") return startTalosLogs();
  return startIloConsole(opts);
}

function graphNodeToModal(n) {
  if (!n) return null;
  if (n.kind === "vm") {
    return {
      kind: "vm",
      key: n.id || `vm:${n.vmId}`,
      name: n.title || n.name || n.vmId,
      vmId: n.vmId,
      machineName: n.machineName,
      status: n.status,
      flavor: n.flavor,
      image: n.image,
      host: n.host,
      addresses: n.addresses,
      state: n.state,
      console: "vnc",
    };
  }
  if (n.kind === "pod") {
    return {
      kind: "pod",
      key: n.id || `pod:${n.namespace || "default"}/${n.name}`,
      name: n.name || n.title,
      ns: n.namespace || "default",
      machineName: n.machineName,
      console: canRun() ? "exec" : "logs",
    };
  }
  if (n.kind === "machine" || n.kind === "k8s" || n.kind === "osrole") {
    const name = n.machineName || (n.id || "").replace(/^m:/, "");
    return { kind: "machine", key: `m:${name}`, name, console: "ilo" };
  }
  return null;
}

async function openNodeModal(n, opts = {}) {
  const next = graphNodeToModal(n);
  if (!next || !envId) return;
  if (opts.console) next.console = opts.console;
  const modal = ensureHostModal();
  const same = nodeModal && nodeModal.key === next.key && !modal.hidden;
  if (same && next.console && next.console !== nodeModal.console) {
    nodeModal.console = next.console;
    await startNodeConsole({ reconnect: true });
    refreshNodeModal();
    return;
  }
  const already = same && nodeModal.console === next.console;
  const switching = nodeModal && nodeModal.key !== next.key;
  if (switching) stopNodeConsole();
  nodeModal = next;
  modal.hidden = false;
  applyNestConsoleStage(n && n.id);
  const facts = document.getElementById("dm-host-facts");
  if (facts) facts.dataset.last = "";
  refreshNodeModal();
  renderNodePop();
  if (!hostEscBound) {
    document.addEventListener("keydown", onHostKey);
    hostEscBound = true;
  }
  if (!already) await startNodeConsole({ reconnect: false });
  refreshNodeModal();
}

async function openHostModal(name) {
  if (!name) return;
  await openNodeModal({ kind: "machine", machineName: name, id: `m:${name}` });
}

function k8sNodeName(m) {
  if (!m) return "";
  const st = machineState(m);
  return st.k8sName || m.name || "";
}

let toolEscBound = false;
let execSess = null;
let logFollow = null;
let stageModalId = "";

function ensureToolModal() {
  let el = document.getElementById("dm-tool-modal");
  if (el) return el;
  el = document.createElement("div");
  el.id = "dm-tool-modal";
  el.className = "os-console-modal";
  el.hidden = true;
  el.innerHTML = `<div class="os-console-panel" role="dialog" aria-modal="true" aria-labelledby="dm-tool-title">
      <div class="os-console-head">
        <h2 id="dm-tool-title">Console</h2>
        <div class="os-console-actions">
          <button type="button" class="secondary btn-sm" data-dm-tool-close>Close</button>
        </div>
      </div>
      <div id="dm-tool-body" class="dm-tool-body"></div>
    </div>`;
  document.body.appendChild(el);
  el.addEventListener("click", (e) => {
    if (e.target === el || e.target.closest("[data-dm-tool-close]")) closeToolModal();
    const svc = e.target.closest("[data-dm-talos-svc]");
    if (svc) {
      talosService(svc.getAttribute("data-name"), svc.getAttribute("data-svc"), svc.getAttribute("data-act"));
    }
  });
  return el;
}

function onToolKey(e) {
  if (e.key === "Escape") {
    e.preventDefault();
    closeToolModal();
  }
}

function stopLogFollowTimer() {
  if (logFollow && logFollow.timer) {
    clearInterval(logFollow.timer);
    logFollow.timer = null;
  }
}

function stopLogFollow() {
  stopLogFollowTimer();
  logFollow = null;
}

function logModalOpen() {
  const modal = document.getElementById("dm-tool-modal");
  return !!(modal && !modal.hidden && logFollow && logFollow.pre && document.body.contains(logFollow.pre));
}

function startLogFollowTimer(sess) {
  stopLogFollowTimer();
  if (!sess || sess !== logFollow || !sess.follow) return;
  sess.timer = setInterval(() => {
    if (!logModalOpen() || logFollow !== sess || !sess.follow) {
      stopLogFollowTimer();
      return;
    }
    refreshPodLogs(sess);
  }, 2500);
}

function closeToolModal() {
  if (execSess && execSess.inTool) closeExec();
  stopLogFollow();
  stageModalId = "";
  const modal = document.getElementById("dm-tool-modal");
  if (modal) modal.hidden = true;
  const body = document.getElementById("dm-tool-body");
  if (body) {
    body.classList.remove("dm-log-follow");
    body.innerHTML = "";
  }
  if (toolEscBound) {
    document.removeEventListener("keydown", onToolKey);
    toolEscBound = false;
  }
}

function openToolModal(title, html) {
  stopLogFollow();
  const modal = ensureToolModal();
  const h = document.getElementById("dm-tool-title");
  const body = document.getElementById("dm-tool-body");
  if (h) h.textContent = title || "Console";
  if (body) {
    body.classList.remove("dm-log-follow");
    body.innerHTML = html || "";
  }
  modal.hidden = false;
  if (!toolEscBound) {
    document.addEventListener("keydown", onToolKey);
    toolEscBound = true;
  }
  return body;
}

function toolPre(text) {
  return `<pre class="dm-log dm-tool-pre">${esc(String(text || "(empty)"))}</pre>`;
}

function formatDescribe(data) {
  const obj = data && typeof data === "object" ? data : {};
  const events = Array.isArray(obj.events) ? obj.events : [];
  const bits = [];
  if (obj.error) bits.push(`error: ${obj.error}`);
  if (events.length) {
    bits.push("Events:");
    for (const ev of events) {
      const info = ev && typeof ev === "object" ? ev : {};
      bits.push(`  ${info.type || "Normal"} ${info.reason || ""} ${info.message || ""}`.trimEnd());
    }
    bits.push("");
  }
  if (obj.object) {
    try {
      bits.push(JSON.stringify(obj.object, null, 2));
    } catch {
      bits.push(String(obj.object));
    }
  } else if (obj.text || obj.yaml) {
    bits.push(obj.text || obj.yaml);
  } else if (!bits.length) {
    bits.push("(empty)");
  }
  return bits.join("\n");
}

function formatEvents(data) {
  const rows = (data && data.events) || [];
  if (data && data.error && !rows.length) return String(data.error);
  if (!rows.length) return "(no events)";
  return rows
    .slice(0, 80)
    .map((ev) => {
      const i = ev && typeof ev === "object" ? ev : {};
      return `${i.lastTimestamp || i.eventTime || ""} ${i.type || ""} ${i.reason || ""} ${i.involvedObject && i.involvedObject.name ? i.involvedObject.name : ""} ${i.message || ""}`.trim();
    })
    .join("\n");
}

function scrollLogToEnd(pre) {
  if (!pre) return;
  pre.scrollTop = pre.scrollHeight;
}

function logNearBottom(pre) {
  if (!pre) return true;
  return pre.scrollHeight - pre.scrollTop - pre.clientHeight < 64;
}

async function openTextTool(title, path, { timeout = 25000, format, stickBottom = false } = {}) {
  openToolModal(title, toolPre("Loading…"));
  try {
    const d = await api(`/api/v1/environments/${encodeURIComponent(envId)}${path}`, { timeout });
    const text = format ? format(d) : (d && (d.text || d.yaml || d.error)) || JSON.stringify(d, null, 2);
    const body = document.getElementById("dm-tool-body");
    if (body) body.innerHTML = toolPre(text);
    if (stickBottom) {
      const pre = body && body.querySelector(".dm-tool-pre");
      requestAnimationFrame(() => scrollLogToEnd(pre || body));
    }
  } catch (e) {
    const body = document.getElementById("dm-tool-body");
    if (body) body.innerHTML = toolPre(e && e.message ? e.message : "unavailable");
  }
}

async function openTalos(name) {
  if (!envId || !name) return;
  await openTextTool(`Kernel logs — ${shortName(name)}`, `/platform/nodes/${encodeURIComponent(name)}/dmesg`, {
    stickBottom: true,
  });
}

async function openTalosHealth(name) {
  if (!envId || !name) return;
  await openTextTool(`Talos health — ${shortName(name)}`, `/platform/nodes/${encodeURIComponent(name)}/health`);
}

async function openTalosServices(name) {
  if (!envId || !name) return;
  openToolModal(`Talos services — ${shortName(name)}`, `<div class="muted">Loading…</div>`);
  try {
    const d = await api(`/api/v1/environments/${encodeURIComponent(envId)}/platform/nodes/${encodeURIComponent(name)}/services`);
    const rows = (d && d.services) || [];
    const body = document.getElementById("dm-tool-body");
    if (!body) return;
    if (!rows.length) {
      body.innerHTML = toolPre((d && (d.text || d.error)) || "(empty)");
      return;
    }
    const acts = (id) =>
      canRun()
        ? ["start", "stop", "restart"]
            .map(
              (a) =>
                `<button type="button" class="secondary btn-sm" data-dm-talos-svc data-name="${esc(name)}" data-svc="${esc(
                  id
                )}" data-act="${a}" ${gate(true, "operator")}>${a}</button>`
            )
            .join(" ")
        : "";
    body.innerHTML = `<table class="dm-svc"><thead><tr><th>Service</th><th>State</th><th>Health</th>${
      canRun() ? "<th></th>" : ""
    }</tr></thead><tbody>${rows
      .map((s) => {
        const id = s.id || s.name || "";
        return `<tr><td><code>${esc(id)}</code></td><td>${esc(s.state || "")}</td><td>${esc(s.health || "")}</td>${
          canRun() ? `<td class="dm-insp-actions" style="margin:0">${acts(id)}</td>` : ""
        }</tr>`;
      })
      .join("")}</tbody></table>`;
  } catch (e) {
    const body = document.getElementById("dm-tool-body");
    if (body) body.innerHTML = toolPre(e && e.message ? e.message : "unavailable");
  }
}

async function talosMutate(name, action, body, confirmMsg) {
  if (!envId || !name) return;
  if (confirmMsg && !confirm(confirmMsg)) return;
  try {
    const opts = { method: "POST", timeout: 30000 };
    if (body) opts.body = JSON.stringify(body);
    const d = await api(
      `/api/v1/environments/${encodeURIComponent(envId)}/platform/nodes/${encodeURIComponent(name)}/${action}`,
      opts
    );
    if (d && d.ok === false) toast(d.error || `${action} failed`, "error");
    else {
      const msg =
        (d && d.job_id && `job ${d.job_id}: ${d.operation || action}`) ||
        (d && d.message) ||
        `${action} requested`;
      toast(msg, "ok");
    }
    if (action.startsWith("service/") && document.getElementById("dm-tool-modal") && !document.getElementById("dm-tool-modal").hidden) {
      openTalosServices(name);
    }
  } catch (e) {
    toast(e && e.message ? e.message : `${action} failed`, "error");
  }
}

function talosService(name, svc, act) {
  if (!name || !svc || !act) return;
  if (!confirm(`${act} ${svc} on ${shortName(name)}?`)) return;
  talosMutate(name, `service/${encodeURIComponent(svc)}/${encodeURIComponent(act)}`);
}

async function k8sNodeAct(name, action) {
  if (!envId || !name) return;
  const msg =
    action === "drain"
      ? `Drain Kubernetes node ${name}? Pods will be evicted.`
      : action === "cordon"
        ? `Cordon ${name}? New pods will not schedule here.`
        : `Uncordon ${name}?`;
  if (!confirm(msg)) return;
  try {
    const d = await api(
      `/api/v1/environments/${encodeURIComponent(envId)}/k8s/nodes/${encodeURIComponent(name)}/${encodeURIComponent(action)}`,
      { method: "POST", timeout: 60000 }
    );
    if (d && d.ok === false) toast(d.error || `${action} failed`, "error");
    else toast((d && d.message) || action, "ok");
  } catch (e) {
    toast(e && e.message ? e.message : `${action} failed`, "error");
  }
}

async function openDescribe(kind, ns, name) {
  if (!envId || !name) return;
  const qs = new URLSearchParams({ kind, name });
  if (kind !== "nodes" && ns) qs.set("namespace", ns);
  await openTextTool(
    `Describe ${kind} ${ns ? ns + "/" : ""}${name}`,
    `/k8s/describe?${qs}`,
    { format: formatDescribe }
  );
}

async function openEvents(ns) {
  if (!envId) return;
  const q = ns ? `?namespace=${encodeURIComponent(ns)}` : "";
  await openTextTool(`Events${ns ? " — " + ns : ""}`, `/k8s/events${q}`, { format: formatEvents });
}

async function fetchPodLogText(ns, pod) {
  const qs = new URLSearchParams({ pod, namespace: ns || "default", tail: "400" });
  const d = await api(`/api/v1/environments/${encodeURIComponent(envId)}/cluster/logs?${qs}`, {
    timeout: 15000,
  });
  if (d && d.error) return String(d.error);
  return (d && d.text) || "(empty)";
}

async function refreshPodLogs(sess) {
  if (!sess || sess.inflight || !envId || logFollow !== sess) return;
  if (!logModalOpen()) {
    stopLogFollow();
    return;
  }
  const pre = sess.pre;
  if (!pre || !document.body.contains(pre)) {
    stopLogFollow();
    return;
  }
  sess.inflight = true;
  try {
    const text = await fetchPodLogText(sess.ns, sess.pod);
    if (logFollow !== sess) return;
    const stick = sess.follow && (sess.jumpOnce || logNearBottom(pre));
    if (pre.textContent !== text) pre.textContent = text;
    if (stick) {
      sess.jumpOnce = false;
      requestAnimationFrame(() => scrollLogToEnd(pre));
    }
    const st = document.getElementById("dm-log-status");
    if (st) st.textContent = sess.follow ? "following" : "paused";
  } catch (e) {
    if (logFollow !== sess) return;
    if (pre && (!pre.textContent || pre.textContent === "Loading…")) {
      pre.textContent = e && e.message ? e.message : "unavailable";
    }
  } finally {
    if (logFollow === sess) sess.inflight = false;
  }
}

async function openPodLogs(ns, pod) {
  if (!envId || !pod) return;
  const namespace = ns || "default";
  const body = openToolModal(
    `Logs — ${namespace}/${pod}`,
    `<div class="dm-log-toolbar">
      <label class="muted"><input type="checkbox" id="dm-log-follow" checked> Follow</label>
      <span id="dm-log-status" class="muted">following</span>
      <button type="button" class="secondary btn-sm" id="dm-log-latest">Latest</button>
    </div>
    <pre class="dm-log dm-tool-pre" id="dm-log-pre">Loading…</pre>`
  );
  if (body) body.classList.add("dm-log-follow");
  const pre = document.getElementById("dm-log-pre");
  const box = document.getElementById("dm-log-follow");
  const sess = {
    ns: namespace,
    pod,
    pre,
    follow: true,
    jumpOnce: true,
    inflight: false,
    timer: null,
  };
  logFollow = sess;
  if (box) {
    box.addEventListener("change", () => {
      if (logFollow !== sess) return;
      sess.follow = !!box.checked;
      if (sess.follow) {
        sess.jumpOnce = true;
        refreshPodLogs(sess);
        startLogFollowTimer(sess);
      } else {
        stopLogFollowTimer();
        const st = document.getElementById("dm-log-status");
        if (st) st.textContent = "paused";
      }
    });
  }
  const latest = document.getElementById("dm-log-latest");
  if (latest) {
    latest.addEventListener("click", () => {
      if (logFollow !== sess) return;
      sess.follow = true;
      sess.jumpOnce = true;
      if (box) box.checked = true;
      const st = document.getElementById("dm-log-status");
      if (st) st.textContent = "following";
      scrollLogToEnd(pre);
      startLogFollowTimer(sess);
    });
  }
  if (pre) {
    pre.addEventListener("scroll", () => {
      if (logFollow !== sess || !sess.follow || !box) return;
      if (!logNearBottom(pre)) {
        sess.follow = false;
        box.checked = false;
        stopLogFollowTimer();
        const st = document.getElementById("dm-log-status");
        if (st) st.textContent = "paused";
      }
    });
  }
  await refreshPodLogs(sess);
  if (logFollow === sess && sess.follow) startLogFollowTimer(sess);
}

async function deletePod(ns, pod, { restart } = {}) {
  if (!envId || !pod) return;
  const namespace = ns || "default";
  const node = lastGraph.byId.get(`pod:${namespace}/${pod}`);
  const owners = (node && node.controllers) || [];
  const msg = restart
    ? owners.length
      ? `Restart ${namespace}/${pod}? Kubernetes will recreate it (${owners.join(", ")}).`
      : `Delete ${namespace}/${pod}? This pod has no controller — it will not come back.`
    : `Delete pod ${namespace}/${pod}?`;
  if (!confirm(msg)) return;
  try {
    const d = await api(
      `/api/v1/environments/${encodeURIComponent(envId)}/k8s/pods/${encodeURIComponent(namespace)}/${encodeURIComponent(pod)}`,
      { method: "DELETE" }
    );
    if (d && d.ok === false) toast(d.error || "delete failed", "error");
    else {
      toast(restart ? "restart requested" : "pod deleted", "ok");
      workloadsAt = 0;
      fetchState();
    }
  } catch (e) {
    toast(e && e.message ? e.message : "delete failed", "error");
  }
}

function loadXterm() {
  if (window.Terminal && window.FitAddon) return Promise.resolve(true);
  const VENDOR = "/static/vendor/xterm";
  return (async () => {
    if (!document.querySelector("link[data-xterm-css]")) {
      const link = document.createElement("link");
      link.rel = "stylesheet";
      link.href = VENDOR + "/xterm.css";
      link.setAttribute("data-xterm-css", "");
      document.head.appendChild(link);
    }
    const loadScript = (src) =>
      new Promise((resolve, reject) => {
        const s = document.createElement("script");
        s.src = src;
        s.onload = resolve;
        s.onerror = reject;
        document.head.appendChild(s);
      });
    if (!window.Terminal) await loadScript(VENDOR + "/xterm.js");
    if (!window.FitAddon) await loadScript(VENDOR + "/xterm-addon-fit.js");
    return !!(window.Terminal && window.FitAddon && window.FitAddon.FitAddon);
  })().catch(() => false);
}

function closeExec() {
  if (!execSess) return;
  const sess = execSess;
  execSess = null;
  try {
    if (sess.ws && sess.ws.readyState <= WebSocket.OPEN) sess.ws.close(1000, "closed");
  } catch {
    /* gone */
  }
  if (sess.term) {
    try {
      sess.term.dispose();
    } catch {
      /* */
    }
  }
}

async function openPodExec(ns, pod, opts = {}) {
  if (!envId || !pod) return;
  if (!canRun()) {
    toast("Console requires operator role", "error");
    return;
  }
  const namespace = ns || "default";
  const node = lastGraph.byId.get(`pod:${namespace}/${pod}`);
  const containers = (node && node.containers) || [];
  let container = "";
  if (containers.length > 1) {
    const picked = window.prompt("Container to exec into", containers[0] || "");
    if (picked == null) return;
    container = String(picked).trim();
  } else if (containers.length === 1) {
    container = containers[0];
  }
  const title = `Console — ${namespace}/${pod}${container ? " · " + container : ""}`;
  const mount = opts.mount || null;
  const body = mount || openToolModal(title, `<div class="muted">Connecting…</div>`);
  if (mount) body.innerHTML = `<div class="muted">Connecting…</div>`;
  closeExec();
  const xtermOk = await loadXterm();
  let ticket;
  try {
    ({ ticket } = await api("/api/v1/auth/ticket", { method: "POST" }));
  } catch (e) {
    if (body) body.innerHTML = toolPre(e && e.message ? e.message : "auth failed");
    return;
  }
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const qs = new URLSearchParams({ ticket });
  if (container) qs.set("container", container);
  const url = `${proto}//${location.host}/api/v1/environments/${encodeURIComponent(
    envId
  )}/k8s/pods/${encodeURIComponent(namespace)}/${encodeURIComponent(pod)}/exec?${qs}`;
  const ws = new WebSocket(url);
  const sess = { ws, term: null, inTool: !mount };
  execSess = sess;
  ws.onopen = () => {
    if (execSess !== sess || !body) return;
    body.innerHTML = "";
    if (xtermOk) {
      const term = new window.Terminal({
        theme: { background: "#0c0c0c", foreground: "#e6e6e6", cursor: "#4ade80" },
        fontSize: 13,
        cursorBlink: true,
        scrollback: 2000,
      });
      const fit = new window.FitAddon.FitAddon();
      term.loadAddon(fit);
      term.open(body);
      try {
        fit.fit();
      } catch {
        /* */
      }
      sess.term = term;
      term.onData((data) => {
        if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "input", data }));
      });
      const sendResize = () => {
        const dims = fit.proposeDimensions();
        if (dims && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "resize", cols: dims.cols, rows: dims.rows }));
        }
      };
      sendResize();
      requestAnimationFrame(() => {
        try {
          fit.fit();
          sendResize();
        } catch {
          /* */
        }
      });
      term.focus();
    } else {
      body.innerHTML = `<pre class="dm-log dm-tool-pre">connected — type is limited without xterm</pre>`;
    }
  };
  ws.onmessage = (ev) => {
    if (execSess !== sess) return;
    let frame;
    try {
      frame = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (frame.type === "output" && sess.term) sess.term.write(frame.data || "");
    if (frame.type === "exit") toast("Container console ended", "info");
  };
  ws.onclose = () => {
    if (execSess === sess) execSess = null;
  };
  ws.onerror = () => toast("container console failed", "error");
}

async function continueFrom(stageId, untilStage) {
  if (!envId || !stageId) return;
  if (stageId === "testing") {
    await runValidation("tempest");
    return;
  }
  const spec = stageSpec(stageId);
  const only = untilStage && untilStage === stageId;
  const label = spec.name || stageId;
  const msg = only
    ? `Run only ${label}? Cluster stays up. This stage is the control point — nothing after it runs.`
    : `Continue OpenStack from ${label}?\n\nTalos stays up. Helm plays from this stage forward. Tempest is not included. This is not a metal wipe.`;
  if (!confirm(msg)) return;
  try {
    const params = { from_stage: stageId, skip_push: true, parallelism: 2 };
    if (only) params.until_stage = stageId;
    const created = await api(`/api/v1/environments/${encodeURIComponent(envId)}/jobs`, {
      method: "POST",
      body: JSON.stringify({
        operation: "genestack.deploy",
        params,
      }),
    });
    toast(
      `${only ? "Stage" : "Deploy"} ${String(created.id || "").slice(0, 8)}… ${only ? "only " : "from "}${label}`,
      "ok"
    );
    beginLive({ collapse: false });
    await fetchState();
    schedule();
  } catch (e) {
    toast(e && e.message ? e.message : "deploy failed to start", "error");
  }
}

function runStageOnly(stageId) {
  return continueFrom(stageId, stageId);
}

function openConsoleFromEl(el) {
  if (!el) return;
  const kind = el.getAttribute("data-kind") || "";
  if (kind === "vm") {
    const vid = el.getAttribute("data-id") || "";
    openNodeModal(lastGraph.byId.get(`vm:${vid}`) || { kind: "vm", vmId: vid, id: `vm:${vid}` }, { console: "vnc" });
    return;
  }
  if (kind === "pod") {
    const ns = el.getAttribute("data-ns") || "default";
    const pod = el.getAttribute("data-pod") || "";
    openNodeModal(lastGraph.byId.get(`pod:${ns}/${pod}`) || { kind: "pod", namespace: ns, name: pod }, { console: "exec" });
    return;
  }
  const name = el.getAttribute("data-name") || "";
  if (name) openHostModal(name);
}

function handleDetailClick(e) {
  const openUrl = e.target.closest("[data-dm-open-url]");
  if (openUrl) {
    const url = openUrl.getAttribute("data-dm-open-url");
    if (url) window.open(url, "_blank", "noopener");
    return true;
  }
  const cons = e.target.closest("[data-dm-open-console]");
  if (cons) {
    openConsoleFromEl(cons);
    return true;
  }
  const work = e.target.closest("[data-dm-work]");
  if (work) {
    openWorkModal();
    return true;
  }
  const stageBtn = e.target.closest("[data-dm-stage]");
  if (stageBtn) {
    openStageModal(stageBtn.getAttribute("data-dm-stage"));
    return true;
  }
  const until = e.target.closest("[data-dm-until]");
  if (until) {
    runStageOnly(until.getAttribute("data-dm-until"));
    return true;
  }
  const cont = e.target.closest("[data-dm-continue]");
  if (cont) {
    continueFrom(cont.getAttribute("data-dm-continue"));
    return true;
  }
  if (e.target.closest("[data-dm-reg-config]")) {
    openRegistryConfig();
    return true;
  }
  if (e.target.closest("[data-dm-warm]")) {
    warmImageCache();
    return true;
  }
  const validateBtn = e.target.closest("[data-dm-validate]");
  if (validateBtn) {
    runValidation(validateBtn.getAttribute("data-dm-validate") || "tempest");
    return true;
  }
  const ilo = e.target.closest("[data-dm-ilo]");
  if (ilo) {
    openIlo(ilo.getAttribute("data-dm-ilo"));
    return true;
  }
  const talos = e.target.closest("[data-dm-talos]");
  if (talos) {
    openTalos(talos.getAttribute("data-dm-talos"));
    return true;
  }
  const dmesg = e.target.closest("[data-dm-talos-dmesg]");
  if (dmesg) {
    openTalos(dmesg.getAttribute("data-name"));
    return true;
  }
  const svcs = e.target.closest("[data-dm-talos-services]");
  if (svcs) {
    openTalosServices(svcs.getAttribute("data-name"));
    return true;
  }
  const health = e.target.closest("[data-dm-talos-health]");
  if (health) {
    openTalosHealth(health.getAttribute("data-name"));
    return true;
  }
  const reboot = e.target.closest("[data-dm-talos-reboot]");
  if (reboot) {
    const n = reboot.getAttribute("data-name");
    talosMutate(n, "reboot", null, `Reboot Talos node ${n}? Workloads on this machine will restart.`);
    return true;
  }
  const shutdown = e.target.closest("[data-dm-talos-shutdown]");
  if (shutdown) {
    const n = shutdown.getAttribute("data-name");
    talosMutate(n, "shutdown", null, `Shut down Talos node ${n}? The machine will power off.`);
    return true;
  }
  const reset = e.target.closest("[data-dm-talos-reset]");
  if (reset) {
    const n = reset.getAttribute("data-name");
    talosMutate(
      n,
      "reset",
      { graceful: true, reboot: false, wipe: true },
      `Reset Talos node ${n}?\n\nGraceful etcd leave. Halt after reset. Wipe system disk.\nThis is destructive.`
    );
    return true;
  }
  const describe = e.target.closest("[data-dm-k8s-describe]");
  if (describe) {
    openDescribe(describe.getAttribute("data-kind") || "nodes", describe.getAttribute("data-ns") || "", describe.getAttribute("data-name"));
    return true;
  }
  const events = e.target.closest("[data-dm-k8s-events]");
  if (events) {
    openEvents(events.getAttribute("data-ns") || "");
    return true;
  }
  const cordon = e.target.closest("[data-dm-k8s-cordon]");
  if (cordon) {
    k8sNodeAct(cordon.getAttribute("data-name"), "cordon");
    return true;
  }
  const uncordon = e.target.closest("[data-dm-k8s-uncordon]");
  if (uncordon) {
    k8sNodeAct(uncordon.getAttribute("data-name"), "uncordon");
    return true;
  }
  const drain = e.target.closest("[data-dm-k8s-drain]");
  if (drain) {
    k8sNodeAct(drain.getAttribute("data-name"), "drain");
    return true;
  }
  const vmcon = e.target.closest("[data-dm-vm-console]");
  if (vmcon) {
    const vid = vmcon.getAttribute("data-id") || "";
    openNodeModal(lastGraph.byId.get(`vm:${vid}`) || { kind: "vm", vmId: vid, id: `vm:${vid}` }, { console: "vnc" });
    return true;
  }
  const logs = e.target.closest("[data-dm-pod-logs]");
  if (logs) {
    const ns = logs.getAttribute("data-ns") || "default";
    const pod = logs.getAttribute("data-pod") || "";
    openNodeModal(lastGraph.byId.get(`pod:${ns}/${pod}`) || { kind: "pod", namespace: ns, name: pod }, { console: "logs" });
    return true;
  }
  const pdesc = e.target.closest("[data-dm-pod-describe]");
  if (pdesc) {
    openDescribe("pods", pdesc.getAttribute("data-ns"), pdesc.getAttribute("data-pod"));
    return true;
  }
  const pexec = e.target.closest("[data-dm-pod-exec]");
  if (pexec) {
    const ns = pexec.getAttribute("data-ns") || "default";
    const pod = pexec.getAttribute("data-pod") || "";
    openNodeModal(lastGraph.byId.get(`pod:${ns}/${pod}`) || { kind: "pod", namespace: ns, name: pod }, { console: "exec" });
    return true;
  }
  const prestart = e.target.closest("[data-dm-pod-restart]");
  if (prestart) {
    deletePod(prestart.getAttribute("data-ns"), prestart.getAttribute("data-pod"), { restart: true });
    return true;
  }
  const pdel = e.target.closest("[data-dm-pod-delete]");
  if (pdel) {
    deletePod(pdel.getAttribute("data-ns"), pdel.getAttribute("data-pod"));
    return true;
  }
  return false;
}

function selectNode(id) {
  selected = id || selected;
  popDismissed = "";
  lastInsp = "";
  renderAll();
}

function positionNestConsole() {
  const modal = document.getElementById("dm-host-modal");
  const panel = modal && modal.querySelector(".dm-host-panel");
  const flow = document.getElementById("dm-flow");
  if (!modal || !panel || !flow || !modal.classList.contains("nest-stage")) return;
  const rect = flow.getBoundingClientRect();
  const size = Math.min(rect.width, rect.height) * 0.58;
  panel.style.width = `${size}px`;
  panel.style.height = `${size}px`;
  panel.style.left = `${rect.left + rect.width / 2 - size / 2}px`;
  panel.style.top = `${rect.top + rect.height / 2 - size / 2 + 6}px`;
}

function applyNestConsoleStage(id) {
  const modal = document.getElementById("dm-host-modal");
  const panel = modal && modal.querySelector(".dm-host-panel");
  if (id && nestOn && isHoneycombEnabled()) {
    stageHoneycomb(id);
    if (modal) modal.classList.add("nest-stage");
    positionNestConsole();
    return;
  }
  clearHoneycombStage();
  if (modal) modal.classList.remove("nest-stage");
  if (panel) {
    panel.style.width = "";
    panel.style.height = "";
    panel.style.left = "";
    panel.style.top = "";
  }
}

function honeycombAction(action, id) {
  const node = lastGraph.byId && lastGraph.byId.get(id);
  if (!node) return;
  if (action === "see") {
    selectNode(id);
    const insp = document.getElementById("dm-inspector");
    if (insp && insp.scrollIntoView) insp.scrollIntoView({ block: "nearest", behavior: "smooth" });
    return;
  }
  if (action === "open") {
    if (node.kind === "vm") {
      openNodeModal(node, { console: "vnc" });
      return;
    }
    if (node.kind === "pod") {
      openNodeModal(node, { console: "exec" });
      return;
    }
    if (node.kind === "ingress" || node.kind === "edge" || node.kind === "gw" || node.kind === "route") {
      const url = node.url || (node.hosts && node.hosts[0] && `https://${node.hosts[0]}`) || (node.address && `https://${node.address}`);
      if (url) window.open(url, "_blank", "noopener");
      return;
    }
    const name = node.machineName || String(node.id || "").replace(/^m:/, "");
    if (name) openHostModal(name);
    return;
  }
  if (action === "logs") {
    if (node.kind === "pod") {
      openNodeModal(node, { console: "logs" });
      return;
    }
    const name = node.machineName || String(node.id || "").replace(/^m:/, "");
    if (name) openTalos(name);
  }
}

function onFlowWheel(e) {
  if (space3dOn || nestOn) return;
  e.preventDefault();
  const flow = e.currentTarget;
  const rect = flow.getBoundingClientRect();
  const mx = e.clientX - rect.left;
  const my = e.clientY - rect.top;
  const next = Math.min(3.2, Math.max(0.08, view.k * (e.deltaY < 0 ? 1.08 : 0.92)));
  const k = next / view.k;
  view.x = mx - (mx - view.x) * k;
  view.y = my - (my - view.y) * k;
  view.k = next;
  view._userMoved = true;
  applyView();
}

function onFlowDown(e) {
  if (space3dOn || nestOn) return;
  if (
    e.target.closest("#dm-minimap") ||
    e.target.closest(".sf-controls") ||
    e.target.closest("#dm-focus-hud") ||
    e.target.closest("#dm-space3d") ||
    e.target.closest("#dm-honeycomb")
  )
    return;
  const onCard = e.target.closest(".sf-card") || e.target.closest(".dm-node-pop") || e.target.closest(".sf-panel");
  const panChord = e.button === 1 || e.altKey || e.shiftKey || e.ctrlKey;
  if (onCard && !panChord) return;
  e.preventDefault();
  dragging = true;
  dragStart = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y };
  e.currentTarget.classList.add("panning");
  if (e.currentTarget.setPointerCapture) e.currentTarget.setPointerCapture(e.pointerId);
}

function onFlowMove(e) {
  if (minimapDrag) return;
  if (!dragging) return;
  view.x = dragStart.vx + (e.clientX - dragStart.x);
  view.y = dragStart.vy + (e.clientY - dragStart.y);
  view._userMoved = true;
  applyView();
}

function onFlowUp(e) {
  dragging = false;
  minimapDrag = false;
  if (e.currentTarget) e.currentTarget.classList.remove("panning");
}

function onMinimapDown(e) {
  e.preventDefault();
  e.stopPropagation();
  dragging = false;
  minimapDrag = true;
  const { wx, wy } = minimapToWorld(e.clientX, e.clientY);
  panToWorld(wx, wy);
  if (e.currentTarget.setPointerCapture) e.currentTarget.setPointerCapture(e.pointerId);
}

function onMinimapMove(e) {
  if (!minimapDrag) return;
  e.preventDefault();
  const { wx, wy } = minimapToWorld(e.clientX, e.clientY);
  panToWorld(wx, wy);
}

function onMinimapUp(e) {
  minimapDrag = false;
}

function onMinimapWheel(e) {
  e.preventDefault();
  e.stopPropagation();
  const { wx, wy } = minimapToWorld(e.clientX, e.clientY);
  const next = Math.min(3.2, Math.max(0.08, view.k * (e.deltaY < 0 ? 1.12 : 0.89)));
  view.k = next;
  panToWorld(wx, wy);
}

function onFlowClick(e) {
  const tog = e.target.closest("[data-toggle]");
  if (tog) {
    e.preventDefault();
    e.stopPropagation();
    toggleCollapsed(tog.getAttribute("data-toggle") || "");
    lastInsp = "";
    renderAll();
    if (mapFocus) fitPlaced();
    return;
  }
  const min = e.target.closest("[data-min]");
  if (min) {
    e.preventDefault();
    e.stopPropagation();
    toggleMinimized(min.getAttribute("data-min") || "");
    lastInsp = "";
    renderAll();
    return;
  }
  const up = e.target.closest("[data-hammer-up]");
  if (up) {
    e.preventDefault();
    e.stopPropagation();
    hammerUp();
    return;
  }
  const ham = e.target.closest("[data-hammer]");
  if (ham) {
    e.preventDefault();
    e.stopPropagation();
    hammerDown(ham.getAttribute("data-hammer") || "");
    return;
  }
  const card = e.target.closest("[data-sf-id]");
  if (!card) return;
  const id = card.getAttribute("data-sf-id");
  if (id && minimized.has(id)) {
    toggleMinimized(id);
    lastInsp = "";
  }
  selectNode(id);
}

function onFlowDblClick(e) {
  if (Date.now() < ignoreFoldUntil) return;
  if (e.target.closest("button")) return;
  const card = e.target.closest("[data-sf-id]");
  if (!card) return;
  e.preventDefault();
  e.stopPropagation();
  const id = card.getAttribute("data-sf-id") || "";
  if (!id) return;
  hammerDown(id);
}

function onFlowKey(e) {
  if (e.key !== "Escape") return;
  if (space3dOn || nestOn) return;
  const host = document.getElementById("dm-host-modal");
  const tool = document.getElementById("dm-tool-modal");
  if ((host && !host.hidden) || (tool && !tool.hidden) || nodeModal) return;
  if (!mapFocus) return;
  e.preventDefault();
  hammerUp();
}

function chartWhere(row) {
  if (row && row.oci && row.registry) return `${row.registry} cache`;
  const raw = String((row && row.url) || "");
  try {
    const host = new URL(raw).host;
    if (host) return host;
  } catch {
    /* not a URL */
  }
  return raw;
}

let registryCaches = [];

function chartTable(rows) {
  if (!rows.length) return `<p class="muted">None.</p>`;
  return `<table><tbody>${rows
    .map(
      (c) =>
        `<tr><td><code>${esc(c.name || "")}</code></td><td class="muted">${esc(chartWhere(c))}</td></tr>`
    )
    .join("")}</tbody></table>`;
}

function registryPayload() {
  return (snap && snap.registry) || {};
}

function cacheSourceSentence(reg) {
  const source = (reg && reg.host_source) || "";
  const bind = (reg && reg.bind) || "";
  if (source === "registry") {
    return "Saved on this environment. Empty follows the PXE next-server, then this console.";
  }
  if (source === "pxe") return `Empty follows the PXE next-server, which is ${bind}.`;
  if (source === "console" && bind) return `Empty uses this console at ${bind}.`;
  return "Set the address machines use to reach this console.";
}

function configRowHtml(row, addr) {
  const name = String((row && row.name) || "");
  const remote = String((row && row.remote) || "");
  const port = Number((row && row.port) || 0);
  const on = !row || row.enabled !== false;
  const builtin = !!(row && row.builtin);
  const listen = addr && port ? `http://${addr}:${port}` : "";
  const remove = builtin
    ? ""
    : `<button type="button" class="secondary btn-sm" data-reg-config-remove>Remove</button>`;
  return `<tr data-builtin="${builtin ? "1" : "0"}">
    <td><input type="checkbox" data-reg-on ${on ? "checked" : ""} aria-label="Mirror ${esc(name || "registry")}" /></td>
    <td><input type="text" data-reg-name value="${esc(name)}" spellcheck="false" autocomplete="off" ${builtin ? "readonly" : ""} /></td>
    <td><input type="text" data-reg-remote value="${esc(remote)}" spellcheck="false" autocomplete="off" /></td>
    <td><input type="number" data-reg-port min="1" max="65535" value="${port || ""}" /></td>
    <td class="reg-listen">${esc(listen)}</td>
    <td>${remove}</td>
  </tr>`;
}

function readConfigRows() {
  const body = document.getElementById("reg-config-rows");
  if (!body) return [];
  return [...body.querySelectorAll("tr")]
    .map((tr) => {
      const nameEl = tr.querySelector("[data-reg-name]");
      const remoteEl = tr.querySelector("[data-reg-remote]");
      const portEl = tr.querySelector("[data-reg-port]");
      const onEl = tr.querySelector("[data-reg-on]");
      return {
        name: nameEl ? nameEl.value : "",
        remote: remoteEl ? remoteEl.value : "",
        port: Number(portEl ? portEl.value : 0),
        enabled: !!(onEl && onEl.checked),
        builtin: tr.getAttribute("data-builtin") === "1",
      };
    })
    .filter((row) => {
      const name = String(row.name || "").trim();
      const remote = String(row.remote || "").trim();
      return name || (remote && remote !== "https://");
    });
}

function paintConfigListens() {
  const hostEl = document.getElementById("reg-config-host");
  const reg = registryPayload();
  const addr = (hostEl && hostEl.value.trim()) || reg.bind || "";
  const body = document.getElementById("reg-config-rows");
  if (!body) return;
  body.querySelectorAll("tr").forEach((tr) => {
    const portEl = tr.querySelector("[data-reg-port]");
    const listen = tr.querySelector(".reg-listen");
    const port = Number(portEl ? portEl.value : 0);
    if (listen) listen.textContent = addr && port ? `http://${addr}:${port}` : "";
  });
}

function paintRegistryConfig(rows) {
  const reg = registryPayload();
  const host = document.getElementById("reg-config-host");
  const source = document.getElementById("reg-config-source");
  const body = document.getElementById("reg-config-rows");
  if (!body) return;
  if (host && rows == null && document.activeElement !== host) {
    host.value = reg.configured_host || "";
    host.placeholder = reg.bind || "";
  } else if (host && !host.placeholder) {
    host.placeholder = reg.bind || "";
  }
  if (source) source.textContent = cacheSourceSentence(reg);
  const list =
    rows ||
    (Array.isArray(reg.upstreams) && reg.upstreams.length ? reg.upstreams : reg.defaults || []);
  const addr = (host && host.value.trim()) || reg.bind || "";
  body.innerHTML = list.map((row) => configRowHtml(row, addr)).join("");
  const save = document.getElementById("reg-config-save");
  const start = document.getElementById("reg-config-start");
  const warm = document.getElementById("reg-config-warm");
  if (save) save.disabled = !canRun();
  if (start) start.disabled = !canAdmin();
  if (warm) warm.disabled = !canAdmin();
}

function ensureConfigModal() {
  let modal = document.getElementById("reg-config-modal");
  if (modal) return modal;
  modal = document.createElement("div");
  modal.id = "reg-config-modal";
  modal.className = "gsc-modal";
  modal.hidden = true;
  modal.innerHTML = `
    <div class="gsc-modal-card gsc-modal-wide" role="dialog" aria-modal="true" aria-labelledby="reg-config-title">
      <div class="toolbar">
        <h2 id="reg-config-title">Configure image cache</h2>
        <button type="button" class="secondary btn-sm" data-reg-config-close>Close</button>
      </div>
      <p class="muted">Machines on this environment pull container images from this console. A checked registry is mirrored here. This console fetches a miss from the upstream.</p>
      <label class="field">Address
        <input id="reg-config-host" type="text" autocomplete="off" spellcheck="false" placeholder="" />
      </label>
      <p id="reg-config-source" class="muted reg-config-note"></p>
      <div class="reg-config-scroll">
        <table class="reg-config-table">
          <thead>
            <tr><th>On</th><th>Registry</th><th>Upstream</th><th>Port</th><th>Listen</th><th></th></tr>
          </thead>
          <tbody id="reg-config-rows"></tbody>
        </table>
      </div>
      <div class="dm-insp-actions">
        <button type="button" class="secondary btn-sm" id="reg-config-add">Add a registry</button>
        <button type="button" class="secondary btn-sm" id="reg-config-reset">Standard registries</button>
      </div>
      <p id="reg-config-msg" class="muted"></p>
      <div class="dm-insp-actions">
        <button type="button" class="btn-sm" id="reg-config-save">Save</button>
        <button type="button" class="secondary btn-sm" id="reg-config-start">Start caches</button>
        <button type="button" class="secondary btn-sm" id="reg-config-warm">Cache images and charts</button>
      </div>
    </div>`;
  document.body.appendChild(modal);
  modal.addEventListener("click", onConfigClick);
  modal.addEventListener("input", () => paintConfigListens());
  return modal;
}

function closeRegistryConfig() {
  const modal = document.getElementById("reg-config-modal");
  if (modal) modal.hidden = true;
}

async function openRegistryConfig() {
  if (!canRun()) {
    toast("An operator configures the image cache", "warn");
    return;
  }
  const modal = ensureConfigModal();
  document.body.appendChild(modal);
  modal.hidden = false;
  const msg = document.getElementById("reg-config-msg");
  if (msg) msg.textContent = "";
  if (envId) {
    try {
      const reg = await api(`/api/v1/environments/${encodeURIComponent(envId)}/registry`, {
        timeout: 20000,
      });
      if (reg && typeof reg === "object") {
        snap = Object.assign({}, snap || {}, { registry: reg });
        cacheCardSig = "";
        renderCacheCard();
      }
    } catch (err) {
      if (msg) msg.textContent = (err && err.message) || "Registries unavailable.";
    }
  }
  paintRegistryConfig();
  const host = document.getElementById("reg-config-host");
  if (host) host.focus();
}

function nextRegistryPort(rows) {
  const used = new Set(rows.map((row) => Number(row.port) || 0));
  let port = 5010;
  while (used.has(port)) port += 1;
  return port;
}

async function saveRegistryConfig() {
  if (!envId || !canRun()) return;
  const msg = document.getElementById("reg-config-msg");
  const hostEl = document.getElementById("reg-config-host");
  const host = hostEl ? hostEl.value.trim() : "";
  const upstreams = readConfigRows().map((row) => ({
    name: String(row.name || "").trim(),
    remote: String(row.remote || "").trim(),
    port: Number(row.port),
    enabled: !!row.enabled,
  }));
  if (msg) msg.textContent = "Saving…";
  try {
    const saved = await api(`/api/v1/environments/${encodeURIComponent(envId)}/registry`, {
      method: "PUT",
      body: JSON.stringify({ host, upstreams }),
    });
    snap = Object.assign({}, snap || {}, { registry: saved });
    cacheCardSig = "";
    renderCacheCard();
    if (msg) {
      msg.textContent =
        "Saved. Start caches brings the proxies up. Cache images and charts also pulls what this cluster is running.";
    }
    toast("Image cache saved", "ok");
    renderAll();
  } catch (err) {
    const text = (err && err.message) || "Save failed";
    if (msg) msg.textContent = text;
    toast(text, "error");
  }
}

async function startImageCaches() {
  if (!envId || !canAdmin()) return;
  const msg = document.getElementById("reg-config-msg");
  try {
    const created = await api(`/api/v1/environments/${encodeURIComponent(envId)}/jobs`, {
      method: "POST",
      body: JSON.stringify({ operation: "registry.mirror", params: { start_only: true } }),
    });
    const line = `Starting caches ${String(created.id || "").slice(0, 8)}…`;
    if (msg) msg.textContent = line;
    toast(line, "ok");
    beginLive({ collapse: true });
    await fetchState();
    schedule();
  } catch (err) {
    const text = (err && err.message) || "caches failed to start";
    if (msg) msg.textContent = text;
    toast(text, "error");
  }
}

function onConfigClick(e) {
  if (e.target.id === "reg-config-modal" || e.target.closest("[data-reg-config-close]")) {
    closeRegistryConfig();
    return;
  }
  if (e.target.closest("#reg-config-save")) {
    saveRegistryConfig();
    return;
  }
  if (e.target.closest("#reg-config-start")) {
    startImageCaches();
    return;
  }
  if (e.target.closest("#reg-config-warm")) {
    warmImageCache();
    return;
  }
  if (e.target.closest("#reg-config-reset")) {
    const defaults = registryPayload().defaults || [];
    paintRegistryConfig(defaults);
    const msg = document.getElementById("reg-config-msg");
    if (msg) msg.textContent = "Standard registries are in the form. Save stores them.";
    return;
  }
  if (e.target.closest("#reg-config-add")) {
    const rows = readConfigRows();
    rows.push({ name: "", remote: "https://", port: nextRegistryPort(rows), enabled: true, builtin: false });
    paintRegistryConfig(rows);
    return;
  }
  const remove = e.target.closest("[data-reg-config-remove]");
  if (remove) {
    const tr = remove.closest("tr");
    if (tr) tr.remove();
    paintConfigListens();
  }
}

function openRegistryModal(name) {
  const modal = document.getElementById("reg-cache-modal");
  const title = document.getElementById("reg-cache-modal-title");
  const body = document.getElementById("reg-cache-modal-body");
  if (!modal || !body) return;
  const row = registryCaches.find((c) => String(c.registry || "") === name) || null;
  const repos = row && Array.isArray(row.repositories) ? row.repositories : [];
  if (title) title.textContent = name || "Registry";
  body.innerHTML = repos.length
    ? `<ul>${repos.map((repo) => `<li><code>${esc(repo)}</code></li>`).join("")}</ul>`
    : `<p class="muted">No images stored yet.</p>`;
  modal.hidden = false;
}

function closeRegistryModal() {
  const modal = document.getElementById("reg-cache-modal");
  if (modal) modal.hidden = true;
}

function renderCacheCard() {
  const regs = document.getElementById("reg-cache-regs");
  const meta = document.getElementById("reg-cache-meta");
  const list = document.getElementById("reg-cache-chart-list");
  if (!regs) return;
  const r = (snap && snap.registry) || {};
  const caches = Array.isArray(r.caches) ? r.caches : [];
  const charts = Array.isArray(r.charts) ? r.charts : [];
  const sig = JSON.stringify([
    r.bind,
    r.host_source,
    r.configured_host,
    r.ready_count,
    r.cache_count,
    r.image_count,
    r.error,
    r.last_mirror && r.last_mirror.id,
    r.last_mirror && r.last_mirror.status,
    caches,
    charts.map((c) => c && c.name),
  ]);
  if (sig === cacheCardSig) return;
  cacheCardSig = sig;
  registryCaches = caches;
  const last = r.last_mirror;
  const lastLine = last
    ? `last cache ${last.status || ""} ${String(last.id || "").slice(0, 8)}`
    : "not cached yet";
  const head = caches.length
    ? `${r.bind || ""} · ${Number(r.ready_count || 0)}/${Number(r.cache_count || caches.length)} up · ${Number(
        r.image_count || 0
      )} images · ${lastLine}`
    : r.error
      ? String(r.error)
      : "";
  if (meta) {
    if (!caches.length && !r.error && !head) meta.innerHTML = skeletonHtml(3);
    else meta.textContent = head;
  }
  const setup = document.getElementById("reg-cache-setup");
  if (setup) {
    const where =
      r.host_source === "registry"
        ? "saved on this environment"
        : r.host_source === "pxe"
          ? "PXE next-server"
          : r.host_source === "console"
            ? "this console"
            : "address not set";
    setup.textContent = r.bind
      ? `${r.bind} · ${where}. Configure sets the address and which registries are mirrored.`
      : "Configure sets the address machines pull from, and which registries are mirrored.";
  }
  if (!caches.length) {
    regs.innerHTML = r.error ? `<p class="muted">${esc(String(r.error))}</p>` : "";
  } else {
    regs.innerHTML = `<div class="reg-grid">${caches
      .map((c) => {
        const repos = Array.isArray(c.repositories) ? c.repositories : [];
        const name = String(c.registry || "");
        return `<button type="button" class="reg-card" data-reg-open="${esc(name)}">
          <strong><code>${esc(name)}</code></strong>
          <span class="pill ${c.running ? "ok" : "bad"}">${c.running ? "up" : "down"}</span>
          <span>${esc(String(c.images || repos.length || 0))} images</span>
          <span class="muted">${esc(c.endpoint || "")}</span>
        </button>`;
      })
      .join("")}</div>`;
  }
  if (!list) return;
  const onConsole = charts.filter((c) => c && c.oci).sort((a, b) => String(a.name).localeCompare(String(b.name)));
  const helm = charts.filter((c) => c && !c.oci).sort((a, b) => String(a.name).localeCompare(String(b.name)));
  if (!charts.length) {
    list.innerHTML = `<p class="muted">${caches.length ? "No charts listed." : ""}</p>`;
    return;
  }
  list.innerHTML = `<h3 class="lc-title">On this console</h3>${chartTable(onConsole)}<h3 class="lc-title">Helm repo</h3>${chartTable(helm)}`;
}

function refreshRegistry(id, opts) {
  if (!id) return Promise.resolve();
  const gen = ++registryGen;
  const cardOnly = !!(opts && opts.cardOnly);
  return api(`/api/v1/environments/${encodeURIComponent(id)}/registry`, { timeout: 12000 })
    .then((regRes) => {
      if (gen !== registryGen || envId !== id || !regRes || typeof regRes !== "object") return;
      snap = Object.assign({}, snap || {}, { registry: regRes });
      if (!envName && regRes.environment_name) envName = regRes.environment_name;
      if (cardOnly) renderCacheCard();
      else renderAll();
    })
    .catch(() => {
      if (gen !== registryGen || envId !== id) return;
      const meta = document.getElementById("reg-cache-meta");
      if (meta && cardOnly) meta.textContent = "Registries unavailable.";
    });
}

export function imageCacheHtml() {
  return `
  <div class="card reg-cache" id="reg-cache-card">
    <div class="toolbar">
      <h2>Image cache</h2>
      <button type="button" class="btn-sm" id="reg-cache-config" ${gate(canRun(), "operator")}>Configure</button>
      <button type="button" class="secondary btn-sm" id="reg-cache-warm" ${gate(canAdmin(), "admin")}>Cache images and charts</button>
    </div>
    <p class="muted">Registries, container images, and Helm charts on this console. Nodes pull from here.</p>
    <p id="reg-cache-setup" class="muted">Configure sets the address machines pull from, and which registries are mirrored.</p>
    <div id="reg-cache-meta">${skeletonHtml(3)}</div>
    <div id="reg-cache-regs"></div>
    <div id="reg-cache-charts">
      <h3 class="lc-title">Helm charts</h3>
      <p class="muted">OCI charts sit in the registry cache. The others are Helm repos.</p>
      <div id="reg-cache-chart-list"></div>
    </div>
    <div id="reg-cache-modal" class="gsc-modal" hidden>
      <div class="gsc-modal-card gsc-modal-wide" role="dialog" aria-modal="true" aria-labelledby="reg-cache-modal-title">
        <div class="toolbar">
          <h2 id="reg-cache-modal-title">Registry</h2>
          <button type="button" class="secondary btn-sm" data-reg-close>Close</button>
        </div>
        <div id="reg-cache-modal-body"></div>
      </div>
    </div>
  </div>`;
}

export function loadImageCache(id) {
  const next = id || "";
  if (!next) return Promise.resolve();
  if (!envId) envId = next;
  return refreshRegistry(next, { cardOnly: true });
}

export function deployMapHtml() {
  return `
  <div id="dm-card" class="sf-shell">
    <div class="sf-chrome">
      <div class="sf-next-tools">
        <button type="button" class="secondary btn-sm" id="dm-maint" aria-expanded="false" aria-controls="dm-maint-menu" title="Repair, restack, image cache, Tempest, and metal wipe">Maintenance</button>
        <div id="dm-maint-menu" class="sf-maint-menu" hidden>
          <button type="button" class="secondary btn-sm" id="dm-repair">Repair cluster</button>
          <button type="button" class="btn-sm" id="dm-restack" title="Helm from OpenStack core. Does not upgrade kube-ovn.">Restack OpenStack</button>
          <button type="button" class="secondary btn-sm" id="dm-warm">Cache images and charts</button>
          <button type="button" class="secondary btn-sm" id="dm-validate" title="Tempest is its own job">Run Tempest</button>
          <button type="button" class="danger btn-sm" id="dm-greenfield" title="PXE every host. Destroys the cluster.">Greenfield metal wipe</button>
        </div>
      </div>
      <div id="dm-next" class="sf-next dm-next"></div>
    </div>
    <div id="dm-pipe" class="dm-pipe" hidden></div>
    <div id="dm-flow" class="sf-flow" tabindex="0" aria-label="Environment flow">
      <div id="dm-world" class="sf-world">
        <svg id="dm-edges" class="sf-edges" aria-hidden="true"></svg>
        <div id="dm-nodes" class="sf-nodes"></div>
      </div>
      <div class="sf-controls" id="dm-controls">
        <button type="button" data-zoom="in" title="Zoom in">+</button>
        <button type="button" data-zoom="out" title="Zoom out">−</button>
        <button type="button" id="dm-fit" title="Center on start">⊡</button>
        <button type="button" id="dm-hammer" title="Hammer down the selected node">↧</button>
        <button type="button" id="dm-expand" title="Expand all nodes">⊞</button>
        <button type="button" id="dm-collapse" title="Collapse branches">⊟</button>
        <button type="button" id="dm-refresh" title="Refresh">↻</button>
        <button type="button" id="dm-space3d-toggle" title="3D space — layers, live links">3D</button>
        <button type="button" id="dm-nest-toggle" title="Honeycomb nest — drill one cell at a time">nest</button>
      </div>
      <div id="dm-focus-hud" class="sf-focus-hud" hidden></div>
      <canvas id="dm-space3d" class="sf-space3d" hidden></canvas>
      <canvas id="dm-honeycomb" class="sf-honeycomb" hidden></canvas>
      <canvas id="dm-minimap" class="sf-minimap" width="160" height="110" title="Drag to pan · scroll to zoom"></canvas>
    </div>
    <aside class="sf-side">
      <div class="sf-side-head"><span>Detail</span><span id="dm-meta" class="muted"></span></div>
      <div id="dm-inspector" class="dm-inspector"></div>
      <div class="dm-feed-wrap">
        <div class="dm-feed-label">Live events</div>
        <div id="dm-feed" class="dm-feed" aria-live="polite"><div class="dm-feed-row api dm-feed-wait">waiting for DHCP, iLO, jobs, and API…</div></div>
      </div>
    </aside>
  </div>`;
}

export function wireDeployMap() {
  watchShellSize();
  const flow = document.getElementById("dm-flow");
  if (flow && !wiredFlow) {
    wiredFlow = true;
    flow.addEventListener("wheel", onFlowWheel, { passive: false });
    flow.addEventListener("pointerdown", onFlowDown);
    flow.addEventListener("pointermove", onFlowMove);
    flow.addEventListener("pointerup", onFlowUp);
    flow.addEventListener("pointerleave", onFlowUp);
    flow.addEventListener("click", onFlowClick);
    flow.addEventListener("dblclick", onFlowDblClick);
    flow.addEventListener("keydown", onFlowKey);
  }
  const mini = document.getElementById("dm-minimap");
  if (mini && !mini.dataset.wired) {
    mini.dataset.wired = "1";
    mini.addEventListener("pointerdown", onMinimapDown);
    mini.addEventListener("pointermove", onMinimapMove);
    mini.addEventListener("pointerup", onMinimapUp);
    mini.addEventListener("pointerleave", onMinimapUp);
    mini.addEventListener("wheel", onMinimapWheel, { passive: false });
  }
  const controls = document.getElementById("dm-controls");
  if (controls && !controls.dataset.wired) {
    controls.dataset.wired = "1";
    controls.addEventListener("click", (e) => {
      const z = e.target.closest("[data-zoom]");
      if (z) {
        const dir = z.getAttribute("data-zoom");
        const flowEl = document.getElementById("dm-flow");
        const cx = flowEl ? flowEl.clientWidth / 2 : 0;
        const cy = flowEl ? flowEl.clientHeight / 2 : 0;
        if (space3dOn) {
          zoomSpace3d(dir);
          return;
        }
        if (nestOn) return;
        const next = Math.min(3.2, Math.max(0.08, view.k * (dir === "in" ? 1.15 : 0.87)));
        const k = next / view.k;
        view.x = cx - (cx - view.x) * k;
        view.y = cy - (cy - view.y) * k;
        view.k = next;
        view._userMoved = true;
        applyView();
      }
    });
  }
  const fit = document.getElementById("dm-fit");
  if (fit && !fit.dataset.wired) {
    fit.dataset.wired = "1";
    fit.addEventListener("click", () => {
      if (space3dOn) {
        fitSpace3d();
        return;
      }
      if (nestOn) {
        honeycombReset();
        return;
      }
      view._userMoved = false;
      fitView();
    });
  }
  const refresh = document.getElementById("dm-refresh");
  if (refresh && !refresh.dataset.wired) {
    refresh.dataset.wired = "1";
    refresh.addEventListener("click", () => tick());
  }
  const hammerBtn = document.getElementById("dm-hammer");
  if (hammerBtn && !hammerBtn.dataset.wired) {
    hammerBtn.dataset.wired = "1";
    hammerBtn.addEventListener("click", () => {
      if (nestOn || space3dOn) return;
      hammerDown(mapFocus === selected ? "" : selected);
    });
  }
  const expand = document.getElementById("dm-expand");
  if (expand && !expand.dataset.wired) {
    expand.dataset.wired = "1";
    expand.addEventListener("click", () => expandAll());
  }
  const collapseBtn = document.getElementById("dm-collapse");
  if (collapseBtn && !collapseBtn.dataset.wired) {
    collapseBtn.dataset.wired = "1";
    collapseBtn.addEventListener("click", () => collapseAll());
  }
  const spaceBtn = document.getElementById("dm-space3d-toggle");
  const flowEl = document.getElementById("dm-flow");
  if (flowEl) {
    mountSpace3d(flowEl, {
      getGraph: () => lastGraph,
      onSelect: (id) => selectNode(id),
    });
    mountHoneycomb(flowEl, {
      getGraph: () => lastGraph,
      onSelect: (id) => selectNode(id),
      onAction: (action, id) => honeycombAction(action, id),
    });
  }
  if (spaceBtn && !spaceBtn.dataset.wired) {
    spaceBtn.dataset.wired = "1";
    spaceBtn.addEventListener("click", () => {
      applySpace3dView(!isSpace3dEnabled());
    });
  }
  const nestBtn = document.getElementById("dm-nest-toggle");
  if (nestBtn && !nestBtn.dataset.wired) {
    nestBtn.dataset.wired = "1";
    nestBtn.addEventListener("click", () => {
      applyNestView(!isHoneycombEnabled());
    });
  }
  const repair = document.getElementById("dm-repair");
  if (repair && !repair.dataset.wired) {
    repair.dataset.wired = "1";
    repair.addEventListener("click", () => repairCluster());
  }
  const restack = document.getElementById("dm-restack");
  if (restack && !restack.dataset.wired) {
    restack.dataset.wired = "1";
    restack.addEventListener("click", () => restackOpenstack());
  }
  const greenfield = document.getElementById("dm-greenfield");
  if (greenfield && !greenfield.dataset.wired) {
    greenfield.dataset.wired = "1";
    greenfield.addEventListener("click", () => greenfieldRedeploy());
  }
  const warm = document.getElementById("dm-warm");
  if (warm && !warm.dataset.wired) {
    warm.dataset.wired = "1";
    warm.addEventListener("click", () => warmImageCache());
  }
  const cacheWarm = document.getElementById("reg-cache-warm");
  if (cacheWarm && !cacheWarm.dataset.wired) {
    cacheWarm.dataset.wired = "1";
    cacheWarm.addEventListener("click", () => warmImageCache());
  }
  const cacheConfig = document.getElementById("reg-cache-config");
  if (cacheConfig && !cacheConfig.dataset.wired) {
    cacheConfig.dataset.wired = "1";
    cacheConfig.addEventListener("click", () => openRegistryConfig());
  }
  const cacheCard = document.getElementById("reg-cache-card");
  if (cacheCard && !cacheCard.dataset.regWired) {
    cacheCard.dataset.regWired = "1";
    cacheCard.addEventListener("click", (e) => {
      if (e.target.closest("[data-reg-close]") || e.target.id === "reg-cache-modal") {
        closeRegistryModal();
        return;
      }
      const open = e.target.closest("[data-reg-open]");
      if (open) openRegistryModal(open.dataset.regOpen || "");
    });
  }
  if (!window.__regCacheEsc) {
    window.__regCacheEsc = true;
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      const config = document.getElementById("reg-config-modal");
      if (config && !config.hidden) {
        closeRegistryConfig();
        return;
      }
      const modal = document.getElementById("reg-cache-modal");
      if (!modal || modal.hidden) return;
      closeRegistryModal();
    });
  }
  const validate = document.getElementById("dm-validate");
  if (validate && !validate.dataset.wired) {
    validate.dataset.wired = "1";
    validate.addEventListener("click", () => runValidation("tempest"));
  }
  const maint = document.getElementById("dm-maint");
  const maintMenu = document.getElementById("dm-maint-menu");
  if (maint && maintMenu && !maint.dataset.wired) {
    maint.dataset.wired = "1";
    const closeMaint = () => {
      maintMenu.hidden = true;
      maint.setAttribute("aria-expanded", "false");
    };
    maint.addEventListener("click", (e) => {
      e.stopPropagation();
      const open = maintMenu.hidden;
      maintMenu.hidden = !open;
      maint.setAttribute("aria-expanded", open ? "true" : "false");
    });
    maintMenu.addEventListener("click", (e) => {
      if (e.target.closest("button")) closeMaint();
    });
    if (!window.__dmMaintDoc) {
      window.__dmMaintDoc = true;
      document.addEventListener("click", (e) => {
        const menu = document.getElementById("dm-maint-menu");
        const btn = document.getElementById("dm-maint");
        if (!menu || menu.hidden) return;
        if (e.target.closest("#dm-maint-menu")) return;
        menu.hidden = true;
        if (btn) btn.setAttribute("aria-expanded", "false");
      });
    }
  }
  const card = document.getElementById("dm-card");
  if (card && !card.dataset.inspWired) {
    card.dataset.inspWired = "1";
    card.addEventListener("click", (e) => {
      handleDetailClick(e);
    });
  }
  if (!window.__dmJobHook) {
    window.__dmJobHook = true;
    window.addEventListener("deploy-job-started", () => tick());
  }
}

export async function loadDeployMap(id) {
  const nextId = id || "";
  const same = nextId && nextId === envId;
  envId = nextId;
  bindLiveEnv(nextId);
  popDismissed = "";
  closeHostModal();
  if (timer) clearTimeout(timer);
  if (stream) {
    stream.close();
    stream = null;
  }
  lastInsp = "";
  lastNext = "";
  lastIds = "";
  resetFeed();
  liveMetalHosts = new Map();
  pendingPulse = "";
  lastMetalKey = "";
  lastMetalAt = 0;
  if (!same) {
    selected = "infra";
    mapFocus = "";
    restoreFold();
    view = { x: 48, y: 36, k: 0.85 };
    workloads = null;
    workloadsError = "";
    cacheCardSig = "";
    registryGen += 1;
    workloadsAt = 0;
    liveMetrics = null;
    shownLive = null;
    liveHist = { cluster: [], nodes: {}, pods: {} };
    metricsMissing = false;
    platform = null;
    inventoryServers = [];
  }
  if (metricsTimer) clearTimeout(metricsTimer);
  metricsTimer = null;
  stopLiveSmooth();
  if (!envId) return;
  if (!same) applyCache(readCache(envId));
  rememberEnvName();
  if (snap || platform || inventoryServers.length) renderAll();
  else renderEnvHealth();
  const topics = ["jobs", "activity"];
  topics.push(`env:${envId}`);
  const onMetal = (payload) => {
    if (!payload) return;
    if (!forThisEnv(payload)) return;
    const msg = String(payload.message || "").trim();
    if (!msg) return;
    const kind = String(payload.kind || classifyLiveLine(msg) || "net");
    const key = `${kind}|${payload.host || ""}|${msg}`;
    const now = Date.now();
    if (key === lastMetalKey && now - lastMetalAt < 400) return;
    lastMetalKey = key;
    lastMetalAt = now;
    pushFeed(kind, msg, payload.host);
    flushLiveUi();
  };
  const onJob = (payload) => {
    if (!payload) return;
    if (payload.type === "metal") {
      onMetal(payload);
      return;
    }
    if (!forThisEnv(payload)) return;
    if (payload.id == null) return;
    if (payload.type === "job_log") {
      job = {
        ...(job || {}),
        id: payload.id,
        status: payload.status || (job && job.status),
        error: payload.error,
        log_text: mergeJobLog(job && job.log_text, payload.log_tail),
        operation: payload.operation,
      };
      if (payload.current) {
        if (!pipe) pipe = {};
        pipe.current = { ...(pipe.current || {}), ...payload.current };
        pipe.running = ACTIVE.has(String(payload.status || pipe.running || ""));
      }
      if (pipe) {
        pipe.running = ACTIVE.has(String(payload.status || ""));
        if (pipe.running) {
          pipe.failed_at = null;
          pipe.can_continue = false;
        }
      }
      lastFetch = Date.now();
      ingestJobLines(payload.log_tail || "");
      if (isLive()) autoReveal = true;
      flushLiveUi();
      if (isLive() && Date.now() - lastLiveRefresh > 2500) {
        lastLiveRefresh = Date.now();
        refreshPods(envId);
        api(`/api/v1/environments/${encodeURIComponent(envId)}/vms`, { timeout: 12000 })
          .then((res) => {
            if (res && Array.isArray(res.vms) && (res.vms.length || res.source === "live")) osVms = res.vms;
            renderAll();
          })
          .catch(() => {});
        renderAll();
      }
      return;
    }
    if (payload.type === "job") {
      pushFeed("job", `${payload.operation || "job"} → ${payload.status || ""}`);
      if (
        /genestack\.(deploy|greenfield|tempest|verify)|registry\.mirror/.test(String(payload.operation || "")) &&
        ACTIVE.has(String(payload.status || ""))
      ) {
        beginLive({ collapse: false });
      }
      flushLiveUi();
    }
    tick();
  };
  const onActivity = (payload) => {
    if (payload && payload.type === "metal") {
      onMetal(payload);
      return;
    }
    if (!payload || payload.type !== "api") return;
    if (!forThisEnv(payload)) return;
    pushFeed("api", prettyApiLine(payload));
    const method = String(payload.method || "");
    const path = String(payload.path || "");
    if (method !== "GET" && /\/(jobs|deploy|greenfield|k8s|platform|baremetal)\b/.test(path)) {
      if (/\/(jobs|deploy|greenfield)\b/.test(path) && payload.status >= 200 && payload.status < 300) {
        beginLive({ collapse: false });
      }
      tick();
    }
  };
  const handlers = { jobs: onJob, activity: onActivity };
  handlers[`env:${envId}`] = (payload) => {
    if (!payload) return;
    if (payload.type === "metal") onMetal(payload);
    else if (payload.type === "api") onActivity(payload);
    else onJob(payload);
  };
  stream = connect(topics, handlers);
  refreshLiveMetrics();
  scheduleMetrics();
  await tick();
}

export function destroyDeployMap() {
  closeToolModal();
  if (resizeObs) {
    resizeObs.disconnect();
    resizeObs = null;
  }
  if (timer) clearTimeout(timer);
  timer = null;
  if (metricsTimer) clearTimeout(metricsTimer);
  metricsTimer = null;
  stopLiveSmooth();
  liveMetrics = null;
  shownLive = null;
  metricsMissing = false;
  if (stream) stream.close();
  stream = null;
  applySpace3dView(false);
  applyNestView(false);
  destroySpace3d();
  destroyHoneycomb();
  space3dOn = false;
  nestOn = false;
  wiredFlow = false;
  mapFocus = "";
  envId = "";
  job = null;
  pipe = null;
  snap = null;
  cacheCardSig = "";
  registryGen += 1;
  platform = null;
  inventoryServers = [];
  workloads = null;
  osVms = [];
  osCloud = null;
  k8sIngress = [];
  k8sServices = [];
  k8sGateways = [];
  k8sRoutes = [];
  k8sPools = [];
  bmLive = [];
  liveMetalHosts = new Map();
  pendingPulse = "";
  lastIds = "";
  lastInsp = "";
  lastNext = "";
  resetFeed();
  lastGraph = { nodes: [], edges: [], byId: new Map(), podsByHost: new Map() };
}
