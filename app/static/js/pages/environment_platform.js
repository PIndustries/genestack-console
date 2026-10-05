// pages/environment_platform.js — Omni-class Talos cluster: overview, machines, inspector.
import { api, downloadAuth, esc, toast } from "../api.js";
import { canRun, envName, gate, store } from "../store.js";
import { applyLive, bindLiveEnv, live } from "./environment_live_state.js?v=ls5";

const REFRESH_MS = 20000;

let activeEnvId = "";
let envIdGetter = null;
let cache = null;
let selected = "";
let filter = "all";
let inspTab = "overview";
let menuOpen = "";
let refreshTimer = null;
let inflight = null;
let logsText = "";
let logsFor = "";
let servicesState = null;
let servicesFor = "";
let inspState = {};
let logService = "kubelet";
let configYaml = "";
let configFor = "";
let configMode = "auto";
let resetOpts = { graceful: true, reboot: true, wipe: true };

const LOG_SERVICES = ["dmesg", "kubelet", "containerd", "machined", "apid", "etcd", "cri", "udevd", "trustd"];
const INSP_GET_TABS = ["health", "etcd", "config", "disks", "resources", "events", "containers"];
const INSP_TIMEOUT = { resources: 40000, health: 35000, etcd: 30000, config: 25000, disks: 30000, events: 25000, containers: 25000 };

function pill(ok, label, kind) {
  if (ok === true) return `<span class="pill ok">${esc(label)}</span>`;
  if (ok === false) return `<span class="pill bad">${esc(label)}</span>`;
  return `<span class="pill">${esc(label || "—")}</span>`;
}

function roleList(roles) {
  if (Array.isArray(roles)) return roles.map((r) => String(r).toLowerCase());
  if (typeof roles === "string") {
    return roles
      .split(/[,\s]+/)
      .map((r) => r.toLowerCase())
      .filter(Boolean);
  }
  return [];
}

function isControlPlane(roles) {
  const r = roleList(roles);
  return r.includes("k8s_control_plane") || r.includes("control-plane") || r.includes("controlplane");
}

function k8sReady(kn) {
  if (!kn) return null;
  return String(kn.status || "").toLowerCase() === "ready";
}

function novaUp(os) {
  if (!os) return null;
  return String(os.state || "").toLowerCase() === "up";
}

function nodeCordoned(info) {
  const n = info && typeof info === "object" ? info : {};
  const st = String(n.status || "");
  if (/schedulingdisabled/i.test(st)) return true;
  if (n.unschedulable === true || n.cordoned === true) return true;
  if (n.unschedulable === false || n.cordoned === false) return false;
  return null;
}

function talosUp(info) {
  return !!(info && info.talos && info.talos.reachable);
}

function talosNodes(nodes) {
  return (nodes || []).filter((n) => !n || n.os !== "ubuntu");
}

function talosStatusLabel(talos) {
  if (talos && talos.reachable) return talos.version || "up";
  const err = String((talos && talos.error) || "");
  if (!err || err === "no ip") return "down";
  if (/unavailable|no route|connection|dial|timeout|refused|unreachable/i.test(err)) return "unreachable";
  return err.length > 42 ? `${err.slice(0, 39)}…` : err;
}

function unique(list) {
  return Array.from(new Set((list || []).filter(Boolean)));
}

function clusterFrom(data) {
  const given = data && data.cluster && typeof data.cluster === "object" ? data.cluster : null;
  if (given && typeof given.machines === "number") return given;
  const nodes = (data && data.nodes) || [];
  let ready = 0;
  let notReady = 0;
  let cp = 0;
  let workers = 0;
  let talosReach = 0;
  const talosVers = [];
  const k8sVers = [];
  for (const n of nodes) {
    if (isControlPlane(n && n.roles)) cp += 1;
    else workers += 1;
    const kn = n && n.kubernetes;
    if (kn) {
      if (k8sReady(kn)) ready += 1;
      else notReady += 1;
      if (kn.version) k8sVers.push(String(kn.version));
    }
    if (talosUp(n)) {
      talosReach += 1;
      if (n.talos && n.talos.version) talosVers.push(String(n.talos.version));
    }
  }
  return {
    name: envName(activeEnvId),
    machines: nodes.length,
    control_planes: cp,
    workers,
    ready,
    not_ready: notReady,
    talos_reachable: talosReach,
    talos_versions: unique(talosVers).sort(),
    kubernetes_versions: unique(k8sVers).sort(),
    install_image: (data && data.install_image) || "",
  };
}

function roleChips(roles) {
  const r = roleList(roles);
  const chips = [];
  if (isControlPlane(r)) chips.push('<span class="om-role om-role-cp">CP</span>');
  if (r.includes("etcd")) chips.push('<span class="om-role">etcd</span>');
  if (r.includes("compute") || r.includes("worker")) chips.push('<span class="om-role om-role-w">W</span>');
  if (r.includes("control") && !isControlPlane(r)) chips.push('<span class="om-role">OS</span>');
  if (r.includes("network")) chips.push('<span class="om-role">net</span>');
  if (r.includes("storage") || r.includes("storage-ceph") || r.includes("storage-cinder")) {
    chips.push('<span class="om-role">sto</span>');
  }
  return chips.length ? chips.join(" ") : '<span class="om-role om-role-mute">—</span>';
}

function dot(ok) {
  if (ok === true) return '<span class="om-dot om-dot-ok" title="up"></span>';
  if (ok === false) return '<span class="om-dot om-dot-bad" title="down"></span>';
  return '<span class="om-dot om-dot-mute" title="unknown"></span>';
}

