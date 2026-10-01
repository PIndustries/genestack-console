// pages/environment_cluster.js — "Kubernetes" card on the environment detail
// page. Shows live cluster state from GET /api/v1/environments/{id}/cluster:
// nodes, pod rollup + problem pods, warnings, helm releases, and access.
// Workloads/pods/node manage lives in a sibling section (GET k8s/workloads)
// so a failed manage probe never blanks the rest of the card.
// Defensive: the endpoint may 404, return partial payloads, or omit the
// optional health/warnings/access keys — every path degrades, never a page break.
import { api, downloadAuth, esc, fmtAge, toast } from "../api.js";
import { canRun, gate } from "../store.js";
import { connect } from "../stream.js";
import { applyLive, bindLiveEnv, live } from "./environment_live_state.js?v=ls5";

const PROBLEM_POD_LIMIT = 15;
const WARN_MSG_LIMIT = 96;
const POD_ROW_LIMIT = 80;
const REFRESH_MS = 20000;
const LIVE_REFRESH_MS = 8000;
const WORKLOADS_TIMEOUT = 25000;
const K8S_PANELS = [
  ["overview", "Overview"],
  ["workloads", "Workloads"],
  ["events", "Events"],
  ["networking", "Networking"],
  ["storage", "Storage"],
  ["config", "Config"],
  ["helm", "Helm"],
  ["nodes", "Nodes"],
];
const TAINT_EFFECTS = ["NoSchedule", "PreferNoSchedule", "NoExecute"];

let activeEnvId = ""; // guards against stale DOM after navigation/env switch
let envIdGetter = null;
let onProblemsClick = null;
let refreshTimer = null;
let inflight = null;
let inflightFor = "";
const downloadMissing = new Set(); // `${envId}:kubeconfig` / `:talosconfig` after a 404
let logsTarget = null; // { ns, pod }
let workloadsNs = "";
let workloadsPodFilter = "";
let workloadsCache = null;
let workloadsError = "";
let workloadsTimer = null;
let workloadsInflight = null;
let workloadsInflightKey = "";
let k8sPanel = "overview";
let clusterCache = null;
const panelCache = {};
const panelError = {};
const panelInflight = {};
const panelFetchedAt = {};
let helmDetail = null;
let jobsStream = null;
let helmDebounce = null;

function unavail(note) {
  return `<div class="muted">Unavailable${note ? " — " + esc(note) : ""}.</div>`;
}

function trunc(s, n = WARN_MSG_LIMIT) {
  const t = String(s ?? "");
  if (t.length <= n) return t;
  return t.slice(0, Math.max(0, n - 1)) + "…";
}

function hasKey(obj, key) {
  return obj && typeof obj === "object" && Object.prototype.hasOwnProperty.call(obj, key);
}

function reachPillHtml(reachable) {
  if (reachable === true) return '<span class="pill ok">reachable</span>';
  if (reachable === false) return '<span class="pill bad">unreachable</span>';
  return '<span class="pill">unknown</span>';
}

function healthPillHtml(health) {
  const h = String(health || "").toLowerCase();
  if (h === "healthy") return '<span class="pill ok">healthy</span>';
  if (h === "degraded") return '<span class="pill warn">degraded</span>';
  if (h === "down") return '<span class="pill bad">down</span>';
  return '<span class="pill">unknown</span>';
}

function nodeStatusKey(status) {
  return String(status || "").toLowerCase().replace(/\s+/g, "");
}

function nodeStatusPillHtml(status) {
  const s = nodeStatusKey(status);
  if (s === "ready") return '<span class="pill ok">Ready</span>';
  if (s === "notready") return '<span class="pill bad">NotReady</span>';
  return `<span class="pill warn">${esc(status || "unknown")}</span>`;
}

function memGiLabel(node) {
  if (node && node.mem_gi != null && node.mem_gi !== "") {
    const n = Number(node.mem_gi);
    if (!Number.isNaN(n)) return `${n} GiB`;
  }
  const raw = node && node.mem_capacity;
  if (raw == null || raw === "") return "";
  return String(raw);
}

function capacityHtml(node) {
  const cpu = node.cpu_capacity != null && node.cpu_capacity !== "" ? `${esc(node.cpu_capacity)} CPU` : "";
  const mem = memGiLabel(node);
  const parts = [cpu, mem ? esc(mem) : ""].filter(Boolean);
  return parts.length ? `<span class="muted">${parts.join(" · ")}</span>` : '<span class="muted">—</span>';
}

function nodeCordoned(info) {
  const n = info && typeof info === "object" ? info : {};
  const st = String(n.status || "");
  if (/schedulingdisabled/i.test(st)) return true;
  if (n.unschedulable === true || n.cordoned === true) return true;
  if (n.unschedulable === false || n.cordoned === false) return false;
  return null;
}

function k8sNodeActionsHtml(name, info) {
  if (!name) return "";
  const flagged = nodeCordoned(info) === true;
  const n = esc(name);
  const btns = [
    `<button type="button" class="secondary btn-sm" data-k8s-describe data-kind="nodes" data-name="${n}">Describe</button>`,
  ];
  if (!canRun()) return btns.join(" ");
  if (flagged) {
    btns.push(
      `<button type="button" class="secondary btn-sm" data-k8s-node-act="uncordon" data-k8s-name="${n}">Uncordon</button>`
    );
  } else {
    btns.push(
      `<button type="button" class="secondary btn-sm" data-k8s-node-act="cordon" data-k8s-name="${n}">Cordon</button>`
    );
    btns.push(
      `<button type="button" class="secondary btn-sm" data-k8s-node-act="uncordon" data-k8s-name="${n}">Uncordon</button>`
    );
  }
  btns.push(
    `<button type="button" class="secondary btn-sm" data-k8s-node-act="drain" data-k8s-name="${n}">Drain</button>`
  );
  return btns.join(" ");
}

function nodesHtml(nodes, resources) {
  if (!nodes.length) return '<div class="muted lc-section">No node data.</div>';
  const rows = nodes
    .map((n) => {
      const info = n && typeof n === "object" ? n : {};
      const roles = Array.isArray(info.roles) ? info.roles.join(", ") : info.roles || "—";
      const name = info.name || "";
      const acts = `<td class="os-actions">${k8sNodeActionsHtml(name, info)}</td>`;
      return `<tr>
        <td><code>${esc(info.name || "?")}</code></td>
        <td class="muted">${esc(roles)}</td>
        <td>${nodeStatusPillHtml(info.status)}</td>
        <td class="muted">${esc(info.version || "—")}</td>
        <td>${capacityHtml(info)}</td>
        ${acts}
      </tr>`;
    })
    .join("");
  let foot = "";
  if (resources && typeof resources === "object") {
    const cpu = resources.cpu != null ? `${esc(resources.cpu)} CPU` : "";
    const mem = resources.memory_gi != null ? `${esc(resources.memory_gi)} GiB` : "";
    const tot = [cpu, mem].filter(Boolean).join(" · ");
    if (tot) {
      foot = `<tfoot><tr><td colspan="4"><strong>All nodes</strong> <span class="muted">(control-plane + workers)</span></td><td>${esc(tot)}</td><td></td></tr></tfoot>`;
    }
  }
  return `<h3 class="lc-title">Nodes</h3>
    <table>
      <thead><tr><th>Name</th><th>Roles</th><th>Status</th><th>Version</th><th>Capacity</th><th></th></tr></thead>
      <tbody>${rows}</tbody>
      ${foot}
    </table>`;
}

// "pods: X running / Y total" — amber when problem pods exist.
function podsLineHtml(pods) {
  if (!pods || typeof pods !== "object") return "";
  const total = pods.total != null ? pods.total : "?";
  const running = pods.running != null ? pods.running : "?";
  const problems = Array.isArray(pods.problems) ? pods.problems : [];
  const cls = problems.length ? " warn" : "";
  const suffix = problems.length ? ` · ${problems.length} problem${problems.length === 1 ? "" : "s"}` : "";
  return `<div class="lc-pods${cls}">pods: ${esc(running)} running / ${esc(total)} total${esc(suffix)}</div>`;
}

function problemPodsHtml(pods) {
  const problems = pods && typeof pods === "object" && Array.isArray(pods.problems) ? pods.problems : [];
  if (!problems.length) return "";
  const shown = problems.slice(0, PROBLEM_POD_LIMIT);
  const rows = shown
    .map((p) => {
      const info = p && typeof p === "object" ? p : {};
      const reason = info.reason || info.status || "unknown";
      const ns = info.namespace || "default";
      const pod = info.name || "";
      const acts = [
        `<button type="button" class="secondary btn-sm" data-pod-logs data-ns="${esc(ns)}" data-pod="${esc(pod)}">Logs</button>`,
      ];
      acts.push(
        `<button type="button" class="secondary btn-sm" data-k8s-describe data-kind="pods" data-ns="${esc(ns)}" data-name="${esc(pod)}">Describe</button>`
      );
      if (canRun() && pod) {
        acts.push(
          `<button type="button" class="secondary btn-sm" data-pod-del data-ns="${esc(ns)}" data-pod="${esc(pod)}">Delete</button>`
        );
      }
      return `<tr>
        <td class="muted">${esc(info.namespace || "—")}</td>
        <td><code>${esc(info.name || "?")}</code></td>
        <td><span class="pill bad">${esc(reason)}</span></td>
        <td class="os-actions">${acts.join(" ")}</td>
      </tr>`;
    })
    .join("");
  const more =
    problems.length > shown.length
      ? `<div class="muted lc-more">+ ${problems.length - shown.length} more problem pods not shown</div>`
      : "";
  return `<h3 class="lc-title">Problem pods</h3>
    <table>
      <thead><tr><th>Namespace</th><th>Pod</th><th>Status / reason</th><th></th></tr></thead>
      <tbody>${rows}</tbody>
    </table>${more}`;
}

function seenHtml(v) {
  const age = fmtAge(v);
  if (age) return esc(age);
  if (v) return esc(String(v));
  return "—";
}

