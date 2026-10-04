// pages/environment_detail.js — tabbed admin layout.
// Overview is the live environment tree (internal tab id: workflow).
import { api, esc, fmtTime, toast } from "../api.js";
import { store, loadEnvs, envOptionsHtml, canRun, isDemoEnv } from "../store.js";
import { configCardHtml, wireConfigCard, loadConfigCard, destroyConfigCard } from "./environment_config.js";
import { serversCardHtml, wireServersCard, loadServersCard, destroyServersCard } from "./environment_servers.js?v=ls18";
import { baremetalCardHtml, wireBaremetalCard, loadBaremetalCard, destroyBaremetalCard } from "./environment_baremetal.js";
import { discoveryCardHtml, wireDiscoveryCard, loadDiscoveryCard, destroyDiscoveryCard } from "./environment_discovery.js";
import { pxeCardHtml, wirePxeCard, loadPxeCard, destroyPxeCard } from "./environment_pxe.js";
import { destroyWorkflowCard } from "./environment_workflow.js?v=slot2";
import { platformCardHtml, wirePlatformCard, loadPlatformCard, destroyPlatformCard } from "./environment_platform.js?v=tu2";
import { clusterCardHtml, wireClusterCard, loadClusterCard, destroyClusterCard } from "./environment_cluster.js";
import { wireOpenstackCard, destroyOpenstackCard } from "./environment_openstack.js";
import { cloudCardHtml, wireCloudCard, loadCloudCard, destroyCloudCard } from "./environment_cloud.js";
import { progressCardHtml, wireProgressCard, loadProgressCard, destroyProgressCard } from "./environment_progress.js";
import { deployMapHtml, wireDeployMap, loadDeployMap, destroyDeployMap } from "./environment_deploy_map.js?v=ls24";
import { componentsCardHtml, wireComponentsCard, loadComponentsCard, destroyComponentsCard } from "./environment_components.js";
import { terminalCardHtml, wireTerminalCard, loadTerminalCard, destroyTerminalCard } from "./environment_terminal.js";
import { sshKeysCardHtml, wireSshKeysCard, loadSshKeysCard } from "./environment_sshkeys.js";
import { agentsCardHtml, wireAgentsCard, loadAgentsCard, destroyAgentsCard } from "./environment_agents.js";
import { reachCardHtml, wireReachCard, loadReachCard, destroyReachCard } from "./environment_reach.js";
import { hostsCardHtml, wireHostsCard, loadHostsCard, destroyHostsCard } from "./environment_hosts.js";
import { appsCardHtml, wireAppsCard, loadAppsCard, destroyAppsCard } from "./environment_apps.js?v=slot2";
import { observeCardHtml, wireObserveCard, loadObserveCard, destroyObserveCard } from "./environment_observe.js";
import { setBreadcrumbs } from "../components/breadcrumbs.js";

export const title = "Environment Detail";

let envId = "";
let activePtab = "ovh";