function k8sNodeActionsHtml(name, info, compact) {
  if (!canRun() || !name) return "";
  const flagged = nodeCordoned(info) === true;
  const n = esc(name);
  const items = [];
  if (flagged) {
    items.push(
      `<button type="button" class="om-menu-item" data-k8s-node-act="uncordon" data-k8s-name="${n}">Uncordon</button>`
    );
  } else {
    items.push(
      `<button type="button" class="om-menu-item" data-k8s-node-act="cordon" data-k8s-name="${n}">Cordon</button>`
    );
    items.push(
      `<button type="button" class="om-menu-item" data-k8s-node-act="drain" data-k8s-name="${n}">Drain</button>`
    );
  }
  if (compact) return items.join("");
  return items
    .map((h) => h.replace("om-menu-item", "secondary btn-sm"))
    .join(" ");
}

function filteredNodes(nodes) {
  const list = Array.isArray(nodes) ? nodes : [];
  return list.filter((n) => {
    if (filter === "cp") return isControlPlane(n && n.roles);
    if (filter === "worker") return !isControlPlane(n && n.roles);
    if (filter === "down") return !talosUp(n) || k8sReady(n && n.kubernetes) === false;
    return true;
  });
}

function overviewHtml(data) {
  const c = clusterFrom(data);
  const nodes = data.nodes || [];
  const talos = (c.talos_versions || []).join(" · ") || "—";
  const k8s = (c.kubernetes_versions || []).join(" · ") || "—";
  const readyOk = c.machines > 0 && c.ready === c.machines && c.talos_reachable === c.machines;
  const env = (store.envs || []).find((e) => e.id === activeEnvId) || {};
  const title = c.name || env.name || envName(activeEnvId);
  const run = canRun();
  return `
  <div class="om-overview">
    <div class="om-overview-head">
      <div>
        <div class="om-kicker">Talos</div>
        <h2 class="om-title">${esc(title)}</h2>
        <div class="muted om-sub">Versions, Ready, logs, and upgrades for these machines.</div>
      </div>
      <div class="om-overview-actions">
        <button type="button" class="secondary btn-sm" data-pf-dl="kubeconfig">Kubeconfig</button>
        <button type="button" class="secondary btn-sm" data-pf-dl="talosconfig">Talosconfig</button>
        ${
          run
            ? `<button type="button" class="secondary btn-sm" data-pf-upgrade-all ${gate(canRun(), "operator")}>Upgrade Talos</button>`
            : ""
        }
      </div>
    </div>
    <div class="om-stats">
      <div class="om-stat">
        <div class="om-stat-k">Ready</div>
        <div class="om-stat-v">${pill(readyOk, `${c.ready}/${c.machines || 0}`)}</div>
      </div>
      <div class="om-stat">
        <div class="om-stat-k">Talos</div>
        <div class="om-stat-v">${dot(c.talos_reachable === c.machines && c.machines > 0)} <code>${esc(talos)}</code></div>
      </div>
      <div class="om-stat">
        <div class="om-stat-k">Kubernetes</div>
        <div class="om-stat-v"><code>${esc(k8s)}</code></div>
      </div>
      <div class="om-stat">
        <div class="om-stat-k">Control plane</div>
        <div class="om-stat-v">${esc(String(c.control_planes || 0))}</div>
      </div>
      <div class="om-stat">
        <div class="om-stat-k">Workers</div>
        <div class="om-stat-v">${esc(String(c.workers || 0))}</div>
      </div>
      <div class="om-stat">
        <div class="om-stat-k">Reachable</div>
        <div class="om-stat-v">${esc(String(c.talos_reachable || 0))}/${esc(String(nodes.length))}</div>
      </div>
    </div>
  </div>`;
}

function filterHtml(nodes) {
  const all = nodes.length;
  const cp = nodes.filter((n) => isControlPlane(n.roles)).length;
  const w = all - cp;
  const down = nodes.filter((n) => !talosUp(n) || k8sReady(n.kubernetes) === false).length;
  const btn = (id, label, count) => {
    const on = filter === id ? " active" : "";
    return `<button type="button" class="om-filter${on}" data-pf-filter="${id}">${esc(label)} <span class="om-count">${count}</span></button>`;
  };
  return `<div class="om-filters" role="tablist" aria-label="Machine filters">
    ${btn("all", "All", all)}
    ${btn("cp", "Control plane", cp)}
    ${btn("worker", "Workers", w)}
    ${btn("down", "Attention", down)}
  </div>`;
}

function menuHtml(info) {
  const name = info.name || "";
  const kn = info.kubernetes;
  const open = menuOpen === name;
  const talosVer = (info.talos && info.talos.version) || "";
  return `<div class="om-menu-wrap">
    <button type="button" class="secondary btn-sm om-more" data-pf-menu="${esc(name)}" aria-expanded="${open}" title="Machine actions">⋯</button>
    <div class="om-menu${open ? " open" : ""}" ${open ? "" : "hidden"}>
      <button type="button" class="om-menu-item" data-talos-dmesg data-name="${esc(name)}">Kernel logs</button>
      <button type="button" class="om-menu-item" data-talos-services data-name="${esc(name)}">Services</button>
      ${
        canRun()
          ? `<button type="button" class="om-menu-item" data-talos-reboot data-name="${esc(name)}">Reboot</button>
             <button type="button" class="om-menu-item" data-talos-shutdown data-name="${esc(name)}">Shutdown</button>
             <button type="button" class="om-menu-item" data-talos-reset data-name="${esc(name)}">Reset</button>
             <button type="button" class="om-menu-item" data-talos-upgrade data-name="${esc(name)}"${
               talosVer ? ` data-version="${esc(talosVer)}"` : ""
             }>Upgrade Talos</button>
             ${kn && kn.name ? k8sNodeActionsHtml(kn.name, kn, true) : ""}`
          : ""
      }
    </div>
  </div>`;
}