function warningsHtml(warnings) {
  if (!Array.isArray(warnings) || !warnings.length) return "";
  const rows = warnings
    .map((w) => {
      const info = w && typeof w === "object" ? w : {};
      const obj = [info.object, info.name].filter((x) => x != null && x !== "").join(" ") || "—";
      const msg = String(info.message || "");
      const count = info.count != null && info.count !== "" ? esc(info.count) : "—";
      return `<tr>
        <td class="muted">${esc(info.namespace || "—")}</td>
        <td><code>${esc(obj)}</code></td>
        <td class="lc-msg" title="${esc(msg)}">${esc(trunc(msg))}</td>
        <td class="muted">${count}</td>
        <td class="muted">${seenHtml(info.last_seen)}</td>
      </tr>`;
    })
    .join("");
  return `<h3 class="lc-title">Warnings</h3>
    <table>
      <thead><tr><th>Namespace</th><th>Object</th><th>Message</th><th>Count</th><th>Last seen</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function accessLineHtml(access) {
  if (!access || typeof access !== "object") return "";
  const parts = [];
  if (access.api_server) parts.push(`<code>${esc(access.api_server)}</code>`);
  if (access.gateway) parts.push(`gateway ${esc(access.gateway)}`);
  if (access.horizon) {
    parts.push(
      `<a href="${esc(access.horizon)}" target="_blank" rel="noopener noreferrer">horizon ↗</a>`
    );
  }
  if (!parts.length) return "";
  return `<div class="lc-access">Access: ${parts.join(" · ")}</div>`;
}

function releaseStatusPillHtml(status) {
  const s = String(status || "").toLowerCase();
  if (s === "deployed") return '<span class="pill ok">deployed</span>';
  if (s === "failed") return '<span class="pill bad">failed</span>';
  return `<span class="pill warn">${esc(status || "unknown")}</span>`;
}

function releasesHtml(releases) {
  if (!releases.length) return "";
  const sorted = releases.slice().sort((a, b) => {
    const as = String((a && a.status) || "").toLowerCase() === "deployed" ? 1 : 0;
    const bs = String((b && b.status) || "").toLowerCase() === "deployed" ? 1 : 0;
    return as - bs;
  });
  const rows = sorted
    .map((r) => {
      const info = r && typeof r === "object" ? r : {};
      return `<tr>
        <td><strong>${esc(info.name || "?")}</strong></td>
        <td class="muted">${esc(info.namespace || "—")}</td>
        <td>${releaseStatusPillHtml(info.status)}</td>
        <td><code>${esc(info.chart || "—")}</code></td>
        <td class="muted">${esc(info.version || "—")}</td>
      </tr>`;
    })
    .join("");
  return `<h3 class="lc-title">Helm releases</h3>
    <table>
      <thead><tr><th>Release</th><th>Namespace</th><th>Status</th><th>Chart</th><th>Version</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function clusterBodyHtml(d) {
  const data = d && typeof d === "object" ? d : {};
  const nodes = Array.isArray(data.nodes) ? data.nodes : null;
  const releases = Array.isArray(data.releases) ? data.releases : null;
  const health = String(data.health || "").toLowerCase();
  const degraded = health === "degraded";
  const problems = problemPodsHtml(data.pods);
  const warnings = hasKey(data, "warnings") ? warningsHtml(data.warnings) : "";
  const nodesBlock = nodes ? nodesHtml(nodes, data.resources) : "";
  const releasesBlock = releases ? releasesHtml(releases) : "";
  const access = hasKey(data, "access") ? accessLineHtml(data.access) : "";
  const podsLine = podsLineHtml(data.pods);
  const mid = degraded
    ? [problems, warnings, nodesBlock, releasesBlock]
    : [nodesBlock, problems, warnings, releasesBlock];
  const parts = [access, podsLine, ...mid];
  const errNote = data.error ? `<div class="hint muted">reported error: ${esc(data.error)}</div>` : "";
  return parts.join("") + errNote;
}

function asList(v) {
  return Array.isArray(v) ? v : [];
}

function kindPlural(kind) {
  const k = String(kind || "").toLowerCase();
  if (k === "deployment" || k === "deployments") return "deployments";
  if (k === "statefulset" || k === "statefulsets") return "statefulsets";
  if (k === "daemonset" || k === "daemonsets") return "daemonsets";
  return k;
}

function kindLabel(kind) {
  const k = kindPlural(kind);
  if (k === "deployments") return "Deployment";
  if (k === "statefulsets") return "StatefulSet";
  if (k === "daemonsets") return "DaemonSet";
  return kind || "—";
}

function readyLabel(info) {
  const ready = info.ready ?? info.ready_replicas ?? info.readyReplicas ?? "?";
  const replicas =
    info.replicas ?? info.desired ?? info.spec_replicas ?? info.desiredNumberScheduled ?? "?";
  return `${ready}/${replicas}`;
}

function currentReplicas(info) {
  const n = Number(info.replicas ?? info.desired ?? info.spec_replicas ?? info.ready ?? 0);
  return Number.isFinite(n) ? n : 0;
}

function imagesLabel(info) {
  let imgs = info.images;
  if (Array.isArray(imgs)) imgs = imgs.filter(Boolean).join(", ");
  else if (imgs && typeof imgs === "object") imgs = Object.values(imgs).filter(Boolean).join(", ");
  else if (imgs == null || imgs === "") imgs = info.image || "";
  return String(imgs || "—");
}

function phasePillHtml(phase) {
  const p = String(phase || "").toLowerCase();
  if (p === "running" || p === "succeeded") return `<span class="pill ok">${esc(phase)}</span>`;
  if (p === "failed" || p === "unknown" || p === "error") return `<span class="pill bad">${esc(phase)}</span>`;
  if (p === "pending") return `<span class="pill warn">${esc(phase)}</span>`;
  if (!phase) return '<span class="muted">—</span>';
  return `<span class="pill">${esc(phase)}</span>`;
}

function collectWorkloads(data) {
  const d = data && typeof data === "object" ? data : {};
  const out = [];
  const groups = [
    ["deployments", asList(d.deployments)],
    ["statefulsets", asList(d.statefulsets)],
    ["daemonsets", asList(d.daemonsets)],
  ];
  for (const [kind, rows] of groups) {
    for (const row of rows) {
      const info = row && typeof row === "object" ? row : {};
      out.push({ ...info, kind: kindPlural(info.kind || kind) });
    }
  }
  if (!out.length) {
    for (const row of asList(d.workloads)) {
      const info = row && typeof row === "object" ? row : {};
      out.push({ ...info, kind: kindPlural(info.kind) });
    }
  }
  out.sort((a, b) => {
    const ns = String(a.namespace || "").localeCompare(String(b.namespace || ""));
    if (ns) return ns;
    return String(a.name || "").localeCompare(String(b.name || ""));
  });
  return out;
}

function collectPods(data) {
  const d = data && typeof data === "object" ? data : {};
  let rows = asList(d.pods);
  if (!rows.length && d.pods && typeof d.pods === "object") {
    rows = asList(d.pods.items);
  }
  return rows.map((p) => (p && typeof p === "object" ? p : {})).filter((p) => p.name);
}

function matchesPodText(info) {
  if (!workloadsPodFilter) return true;
  const blob = `${info.namespace || ""} ${info.name || ""} ${info.node || ""} ${info.node_name || ""} ${info.phase || ""} ${info.status || ""}`.toLowerCase();
  return blob.includes(workloadsPodFilter.toLowerCase());
}

function workloadsTableHtml(rows) {
  if (!rows.length) return '<div class="muted">No workloads.</div>';
  const run = canRun();
  const body = rows
    .map((info) => {
      const kind = kindPlural(info.kind);
      const ns = info.namespace || "default";
      const name = info.name || "";
      const imgs = imagesLabel(info);
      const acts = [];
      if (run && name) {
        if (kind === "deployments" || kind === "statefulsets") {
          acts.push(
            `<button type="button" class="secondary btn-sm" data-wl-scale data-kind="${esc(kind)}" data-ns="${esc(ns)}" data-name="${esc(name)}" data-replicas="${esc(currentReplicas(info))}">Scale</button>`
          );
        }
        acts.push(
          `<button type="button" class="secondary btn-sm" data-wl-restart data-kind="${esc(kind)}" data-ns="${esc(ns)}" data-name="${esc(name)}">Restart</button>`
        );
        acts.push(
          `<button type="button" class="secondary btn-sm" data-k8s-describe data-kind="${esc(kind)}" data-ns="${esc(ns)}" data-name="${esc(name)}">Describe</button>`
        );
        acts.push(
          `<button type="button" class="secondary btn-sm" data-wl-del data-kind="${esc(kind)}" data-ns="${esc(ns)}" data-name="${esc(name)}" ${gate(true, "operator")}>Delete</button>`
        );
      } else if (name) {
        acts.push(
          `<button type="button" class="secondary btn-sm" data-k8s-describe data-kind="${esc(kind)}" data-ns="${esc(ns)}" data-name="${esc(name)}">Describe</button>`
        );
      }
      return `<tr>
        <td>${esc(kindLabel(kind))}</td>
        <td class="muted">${esc(ns)}</td>
        <td><code>${esc(name || "?")}</code></td>
        <td>${esc(readyLabel(info))}</td>
        <td class="muted kc-wl-images" title="${esc(imgs)}">${esc(trunc(imgs, 48))}</td>
        <td class="os-actions">${acts.join(" ")}</td>
      </tr>`;
    })
    .join("");
  return `<table class="os-table">
    <thead><tr><th>Kind</th><th>Namespace</th><th>Name</th><th>Ready</th><th>Images</th><th></th></tr></thead>
    <tbody>${body}</tbody>
  </table>`;
}

function podsTableHtml(rows) {
  const filtered = rows.filter(matchesPodText);
  const shown = filtered.slice(0, POD_ROW_LIMIT);
  if (!shown.length) return '<div class="muted">No pods.</div>';
  const run = canRun();
  const body = shown
    .map((info) => {
      const ns = info.namespace || "default";
      const name = info.name || "";
      const node = info.node || info.node_name || info.nodeName || "—";
      const phase = info.phase || info.status || "";
      const restarts = info.restarts ?? info.restart_count ?? info.restartCount ?? 0;
      const acts = [
        `<button type="button" class="secondary btn-sm" data-pod-logs data-ns="${esc(ns)}" data-pod="${esc(name)}">Logs</button>`,
      ];
      acts.push(
        `<button type="button" class="secondary btn-sm" data-k8s-describe data-kind="pods" data-ns="${esc(ns)}" data-name="${esc(name)}">Describe</button>`
      );
      if (run && name) {
        acts.push(
          `<button type="button" class="secondary btn-sm" data-pod-del data-ns="${esc(ns)}" data-pod="${esc(name)}">Delete</button>`
        );
      }
      return `<tr>
        <td class="muted">${esc(ns)}</td>
        <td><code>${esc(name || "?")}</code></td>
        <td class="muted">${esc(node)}</td>
        <td>${phasePillHtml(phase)}</td>
        <td class="muted">${esc(restarts)}</td>
        <td class="os-actions">${acts.join(" ")}</td>
      </tr>`;
    })
    .join("");
  const more =
    filtered.length > shown.length
      ? `<div class="muted lc-more">+ ${filtered.length - shown.length} more pods not shown</div>`
      : "";
  return `<table class="os-table">
    <thead><tr><th>Namespace</th><th>Name</th><th>Node</th><th>Phase</th><th>Restarts</th><th></th></tr></thead>
    <tbody>${body}</tbody>
  </table>${more}`;
}

function setK8sPanel(id) {
  const next = K8S_PANELS.some(([p]) => p === id) ? id : "overview";
  k8sPanel = next;
  const tabs = document.getElementById("kc-tabs");
  if (tabs) {
    tabs.querySelectorAll(".tab-btn").forEach((b) =>
      b.classList.toggle("active", b.dataset.kcPanel === k8sPanel)
    );
  }
  renderK8sPanel();
  loadK8sPanel();
  startClusterRefresh();
}

function nsQuery() {
  return workloadsNs ? `?namespace=${encodeURIComponent(workloadsNs)}` : "";
}

function k8sPath(suffix) {
  const envId = (envIdGetter && envIdGetter()) || activeEnvId;
  return `/api/v1/environments/${encodeURIComponent(envId)}${suffix}`;
}

function emptyNote(kind) {
  return `<div class="muted">No ${esc(kind)}.</div>`;
}

function panelUnavailable(err) {
  return `<div class="muted">Unavailable — ${esc(err || "no data")}</div>`;
}

function kvChips(obj, limit = 8) {
  if (!obj || typeof obj !== "object") return '<span class="muted">—</span>';
  const entries = Object.entries(obj);
  if (!entries.length) return '<span class="muted">—</span>';
  const shown = entries.slice(0, limit);
  const bits = shown.map(([k, v]) => {
    const val = v === "" || v == null ? k : `${k}=${v}`;
    return `<code class="kc-chip">${esc(val)}</code>`;
  });
  if (entries.length > shown.length) bits.push(`<span class="muted">+${entries.length - shown.length}</span>`);
  return bits.join(" ");
}

function taintChips(taints) {
  const rows = asList(taints);
  if (!rows.length) return '<span class="muted">—</span>';
  return rows
    .map((t) => {
      const info = t && typeof t === "object" ? t : {};
      const label = `${info.key || "?"}${info.value ? `=${info.value}` : ""}:${info.effect || ""}`;
      return `<code class="kc-chip">${esc(label)}</code>`;
    })
    .join(" ");
}

function renderWorkloads() {
  if (k8sPanel !== "workloads") return;
  const body = document.getElementById("kc-body");
  if (!body) return;
  if (!activeEnvId) {
    body.classList.add("muted");
    body.innerHTML = "Select an environment.";
    return;
  }
  if (workloadsError && !workloadsCache) {
    body.classList.add("muted");
    body.innerHTML = `Workloads unavailable — ${esc(workloadsError)}`;
    return;
  }
  const data = workloadsCache && typeof workloadsCache === "object" ? workloadsCache : {};
  const wl = collectWorkloads(data);
  const pods = collectPods(data);
  const jobs = asList(data.jobs);
  const err =
    workloadsError
      ? `<div class="muted">Workloads unavailable — ${esc(workloadsError)}</div>`
      : "";
  const jobRows = jobs
    .map((info) => {
      const ns = info.namespace || "default";
      const name = info.name || "";
      const done = `${info.succeeded ?? 0}/${info.completions ?? "?"}`;
      return `<tr>
        <td class="muted">${esc(ns)}</td>
        <td><code>${esc(name || "?")}</code></td>
        <td class="muted">${esc(done)}</td>
        <td class="muted">fail ${esc(info.failed ?? 0)} · active ${esc(info.active ?? 0)}</td>
        <td class="os-actions"><button type="button" class="secondary btn-sm" data-k8s-describe data-kind="jobs" data-ns="${esc(ns)}" data-name="${esc(name)}">Describe</button></td>
      </tr>`;
    })
    .join("");
  const jobsHtml = jobs.length
    ? `<h3 class="lc-title">Jobs</h3>
      <table class="os-table">
        <thead><tr><th>Namespace</th><th>Name</th><th>Completions</th><th></th><th></th></tr></thead>
        <tbody>${jobRows}</tbody>
      </table>`
    : "";
  body.classList.remove("muted");
  body.innerHTML =
    err +
    `<div class="kc-wl-toolbar">
      <input id="kc-wl-pod-q" type="search" placeholder="filter pods…" autocomplete="off" value="${esc(workloadsPodFilter)}">
      <span class="muted">${esc(wl.length)} workloads · ${esc(pods.length)} pods</span>
    </div>` +
    workloadsTableHtml(wl) +
    `<h3 class="lc-title">Pods</h3>` +
    podsTableHtml(pods) +
    jobsHtml;
}

function resetWorkloadsFilters() {
  workloadsNs = "";
  workloadsPodFilter = "";
  workloadsCache = null;
  workloadsError = "";
  const nsInput = document.getElementById("kc-wl-ns");
  if (nsInput) nsInput.value = "";
  const qInput = document.getElementById("kc-wl-pod-q");
  if (qInput) qInput.value = "";
}

async function loadWorkloads() {
  const envId = (envIdGetter && envIdGetter()) || activeEnvId;
  const msg = document.getElementById("kc-msg");
  if (!document.getElementById("kc-card")) return;
  if (!envId) {
    workloadsCache = null;
    workloadsError = "";
    if (msg) msg.textContent = "";
    renderWorkloads();
    return;
  }
  const ns = workloadsNs;
  const key = `${envId}::${ns}`;
  if (msg && !workloadsCache) msg.textContent = "Loading…";
  let pending;
  if (workloadsInflight && workloadsInflightKey === key) {
    pending = workloadsInflight;
  } else {
    workloadsInflightKey = key;
    const qs = ns ? `?namespace=${encodeURIComponent(ns)}` : "";
    pending = Promise.all([
      api(`/api/v1/environments/${encodeURIComponent(envId)}/k8s/workloads${qs}`, {
        timeout: WORKLOADS_TIMEOUT,
      }),
      api(`/api/v1/environments/${encodeURIComponent(envId)}/k8s/jobs${qs}`, {
        timeout: WORKLOADS_TIMEOUT,
      }).catch(() => ({ jobs: [] })),
    ]).then(([wl, jobs]) => {
      const data = wl && typeof wl === "object" ? { ...wl } : {};
      const jobPayload = jobs && typeof jobs === "object" ? jobs : {};
      data.jobs = asList(jobPayload.jobs);
      if (jobPayload.ok === false && !data.error) data.error = jobPayload.error;
      return data;
    });
    workloadsInflight = pending;
    pending.finally(() => {
      if (workloadsInflightKey === key) {
        workloadsInflight = null;
        workloadsInflightKey = "";
      }
    });
  }
  let d;
  try {
    d = await pending;
  } catch (e) {
    if (((envIdGetter && envIdGetter()) || activeEnvId) !== envId) return;
    if (workloadsNs !== ns) return;
    workloadsError = e && e.message ? e.message : "unavailable";
    if (msg) msg.textContent = "";
    renderWorkloads();
    return;
  }
  if (((envIdGetter && envIdGetter()) || activeEnvId) !== envId) return;
  if (workloadsNs !== ns) return;
  const data = d && typeof d === "object" ? d : {};
  if (data.ok === false) {
    workloadsError = data.error || data.message || "unavailable";
    if (msg) msg.textContent = "";
    renderWorkloads();
    return;
  }
  workloadsError = data.error ? String(data.error) : "";
  workloadsCache = data;
  if (msg) msg.textContent = "";
  renderWorkloads();
}

async function fetchK8s(suffix) {
  return api(k8sPath(suffix), { timeout: WORKLOADS_TIMEOUT });
}

async function loadK8sPanel() {
  const envId = (envIdGetter && envIdGetter()) || activeEnvId;
  if (!envId) {
    renderK8sPanel();
    return;
  }
  if (k8sPanel === "overview") {
    renderK8sPanel();
    return;
  }
  if (k8sPanel === "workloads") {
    await loadWorkloads();
    return;
  }
  const inflightKey = `${k8sPanel}::${envId}::${workloadsNs}`;
  if (panelInflight[inflightKey]) {
    try {
      await panelInflight[inflightKey];
    } catch {
      /* rendered below */
    }
    return;
  }
  const pending = (async () => {
    const qs = nsQuery();
    if (k8sPanel === "events") return fetchK8s(`/k8s/events${qs}`);
    if (k8sPanel === "networking") {
      const [svc, ing] = await Promise.all([
        fetchK8s(`/k8s/services${qs}`),
        fetchK8s(`/k8s/ingresses${qs}`),
      ]);
      return {
        ok: svc.ok !== false && ing.ok !== false,
        services: asList(svc.services),
        ingresses: asList(ing.ingresses),
        error: svc.error || ing.error || null,
      };
    }
    if (k8sPanel === "storage") return fetchK8s(`/k8s/persistentvolumeclaims${qs}`);
    if (k8sPanel === "config") {
      const [nss, cms, secrets] = await Promise.all([
        fetchK8s("/k8s/namespaces"),
        fetchK8s(`/k8s/configmaps${qs}`),
        fetchK8s(`/k8s/secrets${qs}`),
      ]);
      return {
        ok: nss.ok !== false && cms.ok !== false && secrets.ok !== false,
        namespaces: asList(nss.namespaces),
        configmaps: asList(cms.configmaps),
        secrets: asList(secrets.secrets),
        error: nss.error || cms.error || secrets.error || null,
      };
    }
    if (k8sPanel === "helm") return fetchK8s("/k8s/helm");
    if (k8sPanel === "nodes") return fetchK8s("/k8s/nodes");
    return {};
  })();
  panelInflight[inflightKey] = pending;
  let data;
  try {
    data = await pending;
  } catch (e) {
    if (((envIdGetter && envIdGetter()) || activeEnvId) !== envId) return;
    panelError[k8sPanel] = e && e.message ? e.message : "unavailable";
    delete panelCache[k8sPanel];
    renderK8sPanel();
    return;
  } finally {
    if (panelInflight[inflightKey] === pending) delete panelInflight[inflightKey];
  }
  if (((envIdGetter && envIdGetter()) || activeEnvId) !== envId) return;
  const payload = data && typeof data === "object" ? data : {};
  if (payload.ok === false) {
    panelError[k8sPanel] = payload.error || payload.message || "unavailable";
    panelCache[k8sPanel] = payload;
  } else {
    panelError[k8sPanel] = payload.error ? String(payload.error) : "";
    panelCache[k8sPanel] = payload;
    panelFetchedAt[k8sPanel] = Date.now();
  }
  const msg = document.getElementById("kc-msg");
  if (msg && k8sPanel !== "overview") {
    const ts = panelFetchedAt[k8sPanel];
    msg.textContent = ts ? `live ${new Date(ts).toLocaleTimeString()}` : "";
  }
  renderK8sPanel();
}

function renderK8sPanel() {
  const body = document.getElementById("kc-body");
  if (!body) return;
  if (!activeEnvId) {
    body.classList.add("muted");
    body.innerHTML = "Select an environment.";
    return;
  }
  if (k8sPanel === "overview") {
    const data = clusterCache && typeof clusterCache === "object" ? clusterCache : {};
    body.classList.remove("muted");
    if (data.reachable === false) {
      body.innerHTML = unavail(data.error || "cluster unreachable") + clusterBodyHtml(data);
    } else {
      const html = clusterBodyHtml(data);
      body.innerHTML = html || unavail("empty cluster response");
    }
    return;
  }
  if (k8sPanel === "workloads") {
    renderWorkloads();
    return;
  }
  const err = panelError[k8sPanel];
  const data = panelCache[k8sPanel];
  if (err && !data) {
    body.classList.add("muted");
    body.innerHTML = panelUnavailable(err);
    return;
  }
  body.classList.remove("muted");
  const note = err ? `<div class="muted">${esc(err)}</div>` : "";
  if (k8sPanel === "events") body.innerHTML = note + eventsTableHtml(data);
  else if (k8sPanel === "networking") body.innerHTML = note + networkingHtml(data);
  else if (k8sPanel === "storage") body.innerHTML = note + storageHtml(data);
  else if (k8sPanel === "config") body.innerHTML = note + configHtml(data);
  else if (k8sPanel === "helm") body.innerHTML = note + helmHtml(data);
  else if (k8sPanel === "nodes") body.innerHTML = note + nodesDetailHtml(data);
}

function eventsTableHtml(data) {
  const rows = asList(data && data.events);
  if (!rows.length) return emptyNote("events");
  const body = rows
    .map((ev) => {
      const info = ev && typeof ev === "object" ? ev : {};
      const typ = String(info.type || "Normal");
      const pillCls = typ.toLowerCase() === "warning" ? "warn" : "ok";
      return `<tr>
        <td><span class="pill ${pillCls}">${esc(typ)}</span></td>
        <td class="muted">${esc(info.namespace || "—")}</td>
        <td class="muted">${esc(info.involved_object || "—")}</td>
        <td>${esc(info.reason || "—")}</td>
        <td class="lc-msg" title="${esc(info.message || "")}">${esc(trunc(info.message || ""))}</td>
        <td class="muted">${esc(info.count ?? "—")}</td>
        <td class="muted">${seenHtml(info.last_timestamp)}</td>
      </tr>`;
    })
    .join("");
  return `<table class="os-table">
    <thead><tr><th>Type</th><th>Namespace</th><th>Object</th><th>Reason</th><th>Message</th><th>Count</th><th>Last</th></tr></thead>
    <tbody>${body}</tbody>
  </table>`;
}

function networkingHtml(data) {
  const d = data && typeof data === "object" ? data : {};
  const svcs = asList(d.services);
  const ings = asList(d.ingresses);
  const run = canRun();
  const svcRows = svcs
    .map((info) => {
      const ns = info.namespace || "default";
      const name = info.name || "";
      const ports = Array.isArray(info.ports) ? info.ports.join(", ") : info.ports || "—";
      const acts = [
        `<button type="button" class="secondary btn-sm" data-k8s-describe data-kind="services" data-ns="${esc(ns)}" data-name="${esc(name)}">Describe</button>`,
      ];
      if (run && name) {
        acts.push(
          `<button type="button" class="secondary btn-sm" data-svc-del data-ns="${esc(ns)}" data-name="${esc(name)}" ${gate(true, "operator")}>Delete</button>`
        );
      }
      return `<tr>
        <td class="muted">${esc(ns)}</td>
        <td><code>${esc(name || "?")}</code></td>
        <td>${esc(info.type || "ClusterIP")}</td>
        <td class="muted">${esc(info.cluster_ip || "—")}</td>
        <td class="muted">${esc(ports)}</td>
        <td class="os-actions">${acts.join(" ")}</td>
      </tr>`;
    })
    .join("");
  const ingRows = ings
    .map((info) => {
      const ns = info.namespace || "default";
      const name = info.name || "";
      const hosts = Array.isArray(info.hosts) ? info.hosts.join(", ") : info.hosts || "—";
      return `<tr>
        <td class="muted">${esc(ns)}</td>
        <td><code>${esc(name || "?")}</code></td>
        <td class="muted">${esc(info.class || "—")}</td>
        <td>${esc(hosts)}</td>
        <td class="muted">${esc(info.address || "—")}</td>
        <td>${info.tls ? '<span class="pill ok">TLS</span>' : '<span class="muted">—</span>'}</td>
        <td class="os-actions"><button type="button" class="secondary btn-sm" data-k8s-describe data-kind="ingresses" data-ns="${esc(ns)}" data-name="${esc(name)}">Describe</button></td>
      </tr>`;
    })
    .join("");
  return `<h3 class="lc-title">Services</h3>
    ${svcs.length ? `<table class="os-table"><thead><tr><th>Namespace</th><th>Name</th><th>Type</th><th>ClusterIP</th><th>Ports</th><th></th></tr></thead><tbody>${svcRows}</tbody></table>` : emptyNote("services")}
    <h3 class="lc-title">Ingresses</h3>
    ${ings.length ? `<table class="os-table"><thead><tr><th>Namespace</th><th>Name</th><th>Class</th><th>Hosts</th><th>Address</th><th>TLS</th><th></th></tr></thead><tbody>${ingRows}</tbody></table>` : emptyNote("ingresses")}`;
}

function storageHtml(data) {
  const d = data && typeof data === "object" ? data : {};
  const pvcs = asList(d.persistentvolumeclaims);
  const pvs = asList(d.persistentvolumes);
  const scs = asList(d.storageclasses);
  const run = canRun();
  const pvcRows = pvcs
    .map((info) => {
      const ns = info.namespace || "default";
      const name = info.name || "";
      const acts = [
        `<button type="button" class="secondary btn-sm" data-k8s-describe data-kind="persistentvolumeclaims" data-ns="${esc(ns)}" data-name="${esc(name)}">Describe</button>`,
      ];
      if (run && name) {
        acts.push(
          `<button type="button" class="secondary btn-sm" data-pvc-del data-ns="${esc(ns)}" data-name="${esc(name)}" ${gate(true, "operator")}>Delete</button>`
        );
      }
      return `<tr>
        <td class="muted">${esc(ns)}</td>
        <td><code>${esc(name || "?")}</code></td>
        <td>${phasePillHtml(info.phase)}</td>
        <td class="muted">${esc(info.storage_class || "—")}</td>
        <td class="muted">${esc(info.capacity || "—")}</td>
        <td class="muted">${esc((info.access_modes || []).join(", ") || "—")}</td>
        <td class="os-actions">${acts.join(" ")}</td>
      </tr>`;
    })
    .join("");
  const pvRows = pvs
    .map((info) => `<tr>
      <td><code>${esc(info.name || "?")}</code></td>
      <td>${phasePillHtml(info.phase)}</td>
      <td class="muted">${esc(info.storage_class || "—")}</td>
      <td class="muted">${esc(info.capacity || "—")}</td>
      <td class="muted">${esc(info.claim || "—")}</td>
      <td class="muted">${esc(info.reclaim_policy || "—")}</td>
    </tr>`)
    .join("");
  const scRows = scs
    .map((info) => `<tr>
      <td><code>${esc(info.name || "?")}</code></td>
      <td class="muted">${esc(info.provisioner || "—")}</td>
      <td class="muted">${esc(info.reclaim_policy || "—")}</td>
      <td class="muted">${esc(info.volume_binding_mode || "—")}</td>
      <td>${info.default ? '<span class="pill ok">default</span>' : ""}</td>
    </tr>`)
    .join("");
  return `<h3 class="lc-title">PersistentVolumeClaims</h3>
    ${pvcs.length ? `<table class="os-table"><thead><tr><th>Namespace</th><th>Name</th><th>Phase</th><th>Class</th><th>Capacity</th><th>Modes</th><th></th></tr></thead><tbody>${pvcRows}</tbody></table>` : emptyNote("PVCs")}
    <h3 class="lc-title">PersistentVolumes</h3>
    ${pvs.length ? `<table class="os-table"><thead><tr><th>Name</th><th>Phase</th><th>Class</th><th>Capacity</th><th>Claim</th><th>Reclaim</th></tr></thead><tbody>${pvRows}</tbody></table>` : emptyNote("PVs")}
    <h3 class="lc-title">StorageClasses</h3>
    ${scs.length ? `<table class="os-table"><thead><tr><th>Name</th><th>Provisioner</th><th>Reclaim</th><th>Binding</th><th></th></tr></thead><tbody>${scRows}</tbody></table>` : emptyNote("StorageClasses")}`;
}

function configHtml(data) {
  const d = data && typeof data === "object" ? data : {};
  const nss = asList(d.namespaces);
  const cms = asList(d.configmaps);
  const secrets = asList(d.secrets);
  const run = canRun();
  const nsForm = run
    ? `<div class="kc-wl-toolbar">
        <button type="button" class="btn-sm" data-ns-create ${gate(true, "operator")}>New namespace</button>
      </div>`
    : "";
  const nsRows = nss
    .map((info) => {
      const name = info.name || "";
      const acts = [
        `<button type="button" class="secondary btn-sm" data-k8s-describe data-kind="namespaces" data-name="${esc(name)}">Describe</button>`,
      ];
      if (run && name) {
        acts.push(
          `<button type="button" class="secondary btn-sm" data-ns-del data-name="${esc(name)}" ${gate(true, "operator")}>Delete</button>`
        );
      }
      return `<tr>
        <td><code>${esc(name || "?")}</code></td>
        <td>${phasePillHtml(info.phase)}</td>
        <td>${kvChips(info.labels, 4)}</td>
        <td class="os-actions">${acts.join(" ")}</td>
      </tr>`;
    })
    .join("");
  const cmRows = cms
    .map((info) => {
      const ns = info.namespace || "default";
      const name = info.name || "";
      const keys = Array.isArray(info.keys) ? info.keys.join(", ") : "";
      return `<tr>
        <td class="muted">${esc(ns)}</td>
        <td><code>${esc(name || "?")}</code></td>
        <td class="muted">${esc(keys || "—")}</td>
        <td class="os-actions"><button type="button" class="secondary btn-sm" data-k8s-describe data-kind="configmaps" data-ns="${esc(ns)}" data-name="${esc(name)}">Describe</button></td>
      </tr>`;
    })
    .join("");
  const secretRows = secrets
    .map((info) => {
      const ns = info.namespace || "default";
      const name = info.name || "";
      const keys = Array.isArray(info.keys) ? `${info.keys.length} keys` : "";
      return `<tr>
        <td class="muted">${esc(ns)}</td>
        <td><code>${esc(name || "?")}</code></td>
        <td class="muted">${esc(info.type || "Opaque")}</td>
        <td class="muted">${esc(keys || "—")}</td>
      </tr>`;
    })
    .join("");
  return `${nsForm}
    <h3 class="lc-title">Namespaces</h3>
    ${nss.length ? `<table class="os-table"><thead><tr><th>Name</th><th>Phase</th><th>Labels</th><th></th></tr></thead><tbody>${nsRows}</tbody></table>` : emptyNote("namespaces")}
    <h3 class="lc-title">ConfigMaps</h3>
    ${cms.length ? `<table class="os-table"><thead><tr><th>Namespace</th><th>Name</th><th>Keys</th><th></th></tr></thead><tbody>${cmRows}</tbody></table>` : emptyNote("configmaps")}
    <h3 class="lc-title">Secrets</h3>
    <p class="muted">Names and types only. Values are never shown.</p>
    ${secrets.length ? `<table class="os-table"><thead><tr><th>Namespace</th><th>Name</th><th>Type</th><th>Keys</th></tr></thead><tbody>${secretRows}</tbody></table>` : emptyNote("secrets")}`;
}

function helmHtml(data) {
  const releases = asList(data && data.releases);
  const run = canRun();
  const rows = releases
    .map((info) => {
      const ns = info.namespace || "default";
      const name = info.name || "";
      const acts = [
        `<button type="button" class="secondary btn-sm" data-helm-hist data-ns="${esc(ns)}" data-name="${esc(name)}">History</button>`,
      ];
      if (run && name) {
        acts.push(
          `<button type="button" class="secondary btn-sm" data-helm-rollback data-ns="${esc(ns)}" data-name="${esc(name)}" ${gate(true, "operator")}>Rollback</button>`
        );
        acts.push(
          `<button type="button" class="secondary btn-sm" data-helm-upgrade data-ns="${esc(ns)}" data-name="${esc(name)}" ${gate(true, "operator")}>Upgrade</button>`
        );
      }
      return `<tr>
        <td><strong>${esc(name || "?")}</strong></td>
        <td class="muted">${esc(ns)}</td>
        <td class="muted">${esc(info.revision ?? "—")}</td>
        <td>${releaseStatusPillHtml(info.status)}</td>
        <td><code>${esc(info.chart || "—")}</code></td>
        <td class="muted">${esc(info.app_version || info.version || "—")}</td>
        <td class="os-actions">${acts.join(" ")}</td>
      </tr>`;
    })
    .join("");
  let detail = "";
  if (helmDetail) {
    const hist = asList(helmDetail.history);
    const st = helmDetail.status && typeof helmDetail.status === "object" ? helmDetail.status : {};
    const histRows = hist
      .map(
        (h) => `<tr>
          <td class="muted">${esc(h.revision ?? "—")}</td>
          <td>${releaseStatusPillHtml(h.status)}</td>
          <td><code>${esc(h.chart || "—")}</code></td>
          <td class="muted">${esc(h.description || "")}</td>
          <td class="muted">${esc(h.updated || "—")}</td>
        </tr>`
      )
      .join("");
    detail = `<div class="os-detail">
      <h3 class="lc-title">${esc(helmDetail.namespace || "")}/${esc(helmDetail.name || "")}</h3>
      <div class="muted">${esc(st.status || "")} · chart ${esc(st.chart || "—")} ${esc(st.chart_version || "")} · app ${esc(st.app_version || "—")}</div>
      ${hist.length ? `<table class="os-table"><thead><tr><th>Rev</th><th>Status</th><th>Chart</th><th>Description</th><th>Updated</th></tr></thead><tbody>${histRows}</tbody></table>` : emptyNote("history")}
    </div>`;
  }
  const when = panelFetchedAt.helm ? ` <span class="muted">updated ${esc(new Date(panelFetchedAt.helm).toLocaleTimeString())}</span>` : "";
  return `${when}${releases.length ? `<table class="os-table"><thead><tr><th>Release</th><th>Namespace</th><th>Rev</th><th>Status</th><th>Chart</th><th>App</th><th></th></tr></thead><tbody>${rows}</tbody></table>` : emptyNote("helm releases")}${detail}`;
}

function nodesDetailHtml(data) {
  const nodes = asList(data && data.nodes);
  if (!nodes.length) return emptyNote("nodes");
  const run = canRun();
  const rows = nodes
    .map((info) => {
      const name = info.name || "";
      const unsched = info.unschedulable === true;
      const conds = asList(info.conditions)
        .filter((c) => c && c.status && c.status !== "False" && c.type !== "Ready")
        .map((c) => c.type)
        .join(", ");
      const cap = info.capacity && typeof info.capacity === "object" ? info.capacity : {};
      const capLabel = [cap.cpu ? `${cap.cpu} CPU` : "", cap.memory || ""].filter(Boolean).join(" · ");
      const acts = [
        `<button type="button" class="secondary btn-sm" data-k8s-describe data-kind="nodes" data-name="${esc(name)}">Describe</button>`,
      ];
      if (run && name) {
        acts.push(
          `<button type="button" class="secondary btn-sm" data-k8s-node-act="${unsched ? "uncordon" : "cordon"}" data-k8s-name="${esc(name)}" ${gate(true, "operator")}>${unsched ? "Uncordon" : "Cordon"}</button>`
        );
        acts.push(
          `<button type="button" class="secondary btn-sm" data-k8s-node-act="drain" data-k8s-name="${esc(name)}" ${gate(true, "operator")}>Drain</button>`
        );
        acts.push(
          `<button type="button" class="secondary btn-sm" data-node-taint data-name="${esc(name)}" ${gate(true, "operator")}>Taint</button>`
        );
        acts.push(
          `<button type="button" class="secondary btn-sm" data-node-untaint data-name="${esc(name)}" ${gate(true, "operator")}>Untaint</button>`
        );
        acts.push(
          `<button type="button" class="secondary btn-sm" data-node-label data-name="${esc(name)}" ${gate(true, "operator")}>Label</button>`
        );
      }
      return `<tr>
        <td><code>${esc(name || "?")}</code></td>
        <td>${nodeStatusPillHtml(info.status)}${unsched ? ' <span class="pill warn">SchedulingDisabled</span>' : ""}</td>
        <td class="muted">${esc((info.roles || []).join(", ") || "—")}</td>
        <td>${kvChips(info.labels, 3)}</td>
        <td>${taintChips(info.taints)}</td>
        <td class="muted">${esc(capLabel || "—")}</td>
        <td class="muted">${esc(conds || "—")}</td>
        <td class="os-actions">${acts.join(" ")}</td>
      </tr>`;
    })
    .join("");
  return `<table class="os-table">
    <thead><tr><th>Name</th><th>Status</th><th>Roles</th><th>Labels</th><th>Taints</th><th>Capacity</th><th>Conditions</th><th></th></tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

async function k8sMutate(method, suffix, body) {
  const envId = (envIdGetter && envIdGetter()) || activeEnvId;
  if (!envId) return false;
  try {
    const d = await api(`/api/v1/environments/${encodeURIComponent(envId)}${suffix}`, {
      method,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      timeout: 60000,
    });
    if (d && d.ok === false) {
      toast(d.error || d.message || "action failed", "error");
      return false;
    }
    toast((d && d.message) || "accepted", "ok");
    return true;
  } catch (err) {
    toast(err && err.message ? err.message : "action failed", "error");
    return false;
  }
}

function drainConfirm(name) {
  return window.confirm(`Drain ${name}? Pods will be evicted. DaemonSets are left in place.`);
}

async function runNodeAction(name, action) {
  if (!name || !action) return;
  if (action === "drain") {
    if (!drainConfirm(name)) return;
  } else if (action === "cordon") {
    if (!window.confirm(`Cordon ${name}? New pods will not be scheduled on this node.`)) return;
  } else if (action === "uncordon") {
    if (!window.confirm(`Uncordon ${name}?`)) return;
  } else {
    return;
  }
  const ok = await k8sMutate(
    "POST",
    `/k8s/nodes/${encodeURIComponent(name)}/${encodeURIComponent(action)}`
  );
  if (ok) {
    const envId = (envIdGetter && envIdGetter()) || activeEnvId;
    if (envId) loadClusterCard(envId);
    if (k8sPanel === "nodes") loadK8sPanel();
  }
}

async function deletePod(ns, pod) {
  if (!pod) return;
  const namespace = ns || "default";
  if (!window.confirm(`Delete pod ${namespace}/${pod}?`)) return;
  const ok = await k8sMutate(
    "DELETE",
    `/k8s/pods/${encodeURIComponent(namespace)}/${encodeURIComponent(pod)}`
  );
  if (ok) {
    const envId = (envIdGetter && envIdGetter()) || activeEnvId;
    await loadWorkloads();
    if (envId) loadClusterCard(envId);
  }
}

async function scaleWorkload(kind, ns, name, current) {
  if (!kind || !name) return;
  const namespace = ns || "default";
  const raw = window.prompt(
    `Scale ${kindLabel(kind)} ${namespace}/${name} to how many replicas?`,
    String(current ?? 1)
  );
  if (raw == null) return;
  const replicas = Number(String(raw).trim());
  if (!Number.isInteger(replicas) || replicas < 0 || replicas > 100) {
    toast("Replicas must be an integer from 0 to 100", "error");
    return;
  }
  const ok = await k8sMutate(
    "POST",
    `/k8s/workloads/${encodeURIComponent(kindPlural(kind))}/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}/scale`,
    { replicas }
  );
  if (ok) await loadWorkloads();
}

async function restartWorkload(kind, ns, name) {
  if (!kind || !name) return;
  const namespace = ns || "default";
  if (!window.confirm(`Restart ${kindLabel(kind)} ${namespace}/${name}?`)) return;
  const ok = await k8sMutate(
    "POST",
    `/k8s/workloads/${encodeURIComponent(kindPlural(kind))}/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}/restart`
  );
  if (ok) await loadWorkloads();
}

async function deleteWorkload(kind, ns, name) {
  if (!kind || !name) return;
  const namespace = ns || "default";
  if (!window.confirm(`Delete ${kindLabel(kind)} ${namespace}/${name}?`)) return;
  const ok = await k8sMutate(
    "DELETE",
    `/k8s/workloads/${encodeURIComponent(kindPlural(kind))}/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}`
  );
  if (ok) await loadWorkloads();
}

async function deleteService(ns, name) {
  if (!name) return;
  const namespace = ns || "default";
  if (!window.confirm(`Delete Service ${namespace}/${name}?`)) return;
  const ok = await k8sMutate(
    "DELETE",
    `/k8s/services/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}`
  );
  if (ok) await loadK8sPanel();
}

async function deletePvc(ns, name) {
  if (!name) return;
  const namespace = ns || "default";
  if (!window.confirm(`Delete PVC ${namespace}/${name}?`)) return;
  const ok = await k8sMutate(
    "DELETE",
    `/k8s/persistentvolumeclaims/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}`
  );
  if (ok) await loadK8sPanel();
}

async function createNamespace() {
  const raw = window.prompt("New namespace name");
  if (raw == null) return;
  const name = String(raw).trim();
  if (!name) return;
  const ok = await k8sMutate("POST", "/k8s/namespaces", { name });
  if (ok) await loadK8sPanel();
}

async function deleteNamespace(name) {
  if (!name) return;
  if (!window.confirm(`Delete namespace ${name}? This removes everything in it.`)) return;
  const ok = await k8sMutate("DELETE", `/k8s/namespaces/${encodeURIComponent(name)}`);
  if (ok) await loadK8sPanel();
}

async function taintNode(name) {
  if (!name) return;
  const key = window.prompt(`Taint key for ${name}`);
  if (key == null || !String(key).trim()) return;
  const value = window.prompt("Taint value (optional)", "") ?? "";
  const effect = window.prompt(`Effect (${TAINT_EFFECTS.join(", ")})`, "NoSchedule");
  if (effect == null) return;
  const ok = await k8sMutate("POST", `/k8s/nodes/${encodeURIComponent(name)}/taint`, {
    key: String(key).trim(),
    value: String(value),
    effect: String(effect).trim(),
  });
  if (ok) await loadK8sPanel();
}

async function untaintNode(name) {
  if (!name) return;
  const key = window.prompt(`Remove taint key on ${name}`);
  if (key == null || !String(key).trim()) return;
  const effect = window.prompt(`Effect (${TAINT_EFFECTS.join(", ")})`, "NoSchedule");
  if (effect == null) return;
  const ok = await k8sMutate("DELETE", `/k8s/nodes/${encodeURIComponent(name)}/taint`, {
    key: String(key).trim(),
    effect: String(effect).trim(),
  });
  if (ok) await loadK8sPanel();
}

async function labelNode(name) {
  if (!name) return;
  const key = window.prompt(`Label key for ${name}`);
  if (key == null || !String(key).trim()) return;
  const value = window.prompt("Label value", "");
  if (value == null) return;
  const ok = await k8sMutate("POST", `/k8s/nodes/${encodeURIComponent(name)}/label`, {
    key: String(key).trim(),
    value: String(value),
  });
  if (ok) await loadK8sPanel();
}

async function helmRollback(ns, name) {
  if (!name) return;
  const namespace = ns || "default";
  const raw = window.prompt(`Rollback ${namespace}/${name} to revision (empty = previous)`);
  if (raw == null) return;
  const body = {};
  const trimmed = String(raw).trim();
  if (trimmed) {
    const revision = Number(trimmed);
    if (!Number.isInteger(revision) || revision < 1) {
      toast("Revision must be a positive integer", "error");
      return;
    }
    body.revision = revision;
  }
  const ok = await k8sMutate(
    "POST",
    `/k8s/helm/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}/rollback`,
    body
  );
  if (ok) {
    helmDetail = null;
    await loadK8sPanel();
  }
}

async function helmUpgrade(ns, name) {
  if (!name) return;
  const namespace = ns || "default";
  const chart = window.prompt(
    `Upgrade ${namespace}/${name}. Chart (empty = reuse release chart)`,
    ""
  );
  if (chart == null) return;
  const rawVals = window.prompt("Optional values JSON object (empty = --reuse-values only)", "");
  if (rawVals == null) return;
  const body = {};
  if (String(chart).trim()) body.chart = String(chart).trim();
  if (String(rawVals).trim()) {
    try {
      const parsed = JSON.parse(String(rawVals));
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        toast("Values must be a JSON object", "error");
        return;
      }
      body.values = parsed;
    } catch {
      toast("Values must be valid JSON", "error");
      return;
    }
  }
  const ok = await k8sMutate(
    "POST",
    `/k8s/helm/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}/upgrade`,
    body
  );
  if (ok) {
    helmDetail = null;
    await loadK8sPanel();
  }
}

async function showHelmHistory(ns, name) {
  const namespace = ns || "default";
  try {
    const [st, hist] = await Promise.all([
      api(k8sPath(`/k8s/helm/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}`), {
        timeout: WORKLOADS_TIMEOUT,
      }),
      api(
        k8sPath(`/k8s/helm/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}/history`),
        { timeout: WORKLOADS_TIMEOUT }
      ),
    ]);
    helmDetail = {
      namespace,
      name,
      status: st && st.status,
      history: asList(hist && hist.history),
    };
    renderK8sPanel();
  } catch (e) {
    toast(e && e.message ? e.message : "helm history unavailable", "error");
  }
}

async function applyYaml() {
  const ta = document.getElementById("kc-apply-yaml");
  const text = ta && ta.value ? String(ta.value) : "";
  if (!text.trim()) {
    toast("YAML is empty", "error");
    return;
  }
  if (!window.confirm("Apply this YAML to the cluster?")) return;
  const ok = await k8sMutate("POST", "/k8s/apply", { yaml: text });
  if (ok) await loadK8sPanel();
}

function onClusterClick(e) {
  const tab = e.target.closest("[data-kc-panel]");
  if (tab) {
    setK8sPanel(tab.dataset.kcPanel);
    return;
  }
  const applyBtn = e.target.closest("#kc-apply-btn");
  if (applyBtn) {
    applyYaml();
    return;
  }
  const logsBtn = e.target.closest("[data-pod-logs]");
  if (logsBtn) {
    openPodLogs(logsBtn.dataset.ns, logsBtn.dataset.pod);
    return;
  }
  const nodeAct = e.target.closest("[data-k8s-node-act]");
  if (nodeAct) {
    runNodeAction(nodeAct.dataset.k8sName, nodeAct.dataset.k8sNodeAct);
    return;
  }
  const del = e.target.closest("[data-pod-del]");
  if (del) {
    deletePod(del.dataset.ns, del.dataset.pod);
    return;
  }
  const wlDel = e.target.closest("[data-wl-del]");
  if (wlDel) {
    deleteWorkload(wlDel.dataset.kind, wlDel.dataset.ns, wlDel.dataset.name);
    return;
  }
  const svcDel = e.target.closest("[data-svc-del]");
  if (svcDel) {
    deleteService(svcDel.dataset.ns, svcDel.dataset.name);
    return;
  }
  const pvcDel = e.target.closest("[data-pvc-del]");
  if (pvcDel) {
    deletePvc(pvcDel.dataset.ns, pvcDel.dataset.name);
    return;
  }
  const nsCreate = e.target.closest("[data-ns-create]");
  if (nsCreate) {
    createNamespace();
    return;
  }
  const nsDel = e.target.closest("[data-ns-del]");
  if (nsDel) {
    deleteNamespace(nsDel.dataset.name);
    return;
  }
  const taint = e.target.closest("[data-node-taint]");
  if (taint) {
    taintNode(taint.dataset.name);
    return;
  }
  const untaint = e.target.closest("[data-node-untaint]");
  if (untaint) {
    untaintNode(untaint.dataset.name);
    return;
  }
  const label = e.target.closest("[data-node-label]");
  if (label) {
    labelNode(label.dataset.name);
    return;
  }
  const helmHist = e.target.closest("[data-helm-hist]");
  if (helmHist) {
    showHelmHistory(helmHist.dataset.ns, helmHist.dataset.name);
    return;
  }
  const helmRb = e.target.closest("[data-helm-rollback]");
  if (helmRb) {
    helmRollback(helmRb.dataset.ns, helmRb.dataset.name);
    return;
  }
  const helmUp = e.target.closest("[data-helm-upgrade]");
  if (helmUp) {
    helmUpgrade(helmUp.dataset.ns, helmUp.dataset.name);
    return;
  }
  const scale = e.target.closest("[data-wl-scale]");
  if (scale) {
    scaleWorkload(scale.dataset.kind, scale.dataset.ns, scale.dataset.name, scale.dataset.replicas);
    return;
  }
  const restart = e.target.closest("[data-wl-restart]");
  if (restart) {
    restartWorkload(restart.dataset.kind, restart.dataset.ns, restart.dataset.name);
    return;
  }
  const describe = e.target.closest("[data-k8s-describe]");
  if (describe) {
    openDescribe(describe.dataset.kind, describe.dataset.ns, describe.dataset.name);
  }
}

function onClusterInput(e) {
  const t = e.target;
  if (!(t instanceof HTMLElement)) return;
  if (t.id === "kc-wl-ns") {
    workloadsNs = (t.value || "").trim();
    if (workloadsTimer) clearTimeout(workloadsTimer);
    workloadsTimer = setTimeout(() => {
      workloadsTimer = null;
      loadWorkloads();
    }, 300);
    return;
  }
  if (t.id === "kc-wl-pod-q") {
    workloadsPodFilter = t.value || "";
    renderWorkloads();
  }
}

function nodeReadyCounts(nodes) {
  const list = Array.isArray(nodes) ? nodes : [];
  let ready = 0;
  for (const n of list) {
    const info = n && typeof n === "object" ? n : {};
    if (nodeStatusKey(info.status) === "ready") ready += 1;
  }
  return { ready, total: list.length };
}

function showDownload(envId, kind, access) {
  if (!canRun()) return false;
  if (!access || typeof access !== "object") return false;
  if (!access[kind]) return false;
  return !downloadMissing.has(`${envId}:${kind}`);
}

function healthStripHtml(data, envId) {
  const bits = [];
  if (hasKey(data, "health")) bits.push(healthPillHtml(data.health));
  if (hasKey(data, "reachable")) bits.push(reachPillHtml(data.reachable));
  if (hasKey(data, "nodes") && Array.isArray(data.nodes)) {
    const { ready, total } = nodeReadyCounts(data.nodes);
    bits.push(`<span>${esc(ready)}/${esc(total)} nodes Ready</span>`);
  }
  const problems = data.pods && typeof data.pods === "object" && Array.isArray(data.pods.problems)
    ? data.pods.problems
    : null;
  if (problems) {
    const n = problems.length;
    const label = `${n} problem pod${n === 1 ? "" : "s"}`;
    if (n > 0) {
      bits.push(
        `<button type="button" class="env-health-link" data-health-problems>${esc(label)}</button>`
      );
    } else {
      bits.push(`<span class="muted">${esc(label)}</span>`);
    }
  }
  if (hasKey(data, "warnings") && Array.isArray(data.warnings) && data.warnings.length) {
    const n = data.warnings.length;
    bits.push(
      `<button type="button" class="env-health-link" data-health-warnings>${esc(n)} warning${n === 1 ? "" : "s"}</button>`
    );
  }
  if (hasKey(data, "resources") && data.resources && typeof data.resources === "object") {
    const cpu = data.resources.cpu != null ? `${data.resources.cpu} CPU` : "";
    const mem = data.resources.memory_gi != null ? `${data.resources.memory_gi} GiB` : "";
    const tot = [cpu, mem].filter(Boolean).join(" · ");
    if (tot) bits.push(`<span title="All Ready nodes, including control-plane">${esc(tot)}</span>`);
  }
  const access = hasKey(data, "access") ? data.access : null;
  const actions = [];
  if (showDownload(envId, "kubeconfig", access)) {
    actions.push('<button type="button" class="secondary btn-sm" data-dl-kubeconfig>Kubeconfig</button>');
  }
  if (showDownload(envId, "talosconfig", access)) {
    actions.push('<button type="button" class="secondary btn-sm" data-dl-talosconfig>Talosconfig</button>');
  }
  if (access && access.horizon) {
    actions.push(
      `<a class="secondary btn-sm" href="${esc(access.horizon)}" target="_blank" rel="noopener noreferrer">Open Horizon</a>`
    );
  }
  const stats = bits.length
    ? bits.join('<span class="env-health-sep" aria-hidden="true">·</span>')
    : '<span class="muted">Cluster status loaded.</span>';
  const btns = actions.length ? `<span class="env-health-actions">${actions.join("")}</span>` : "";
  const why =
    hasKey(data, "health_reason") && data.health_reason
      ? `<span class="env-health-reason muted">${esc(data.health_reason)}</span>`
      : "";
  return stats + why + btns;
}

function setHealthStrip(html) {
  const el = document.getElementById("env-health");
  if (!el) return;
  el.innerHTML = html;
}

function renderHealthStrip(data, err) {
  const el = document.getElementById("env-health");
  if (!el) return;
  try {
    if (!activeEnvId) {
      el.innerHTML = '<span class="muted">Select an environment.</span>';
      return;
    }
    if (err) {
      const note = trunc(err.message || "unavailable", 80);
      el.innerHTML = `<span class="muted">Cluster unavailable — ${esc(note)}</span>`;
      return;
    }
    el.innerHTML = healthStripHtml(data && typeof data === "object" ? data : {}, activeEnvId);
  } catch {
    el.innerHTML = '<span class="muted">Cluster status unavailable.</span>';
  }
}

function clusterPanelActive() {
  const panel = document.querySelector('.tab-panel[data-panel="platform"]');
  if (!(panel && panel.classList.contains("active"))) return false;
  const sub = panel.querySelector('.ptab-panel[data-ppanel="kubernetes"]');
  return !!(sub && sub.classList.contains("active"));
}

function stopClusterRefresh() {
  if (refreshTimer) {
    clearInterval(refreshTimer);
    refreshTimer = null;
  }
}

function startClusterRefresh() {
  stopClusterRefresh();
  const tick = () => {
    if (document.hidden || !clusterPanelActive()) return;
    const id = (envIdGetter && envIdGetter()) || activeEnvId;
    if (!id) return;
    if (k8sPanel !== "overview") delete panelCache[k8sPanel];
    loadClusterCard(id, { silent: true });
  };
  refreshTimer = setInterval(tick, k8sPanel === "overview" ? REFRESH_MS : LIVE_REFRESH_MS);
}

function fetchCluster(envId) {
  if (inflight && inflightFor === envId) return inflight;
  inflightFor = envId;
  inflight = api(`/api/v1/environments/${encodeURIComponent(envId)}/cluster`, { timeout: 12000 }).finally(() => {
    if (inflightFor === envId) {
      inflight = null;
      inflightFor = "";
    }
  });
  return inflight;
}

async function downloadConfig(kind) {
  const envId = (envIdGetter && envIdGetter()) || activeEnvId;
  if (!envId) return;
  const filename = kind === "talosconfig" ? "talosconfig" : "kubeconfig";
  const path = `/api/v1/environments/${encodeURIComponent(envId)}/access/${kind}`;
  try {
    await downloadAuth(path, filename);
  } catch (e) {
    if (e && e.status === 404) {
      downloadMissing.add(`${envId}:${kind}`);
      const btn = document.querySelector(kind === "talosconfig" ? "[data-dl-talosconfig]" : "[data-dl-kubeconfig]");
      if (btn) btn.remove();
      return;
    }
    toast(`Download failed: ${e && e.message ? e.message : "unavailable"}`, "error");
  }
}

// ---------- public API ----------

export function clusterCardHtml() {
  const tabs = K8S_PANELS.map(
    ([id, label], i) =>
      `<button type="button" class="tab-btn${i === 0 ? " active" : ""}" data-kc-panel="${esc(id)}">${esc(label)}</button>`
  ).join("");
  const apply = canRun()
    ? `<details class="os-form kc-apply" id="kc-apply">
        <summary>Apply YAML</summary>
        <textarea id="kc-apply-yaml" class="kc-apply-yaml" spellcheck="false" placeholder="apiVersion: v1&#10;kind: ConfigMap&#10;metadata:&#10;  name: demo&#10;  namespace: default"></textarea>
        <div class="kc-wl-toolbar">
          <button type="button" class="btn-sm" id="kc-apply-btn" ${gate(canRun(), "operator")}>Apply</button>
          <span id="kc-apply-msg" class="muted"></span>
        </div>
      </details>`
    : "";
  return `
  <div class="card span-12" id="kc-card">
    <div class="toolbar">
      <h2>Kubernetes</h2>
      <span id="kc-pill"></span>
      <span id="kc-msg" class="muted"></span>
      <input id="kc-wl-ns" type="search" placeholder="all namespaces" autocomplete="off">
      <button class="secondary btn-sm" type="button" data-goto-tab="workflow">Deploy map</button>
      <button class="secondary btn-sm" id="kc-refresh" type="button">Refresh</button>
    </div>
    ${apply}
    <div class="tab-bar" id="kc-tabs">${tabs}</div>
    <div id="kc-body" class="muted">Select an environment.</div>
    <div id="kc-logs" class="lc-logs" hidden>
      <div class="toolbar">
        <h3 class="lc-title" style="margin:0">Pod logs</h3>
        <span id="kc-logs-meta" class="muted"></span>
        <label class="muted"><input type="checkbox" id="kc-logs-previous"> previous</label>
        <button class="secondary btn-sm" type="button" id="kc-logs-refresh">Reload</button>
        <button class="secondary btn-sm" type="button" id="kc-logs-close">Close</button>
      </div>
      <pre class="lc-logs-pre" id="kc-logs-body"></pre>
    </div>
    <div id="kc-describe" class="lc-logs" hidden>
      <div class="toolbar">
        <h3 class="lc-title" style="margin:0">Describe</h3>
        <span id="kc-describe-meta" class="muted"></span>
        <button class="secondary btn-sm" type="button" id="kc-describe-close">Close</button>
      </div>
      <pre class="lc-logs-pre" id="kc-describe-body"></pre>
    </div>
  </div>`;
}

export function wireClusterCard(getEnvId, opts = {}) {
  envIdGetter = typeof getEnvId === "function" ? getEnvId : null;
  if (opts && typeof opts.onProblemsClick === "function") onProblemsClick = opts.onProblemsClick;

  const refreshBtn = document.getElementById("kc-refresh");
  if (refreshBtn && !refreshBtn.dataset.wired) {
    refreshBtn.dataset.wired = "1";
    refreshBtn.addEventListener("click", () => {
      Object.keys(panelCache).forEach((k) => delete panelCache[k]);
      Object.keys(panelError).forEach((k) => delete panelError[k]);
      Object.keys(panelFetchedAt).forEach((k) => delete panelFetchedAt[k]);
      workloadsCache = null;
      helmDetail = null;
      const id = (envIdGetter && envIdGetter()) || activeEnvId;
      if (id) loadClusterCard(id);
    });
  }

  const strip = document.getElementById("env-health");
  if (strip && !strip.dataset.wired) {
    strip.dataset.wired = "1";
    strip.addEventListener("click", (e) => {
      if (e.target.closest("[data-health-problems]") || e.target.closest("[data-health-warnings]")) {
        if (onProblemsClick) onProblemsClick();
        return;
      }
      if (e.target.closest("[data-dl-kubeconfig]")) {
        downloadConfig("kubeconfig");
        return;
      }
      if (e.target.closest("[data-dl-talosconfig]")) {
        downloadConfig("talosconfig");
      }
    });
  }

  const card = document.getElementById("kc-card");
  if (card && !card.dataset.logsWired) {
    card.dataset.logsWired = "1";
    card.addEventListener("click", onClusterClick);
    card.addEventListener("input", onClusterInput);
  }
  const logsClose = document.getElementById("kc-logs-close");
  if (logsClose && !logsClose.dataset.wired) {
    logsClose.dataset.wired = "1";
    logsClose.addEventListener("click", hidePodLogs);
  }
  const logsRefresh = document.getElementById("kc-logs-refresh");
  if (logsRefresh && !logsRefresh.dataset.wired) {
    logsRefresh.dataset.wired = "1";
    logsRefresh.addEventListener("click", () => {
      if (logsTarget) loadPodLogs(logsTarget.ns, logsTarget.pod);
    });
  }
  const describeClose = document.getElementById("kc-describe-close");
  if (describeClose && !describeClose.dataset.wired) {
    describeClose.dataset.wired = "1";
    describeClose.addEventListener("click", hideDescribe);
  }

  startClusterRefresh();
  if (!jobsStream) {
    jobsStream = connect(["jobs"], {
      jobs: (payload) => {
        if (!payload || payload.type !== "job_log") return;
        const id = (envIdGetter && envIdGetter()) || activeEnvId;
        if (!id || payload.environment_id !== id) return;
        if (k8sPanel !== "helm" && k8sPanel !== "overview" && k8sPanel !== "workloads") return;
        if (helmDebounce) return;
        helmDebounce = setTimeout(() => {
          helmDebounce = null;
          if (k8sPanel === "overview") loadClusterCard(id, { silent: true });
          else loadK8sPanel();
        }, 1200);
      },
    });
  }
}

function hidePodLogs() {
  logsTarget = null;
  const panel = document.getElementById("kc-logs");
  if (panel) panel.hidden = true;
  const body = document.getElementById("kc-logs-body");
  if (body) body.textContent = "";
}

async function openPodLogs(ns, pod) {
  logsTarget = { ns: ns || "default", pod: pod || "" };
  const panel = document.getElementById("kc-logs");
  if (panel) panel.hidden = false;
  await loadPodLogs(logsTarget.ns, logsTarget.pod);
}

function hideDescribe() {
  const panel = document.getElementById("kc-describe");
  if (panel) panel.hidden = true;
  const body = document.getElementById("kc-describe-body");
  if (body) body.textContent = "";
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
      bits.push(
        `  ${info.type || "Normal"} ${info.reason || ""} ${info.message || ""}`.trimEnd()
      );
    }
    bits.push("");
  }
  if (obj.object) {
    try {
      bits.push(JSON.stringify(obj.object, null, 2));
    } catch {
      bits.push(String(obj.object));
    }
  } else if (!bits.length) {
    bits.push("(empty)");
  }
  return bits.join("\n");
}

async function openDescribe(kind, ns, name) {
  const envId = (envIdGetter && envIdGetter()) || activeEnvId;
  const panel = document.getElementById("kc-describe");
  const meta = document.getElementById("kc-describe-meta");
  const body = document.getElementById("kc-describe-body");
  if (panel) panel.hidden = false;
  const label = [kind, ns, name].filter(Boolean).join("/");
  if (meta) meta.textContent = label;
  if (body) body.textContent = "Loading…";
  if (!envId || !name) {
    if (body) body.textContent = "Nothing selected.";
    return;
  }
  const qs = new URLSearchParams({ kind: kindPlural(kind) || kind || "pods", name });
  if (kindPlural(kind) !== "nodes" && ns) qs.set("namespace", ns);
  try {
    const d = await api(
      `/api/v1/environments/${encodeURIComponent(envId)}/k8s/describe?${qs}`,
      { timeout: 25000 }
    );
    if (body) body.textContent = formatDescribe(d);
  } catch (e) {
    if (body) body.textContent = e && e.message ? e.message : "unavailable";
  }
}

async function loadPodLogs(ns, pod) {
  const envId = (envIdGetter && envIdGetter()) || activeEnvId;
  const meta = document.getElementById("kc-logs-meta");
  const body = document.getElementById("kc-logs-body");
  const prev = document.getElementById("kc-logs-previous");
  if (meta) meta.textContent = `${ns}/${pod}`;
  if (body) body.textContent = "Loading…";
  if (!envId || !pod) {
    if (body) body.textContent = "No pod selected.";
    return;
  }
  const qs = new URLSearchParams({ pod, namespace: ns || "default", tail: "200" });
  if (prev && prev.checked) qs.set("previous", "true");
  try {
    const d = await api(
      `/api/v1/environments/${encodeURIComponent(envId)}/cluster/logs?${qs}`
    );
    const data = d && typeof d === "object" ? d : {};
    if (body) {
      body.textContent = data.error ? data.error : data.text || "(empty)";
    }
  } catch (e) {
    if (body) body.textContent = e && e.message ? e.message : "unavailable";
  }
}

export async function loadClusterCard(envId, { silent = false } = {}) {
  const prev = activeEnvId;
  activeEnvId = envId || "";
  const msg = document.getElementById("kc-msg");
  const pill = document.getElementById("kc-pill");

  if (!activeEnvId) {
    if (msg) msg.textContent = "";
    if (pill) pill.innerHTML = "";
    clusterCache = null;
    Object.keys(panelCache).forEach((k) => delete panelCache[k]);
    Object.keys(panelError).forEach((k) => delete panelError[k]);
    helmDetail = null;
    renderHealthStrip(null);
    resetWorkloadsFilters();
    renderK8sPanel();
    return;
  }

  if (prev !== activeEnvId) {
    downloadMissing.clear();
    resetWorkloadsFilters();
    clusterCache = null;
    Object.keys(panelCache).forEach((k) => delete panelCache[k]);
    Object.keys(panelError).forEach((k) => delete panelError[k]);
    helmDetail = null;
    setHealthStrip('<span class="muted">Checking cluster…</span>');
  }

  if (!silent && msg) msg.textContent = "Loading…";

  let d;
  try {
    d = await fetchCluster(activeEnvId);
  } catch (e) {
    if (activeEnvId !== envId) return;
    if (live.cluster && live.envId === envId) {
      clusterCache = live.cluster;
      if (msg) msg.textContent = "";
      renderHealthStrip(live.cluster);
      renderK8sPanel();
      return;
    }
    if (msg) msg.textContent = "";
    if (pill) pill.innerHTML = "";
    clusterCache = { reachable: false, error: e.message };
    renderHealthStrip(null, e);
    renderK8sPanel();
    if (k8sPanel !== "overview") loadK8sPanel();
    return;
  }
  if (activeEnvId !== envId) return;
  if (msg) msg.textContent = "";

  const data = d && typeof d === "object" ? d : {};
  clusterCache = data;
  bindLiveEnv(activeEnvId);
  applyLive({ cluster: data });
  renderHealthStrip(data);
  try {
    const pills = [];
    if (hasKey(data, "health")) pills.push(healthPillHtml(data.health));
    pills.push(reachPillHtml(data.reachable));
    if (pill) pill.innerHTML = pills.join(" ");
  } catch {
    if (pill) pill.innerHTML = "";
  }
  renderK8sPanel();
  if (k8sPanel !== "overview") loadK8sPanel();
}

export function destroyClusterCard() {
  stopClusterRefresh();
  if (jobsStream) {
    jobsStream.close();
    jobsStream = null;
  }
  if (helmDebounce) {
    clearTimeout(helmDebounce);
    helmDebounce = null;
  }
  if (workloadsTimer) {
    clearTimeout(workloadsTimer);
    workloadsTimer = null;
  }
  inflight = null;
  inflightFor = "";
  workloadsInflight = null;
  workloadsInflightKey = "";
  envIdGetter = null;
  onProblemsClick = null;
  downloadMissing.clear();
  activeEnvId = "";
  clusterCache = null;
  helmDetail = null;
  k8sPanel = "overview";
  Object.keys(panelCache).forEach((k) => delete panelCache[k]);
  Object.keys(panelError).forEach((k) => delete panelError[k]);
  resetWorkloadsFilters();
  hidePodLogs();
}