function showPageLoading() {
  removePageLoading();
  const page = document.getElementById("page");
  if (!page) return;
  const overlay = document.createElement("div");
  overlay.id = "page-loading";
  overlay.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;z-index:1000;color:var(--fg,#ccc);font-size:.9rem;gap:.6rem";
  overlay.innerHTML = `<span style="display:inline-block;width:1.2rem;height:1.2rem;border:2px solid rgba(255,255,255,.2);border-top-color:var(--accent,#4a9eff);border-radius:50%;animation:spin .6s linear infinite"></span> Loading environment…`;
  const style = document.createElement("style");
  style.textContent = "@keyframes spin{to{transform:rotate(360deg)}}";
  overlay.appendChild(style);
  page.appendChild(overlay);
}

function removePageLoading() {
  const el = document.getElementById("page-loading");
  if (el) el.remove();
}

function unavail(note) {
  return `<div class="muted">Unavailable${note ? " — " + esc(note) : ""}.</div>`;
}

function errorNote(err) {
  return `<div class="error">${esc(typeof err === "string" ? err : "section reported an error")}</div>`;
}

// ---------- section renderers ----------

function identityHtml(d) {
  const env = d && d.environment && typeof d.environment === "object" ? d.environment : {};
  const listed = store.envs.find((e) => e.id === envId);
  const demo = isDemoEnv(env) || isDemoEnv(listed);
  const pobj = d && d.provider && typeof d.provider === "object" ? d.provider : { provider: d ? d.provider : null };
  const provider = pobj.provider || null;
  const rows = [
    ["ID", env.id || envId || "—"],
    ["Region", env.region || "—"],
    ["Tier", env.tier || "—"],
    ["Provider", provider || "—"],
    ["Description", env.description || "—"],
  ];
  return `
    <div class="detail-header">
      <div>
        <h2>${esc(env.name || listed?.name || envId || "Environment")}${demo ? ' <span class="pill warn">walkthrough</span> <span class="pill">sample</span>' : ""}</h2>
        <div class="muted" style="font-size:.78rem">descriptor v${esc(d && d.descriptor_version != null ? d.descriptor_version : "?")} · generated ${esc(fmtTime(d && d.generated_at)) || "—"}</div>
      </div>
      ${provider ? `<span class="badge">${esc(provider)}</span>` : ""}
    </div>
    <div class="detail-grid" style="margin-top:.75rem">
      ${rows.map(([k, v]) => `<div><div class="k">${k}</div>${esc(v)}</div>`).join("")}
    </div>
    ${pobj.error ? `<div class="hint muted">provider detection: ${esc(pobj.error)}</div>` : ""}`;
}

function overlayLink(rel, label) {
  return `<button type="button" class="linkish overlay-file" data-overlay-path="${esc(rel)}"><code>${esc(label)}</code></button>`;
}

function helmOverridesHtml(ho) {
  if (!ho || typeof ho !== "object") return unavail("no helm override data");
  if (ho.error) return errorNote(ho.error);
  const services = ho.services && typeof ho.services === "object" ? ho.services : {};
  const baseDefaults = ho.base_defaults && typeof ho.base_defaults === "object" ? ho.base_defaults : {};
  const fileList = (v) =>
    Array.isArray(v) ? v : v && typeof v === "object" && Array.isArray(v.files) ? v.files : [];
  const globals = Array.isArray(ho.global_overrides)
    ? ho.global_overrides
    : fileList(services.global_overrides);
  const names = Object.keys(services).filter((n) => n !== "global_overrides").sort();
  if (!names.length && !globals.length) return unavail("no helm overrides found");
  const rows = names
    .map((name) => {
      const files = fileList(services[name]);
      const yeFile = `${name}-helm-overrides.yaml`;
      const extra = files.filter((f) => f !== yeFile);
      const extraHtml = extra.length
        ? extra.map((f) => overlayLink(`helm-configs/${name}/${f}`, f)).join("<br>")
        : '<span class="muted">—</span>';
      return `<tr>
        <td>${overlayLink(`helm-configs/${name}/${yeFile}`, name)}</td>
        <td>${extraHtml}</td>
        <td>${
          baseDefaults[name] === true
            ? '<span class="pill ok">base defaults</span>'
            : '<span class="muted">—</span>'
        }</td>
      </tr>`;
    })
    .join("");
  const table = names.length
    ? `<table>
      <thead><tr><th>Service (ye)</th><th>Extra files</th><th>Base defaults</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`
    : "";
  const globalsHtml = globals.length
    ? `<h3 style="font-size:.9rem;margin:.75rem 0 .3rem">Global overrides</h3><div>${globals
        .map((f) => overlayLink(`helm-configs/global_overrides/${f}`, f))
        .join("<br>")}</div>`
    : "";
  return `<div class="muted" style="font-size:.78rem;margin-bottom:.4rem">Open a service to edit the <strong>merged</strong> result (chart values → base-helm-configs → local). Save writes only the delta to helm-configs — same as <code>ye</code>.</div>` + table + globalsHtml;
}

function kustomizeHtml(overlays) {
  if (!Array.isArray(overlays)) return unavail("no kustomize overlay data");
  if (!overlays.length) return '<div class="muted">No kustomize overlays.</div>';
  return `<div>${overlays
    .map((o) => overlayLink(`kustomize/${o}/overlay/kustomization.yaml`, o))
    .join(" ")}</div>`;
}

function gatewayHtml(gw) {
  if (!gw || typeof gw !== "object") return unavail("no gateway data");
  if (gw.error) return errorNote(gw.error);
  const sections = [
    ["Gateway API", Array.isArray(gw.gateway_api) ? gw.gateway_api : [], "gateway-api"],
    ["MetalLB", Array.isArray(gw.metallb) ? gw.metallb : [], "manifests/metallb"],
  ];
  if (!sections.some(([, files]) => files.length)) return '<div class="muted">No gateway config files.</div>';
  return sections
    .filter(([, files]) => files.length)
    .map(
      ([label, files, prefix]) => `<h3 style="font-size:.9rem;margin:.25rem 0 .3rem">${label}</h3>
      <div>${files.map((f) => overlayLink(`${prefix}/${f}`, f)).join("<br>")}</div>`
    )
    .join("");
}

// ---------- tab switching ----------

const PTAB_LEAD = {
  ovh: "Metal hosts — BMC, NICs, and the boxes under the cloud.",
  machines: "Talos cluster — versions, Ready machines, logs, services, and day-2 upgrades.",
  kubernetes: "Workloads, pods, and nodes — scale, restart, drain, describe.",
  openstack: "Instances, images, volumes, networks, and in-portal consoles.",
};

function envTabHash(tabName, ptab) {
  const q = new URLSearchParams();
  q.set("tab", tabName);
  if (tabName === "platform") q.set("ptab", ptab || activePtab || "ovh");
  return `#/environment_detail/${encodeURIComponent(envId)}?${q}`;
}

function switchSettingsSubtab(name) {
  const id = name || "config";
  document.querySelectorAll("#stab-bar [data-stab]").forEach((t) => t.classList.toggle("active", t.dataset.stab === id));
  document.querySelectorAll("[data-spanel]").forEach((p) => p.classList.toggle("active", p.dataset.spanel === id));
}

function switchTab(tabName, ptab, opts = {}) {
  const alias = { config: "settings", apps: "settings", inventory: "settings", expert: "settings" };
  const stab = { config: "config", apps: "apps", inventory: "access", expert: "expert" };
  const resolved = alias[tabName] || tabName;
  activeTab = resolved;
  const root = document.querySelector(".detail-page");
  if (!root) return;
  root.querySelectorAll(".tab-bar > .tab[data-tab]").forEach((t) => t.classList.remove("active"));
  root.querySelectorAll(".tab-panel[data-panel]").forEach((p) => p.classList.remove("active"));
  const tabBtn = root.querySelector(`.tab-bar > .tab[data-tab="${resolved}"]`);
  const panel = root.querySelector(`.tab-panel[data-panel="${resolved}"]`);
  if (tabBtn) tabBtn.classList.add("active");
  if (panel) panel.classList.add("active");
  if (resolved === "platform") switchPlatformSubtab(ptab || activePtab);
  else if (resolved === "settings") switchSettingsSubtab(stab[tabName] || ptab || "config");
  loadTab(resolved);
  if (resolved === "settings") loadDescriptor().catch(() => {});
  if (!opts.silent && envId) {
    history.replaceState(null, "", envTabHash(resolved, ptab));
    window.dispatchEvent(new CustomEvent("gsc-nav-sync"));
  }
}

export function applyQuery({ param, query }) {
  if (!param || param !== envId) return false;
  const tab = (query && query.get("tab")) || "workflow";
  const ptab = query && query.get("ptab");
  switchTab(tab, tab === "platform" ? (ptab || "ovh") : ptab || undefined, { silent: true });
  return true;
}

function switchPlatformSubtab(name) {
  const id = name && PTAB_LEAD[name] ? name : "ovh";
  activePtab = id;
  const panel = document.getElementById("panel-platform");
  if (!panel) return;
  panel.querySelectorAll(".ptab").forEach((t) => t.classList.toggle("active", t.dataset.ptab === id));
  panel.querySelectorAll(".ptab-panel").forEach((p) => p.classList.toggle("active", p.dataset.ppanel === id));
  const lead = document.getElementById("ptab-lead");
  if (lead) lead.textContent = PTAB_LEAD[id] || "";
  loadPlatformLayer(id);
}

function loadPlatformLayer(name) {
  if (!envId) return;
  if (name === "ovh") loadServersCard(envId);
  else if (name === "machines") loadPlatformCard(envId, { silent: tabLoads.has("platform:machines") });
  else if (name === "kubernetes") loadClusterCard(envId);
  else if (name === "openstack") loadCloudCard(envId, { silent: tabLoads.has("platform:openstack") });
  tabLoads.set("platform", true);
  tabLoads.set(`platform:${name}`, true);
}

// Lazy-load cards for each tab on first visit. Cards stay in DOM after first load.
const tabLoads = new Map();
// The tab the user is viewing (so env-switch / Refresh reload the visible tab,
// not a hardcoded one).
let activeTab = "workflow";
const TAB_LOADERS = {
  workflow: [loadDeployMap, loadProgressCard, loadTerminalCard],
  observe: [loadObserveCard],
  settings: [loadConfigCard, loadAppsCard, loadSshKeysCard, loadAgentsCard, loadReachCard, loadHostsCard, loadBaremetalCard, loadDiscoveryCard, loadPxeCard, loadComponentsCard],
  inventory: [loadSshKeysCard, loadAgentsCard, loadReachCard, loadHostsCard, loadBaremetalCard, loadDiscoveryCard, loadPxeCard],
  config: [loadConfigCard],
  apps: [loadAppsCard],
  expert: [loadComponentsCard],
};
function loadTab(tabName) {
  if (tabName === "platform") {
    loadPlatformLayer(activePtab);
    return;
  }
  const loaders = TAB_LOADERS[tabName];
  if (!loaders) return;
  tabLoads.set(tabName, true);
  loaders.forEach((fn) => fn(envId));
}

// ---------- page ----------

export async function render(root, { param, query } = {}) {
  showPageLoading();
  activeTab = (query && query.get("tab")) || "workflow";
  activePtab = (query && query.get("ptab")) || "ovh";
  tabLoads.clear();
  if (!store.envs.length) await loadEnvs().catch(() => {});
  if (param) envId = param;

  root.innerHTML = `
  <style>
    .detail-page { padding: 0; }
    .tab-panel { display: none; padding: .75rem 0; }
    .tab-panel.active { display: block; }
    .ptab-panel { display: none; }
    .ptab-panel.active { display: block; }
    button.linkish {
      background: none; border: 0; padding: 0; margin: 0;
      color: var(--link, #93c5fd); cursor: pointer; font: inherit;
      text-decoration: underline; text-underline-offset: 2px;
    }
    .overlay-file { font-size: .82rem; }
    textarea.overlay-editor {
      width: 100%; min-height: 22rem; resize: vertical;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: .78rem; line-height: 1.4;
      background: var(--inset, #070b14); color: var(--text, #e7eefc);
      border: 1px solid var(--border, #243049); border-radius: .4rem;
      padding: .55rem .65rem;
    }
    pre.overlay-base {
      margin: .55rem 0 0; max-height: 16rem; overflow: auto;
      font-size: .75rem; background: var(--inset, #070b14);
      border: 1px dashed var(--border, #243049); border-radius: .4rem;
      padding: .55rem .65rem; white-space: pre-wrap;
    }

    /* 2-column card grid inside panels */
    .env-grid {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: .75rem;
    }
    .env-grid.full { grid-template-columns: 1fr; }
    @media (max-width: 960px) {
      .env-grid { grid-template-columns: 1fr; }
    }
  </style>
  <div class="detail-page">
    <div id="demo-banner" class="demo-banner hidden" style="display:none;align-items:center;gap:.55rem;flex-wrap:wrap;padding:.45rem .75rem;border-bottom:1px solid var(--warn-border,#854d0e);background:var(--warn-bg,#1f1705);font-size:.82rem">
      <span class="pill warn">walkthrough</span>
      <span class="pill">sample</span>
      <span>This is a sample full deployment so you can click around. It is not your metal. Create a new environment to provision.</span>
    </div>
    <div id="env-health" class="env-health">${
      envId
        ? '<span class="muted">Overview loading…</span>'
        : '<span class="muted">Select an environment.</span>'
    }</div>
    <div class="tab-bar">
      <button class="tab active" data-tab="workflow">Overview</button>
      <button class="tab" data-tab="platform">Platform</button>
      <button class="tab" data-tab="observe">Observe</button>
      <button class="tab" data-tab="settings">Settings</button>
      <div class="tab-spacer"></div>
      <select id="desc-env">${envOptionsHtml(envId, { includeNone: true, noneLabel: "Environment" })}</select>
      <button class="secondary btn-sm" id="desc-refresh" type="button">Refresh</button>
      <span id="desc-msg" class="muted"></span>
    </div>

    <!-- ═══ OVERVIEW TAB ═══ -->
    <div class="tab-panel active" data-panel="workflow" id="panel-workflow">
      ${deployMapHtml()}
      <div hidden>${progressCardHtml()}</div>
    </div>
    ${terminalCardHtml()}

    <!-- ═══ SETTINGS (config / apps / access / expert) ═══ -->
    <div class="tab-panel" data-panel="settings" id="panel-settings">
      <div class="ptab-bar" id="stab-bar" role="tablist" aria-label="Settings">
        <button type="button" class="ptab active" data-stab="config">Config</button>
        <button type="button" class="ptab" data-stab="apps">Apps</button>
        <button type="button" class="ptab" data-stab="access">Access</button>
        <button type="button" class="ptab" data-stab="expert">Expert</button>
      </div>
    <div class="ptab-panel" data-spanel="access" id="panel-inventory">
      <div class="env-grid full">
        ${sshKeysCardHtml()}
      </div>
      <div class="card" style="margin-top:.75rem">
        <div class="toolbar"><h2>Dedicated servers</h2></div>
        <div class="muted">Metal hosts, NICs, and BMC live on
          <button type="button" class="linkish" data-goto-ptab="ovh">Platform → Hosts</button>.
        </div>
      </div>
      <div class="env-grid" style="margin-top:.75rem">
        ${agentsCardHtml()}
        ${reachCardHtml()}
        ${hostsCardHtml()}
      </div>
      <details class="card" style="margin-top:.75rem">
        <summary>Bare metal provisioning (optional)</summary>
        <div class="env-grid" style="margin-top:.5rem">
          ${baremetalCardHtml()}
          ${pxeCardHtml()}
          ${discoveryCardHtml()}
        </div>
      </details>
    </div>

    <div class="ptab-panel active" data-spanel="config" id="panel-config">
      <div class="env-grid full">
        ${configCardHtml()}
      </div>
    </div>

    <div class="ptab-panel" data-spanel="apps" id="panel-apps">
      <div class="env-grid full">
        ${appsCardHtml()}
      </div>
    </div>
    </div>

    <!-- ═══ OBSERVE TAB ═══ -->
    <div class="tab-panel" data-panel="observe" id="panel-observe">
      <div class="env-grid full">
        ${observeCardHtml()}
      </div>
    </div>

    <!-- ═══ PLATFORM TAB (metal / Talos / Kubernetes / OpenStack) ═══ -->
    <div class="tab-panel" data-panel="platform" id="panel-platform">
      <div class="ptab-bar" role="tablist" aria-label="Platform layers">
        <button type="button" class="ptab active" data-ptab="ovh" role="tab">
          <span class="ptab-kicker">Infrastructure</span>
          Hosts
        </button>
        <button type="button" class="ptab" data-ptab="machines" role="tab">
          <span class="ptab-kicker">Talos</span>
          Machines
        </button>
        <button type="button" class="ptab" data-ptab="kubernetes" role="tab">
          <span class="ptab-kicker">Orchestration</span>
          Kubernetes
        </button>
        <button type="button" class="ptab" data-ptab="openstack" role="tab">
          <span class="ptab-kicker">Cloud</span>
          OpenStack
        </button>
      </div>
      <p class="ptab-lead" id="ptab-lead">${PTAB_LEAD.ovh}</p>
      <div class="ptab-panel active" data-ppanel="ovh">
        <div class="env-grid full">
          ${serversCardHtml()}
        </div>
      </div>
      <div class="ptab-panel" data-ppanel="machines">
        <div class="env-grid full">
          ${platformCardHtml()}
        </div>
      </div>
      <div class="ptab-panel" data-ppanel="kubernetes">
        <div class="env-grid full">
          ${clusterCardHtml()}
        </div>
      </div>
      <div class="ptab-panel" data-ppanel="openstack">
        <div class="env-grid full">
          ${cloudCardHtml()}
        </div>
      </div>
    </div>

    <div class="ptab-panel" data-spanel="expert" id="panel-expert">
      <div class="env-grid full">
        <div class="card">
          <div class="toolbar">
            <h2>Environment descriptor</h2>
          </div>
          <div id="desc-err"></div>
          <div id="desc-identity" class="muted">${envId ? "Loading…" : "Select an environment."}</div>
        </div>
      </div>
      ${componentsCardHtml()}
      <div class="env-grid" style="margin-top:.75rem">
        <div class="card">
          <div class="toolbar"><h2>Helm overrides</h2></div>
          <div id="desc-helm" class="muted">—</div>
        </div>
        <div class="card">
          <div class="toolbar"><h2>Kustomize overlays</h2></div>
          <div id="desc-kustomize" class="muted">—</div>
        </div>
        <div class="card">
          <div class="toolbar"><h2>Gateway</h2></div>
          <div id="desc-gateway" class="muted">—</div>
        </div>
      </div>
      <div class="card" id="desc-overlay-card" hidden style="margin-top:.75rem">
        <div class="toolbar">
          <h2 id="desc-overlay-title">Override</h2>
          <span id="desc-overlay-msg" class="muted"></span>
          <button type="button" class="secondary btn-sm" id="desc-overlay-chart" hidden>Show chart</button>
          <button type="button" class="secondary btn-sm" id="desc-overlay-base" hidden>Show base</button>
          <button type="button" class="secondary btn-sm" id="desc-overlay-local" hidden>Show local</button>
          <button type="button" class="btn-sm" id="desc-overlay-save" hidden>Save</button>
          <button type="button" class="secondary btn-sm" id="desc-overlay-close">Close</button>
        </div>
        <div id="desc-overlay-note" class="muted" style="font-size:.78rem;margin-bottom:.4rem"></div>
        <textarea id="desc-overlay-text" class="overlay-editor" spellcheck="false" wrap="off"></textarea>
        <pre id="desc-overlay-base-pre" class="overlay-base" hidden></pre>
      </div>
      <details class="card" style="margin-top:.75rem">
        <summary>Raw descriptor JSON</summary>
        <pre class="log-inline" id="desc-raw"></pre>
      </details>
    </div>

    <div id="wf-mod-park" hidden></div>
  </div>`;

  // Tab switching
  root.querySelector(".tab-bar").addEventListener("click", (e) => {
    const tab = e.target.closest(".tab[data-tab]");
    if (!tab) return;
    switchTab(tab.dataset.tab);
  });
  const ptabBar = root.querySelector("#panel-platform .ptab-bar");
  if (ptabBar) {
    ptabBar.addEventListener("click", (e) => {
      const tab = e.target.closest(".ptab[data-ptab]");
      if (!tab) return;
      switchTab("platform", tab.dataset.ptab);
    });
  }
  const stabBar = document.getElementById("stab-bar");
  if (stabBar) {
    stabBar.addEventListener("click", (e) => {
      const tab = e.target.closest("[data-stab]");
      if (!tab) return;
      switchSettingsSubtab(tab.dataset.stab);
    });
  }
  root.addEventListener("click", (e) => {
    const goTab = e.target.closest("[data-goto-tab]");
    if (goTab) {
      switchTab(goTab.dataset.gotoTab);
      return;
    }
    const go = e.target.closest("[data-goto-ptab]");
    if (!go) return;
    switchTab("platform", go.dataset.gotoPtab);
  });

  document.getElementById("desc-env").addEventListener("change", (e) => {
    envId = e.target.value;
    history.replaceState(null, "", envId ? envTabHash(activeTab || "workflow") : "#/fleet");
    window.dispatchEvent(new CustomEvent("gsc-nav-sync"));
    loadAll();
  });
  document.getElementById("desc-refresh").addEventListener("click", () => loadAll());

  // Wire all cards (event listeners, etc.)
  wirePlatformCard(() => envId);
  wireClusterCard(() => envId, { onProblemsClick: () => switchTab("platform", "kubernetes") });
  wireOpenstackCard(() => envId);
  wireCloudCard(() => envId);
  wireProgressCard(() => envId);
  wireDeployMap();
  wireConfigCard(() => envId);
  wireServersCard(() => envId);
  wireBaremetalCard(() => envId);
  wireDiscoveryCard(() => envId);
  wirePxeCard(() => envId);
  wireAgentsCard(() => envId);
  wireReachCard(() => envId);
  wireHostsCard(() => envId);
  wireComponentsCard(() => envId);
  wireTerminalCard(() => envId);
  wireSshKeysCard(() => envId);
  wireAppsCard(() => envId);
  wireObserveCard(() => envId, { onGoto: (ptab) => switchTab("platform", ptab) });
  wireOverlayEditor();

  syncDemoBanner();
  if (envId) {
    switchTab(activeTab || "workflow", activePtab, { silent: true });
    history.replaceState(null, "", envTabHash(activeTab || "workflow", activePtab));
    window.dispatchEvent(new CustomEvent("gsc-nav-sync"));
    removePageLoading();
    if (activeTab === "settings" || activeTab === "expert") {
      loadDescriptor().catch(() => {});
    }
    if (activeTab === "platform") loadClusterCard(envId);
  } else {
    loadComponentsCard("", null);
    loadClusterCard("");
    removePageLoading();
  }
}

export function destroy() {
  removePageLoading();
  destroyProgressCard();
  destroyDeployMap();
  destroyWorkflowCard();
  destroyComponentsCard();
  destroyTerminalCard();
  destroyAgentsCard();
  destroyReachCard();
  destroyHostsCard();
  destroyServersCard();
  destroyBaremetalCard();
  destroyDiscoveryCard();
  destroyPxeCard();
  destroyConfigCard();
  destroyPlatformCard();
  destroyClusterCard();
  destroyOpenstackCard();
  destroyCloudCard();
  destroyAppsCard();
  destroyObserveCard();
  closeOverlay();
  tabLoads.clear();
}

function syncDemoBanner() {
  const el = document.getElementById("demo-banner");
  if (!el) return;
  const env = store.envs.find((e) => e.id === envId);
  const on = isDemoEnv(env);
  el.classList.toggle("hidden", !on);
  el.style.display = on ? "flex" : "none";
}

async function loadAll() {
  if (!envId) {
    loadClusterCard("");
    return;
  }
  syncDemoBanner();
  tabLoads.clear();
  loadTab(activeTab || "workflow");
  const msg = document.getElementById("desc-msg");
  if (msg) msg.textContent = "";
  if (activeTab === "settings" || activeTab === "expert") {
    loadDescriptor().catch(() => {});
  }
  if (activeTab === "platform") loadClusterCard(envId);
}

async function loadDescriptor() {
  const msg = document.getElementById("desc-msg");
  const err = document.getElementById("desc-err");
  err.innerHTML = "";
  if (!envId) {
    msg.textContent = "";
    const ident = document.getElementById("desc-identity");
    if (ident) ident.innerHTML = '<div class="muted">Select an environment.</div>';
    return;
  }
  msg.textContent = "Loading…";
  let d;
  try {
    d = await api(`/api/v1/environments/${encodeURIComponent(envId)}/descriptor`);
  } catch (e) {
    msg.textContent = "";
    err.innerHTML = `<div class="error">${esc(e.message)}</div>`;
    const ident = document.getElementById("desc-identity");
    if (ident) ident.innerHTML = unavail("descriptor fetch failed");
    loadComponentsCard(envId, null);
    return;
  }
  if (activeTab === "expert") {
    msg.textContent = `descriptor generated ${fmtTime(d.generated_at) || "—"}`;
  } else {
    msg.textContent = "";
  }
  const env = d && d.environment && typeof d.environment === "object" ? d.environment : {};
  setBreadcrumbs([
    { label: "Environments", href: "#/fleet" },
    { label: env.name || env.id || envId },
  ]);

  const set = (id, html) => {
    const el = document.getElementById(id);
    if (el) { el.classList.remove("muted"); el.innerHTML = html; }
  };
  set("desc-identity", identityHtml(d));
  loadComponentsCard(envId, d.components);
  set("desc-helm", helmOverridesHtml(d.helm_overrides));
  set("desc-kustomize", kustomizeHtml(d.kustomize_overlays));
  set("desc-gateway", gatewayHtml(d.gateway));
  const raw = document.getElementById("desc-raw");
  if (raw) raw.textContent = JSON.stringify(d, null, 2);
}

let overlayPath = "";
let overlayOriginal = "";
let overlayLayers = { chart: "", base: "", local: "" };
let overlayInspect = "";

function overlayEls() {
  return {
    card: document.getElementById("desc-overlay-card"),
    title: document.getElementById("desc-overlay-title"),
    msg: document.getElementById("desc-overlay-msg"),
    note: document.getElementById("desc-overlay-note"),
    text: document.getElementById("desc-overlay-text"),
    save: document.getElementById("desc-overlay-save"),
    chartBtn: document.getElementById("desc-overlay-chart"),
    baseBtn: document.getElementById("desc-overlay-base"),
    localBtn: document.getElementById("desc-overlay-local"),
    basePre: document.getElementById("desc-overlay-base-pre"),
  };
}

function closeOverlay() {
  overlayPath = "";
  overlayOriginal = "";
  overlayLayers = { chart: "", base: "", local: "" };
  overlayInspect = "";
  const el = overlayEls();
  if (el.card) el.card.hidden = true;
  if (el.text) el.text.value = "";
  if (el.basePre) {
    el.basePre.hidden = true;
    el.basePre.textContent = "";
  }
}

async function openOverlay(path) {
  if (!envId || !path) return;
  const el = overlayEls();
  if (!el.card || !el.text) return;
  el.card.hidden = false;
  el.card.scrollIntoView({ block: "nearest" });
  if (el.title) el.title.textContent = path;
  if (el.msg) el.msg.textContent = "Loading…";
  if (el.note) el.note.textContent = "";
  el.text.value = "";
  el.text.readOnly = true;
  if (el.save) el.save.hidden = true;
  if (el.baseBtn) el.baseBtn.hidden = true;
  if (el.basePre) {
    el.basePre.hidden = true;
    el.basePre.textContent = "";
  }
  overlayPath = path;
  overlayInspect = "";
  try {
    const d = await api(
      `/api/v1/environments/${encodeURIComponent(envId)}/overlays?path=${encodeURIComponent(path)}`
    );
    if (overlayPath !== path) return;
    overlayOriginal = d && d.content != null ? String(d.content) : "";
    overlayLayers = {
      chart: d && d.chart_content ? String(d.chart_content) : "",
      base: d && d.base_content ? String(d.base_content) : "",
      local: d && d.local_content ? String(d.local_content) : "",
    };
    el.text.value = overlayOriginal;
    const writable = canRun();
    const ye = d && d.mode === "ye";
    el.text.readOnly = !writable;
    if (el.save) el.save.hidden = !writable;
    if (el.msg) {
      const bits = [];
      if (ye) bits.push("merged");
      if (d && d.service) bits.push(d.service);
      if (d && d.chart_source) bits.push(d.chart_source);
      else if (ye) bits.push("no chart values");
      el.msg.textContent = bits.join(" · ");
    }
    if (el.note) {
      const bits = [];
      if (ye) {
        bits.push("You are editing the stacked result: chart values.yaml → base-helm-configs → local helm-configs.");
        bits.push("Save writes only keys that differ from chart+base into the local file (yaml-editor/ye).");
      } else if (writable) {
        bits.push("Edits write this file on the environment config dir.");
      } else {
        bits.push("Read-only (operator role required to save).");
      }
      if (d && d.base_path) bits.push(`Base: ${d.base_path}`);
      el.note.textContent = bits.join(" ");
    }
    if (el.chartBtn) {
      el.chartBtn.hidden = !overlayLayers.chart;
      el.chartBtn.textContent = "Show chart";
    }
    if (el.baseBtn) {
      el.baseBtn.hidden = !overlayLayers.base;
      el.baseBtn.textContent = "Show base";
    }
    if (el.localBtn) {
      el.localBtn.hidden = !ye;
      el.localBtn.textContent = "Show local";
    }
    if (el.basePre) {
      el.basePre.hidden = true;
      el.basePre.textContent = "";
    }
  } catch (e) {
    if (el.msg) el.msg.textContent = "";
    if (el.note) el.note.textContent = e && e.message ? e.message : "unavailable";
    toast(e && e.message ? e.message : "failed to open overlay", "error");
  }
}

async function saveOverlay() {
  if (!envId || !overlayPath || !canRun()) return;
  const el = overlayEls();
  const content = el.text ? el.text.value : "";
  if (el.msg) el.msg.textContent = "Saving…";
  try {
    const d = await api(`/api/v1/environments/${encodeURIComponent(envId)}/overlays`, {
      method: "PUT",
      body: JSON.stringify({ path: overlayPath, content }),
    });
    overlayOriginal = content;
    if (d && d.delta_content != null) overlayLayers.local = String(d.delta_content);
    if (d && d.content) overlayOriginal = String(d.content);
    if (el.text && d && d.content) el.text.value = d.content;
    const n = Array.isArray(d && d.delta_keys) ? d.delta_keys.length : null;
    const git = d && d.git;
    const shortSha = git && git.sha ? String(git.sha).slice(0, 7) : "";
    let gitBit = "";
    if (git && git.committed) {
      gitBit = shortSha
        ? git.pushed
          ? ` committed ${shortSha}, pushed`
          : ` committed ${shortSha}`
        : git.pushed
          ? " committed, pushed"
          : " committed";
    }
    if (el.msg) {
      let msg =
        d && d.mode === "ye"
          ? `saved local delta${n != null ? ` (${n} top-level key${n === 1 ? "" : "s"})` : ""}`
          : d && d.mtime
            ? `saved ${d.mtime}`
            : "saved";
      if (gitBit) msg += ` ·${gitBit}`;
      if (git && git.error) msg += ` · git: ${git.error}`;
      el.msg.textContent = msg;
    }
    let toastMsg =
      d && d.mode === "ye"
        ? `Wrote local override (stripped values already in chart/base)`
        : `Saved ${overlayPath}`;
    if (gitBit) toastMsg += ` ·${gitBit}`;
    toast(toastMsg, "ok");
  } catch (e) {
    if (el.msg) el.msg.textContent = "";
    toast(e && e.message ? e.message : "save failed", "error");
  }
}

function toggleOverlayLayer(which) {
  const el = overlayEls();
  const labels = { chart: "chart values.yaml", base: "base-helm-configs", local: "local helm-configs (delta)" };
  if (overlayInspect === which) {
    overlayInspect = "";
    if (el.basePre) {
      el.basePre.hidden = true;
      el.basePre.textContent = "";
    }
  } else {
    overlayInspect = which;
    if (el.basePre) {
      el.basePre.hidden = false;
      const body = overlayLayers[which] || "(empty)";
      el.basePre.textContent = `${labels[which] || which}\n\n${body}`;
    }
  }
  if (el.chartBtn) el.chartBtn.textContent = overlayInspect === "chart" ? "Hide chart" : "Show chart";
  if (el.baseBtn) el.baseBtn.textContent = overlayInspect === "base" ? "Hide base" : "Show base";
  if (el.localBtn) el.localBtn.textContent = overlayInspect === "local" ? "Hide local" : "Show local";
}

function wireOverlayEditor() {
  const expert = document.getElementById("panel-expert");
  if (expert && !expert.dataset.overlayWired) {
    expert.dataset.overlayWired = "1";
    expert.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-overlay-path]");
      if (!btn) return;
      openOverlay(btn.dataset.overlayPath);
    });
  }
  const save = document.getElementById("desc-overlay-save");
  if (save && !save.dataset.wired) {
    save.dataset.wired = "1";
    save.addEventListener("click", () => saveOverlay());
  }
  const close = document.getElementById("desc-overlay-close");
  if (close && !close.dataset.wired) {
    close.dataset.wired = "1";
    close.addEventListener("click", () => closeOverlay());
  }
  const baseBtn = document.getElementById("desc-overlay-base");
  if (baseBtn && !baseBtn.dataset.wired) {
    baseBtn.dataset.wired = "1";
    baseBtn.addEventListener("click", () => toggleOverlayLayer("base"));
  }
  const chartBtn = document.getElementById("desc-overlay-chart");
  if (chartBtn && !chartBtn.dataset.wired) {
    chartBtn.dataset.wired = "1";
    chartBtn.addEventListener("click", () => toggleOverlayLayer("chart"));
  }
  const localBtn = document.getElementById("desc-overlay-local");
  if (localBtn && !localBtn.dataset.wired) {
    localBtn.dataset.wired = "1";
    localBtn.addEventListener("click", () => toggleOverlayLayer("local"));
  }
}