function rowsHtml(nodes) {
  const shown = filteredNodes(nodes);
  if (!nodes.length) {
    return `<div class="om-empty">No Talos machines yet. Add a hostname and IP below.</div>`;
  }
  if (!shown.length) {
    return `<div class="om-empty">No machines match this filter.</div>`;
  }
  return `<table class="pf-table om-table">
    <thead><tr>
      <th>Machine</th><th>Hardware</th><th>Fabric</th><th>Talos</th><th>Kubernetes</th><th>OpenStack</th><th></th>
    </tr></thead>
    <tbody>${shown
      .map((n) => {
        const info = n && typeof n === "object" ? n : {};
        const kn = info.kubernetes && typeof info.kubernetes === "object" ? info.kubernetes : null;
        const os = info.openstack && typeof info.openstack === "object" ? info.openstack : null;
        const talos = info.talos && typeof info.talos === "object" ? info.talos : {};
        const name = info.name || "?";
        const sel = name === selected ? " pf-row-sel" : "";
        const ips = [info.private_ip ? `priv ${info.private_ip}` : "", info.public_ip ? `pub ${info.public_ip}` : ""]
          .filter(Boolean)
          .join(" · ");
        const hw = kn
          ? [kn.cpu_capacity ? `${kn.cpu_capacity} CPU` : "", kn.mem_gi != null ? `${kn.mem_gi} GiB` : ""]
              .filter(Boolean)
              .join(" · ")
          : "—";
        const talosCell = `${dot(talos.reachable === true)}${
          talos.reachable
            ? `<code>${esc(talos.version || "up")}</code>`
            : `<span class="muted" title="${esc(talos.error || "")}">${esc(talosStatusLabel(talos))}</span>`
        }`;
        const k8sCell = kn
          ? `${dot(k8sReady(kn))}<code>${esc(kn.version || kn.status || "—")}</code>${
              nodeCordoned(kn) === true ? ' <span class="pill warn">cordoned</span>' : ""
            }`
          : '<span class="muted">not joined</span>';
        const osCell = os
          ? `${dot(novaUp(os))}<span class="muted">${esc(os.state || os.status || "—")}</span>`
          : '<span class="muted">—</span>';
        return `<tr class="${sel.trim()}" data-pf-sel="${esc(name)}">
          <td>
            <div class="om-name">${esc(name)}</div>
            <div class="om-roles">${roleChips(info.roles)}</div>
          </td>
          <td class="muted">${esc(hw)}</td>
          <td class="muted om-ips">${esc(ips || "—")}</td>
          <td class="om-cell-status">${talosCell}</td>
          <td class="om-cell-status">${k8sCell}</td>
          <td class="om-cell-status">${osCell}</td>
          <td class="om-cell-menu">${menuHtml(info)}</td>
        </tr>`;
      })
      .join("")}</tbody>
  </table>`;
}

function inspTabState(tab) {
  const st = inspState[tab];
  if (!st || st.for !== selected) return null;
  return st;
}

function inspPreFrom(st, emptyMsg) {
  if (!st) return `<div class="muted">${esc(emptyMsg)}</div>`;
  if (st.loading) return `<div class="muted">Loading…</div>`;
  const d = st.data && typeof st.data === "object" ? st.data : null;
  if (d && d.ok === false && !(d.text || d.yaml)) {
    return `<div class="error">${esc(d.error || "unavailable")}</div>`;
  }
  const text = (d && (d.text || d.yaml || d.error)) || "(empty)";
  return `<pre class="lc-logs-pre om-logs">${esc(text)}</pre>`;
}

function inspSplit(title, text, err) {
  if (!text && err) return `<div class="om-res-block"><h4>${esc(title)}</h4><div class="error">${esc(err)}</div></div>`;
  if (!text) return "";
  return `<div class="om-res-block"><h4>${esc(title)}</h4><pre class="lc-logs-pre om-logs">${esc(text)}</pre></div>`;
}

function inspOverview(info) {
  const kn = info.kubernetes && typeof info.kubernetes === "object" ? info.kubernetes : null;
  const os = info.openstack && typeof info.openstack === "object" ? info.openstack : {};
  const talos = info.talos && typeof info.talos === "object" ? info.talos : {};
  const copy = (label, value) =>
    value
      ? `<button type="button" class="om-copy" data-copy="${esc(value)}" title="Copy ${esc(label)}">${esc(label)} <code>${esc(
          value
        )}</code></button>`
      : "";
  const chk = (key, label) =>
    `<label><input type="checkbox" data-reset-opt="${key}"${resetOpts[key] ? " checked" : ""} /> ${esc(label)}</label>`;
  return `<div class="os-kv">
      <div><span class="k">Inventory</span> ${esc(info.name || "—")}</div>
      <div><span class="k">Roles</span> ${roleChips(info.roles)}</div>
      <div><span class="k">Private IP</span> ${copy("private", info.private_ip) || "—"}</div>
      <div><span class="k">Public IP</span> ${copy("public", info.public_ip) || "—"}</div>
      <div><span class="k">Talos</span> ${dot(talos.reachable === true)} ${esc(talos.version || talos.error || "—")}</div>
      <div><span class="k">Kubernetes</span> ${
        kn
          ? `${dot(k8sReady(kn))} <code>${esc(kn.name || "—")}</code> ${esc(kn.status || "")} ${esc(kn.version || "")}`
          : "not joined"
      }</div>
      <div><span class="k">Hardware</span> ${
        kn ? esc([kn.cpu_capacity ? `${kn.cpu_capacity} CPU` : "", kn.mem_gi != null ? `${kn.mem_gi} GiB` : ""].filter(Boolean).join(" · ") || "—") : "—"
      }</div>
      <div><span class="k">Nova</span> ${os && os.host ? `${esc(os.host)} ${esc(os.state || "")}` : "—"}</div>
    </div>
    ${
      canRun()
        ? `<div class="om-insp-actions">
            <button type="button" class="secondary btn-sm" data-talos-reboot data-name="${esc(info.name)}">Reboot</button>
            <button type="button" class="secondary btn-sm" data-talos-upgrade data-name="${esc(info.name)}"${
              talos.version ? ` data-version="${esc(talos.version)}"` : ""
            }>Upgrade Talos</button>
            <button type="button" class="secondary btn-sm" data-talos-shutdown data-name="${esc(info.name)}">Shutdown</button>
            ${kn && kn.name ? k8sNodeActionsHtml(kn.name, kn, false) : ""}
          </div>
          <div class="om-insp-actions om-reset-opts">
            ${chk("graceful", "graceful etcd leave")}
            ${chk("reboot", "reboot after reset")}
            ${chk("wipe", "wipe system disk")}
            <button type="button" class="danger btn-sm" data-talos-reset data-name="${esc(info.name)}">Reset</button>
          </div>`
        : ""
    }`;
}

function inspLogs() {
  const opts = LOG_SERVICES.map(
    (s) => `<option value="${esc(s)}"${logService === s ? " selected" : ""}>${esc(s === "dmesg" ? "kernel (dmesg)" : s)}</option>`
  ).join("");
  const text = logsFor === selected ? logsText : "Pick a service and load logs.";
  return `<div class="om-insp-toolbar">
      <label>Service <select id="pf-log-svc">${opts}</select></label>
      <button type="button" class="secondary btn-sm" data-talos-logs-load data-name="${esc(selected)}">Load</button>
    </div>
    <pre class="lc-logs-pre om-logs" id="pf-dmesg">${esc(text)}</pre>`;
}

function inspServices() {
  if (servicesFor !== selected) {
    return `<div class="muted">Open services to query Talos on this node.</div>`;
  }
  if (!servicesState) return `<div class="muted">Loading services…</div>`;
  if (servicesState.error && !(servicesState.services || []).length) {
    return `<div class="error">${esc(servicesState.error)}</div>`;
  }
  const rows = Array.isArray(servicesState.services) ? servicesState.services : [];
  const acts = (id) =>
    canRun() && id
      ? ["start", "stop", "restart"]
          .map(
            (a) =>
              `<button type="button" class="secondary btn-sm" data-talos-svc-act="${a}" data-svc="${esc(id)}" data-name="${esc(
                selected
              )}">${a}</button>`
          )
          .join(" ")
      : "";
  if (!rows.length) return `<pre class="lc-logs-pre om-logs">${esc(servicesState.text || "(empty)")}</pre>`;
  return `<table class="om-svc"><thead><tr><th>Service</th><th>State</th><th>Health</th><th>Last event</th>${
    canRun() ? "<th></th>" : ""
  }</tr></thead><tbody>${rows
    .map((s) => {
      const id = s.id || s.name || "";
      return `<tr>
                <td><code>${esc(id || "—")}</code></td>
                <td>${esc(s.state || "—")}</td>
                <td>${esc(s.health || "—")}</td>
                <td class="muted">${esc(s.last_event || s.event || "—")}</td>
                ${canRun() ? `<td class="om-insp-actions" style="margin:0">${acts(id)}</td>` : ""}
              </tr>`;
    })
    .join("")}</tbody></table>`;
}

function inspConfig() {
  const st = inspTabState("config");
  const fromApi = st && st.data && (st.data.yaml || st.data.text);
  const yaml = configFor === selected && configYaml ? configYaml : fromApi || "";
  const modeOpts = ["auto", "staged", "no-reboot", "reboot"]
    .map((m) => `<option value="${m}"${configMode === m ? " selected" : ""}>${m}</option>`)
    .join("");
  let status = "";
  if (!st || st.for !== selected) status = `<div class="muted">Open Config to load machineconfig.</div>`;
  else if (st.loading) status = `<div class="muted">Loading machineconfig…</div>`;
  else if (st.data && st.data.ok === false && !yaml) status = `<div class="error">${esc(st.data.error || "unavailable")}</div>`;
  return `${status}
    <div class="om-insp-toolbar">
      <label>Mode <select id="pf-mc-mode">${modeOpts}</select></label>
      ${
        canRun()
          ? `<button type="button" class="secondary btn-sm" data-talos-apply data-name="${esc(selected)}">Apply config</button>`
          : ""
      }
    </div>
    <textarea id="pf-mc-yaml" class="om-mc-yaml" spellcheck="false" wrap="off">${esc(yaml)}</textarea>`;
}

function inspEtcd() {
  const st = inspTabState("etcd");
  if (!st) return inspPreFrom(null, "Open Etcd to query members and status.");
  if (st.loading) return `<div class="muted">Loading etcd…</div>`;
  const d = st.data || {};
  if (d.ok === false && !d.members && !d.text) return `<div class="error">${esc(d.error || "unavailable")}</div>`;
  return `${inspSplit("Members", d.members || d.text || "", d.error)}${inspSplit("Status", d.status || "", d.status_error)}`;
}

function inspDisks() {
  const st = inspTabState("disks");
  if (!st) return inspPreFrom(null, "Open Disks to query Talos.");
  if (st.loading) return `<div class="muted">Loading disks…</div>`;
  const d = st.data || {};
  if (d.ok === false && !d.disks && !d.text) return `<div class="error">${esc(d.error || "unavailable")}</div>`;
  return `${inspSplit("Disks", d.disks || d.text || "", d.error)}${inspSplit("Discovered volumes", d.volumes || "", d.volumes_error)}`;
}

function inspResources() {
  const st = inspTabState("resources");
  if (!st) return inspPreFrom(null, "Open Resources to query memory, CPU, and network.");
  if (st.loading) return `<div class="muted">Loading resources…</div>`;
  const d = st.data || {};
  const results = d.results && typeof d.results === "object" ? d.results : null;
  if (results && Object.keys(results).length) {
    return Object.keys(results)
      .map((key) => {
        const item = results[key] || {};
        if (item.ok === false && !item.text) {
          return inspSplit(key, "", item.error || "unavailable");
        }
        return inspSplit(key, item.text || "", item.ok === false ? item.error : "");
      })
      .join("");
  }
  return inspPreFrom(st, "No resource output.");
}

function inspectorHtml(nodes) {
  const info = (nodes || []).find((n) => n && n.name === selected);
  if (!info) {
    return `<div class="om-inspector om-inspector-empty muted">Select a machine for health, logs, config, and day-2 actions.</div>`;
  }
  const tab = (id, label) =>
    `<button type="button" class="om-insp-tab${inspTab === id ? " active" : ""}" data-insp-tab="${id}">${esc(label)}</button>`;
  let body = "";
  if (inspTab === "logs") body = inspLogs();
  else if (inspTab === "services") body = inspServices();
  else if (inspTab === "health") body = inspPreFrom(inspTabState("health"), "Open Health to run talosctl health.");
  else if (inspTab === "etcd") body = inspEtcd();
  else if (inspTab === "config") body = inspConfig();
  else if (inspTab === "disks") body = inspDisks();
  else if (inspTab === "resources") body = inspResources();
  else if (inspTab === "events") body = inspPreFrom(inspTabState("events"), "Open Events to stream recent Talos events.");
  else if (inspTab === "containers") body = inspPreFrom(inspTabState("containers"), "Open Containers to list CRI / system containers.");
  else body = inspOverview(info);
  return `<div class="om-inspector" id="pf-detail">
    <div class="om-insp-head">
      <div>
        <div class="om-kicker">Machine</div>
        <div class="om-insp-name">${esc(info.name)}</div>
      </div>
      <div class="om-insp-tabs" role="tablist">
        ${tab("overview", "Overview")}
        ${tab("health", "Health")}
        ${tab("etcd", "Etcd")}
        ${tab("config", "Config")}
        ${tab("disks", "Disks")}
        ${tab("logs", "Logs")}
        ${tab("resources", "Resources")}
        ${tab("events", "Events")}
        ${tab("containers", "Containers")}
        ${tab("services", "Services")}
      </div>
    </div>
    ${body}
  </div>`;
}

export function platformCardHtml() {
  return `
  <div class="card span-12" id="pf-card">
    <div class="toolbar">
      <h2>Talos</h2>
      <span class="muted">Versions, Ready, logs, and upgrades.</span>
      <span id="pf-msg" class="muted"></span>
      <button class="secondary btn-sm" id="pf-refresh" type="button">Refresh</button>
    </div>
    <div id="pf-overview"></div>
    <div id="pf-filters"></div>
    <div id="pf-body" class="muted">Select an environment.</div>
    <div id="pf-inspector"></div>
  </div>`;
}

export function wirePlatformCard(getEnvId) {
  envIdGetter = typeof getEnvId === "function" ? getEnvId : null;
  const btn = document.getElementById("pf-refresh");
  if (btn && !btn.dataset.wired) {
    btn.dataset.wired = "1";
    btn.addEventListener("click", () => {
      const id = (envIdGetter && envIdGetter()) || activeEnvId;
      if (id) loadPlatformCard(id);
    });
  }
  const card = document.getElementById("pf-card");
  if (card && !card.dataset.wired) {
    card.dataset.wired = "1";
    card.addEventListener("click", onClick);
  }
  if (!document.body.dataset.pfMenuWired) {
    document.body.dataset.pfMenuWired = "1";
    document.addEventListener("click", (e) => {
      if (menuOpen && !e.target.closest(".om-menu-wrap")) {
        menuOpen = "";
        render();
      }
    });
  }
  startRefresh();
}

function platformActive() {
  const p = document.querySelector('.tab-panel[data-panel="platform"]');
  if (!(p && p.classList.contains("active"))) return false;
  const sub = p.querySelector('.ptab-panel[data-ppanel="machines"]');
  return !!(sub && sub.classList.contains("active"));
}

function startRefresh() {
  stopRefresh();
  refreshTimer = setInterval(() => {
    if (document.hidden || !platformActive()) return;
    const id = (envIdGetter && envIdGetter()) || activeEnvId;
    if (id) loadPlatformCard(id, { silent: true });
  }, REFRESH_MS);
}

function stopRefresh() {
  if (refreshTimer) {
    clearInterval(refreshTimer);
    refreshTimer = null;
  }
}

function harvestInspector() {
  const yaml = document.getElementById("pf-mc-yaml");
  if (yaml && selected) {
    configYaml = yaml.value;
    configFor = selected;
  }
  const mode = document.getElementById("pf-mc-mode");
  if (mode) configMode = mode.value || "auto";
  const svc = document.getElementById("pf-log-svc");
  if (svc) logService = svc.value || logService;
  document.querySelectorAll("[data-reset-opt]").forEach((el) => {
    const key = el.dataset.resetOpt;
    if (key) resetOpts[key] = !!el.checked;
  });
}

function render() {
  harvestInspector();
  const body = document.getElementById("pf-body");
  const overview = document.getElementById("pf-overview");
  const filters = document.getElementById("pf-filters");
  const inspector = document.getElementById("pf-inspector");
  if (!body || !cache) return;
  const nodes = talosNodes(cache.nodes || []);
  const view = Object.assign({}, cache, { nodes, cluster: null });
  body.classList.remove("muted");
  if (overview) overview.innerHTML = overviewHtml(view);
  if (filters) filters.innerHTML = filterHtml(nodes);
  const err = cache.error ? `<div class="hint muted">${esc(cache.error)}</div>` : "";
  body.innerHTML = rowsHtml(nodes) + err;
  if (inspector) inspector.innerHTML = inspectorHtml(nodes);
}

async function copyText(text) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      toast("Copied", "ok");
      return;
    }
  } catch {
    /* fall through */
  }
  toast("Copy failed — select the value", "bad");
}

async function downloadConfig(kind) {
  const envId = (envIdGetter && envIdGetter()) || activeEnvId;
  if (!envId) return;
  const filename = kind === "talosconfig" ? "talosconfig" : `${envName(envId) || "cluster"}-kubeconfig`;
  try {
    await downloadAuth(`/api/v1/environments/${encodeURIComponent(envId)}/access/${kind}`, filename);
  } catch (e) {
    toast(`Download failed: ${e && e.message ? e.message : "unavailable"}`, "error");
  }
}

async function loadLogs(envId, name) {
  logService = "dmesg";
  return loadLogsForService(envId, name, "dmesg");
}

async function loadServices(envId, name) {
  servicesFor = name;
  servicesState = null;
  inspTab = "services";
  selected = name;
  render();
  try {
    const d = await api(
      `/api/v1/environments/${encodeURIComponent(envId)}/platform/nodes/${encodeURIComponent(name)}/services`,
      { timeout: 25000 }
    );
    if (servicesFor !== name) return;
    servicesState = d && typeof d === "object" ? d : { text: "", services: [] };
  } catch (err) {
    if (servicesFor !== name) return;
    servicesState = { error: err && err.message ? err.message : "unavailable", services: [], text: "" };
  }
  render();
}

function nodePath(envId, name, suffix) {
  return `/api/v1/environments/${encodeURIComponent(envId)}/platform/nodes/${encodeURIComponent(name)}/${suffix}`;
}

async function loadLogsForService(envId, name, service) {
  logService = service || logService || "kubelet";
  logsFor = name;
  logsText = `Loading ${logService}…`;
  inspTab = "logs";
  selected = name;
  render();
  try {
    let d;
    if (logService === "dmesg") {
      d = await api(nodePath(envId, name, "dmesg"), { timeout: 25000 });
    } else {
      d = await api(`${nodePath(envId, name, "logs")}?service=${encodeURIComponent(logService)}`, { timeout: 25000 });
    }
    if (logsFor !== name) return;
    logsText = (d && (d.text || d.error)) || "(empty)";
  } catch (err) {
    if (logsFor !== name) return;
    logsText = err && err.message ? err.message : "unavailable";
  }
  render();
}

async function loadInspTab(envId, name, tab) {
  const pathTab = tab === "config" ? "machineconfig" : tab;
  inspTab = tab;
  selected = name;
  inspState[tab] = { for: name, loading: true, data: null };
  render();
  try {
    const d = await api(nodePath(envId, name, pathTab), { timeout: INSP_TIMEOUT[tab] || 25000 });
    if (inspState[tab] && inspState[tab].for !== name) return;
    inspState[tab] = { for: name, loading: false, data: d && typeof d === "object" ? d : { text: String(d || "") } };
    if (tab === "config") {
      const yaml = (d && (d.yaml || d.text)) || "";
      configYaml = yaml;
      configFor = name;
    }
  } catch (err) {
    if (inspState[tab] && inspState[tab].for !== name) return;
    inspState[tab] = {
      for: name,
      loading: false,
      data: { ok: false, error: err && err.message ? err.message : "unavailable", text: "" },
    };
  }
  render();
}

async function upgradeAll(envId) {
  const nodes = ((cache && cache.nodes) || []).filter((n) => n && n.name);
  if (!nodes.length) return;
  const image = (cache && cache.install_image) || (cache && cache.cluster && cache.cluster.install_image) || "";
  const names = nodes.map((n) => n.name);
  if (
    !window.confirm(
      `Upgrade Talos on ${nodes.length} machine(s) (${names.join(", ")}) to ${image || "the configured installer"}?\n\nEach node reboots into the new installer. Workloads restart.`
    )
  )
    return;
  const parallel = window.confirm(
    "All at once?\n\nOK = parallel (fastest; lab / this cluster).\nCancel = one at a time (keeps etcd quorum on control planes)."
  );
  const mode = parallel ? "parallel" : "sequential";
  const body = { mode, names };
  if (image) body.image = image;
  try {
    const d = await api(`/api/v1/environments/${encodeURIComponent(envId)}/platform/upgrade`, {
      method: "POST",
      timeout: parallel ? 360000 : Math.min(3600000, 300000 * Math.max(1, names.length)),
      body: JSON.stringify(body),
    });
    if (d && d.ok === false) toast(d.error || "upgrade failed", "error");
    else {
      const msg =
        (d && d.job_id && `job ${d.job_id}: ${d.operation || "upgrade"}`) ||
        (d && d.message) ||
        `upgrade ${mode} requested`;
      toast(msg, "ok");
    }
  } catch (err) {
    toast(err && err.message ? err.message : "upgrade failed", "error");
  }
  await loadPlatformCard(envId);
}

async function onClick(e) {
  const filterBtn = e.target.closest("[data-pf-filter]");
  if (filterBtn) {
    filter = filterBtn.dataset.pfFilter || "all";
    render();
    return;
  }
  const tabBtn = e.target.closest("[data-insp-tab]");
  if (tabBtn) {
    harvestInspector();
    const next = tabBtn.dataset.inspTab || "overview";
    const force = next === inspTab;
    inspTab = next;
    const envId = (envIdGetter && envIdGetter()) || activeEnvId;
    if (inspTab === "logs" && selected && envId && (force || logsFor !== selected)) loadLogsForService(envId, selected, logService);
    else if (inspTab === "services" && selected && envId && (force || servicesFor !== selected)) loadServices(envId, selected);
    else if (INSP_GET_TABS.includes(inspTab) && selected && envId) {
      const st = inspTabState(inspTab);
      if (force || !st || st.for !== selected) loadInspTab(envId, selected, inspTab);
      else render();
    } else render();
    return;
  }
  const logLoad = e.target.closest("[data-talos-logs-load]");
  if (logLoad) {
    const envId = (envIdGetter && envIdGetter()) || activeEnvId;
    const name = logLoad.dataset.name || selected;
    const sel = document.getElementById("pf-log-svc");
    if (sel) logService = sel.value || logService;
    if (envId && name) loadLogsForService(envId, name, logService);
    return;
  }
  const resetOpt = e.target.closest("[data-reset-opt]");
  if (resetOpt) {
    const key = resetOpt.dataset.resetOpt;
    if (key) resetOpts[key] = !!resetOpt.checked;
    return;
  }
  const copyBtn = e.target.closest("[data-copy]");
  if (copyBtn) {
    copyText(copyBtn.dataset.copy || "");
    return;
  }
  const dl = e.target.closest("[data-pf-dl]");
  if (dl) {
    downloadConfig(dl.dataset.pfDl);
    return;
  }
  const upAll = e.target.closest("[data-pf-upgrade-all]");
  if (upAll) {
    const envId = (envIdGetter && envIdGetter()) || activeEnvId;
    if (envId) upgradeAll(envId);
    return;
  }
  const menuBtn = e.target.closest("[data-pf-menu]");
  if (menuBtn) {
    const name = menuBtn.dataset.pfMenu || "";
    menuOpen = menuOpen === name ? "" : name;
    e.stopPropagation();
    render();
    return;
  }
  const row = e.target.closest("[data-pf-sel]");
  if (row && !e.target.closest("button") && !e.target.closest(".om-menu")) {
    selected = row.dataset.pfSel || "";
    inspTab = "overview";
    menuOpen = "";
    render();
    return;
  }
  const envId = (envIdGetter && envIdGetter()) || activeEnvId;
  if (!envId) return;
  const nodeAct = e.target.closest("[data-k8s-node-act]");
  if (nodeAct) {
    const action = nodeAct.dataset.k8sNodeAct;
    const k8sName = nodeAct.dataset.k8sName;
    if (!action || !k8sName) return;
    if (action === "drain") {
      if (!window.confirm(`Drain ${k8sName}? Pods will be evicted. DaemonSets are left in place.`)) return;
    } else if (action === "cordon") {
      if (!window.confirm(`Cordon ${k8sName}? New pods will not be scheduled on this node.`)) return;
    } else if (action === "uncordon") {
      if (!window.confirm(`Uncordon ${k8sName}?`)) return;
    } else {
      return;
    }
    let ok = true;
    try {
      const d = await api(
        `/api/v1/environments/${encodeURIComponent(envId)}/k8s/nodes/${encodeURIComponent(k8sName)}/${encodeURIComponent(action)}`,
        { method: "POST", timeout: 60000 }
      );
      if (d && d.ok === false) {
        toast(d.error || `${action} failed`, "error");
        ok = false;
      } else {
        toast((d && d.message) || "accepted", "ok");
      }
    } catch (err) {
      toast(err && err.message ? err.message : `${action} failed`, "error");
      ok = false;
    }
    if (ok) await loadPlatformCard(envId);
    return;
  }
  const reboot = e.target.closest("[data-talos-reboot]");
  if (reboot) {
    const name = reboot.dataset.name;
    if (!name || !window.confirm(`Reboot Talos node ${name}? Workloads on this machine will restart.`)) return;
    try {
      const d = await api(nodePath(envId, name, "reboot"), { method: "POST", timeout: 25000 });
      if (d && d.ok === false) toast(d.error || "reboot failed", "error");
      else toast((d && d.message) || "reboot requested", "ok");
    } catch (err) {
      toast(err && err.message ? err.message : "reboot failed", "error");
    }
    return;
  }
  const shutdown = e.target.closest("[data-talos-shutdown]");
  if (shutdown) {
    const name = shutdown.dataset.name;
    if (!name || !window.confirm(`Shut down Talos node ${name}? The machine will power off.`)) return;
    try {
      const d = await api(nodePath(envId, name, "shutdown"), { method: "POST", timeout: 25000 });
      if (d && d.ok === false) toast(d.error || "shutdown failed", "error");
      else toast((d && d.message) || "shutdown requested", "ok");
    } catch (err) {
      toast(err && err.message ? err.message : "shutdown failed", "error");
    }
    return;
  }
  const resetBtn = e.target.closest("[data-talos-reset]");
  if (resetBtn) {
    const name = resetBtn.dataset.name;
    if (!name) return;
    harvestInspector();
    const g = resetOpts.graceful ? "graceful etcd leave" : "no etcd leave";
    const r = resetOpts.reboot ? "reboot after reset" : "halt after reset";
    const w = resetOpts.wipe ? "wipe STATE/EPHEMERAL (system-disk)" : "no disk wipe";
    if (
      !window.confirm(
        `Reset Talos node ${name}?\n\n${g}. ${r}. ${w}.\n\nThis is destructive. The node leaves the cluster until you re-apply config.`
      )
    )
      return;
    try {
      const d = await api(nodePath(envId, name, "reset"), {
        method: "POST",
        timeout: 30000,
        body: JSON.stringify({
          graceful: !!resetOpts.graceful,
          reboot: !!resetOpts.reboot,
          wipe: !!resetOpts.wipe,
        }),
      });
      if (d && d.ok === false) toast(d.error || "reset failed", "error");
      else toast((d && d.message) || "reset requested", "ok");
    } catch (err) {
      toast(err && err.message ? err.message : "reset failed", "error");
    }
    return;
  }
  const applyBtn = e.target.closest("[data-talos-apply]");
  if (applyBtn) {
    const name = applyBtn.dataset.name || selected;
    if (!name) return;
    harvestInspector();
    const yaml = configYaml || "";
    if (!yaml.trim()) {
      toast("machineconfig YAML is empty", "error");
      return;
    }
    const mode = configMode || "auto";
    if (!window.confirm(`Apply machineconfig to ${name} (mode=${mode})?`)) return;
    try {
      const d = await api(nodePath(envId, name, "apply-config"), {
        method: "POST",
        timeout: 60000,
        body: JSON.stringify({ yaml, mode }),
      });
      if (d && d.ok === false) toast(d.error || "apply-config failed", "error");
      else toast((d && d.message) || "config applied", "ok");
    } catch (err) {
      toast(err && err.message ? err.message : "apply-config failed", "error");
    }
    return;
  }
  const svcAct = e.target.closest("[data-talos-svc-act]");
  if (svcAct) {
    const name = svcAct.dataset.name || selected;
    const svc = svcAct.dataset.svc;
    const action = svcAct.dataset.talosSvcAct;
    if (!name || !svc || !action) return;
    if (!window.confirm(`${action} ${svc} on ${name}?`)) return;
    try {
      const d = await api(nodePath(envId, name, `service/${encodeURIComponent(svc)}/${encodeURIComponent(action)}`), {
        method: "POST",
        timeout: 25000,
      });
      if (d && d.ok === false) toast(d.error || `${action} failed`, "error");
      else toast((d && d.message) || `${action} ${svc}`, "ok");
      if (d && d.ok !== false) await loadServices(envId, name);
    } catch (err) {
      toast(err && err.message ? err.message : `${action} failed`, "error");
    }
    return;
  }
  const upgrade = e.target.closest("[data-talos-upgrade]");
  if (upgrade) {
    const name = upgrade.dataset.name;
    if (!name) return;
    const row = ((cache && cache.nodes) || []).find((n) => n && n.name === name) || {};
    const version = upgrade.dataset.version || (row.talos && row.talos.version) || "";
    const image = (cache && cache.install_image) || "";
    const target = image || "the configured installer";
    const current = version ? ` (currently ${version})` : "";
    if (
      !window.confirm(
        `Upgrade Talos on ${name}${current} to ${target}? Node reboots into the new installer. Workloads on this machine restart.`
      )
    )
      return;
    const opts = { method: "POST", timeout: 360000 };
    if (image) opts.body = JSON.stringify({ image });
    let ok = true;
    try {
      const d = await api(
        `/api/v1/environments/${encodeURIComponent(envId)}/platform/nodes/${encodeURIComponent(name)}/upgrade`,
        opts
      );
      if (d && d.ok === false) {
        toast(d.error || "upgrade failed", "error");
        ok = false;
      } else {
        const msg =
          (d && d.job_id && `job ${d.job_id}: ${d.operation || "upgrade"}`) ||
          (d && d.message) ||
          "upgrade requested";
        toast(msg, "ok");
      }
    } catch (err) {
      toast(err && err.message ? err.message : "upgrade failed", "error");
      ok = false;
    }
    if (ok) await loadPlatformCard(envId);
    return;
  }
  const dmesg = e.target.closest("[data-talos-dmesg]");
  if (dmesg) {
    menuOpen = "";
    logService = "dmesg";
    loadLogsForService(envId, dmesg.dataset.name, "dmesg");
    return;
  }
  const svc = e.target.closest("[data-talos-services]");
  if (svc) {
    menuOpen = "";
    loadServices(envId, svc.dataset.name);
  }
}

export async function loadPlatformCard(envId, { silent = false } = {}) {
  activeEnvId = envId || "";
  const body = document.getElementById("pf-body");
  const msg = document.getElementById("pf-msg");
  if (!body) return;
  if (!activeEnvId) {
    if (msg) msg.textContent = "";
    body.classList.add("muted");
    body.innerHTML = "Select an environment.";
    cache = null;
    const overview = document.getElementById("pf-overview");
    const filters = document.getElementById("pf-filters");
    const inspector = document.getElementById("pf-inspector");
    if (overview) overview.innerHTML = "";
    if (filters) filters.innerHTML = "";
    if (inspector) inspector.innerHTML = "";
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
  inflight = api(`/api/v1/environments/${encodeURIComponent(activeEnvId)}/platform`, { timeout: 25000 });
  let d;
  try {
    d = await inflight;
  } catch (e) {
    if (activeEnvId !== envId) return;
    if (live.platform && live.envId === envId) {
      if (msg) msg.textContent = "";
      cache = live.platform;
      render();
      return;
    }
    if (msg) msg.textContent = "";
    body.classList.remove("muted");
    body.innerHTML =
      e && e.status === 404
        ? `<div class="muted">Machines join API not deployed yet.</div>`
        : `<div class="muted">Unavailable — ${esc(e.message || "error")}</div>`;
    return;
  } finally {
    inflight = null;
  }
  if (activeEnvId !== envId) return;
  if (msg) msg.textContent = "";
  const next = d && typeof d === "object" ? d : { nodes: [] };
  const nextN = Array.isArray(next.nodes) ? next.nodes.length : 0;
  const prevN = cache && Array.isArray(cache.nodes) ? cache.nodes.length : 0;
  if (nextN || !prevN) cache = next;
  bindLiveEnv(activeEnvId);
  applyLive({ platform: cache });
  if (!selected && Array.isArray(cache.nodes) && cache.nodes[0] && cache.nodes[0].name) {
    selected = cache.nodes[0].name;
  }
  render();
}

export function destroyPlatformCard() {
  stopRefresh();
  envIdGetter = null;
  activeEnvId = "";
  cache = null;
  selected = "";
  filter = "all";
  inspTab = "overview";
  menuOpen = "";
  inflight = null;
  logsText = "";
  logsFor = "";
  servicesState = null;
  servicesFor = "";
  inspState = {};
  logService = "kubelet";
  configYaml = "";
  configFor = "";
  configMode = "auto";
  resetOpts = { graceful: true, reboot: true, wipe: true };
}
