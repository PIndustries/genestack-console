// pages/environment_servers.js — server inventory card for the environment detail page.
// Saved servers render in an Inventory table with role chips, Save, and Remove.
// OVH import is only for an environment bound to an OVH account. A role is a
// saved cluster plan. It does not install an operating system.
// Checked inventory rows can queue one next-boot job each when they have a
// management port. Apply names the servers first. Nothing is queued until
// that confirm. Talos is already installed applies a config to every saved
// address and does not power the machines. Already have an OS only records.
import { api, esc, toast } from "../api.js";
import { canAdmin, canRun, gate } from "../store.js";
import { ROLES, ROLE_LABELS } from "../roles.js";
import { clearOvhPoll } from "../ovh.js";
import { clusterOsHtml, fetchMetalPath, metalPathSentence, osNameHtml } from "../metal_path.js";

// Topology presets — replace raw role checkboxes for the add-host form.
// Each preset maps to a set of roles.  The UI shows human-readable labels.
const TOPOLOGY_PRESETS = {
  aio: {
    label: "All-in-One (single node)",
    desc: "Everything on one host: K8s, etcd, OpenStack, compute, storage",
    roles: ["k8s_control_plane", "etcd", "control", "compute", "network", "storage"],
  },
  control: {
    label: "Control Plane",
    desc: "K8s control + etcd + OpenStack control",
    roles: ["k8s_control_plane", "etcd", "control"],
  },
  worker: {
    label: "Worker (compute + storage)",
    desc: "Runs workloads and persistent storage",
    roles: ["compute", "storage"],
  },
  storage: {
    label: "Storage only",
    desc: "Dedicated storage node (longhorn)",
    roles: ["storage"],
  },
  custom: {
    label: "Custom",
    desc: "Manually select individual roles",
    roles: [],
  },
};

// Minimum roles a valid genestack cluster needs across all its nodes.
const REQUIRED_ROLE_SETS = [
  { label: "K8s control plane", role: "k8s_control_plane", min: 1 },
  { label: "etcd", role: "etcd", min: 1 },
  { label: "OpenStack control", role: "control", min: 1 },
  { label: "Worker (compute)", role: "compute", min: 1 },
  { label: "Storage", role: "storage", min: 1 },
];

let serversLoadedEnvId = ""; // env the card last rendered — guards against env switches
let adoptingOvh = false; // re-entrancy guard for auto-adopt on load
let vrackPollTimer = null; // ovh.vrack.attach job poll

export function serversCardHtml() {
  return `
<style>
#srv-card .toolbar { display:flex; align-items:center; gap:.5rem; padding-bottom:.4rem; margin-bottom:.3rem; border-bottom:1px solid var(--border, #222); flex-wrap:wrap; }
#srv-card .toolbar h2 { margin:0; font-size:.95rem; font-weight:600; }
#srv-card .card-empty { text-align:center; padding:1.5rem 1rem; color:var(--fg-muted, #666); }
#srv-card .card-empty .empty-icon { font-size:1.5rem; opacity:.3; margin-bottom:.4rem; }
#srv-card .card-empty p { font-size:.78rem; margin:.15rem 0; }
#srv-card .card-empty .empty-hint { font-size:.7rem; color:var(--fg-muted, #555); margin-top:.3rem; }
#srv-card .hint-row { display:flex; align-items:center; gap:.5rem; padding:.3rem 0; margin-bottom:.3rem; font-size:.72rem; color:var(--fg-muted, #666); }
#srv-card .pill { display:inline-flex; align-items:center; padding:.15rem .45rem; border-radius:.2rem; font-size:.72rem; font-weight:500; }
#srv-card .os-name { display:inline-flex; align-items:center; gap:.28rem; }
#srv-card .os-mark { width:1rem; height:1rem; flex:none; display:block; }
#srv-card .srv-os-choices { display:inline-flex; flex-wrap:wrap; gap:.25rem; }
#srv-card .srv-os-choice { display:inline-flex; align-items:center; gap:.28rem; margin:0; padding:.12rem .38rem; border:1px solid var(--border,#1a3d28); border-radius:.35rem; color:var(--text,#eafaef); font-size:.78rem; cursor:pointer; }
#srv-card .srv-os-choice.on { border-color:#34c759; background:var(--panel2,#122418); }
#srv-card .srv-os-choice input { margin:0; accent-color:#34c759; }
#srv-card .pill.ok { background:#1a3a1a; color:#4caf50; }
#srv-card .pill.bad { background:#3a1a1a; color:#ef5350; }
#srv-card .pill.warn { background:#3a3a1a; color:#ffc107; }
#srv-card h3 { font-size:.82rem; font-weight:600; margin:.5rem 0 .3rem; }
#srv-card .add-host-section { margin-top:.5rem; padding-top:.4rem; border-top:1px solid var(--border, #222); }
#srv-card .srv-bmc-head { display:flex; align-items:baseline; gap:.5rem; margin:.15rem 0 .35rem; }
#srv-card .srv-bmc-head h3 { margin:0; color:var(--text,#eafaef); }
#srv-bmc-modal { position:fixed; inset:0; z-index:1400; width:100vw; height:100vh; background:#04130a; display:flex; padding:0; }
#srv-bmc-modal[hidden] { display:none !important; }
#srv-bmc-modal .srv-bmc-panel { width:100%; height:100%; max-width:none; padding:0; border:0; border-radius:0; box-shadow:none; display:flex; flex-direction:column; background:var(--panel,#0c1a12); color:var(--text,#eafaef); overflow:hidden; }
#srv-bmc-modal .os-console-head { margin:0; padding:.55rem .75rem; border-bottom:1px solid var(--border,#1a3d28); }
#srv-bmc-modal .srv-bmc-body { display:grid; grid-template-columns:18rem minmax(0,1fr); min-height:0; flex:1; height:100%; }
#srv-bmc-modal .srv-bmc-list { overflow:auto; border-right:1px solid var(--border,#1a3d28); padding:.4rem; display:flex; flex-direction:column; gap:.25rem; }
#srv-bmc-modal button.srv-bmc-pick { text-align:left; background:transparent; color:var(--text,#eafaef); border:1px solid transparent; border-radius:.35rem; padding:.4rem .5rem; cursor:pointer; }
#srv-bmc-modal button.srv-bmc-pick.on { border-color:#34c759; background:var(--panel2,#122418); }
#srv-bmc-modal button.srv-bmc-pick strong { display:block; font-size:.82rem; }
#srv-bmc-modal button.srv-bmc-pick span { display:block; margin-top:.1rem; font-size:.72rem; color:var(--fg-muted,#b7d4c4); font-family:ui-monospace,monospace; }
#srv-bmc-modal .srv-bmc-stage { min-height:0; height:100%; display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,22rem),1fr)); grid-auto-rows:minmax(0,1fr); gap:.45rem; padding:.45rem; overflow:auto; }
#srv-bmc-modal .srv-bmc-empty { grid-column:1 / -1; align-self:center; margin:0; padding:1rem 1.2rem; }
#srv-bmc-modal .srv-bmc-tile { display:flex; flex-direction:column; min-height:0; height:100%; background:#04130a; border:1px solid var(--border,#1a3d28); border-radius:.35rem; overflow:hidden; }
#srv-bmc-modal .srv-bmc-tile header { display:flex; justify-content:space-between; gap:.4rem; font-size:.72rem; padding:.28rem .4rem; color:#eafaef; flex:none; }
#srv-bmc-modal .srv-bmc-tile iframe { flex:1; width:100%; min-height:0; height:100%; border:0; background:#000; }
@media (max-width:700px) {
  #srv-bmc-modal .srv-bmc-body { grid-template-columns:1fr; grid-template-rows:auto minmax(0,1fr); }
  #srv-bmc-modal .srv-bmc-list { max-height:34vh; border-right:0; border-bottom:1px solid var(--border,#1a3d28); }
}
#srv-card .srv-become { display:flex; flex-wrap:wrap; align-items:center; gap:.45rem .75rem; margin:0 0 .7rem; padding:.6rem .7rem; background:var(--panel2,#122418); border:1px solid var(--border,#1a3d28); border-radius:.45rem; color:var(--text,#eafaef); position:sticky; bottom:.4rem; z-index:4; }
#srv-card .srv-become label { display:inline-flex; align-items:center; gap:.35rem; font-size:.82rem; color:var(--text,#eafaef); }
#srv-card .srv-become-note { flex-basis:100%; margin:.15rem 0 0; font-size:.75rem; line-height:1.35; color:var(--fg-muted,#b7d4c4); }
#srv-card .srv-pick { width:1rem; height:1rem; }
#srv-card .srv-path-switch { display:flex; flex-wrap:wrap; align-items:center; gap:.45rem .75rem; margin:0 0 .7rem; color:var(--text,#eafaef); }
#srv-card .srv-path-switch label { display:inline-flex; align-items:center; gap:.35rem; font-size:.82rem; color:var(--text,#eafaef); }
@media (max-width:700px) {
  #srv-card .srv-become { align-items:flex-start; left:0; width:calc(100vw - 1.5rem); max-width:calc(100vw - 1.5rem); box-sizing:border-box; }
  #srv-card .srv-become label { flex-basis:100%; }
  #srv-card .srv-become-note { overflow-wrap:break-word; }
  #srv-card .srv-path-switch label { flex-basis:100%; }
}
#srv-card .srv-os-cell { display:flex; flex-wrap:wrap; gap:.35rem; align-items:center; }
#srv-card select.srv-os {
  background: var(--panel2, #122418);
  color: var(--text, #eafaef);
  border: 1px solid var(--border, #1a3d28);
  border-radius: .35rem;
  font-size: .78rem;
  padding: .2rem .35rem;
}
</style>
<div class="card span-12" id="srv-card">
  <div class="toolbar">
    <h2 id="srv-title">Metal hosts</h2>
    <span class="muted" id="srv-kicker">Hostname and IP are enough. A management port is optional.</span>
    <button class="secondary btn-sm" id="srv-refresh" type="button">Refresh</button>
    <span id="srv-msg" class="muted"></span>
  </div>
  <div class="hint-row" id="srv-hint">Add a host by hostname and IP. Talos is already installed applies a config to every saved address and does not power the machines. Already have an OS records Ubuntu that is already there. A role only saves the cluster plan.</div>
  <div class="hint-row" id="srv-path">Reading the metal path for this environment…</div>
  <div class="srv-path-switch" id="srv-path-switch">
    <label><input type="radio" name="srv-path-choice" value="talos"> ${osNameHtml("talos", "Talos, preferred")}</label>
    <label><input type="radio" name="srv-path-choice" value="kubespray"> Kubespray, adopt an existing OS</label>
    <button class="btn-sm" id="srv-path-save" type="button" disabled>Save path</button>
    <button class="btn-sm" id="srv-talos-installed" type="button">Talos is already installed</button>
    <span id="srv-path-msg" class="muted"></span>
  </div>
  <div id="srv-ovh-banner"></div>
  <div id="srv-vrack"></div>
  <div id="srv-err"></div>
  <div id="srv-bmc-wall"><div class="srv-bmc-head"><h3>BMC wall</h3></div><p class="muted">Loading BMCs…</p></div>
  <h3>Inventory (assigned)</h3>
  <div id="srv-become" class="srv-become hidden">
    <strong id="srv-become-count">0 servers</strong>
    <label><input type="radio" name="srv-become" value="ubuntu"> ${osNameHtml("ubuntu")}</label>
    <label><input type="radio" name="srv-become" value="talos"> ${osNameHtml("talos", "Talos cluster")}</label>
    <label><input type="radio" name="srv-become" value="kubespray"> Kubespray</label>
    <button class="btn-sm" id="srv-become-apply" type="button">Apply</button>
    <button class="secondary btn-sm" id="srv-adapt" type="button">Already have an OS</button>
    <button class="secondary btn-sm" id="srv-adapt-clear" type="button">Clear that record</button>
    <span id="srv-become-msg" class="muted"></span>
    <p class="srv-become-note">Ubuntu brings the selected hosts up by itself. A host that already answers stays on disk and leaves the Talos plan. A host that does not answer is installed, and the job waits until it answers. Talos cluster and Kubespray reboot through a management port. Already have an OS only records machines that already have one.</p>
  </div>
  <table>
    <thead><tr>
      <th style="width:2rem"><input type="checkbox" id="srv-select-all" class="srv-pick" aria-label="Select all hosts"></th>
      <th>Hostname</th><th>IP</th><th>SSH user</th><th>Source</th><th>OS</th><th>Roles</th><th></th>
    </tr></thead>
    <tbody id="srv-tbody"><tr><td colspan="8">
      <div class="card-empty">
        <div class="empty-icon">⬡</div>
        <p>No servers configured</p>
        <p class="empty-hint">Add a host by hostname and IP</p>
      </div>
    </td></tr></tbody>
  </table>
  <div class="add-host-section">
    <div id="srv-add-host"></div>
  </div>
  <div id="srv-discovered"></div>
</div>`;
}

export function wireServersCard(getEnvId) {
  const card = document.getElementById("srv-card");

  document.getElementById("srv-refresh").addEventListener("click", () => loadServersCard(getEnvId()));

  // Event delegation: the add-host hint buttons are re-injected on every
  // loadServersCard render, so direct listeners (wired once at page mount)
  // would never land on them.
  card?.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-srv-copy-key],[data-srv-view-key]");
    if (!btn) return;
    if (btn.hasAttribute("data-srv-copy-key")) {
      const envId = getEnvId();
      try {
        const data = await api(`/api/v1/environments/${encodeURIComponent(envId)}/ssh-key/public`);
        if (data?.public_key) {
          navigator.clipboard.writeText(data.public_key).then(() => {
            btn.textContent = "Copied ✓";
            setTimeout(() => { btn.textContent = "Copy public key"; }, 1500);
          }).catch(() => {
            btn.textContent = "Failed";
            setTimeout(() => { btn.textContent = "Copy public key"; }, 1500);
          });
        }
      } catch {
        btn.textContent = "Failed";
        setTimeout(() => { btn.textContent = "Copy public key"; }, 1500);
      }
    } else {
      const tabBtn = document.querySelector('.tab[data-tab="config"]');
      if (tabBtn) tabBtn.click();
    }
  });

  gateBecome();
  card?.addEventListener("change", (e) => {
    const t = e.target;
    if (!(t instanceof HTMLInputElement)) return;
    if (t.id === "srv-select-all") {
      card.querySelectorAll("input[data-srv-pick]").forEach((cb) => {
        cb.checked = t.checked;
      });
      syncBecomeBar();
      return;
    }
    if (t.matches("input[data-srv-pick]")) syncBecomeBar();
    if (t.name === "srv-path-choice") syncPathSwitch();
  });
  card?.addEventListener("click", (e) => {
    const save = e.target.closest("#srv-path-save");
    if (save && !save.disabled) {
      saveMetalPath(getEnvId());
      return;
    }
    const apply = e.target.closest("#srv-become-apply");
    if (apply && !apply.disabled) {
      applyBecome(getEnvId());
      return;
    }
    const adapt = e.target.closest("#srv-adapt");
    if (adapt && !adapt.disabled) {
      applyAdapt(getEnvId(), "kubespray");
      return;
    }
    const clear = e.target.closest("#srv-adapt-clear");
    if (clear && !clear.disabled) {
      applyAdapt(getEnvId(), "");
      return;
    }
    const installed = e.target.closest("#srv-talos-installed");
    if (installed && !installed.disabled) pointAtInstalledTalos(getEnvId());
  });
  gateInstalled();
}

function assignedRoleChips(roles) {
  const on = ROLES.filter((r) => roles.includes(r));
  if (!on.length) return `<span class="muted">no cluster role</span>`;
  return on.map((r) => `<span class="pill ok">${esc(ROLE_LABELS[r] || r)}</span>`).join(" ");
}

function roleEditor(scope, rowIdx, roles) {
  return `<div class="srv-roles">${assignedRoleChips(roles)}
    <details class="srv-role-edit"><summary>Edit</summary>
      <div class="row" style="gap:0;margin-top:.3rem">${roleBoxes(scope, rowIdx, roles)}</div>
    </details></div>`;
}

let bmcWallNodes = [];
let bmcWallEnv = "";
let bmcWallOn = new Set();
let bmcWallGen = 0;

function sortedBmcNodes(nodes) {
  return (Array.isArray(nodes) ? nodes : [])
    .filter((node) => node && typeof node === "object")
    .slice()
    .sort((a, b) => String(a.name || "").localeCompare(String(b.name || "")));
}

function stopBmcFrames(root) {
  if (!root) return;
  root.querySelectorAll("iframe").forEach((frame) => {
    frame.src = "about:blank";
  });
}

function bmcNodeKey(node, index) {
  return String((node && node.id) || `idx-${index}`);
}

function bmcStageHint() {
  return `<p class="srv-bmc-empty muted">Select servers on the left. Each one you select shows up here. Selecting does not power or boot the machine.</p>`;
}

function closeBmcWall() {
  bmcWallGen += 1;
  bmcWallOn = new Set();
  const modal = document.getElementById("srv-bmc-modal");
  stopBmcFrames(modal);
  if (modal) modal.hidden = true;
}

function ensureBmcModal() {
  let modal = document.getElementById("srv-bmc-modal");
  if (modal) return modal;
  modal = document.createElement("div");
  modal.id = "srv-bmc-modal";
  modal.className = "os-console-modal";
  modal.hidden = true;
  modal.innerHTML = `<div class="os-console-panel srv-bmc-panel" role="dialog" aria-modal="true" aria-labelledby="srv-bmc-title">
      <div class="os-console-head">
        <h2 id="srv-bmc-title">BMC wall</h2>
        <div class="os-console-actions">
          <button type="button" class="secondary btn-sm" data-bmc-all>Show all live</button>
          <button type="button" class="secondary btn-sm" data-bmc-clear>Clear</button>
          <button type="button" class="secondary btn-sm" data-bmc-close>Close</button>
        </div>
      </div>
      <div class="srv-bmc-body">
        <aside class="srv-bmc-list" id="srv-bmc-list"></aside>
        <section class="srv-bmc-stage" id="srv-bmc-stage"></section>
      </div>
    </div>`;
  document.body.appendChild(modal);
  modal.addEventListener("click", (event) => {
    if (event.target === modal || event.target.closest("[data-bmc-close]")) {
      closeBmcWall();
      return;
    }
    if (event.target.closest("[data-bmc-all]")) {
      showAllBmcLive();
      return;
    }
    if (event.target.closest("[data-bmc-clear]")) clearBmcStage();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && modal.hidden === false) closeBmcWall();
  });
  return modal;
}

async function attachBmcFrame(frame, envId, node, gen) {
  const status = frame.parentElement && frame.parentElement.querySelector("[data-bmc-status]");
  if (!node || !node.id) {
    if (status) status.textContent = "No console id";
    return;
  }
  if (status) status.textContent = "Opening…";
  try {
    const data = await api(
      `/api/v1/environments/${encodeURIComponent(envId)}/baremetal/nodes/${encodeURIComponent(node.id)}/console/session`,
      { method: "POST", timeout: 25000 }
    );
    if (gen !== bmcWallGen || !frame.isConnected) return;
    const tile = frame.closest("[data-bmc-key]");
    if (tile && !bmcWallOn.has(tile.dataset.bmcKey || "")) return;
    const embed = data && data.embed_url ? String(data.embed_url) : "";
    if (!data || !data.ok || !embed.startsWith("/") || embed.startsWith("//")) {
      if (status) status.textContent = (data && data.error) || "Console unavailable";
      return;
    }
    frame.src = embed;
    if (status) status.textContent = "Live";
  } catch (err) {
    if (gen !== bmcWallGen || !frame.isConnected) return;
    if (status) status.textContent = err && err.message ? err.message : "Console unavailable";
  }
}

function syncBmcChrome() {
  const modal = document.getElementById("srv-bmc-modal");
  if (!modal) return;
  const title = modal.querySelector("#srv-bmc-title");
  if (title) title.textContent = `BMC wall · ${bmcWallOn.size} of ${bmcWallNodes.length} live`;
  modal.querySelectorAll("[data-bmc-pick]").forEach((btn) => {
    const node = bmcWallNodes[Number(btn.getAttribute("data-bmc-pick"))];
    const on = !!(node && bmcWallOn.has(bmcNodeKey(node, Number(btn.getAttribute("data-bmc-pick")))));
    btn.classList.toggle("on", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
  });
}

function addBmcTile(node, index, delay) {
  const key = bmcNodeKey(node, index);
  const stage = document.getElementById("srv-bmc-stage");
  if (!stage || bmcWallOn.has(key)) return;
  bmcWallOn.add(key);
  const empty = stage.querySelector(".srv-bmc-empty");
  if (empty) empty.remove();
  const tile = document.createElement("article");
  tile.className = "srv-bmc-tile";
  tile.dataset.bmcKey = key;
  tile.innerHTML = `<header><strong>${esc(node.name || "BMC")}</strong><span data-bmc-status>Waiting…</span></header><iframe title="${esc((node && node.name) || "BMC")} console" referrerpolicy="no-referrer"></iframe>`;
  stage.appendChild(tile);
  const frame = tile.querySelector("iframe");
  const gen = bmcWallGen;
  window.setTimeout(() => {
    if (gen !== bmcWallGen || !bmcWallOn.has(key) || !frame || !frame.isConnected) return;
    attachBmcFrame(frame, bmcWallEnv, node, gen);
  }, delay || 0);
}

function removeBmcTile(key) {
  bmcWallOn.delete(key);
  const stage = document.getElementById("srv-bmc-stage");
  const tile = stage && stage.querySelector(`[data-bmc-key="${CSS.escape(key)}"]`);
  if (tile) {
    const frame = tile.querySelector("iframe");
    if (frame) frame.src = "about:blank";
    tile.remove();
  }
  if (stage && !stage.querySelector(".srv-bmc-tile")) stage.innerHTML = bmcStageHint();
}

function toggleBmc(index) {
  const node = bmcWallNodes[index];
  if (!node) return;
  const key = bmcNodeKey(node, index);
  if (bmcWallOn.has(key)) removeBmcTile(key);
  else addBmcTile(node, index, 0);
  syncBmcChrome();
}

function showAllBmcLive() {
  let delay = 0;
  bmcWallNodes.forEach((node, index) => {
    if (bmcWallOn.has(bmcNodeKey(node, index))) return;
    addBmcTile(node, index, delay);
    delay += 300;
  });
  syncBmcChrome();
}

function clearBmcStage() {
  bmcWallGen += 1;
  bmcWallOn = new Set();
  const stage = document.getElementById("srv-bmc-stage");
  stopBmcFrames(stage);
  if (stage) stage.innerHTML = bmcStageHint();
  syncBmcChrome();
}

function paintBmcWall() {
  const modal = ensureBmcModal();
  const listEl = modal.querySelector("#srv-bmc-list");
  const stage = modal.querySelector("#srv-bmc-stage");
  if (!listEl || !stage) return;
  listEl.innerHTML = bmcWallNodes.length
    ? bmcWallNodes
        .map((node, index) => {
          const on = bmcWallOn.has(bmcNodeKey(node, index));
          return `<button type="button" class="srv-bmc-pick${on ? " on" : ""}" data-bmc-pick="${index}" aria-pressed="${on ? "true" : "false"}">
              <strong>${esc(node.name || "BMC")}</strong>
              <span>${esc(node.bmc_host || "No BMC address")}</span>
            </button>`;
        })
        .join("")
    : `<p class="muted">No BMCs are registered.</p>`;
  listEl.querySelectorAll("[data-bmc-pick]").forEach((btn) => {
    btn.addEventListener("click", () => toggleBmc(Number(btn.getAttribute("data-bmc-pick"))));
  });
  if (!stage.querySelector(".srv-bmc-tile") && !stage.querySelector(".srv-bmc-empty")) {
    stage.innerHTML = bmcStageHint();
  }
  syncBmcChrome();
}

function openBmcWall(envId, nodes) {
  bmcWallGen += 1;
  bmcWallOn = new Set();
  bmcWallEnv = envId;
  bmcWallNodes = sortedBmcNodes(nodes);
  const modal = ensureBmcModal();
  const stage = modal.querySelector("#srv-bmc-stage");
  stopBmcFrames(stage);
  if (stage) stage.innerHTML = bmcStageHint();
  modal.hidden = false;
  paintBmcWall();
}

function renderBmcWall(envId, nodes, note) {
  const root = document.getElementById("srv-bmc-wall");
  if (!root) return;
  if (note) {
    closeBmcWall();
    root.innerHTML = `<div class="srv-bmc-head"><h3>BMC wall</h3></div><p class="muted" style="font-size:.78rem">${esc(note)}</p>`;
    return;
  }
  const list = sortedBmcNodes(nodes);
  if (!list.length) {
    closeBmcWall();
    root.innerHTML = `<div class="srv-bmc-head"><h3>BMC wall</h3></div><p class="muted" style="font-size:.78rem">No management ports are registered. A BMC is optional. Hostname and IP are enough to start.</p>`;
    return;
  }
  root.innerHTML = `
    <div class="srv-bmc-head">
      <h3>BMC wall</h3>
      <span class="muted">${list.length} registered</span>
      <button class="btn-sm" type="button" id="srv-bmc-open">Open wall</button>
    </div>
    <p class="hint-row">Open wall lists every BMC on the left. Select any of them and each one shows up live. Show all live selects every BMC. Opening a console does not power or boot the machine.</p>`;
  const open = root.querySelector("#srv-bmc-open");
  if (open) open.addEventListener("click", () => openBmcWall(envId, list));
}

function hostOsLabel(server, bootByName, clusterIps, metalPath) {
  const name = String((server && server.hostname) || "");
  const ip = String((server && (server.private_ip || server.ip)) || "");
  const boot = bootByName.get(name) || {};
  const next = String(boot.next_boot || "");
  const stage = String(boot.boot_stage || "");
  const inCluster = !!(ip && clusterIps.has(ip));
  if (String((server && server.adopt) || "") === "kubespray" && !inCluster) {
    const ubuntuBoot = next === "ubuntu" || stage === "ubuntu";
    return `<span class="pill ok">Kubespray</span> <span class="muted">recorded, not in the cluster${ubuntuBoot ? `. ${osNameHtml("ubuntu")} next boot` : ""}</span>`;
  }
  if (next === "ubuntu" || stage === "ubuntu") {
    return inCluster
      ? `<span class="pill warn">${osNameHtml("ubuntu")}</span> <span class="muted">also in the cluster</span>`
      : `<span class="pill ok">${osNameHtml("ubuntu")}</span> <span class="muted">not in the cluster</span>`;
  }
  if (inCluster) return clusterOsHtml(metalPath);
  if ((Array.isArray(server && server.roles) ? server.roles : []).length) {
    return `<span class="muted">recorded, not in the cluster</span>`;
  }
  return `<span class="muted">not in the cluster</span>`;
}

function roleBoxes(scope, rowIdx, roles) {
  return ROLES.map(
    (r) => `<label class="check" style="margin:0 .6rem 0 0">
      <input type="checkbox" data-scope="${scope}" data-row="${rowIdx}" data-role="${r}"${roles.includes(r) ? " checked" : ""} ${gate(canRun(), "operator")} /> ${ROLE_LABELS[r] || r}
    </label>`
  ).join("");
}

function checkedRoles(scope, rowIdx) {
  const roles = [];
  document
    .querySelectorAll(`#srv-card input[data-scope="${scope}"][data-row="${rowIdx}"][data-role]`)
    .forEach((cb) => {
      if (cb.checked) roles.push(cb.dataset.role);
    });
  return roles;
}

const VRACK_INTERCONNECT = {
  ok: { cls: "ok", label: "interconnected" },
  partial: { cls: "warn", label: "partial" },
  unattached: { cls: "bad", label: "not on vRack" },
  unconfigured: { cls: "warn", label: "vRack not selected" },
  empty: { cls: "", label: "no OVH servers" },
  unknown: { cls: "warn", label: "not checked yet" },
};

function vrackPill(status) {
  const key = String((status && status.interconnect) || "unknown");
  const meta = VRACK_INTERCONNECT[key] || VRACK_INTERCONNECT.unknown;
  return `<span class="pill ${meta.cls}">${esc(meta.label)}</span>`;
}

function vrackOptionLabel(v) {
  const id = (v && v.id) || "";
  const name = String((v && v.name) || "").trim();
  const desc = String((v && v.description) || "").trim();
  if (name && name !== id) {
    return desc && desc !== name ? `${name} — ${desc}` : name;
  }
  if (desc && desc !== id) return desc;
  return id;
}

function fabricSteps(data) {
  const servers = Array.isArray(data.servers) ? data.servers : [];
  const hasServers = servers.length > 0;
  const hasVrack = !!(data.vrack);
  const hasCidr = !!(data.private_cidr);
  const nicsOk =
    hasServers &&
    servers.every((s) => s.private_mac && s.vrack_vni && (s.private_ip || s.public_ip));
  const attached = data.interconnect === "ok";
  const steps = [
    { id: "import", label: "1. Import OVH servers into inventory", done: hasServers },
    { id: "vrack", label: "2. Pick the vRack (name, not just pn- id)", done: hasVrack },
    { id: "vlan", label: "3. Apply defaults (VLAN + private CIDR + .11/.12 IPs)", done: hasVrack && hasCidr },
    { id: "nics", label: "4. Confirm each private NIC (MAC, VNI, private IP)", done: nicsOk },
    { id: "attach", label: "5. Attach private NICs to the vRack", done: attached },
    { id: "deploy", label: "6. Deploy the cluster (Workflow → Deploy)", done: data.interconnect === "ok" },
  ];
  let marked = false;
  return steps.map((s) => {
    const next = !s.done && !marked;
    if (next) marked = true;
    return { ...s, next };
  });
}

function formatCheckedAt(iso) {
  if (!iso) return "never";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return String(iso);
  const ago = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (ago < 15) return "just now";
  if (ago < 60) return `${ago}s ago`;
  if (ago < 3600) return `${Math.round(ago / 60)}m ago`;
  return `${Math.round(ago / 3600)}h ago`;
}

async function loadVrackPanel(envId, opts) {
  const box = document.getElementById("srv-vrack");
  if (!box) return;
  const seed = opts && opts.seed;
  if (seed && box.dataset.dirty !== "1") {
    renderVrackPanel(envId, seed, { checking: true });
  } else if (!box.dataset.ready) {
    // Instant local snapshot (no OVH). Never block the page on the API.
    let cached;
    try {
      cached = await api(`/api/v1/environments/${encodeURIComponent(envId)}/ovh/vrack`);
    } catch (e) {
      if (serversLoadedEnvId !== envId || !document.getElementById("srv-vrack")) return;
      box.innerHTML = `<div class="error" style="font-size:.78rem">vRack status unavailable: ${esc(e.message)}</div>`;
      return;
    }
    if (serversLoadedEnvId !== envId || !document.getElementById("srv-vrack")) return;
    renderVrackPanel(envId, cached || {}, { checking: true });
  }
  // Background live check. Keeps the panel usable while OVH enumerates NICs.
  refreshVrackLive(envId);
}

async function refreshVrackLive(envId) {
  const box = document.getElementById("srv-vrack");
  if (!box) return;
  setVrackChecking(true);
  let data;
  try {
    data = await api(
      `/api/v1/environments/${encodeURIComponent(envId)}/ovh/vrack?refresh=true`
    );
  } catch (e) {
    if (serversLoadedEnvId !== envId || !document.getElementById("srv-vrack")) return;
    setVrackChecking(false);
    const live = document.getElementById("srv-vrack-live");
    if (live) live.innerHTML = `<span class="error">OVH refresh failed: ${esc(e.message)}</span>`;
    return;
  }
  if (serversLoadedEnvId !== envId || !document.getElementById("srv-vrack")) return;
  if (box.dataset.dirty === "1") {
    setVrackChecking(false);
    applyLiveStrip(data || {});
    const msg = document.getElementById("srv-vrack-msg");
    if (msg) msg.textContent = "OVH updated — unsaved NIC edits kept. Refresh after Save to merge.";
    return;
  }
  renderVrackPanel(envId, data || {}, { checking: false });
}

function setVrackChecking(on) {
  const live = document.getElementById("srv-vrack-live");
  if (!live) return;
  live.dataset.checking = on ? "1" : "";
  const spin = live.querySelector("[data-checking]");
  if (spin) spin.style.display = on ? "" : "none";
}

function applyLiveStrip(data) {
  const live = document.getElementById("srv-vrack-live");
  if (!live) return;
  const src = data.source === "live" ? "OVH" : data.source === "cache" ? "cached" : "local";
  live.innerHTML = `${vrackPill(data)}
    <span class="muted" style="font-size:.75rem">Last ${esc(src)} check: ${esc(
      formatCheckedAt(data.checked_at)
    )}</span>
    <span data-checking class="muted" style="font-size:.75rem">checking OVH…</span>`;
}

async function loadVrackPanelFromServers(envId, data) {
  const seed = Object.assign({}, data && data.fabric, data && data.ovh, {
    servers: (data && data.fabric && data.fabric.servers) || [],
  });
  await loadVrackPanel(envId, { seed });
}

function renderVrackPanel(envId, data, opts) {
  const box = document.getElementById("srv-vrack");
  if (!box) return;
  box.dataset.ready = "1";
  box.dataset.dirty = "";
  const checking = !!(opts && opts.checking);
  const vracks = Array.isArray(data.vracks) ? data.vracks.slice() : [];
  const servers = Array.isArray(data.servers) ? data.servers : [];
  const blocks = Array.isArray(data.ip_blocks) ? data.ip_blocks : [];
  const discovered = Array.isArray(data.discovered_vlans) ? data.discovered_vlans : [];
  const suggested = data.suggested_vrack && data.suggested_vrack.id ? data.suggested_vrack : null;
  const defaults = data.defaults && typeof data.defaults === "object" ? data.defaults : {};
  const proposedIps = data.proposed_ips && typeof data.proposed_ips === "object" ? data.proposed_ips : {};
  const selected = data.vrack || (suggested && suggested.id) || "";
  const vlanId = data.vlan_id == null ? defaults.vlan_id ?? 100 : data.vlan_id;
  const cidr = data.private_cidr || defaults.private_cidr || "10.10.0.0/24";
  const attachedN = Array.isArray(data.attached) ? data.attached.length : 0;
  const missingN = Array.isArray(data.missing) ? data.missing.length : 0;
  const listed = new Set(vracks.map((v) => v.id));
  if (selected && !listed.has(selected)) {
    vracks.unshift({ id: selected, name: selected, description: "", selected: true });
  }
  const vrackList = vracks.length
    ? vracks
        .map((v) => {
          const id = v.id || "";
          const isSug = suggested && suggested.id === id;
          const checked = id === selected ? " checked" : "";
          return `<label class="check" style="display:flex;align-items:flex-start;gap:.45rem;margin:.28rem 0;cursor:pointer">
            <input type="radio" name="srv-vrack-choice" value="${esc(id)}"${checked} ${gate(canRun(), "operator")} />
            <span>
              <strong>${esc(vrackOptionLabel(v))}</strong>
              <span class="muted" style="font-size:.72rem"> · ${esc(id)}</span>
              ${isSug ? ' <span class="pill ok">suggested</span>' : ""}
            </span>
          </label>`;
        })
        .join("")
    : '<div class="muted" style="font-size:.78rem">No vRacks on this OVH account yet.</div>';
  const vrackControl = `<input id="srv-vrack-id" type="hidden" value="${esc(selected)}" />`;
  const vlanChips = discovered
    .map(
      (v) =>
        `<button type="button" class="secondary btn-sm" data-vlan="${esc(String(v))}">VLAN ${esc(
          String(v)
        )}${Number(v) === 0 ? " (untagged)" : ""}</button>`
    )
    .join(" ");
  const blockHint = blocks.length
    ? blocks
        .map((b) => {
          const vlan = b.vlan == null ? "" : ` vlan ${b.vlan}`;
          return `${b.ip || "?"}${vlan}`;
        })
        .join(" · ")
    : "";
  const vlanLabel = Number(vlanId) > 0 ? String(vlanId) : "0 untagged";
  const rows = servers.length
    ? servers
        .map((s, i) => {
          const on = s.attached_to
            ? `<span class="pill ok">${esc(s.attached_to)}</span>`
            : data.checked_at
              ? '<span class="pill bad">unattached</span>'
              : '<span class="pill warn">not checked</span>';
          const host = s.hostname || "";
          const nics = Array.isArray(s.nics) ? s.nics : [];
          const nicHint = nics
            .map((n) => `${n.role || n.link_type || "nic"} ${n.mac || ""}`)
            .filter(Boolean)
            .join(" · ");
          return `<tr data-nic-row="${i}" data-host="${esc(host)}">
            <td>
              <strong>${esc(host || "?")}</strong>
              <div class="muted" style="font-size:.7rem">${esc(s.service_name || "")}</div>
              ${nicHint ? `<div class="muted" style="font-size:.68rem">${esc(nicHint)}</div>` : ""}
            </td>
            <td><input data-nic="public_ip" value="${esc(s.public_ip || "")}" placeholder="public IP" style="width:8.5rem" ${gate(canRun(), "operator")} /></td>
            <td><input data-nic="public_mac" value="${esc(s.public_mac || "")}" placeholder="public MAC" style="width:9rem" ${gate(canRun(), "operator")} /></td>
            <td><input data-nic="private_ip" value="${esc(s.private_ip || proposedIps[host] || "")}" placeholder="${esc(
              proposedIps[host] || "10.10.0.x"
            )}" style="width:8.5rem" ${gate(canRun(), "operator")} /></td>
            <td><input data-nic="private_mac" value="${esc(s.private_mac || "")}" placeholder="private MAC" style="width:9rem" ${gate(canRun(), "operator")} /></td>
            <td><input data-nic="vrack_vni" value="${esc(s.vrack_vni || "")}" placeholder="VNI" style="width:8rem" ${gate(canRun(), "operator")} /></td>
            <td class="muted" style="font-size:.75rem">${esc(vlanLabel)}</td>
            <td>${on}</td>
            <td style="white-space:nowrap">
              <button class="secondary btn-sm" type="button" data-nic-save="${i}" ${gate(canRun(), "operator")}>Save NIC</button>
              <button class="secondary btn-sm" type="button" data-nic-attach="${i}" ${gate(canAdmin(), "admin")}>Attach</button>
            </td>
          </tr>`;
        })
        .join("")
    : `<tr><td colspan="9" class="muted">Import OVH servers first (step 1).</td></tr>`;
  const err = data.ok === false && data.error
    ? `<div class="error" style="font-size:.78rem;margin-top:.3rem">${esc(data.error)}</div>`
    : "";
  const steps = fabricSteps(data);
  const next = steps.find((s) => s.next);
  const stepHtml = steps
    .map((s) => {
      const mark = s.done ? "✓" : s.next ? "→" : "·";
      const cls = s.done ? "ok" : s.next ? "" : "muted";
      return `<div class="${cls}" style="font-size:.78rem;margin:.12rem 0">${mark} ${esc(s.label)}</div>`;
    })
    .join("");
  const src = data.source === "live" ? "OVH" : data.source === "cache" ? "cached" : "local";
  box.innerHTML = `
  <div class="ovh-panel" style="border:1px solid var(--border,#ddd);border-radius:6px;padding:.6rem .7rem;margin:.4rem 0 .7rem">
    <div class="row" style="align-items:center;gap:.5rem;flex-wrap:wrap">
      <strong style="font-size:.82rem">vRack fabric</strong>
      <span id="srv-vrack-live" class="row" style="gap:.4rem;align-items:center;flex-wrap:wrap">
        ${vrackPill(data)}
        <span class="muted" style="font-size:.75rem">Last ${esc(src)} check: ${esc(formatCheckedAt(data.checked_at))}</span>
        <span data-checking class="muted" style="font-size:.75rem;${checking ? "" : "display:none"}">checking OVH…</span>
      </span>
      <span class="muted" style="font-size:.75rem">${attachedN} attached${missingN ? ` · ${missingN} missing` : ""}</span>
    </div>
    <div style="margin:.45rem 0 .55rem;padding:.4rem .5rem;background:var(--bg-2,#111);border-radius:4px">
      <div style="font-size:.78rem;font-weight:600;margin-bottom:.2rem">${
        next
          ? `Next: ${esc(next.label.replace(/^\d+\.\s*/, ""))}`
          : "Fabric is ready — Deploy from the Guide tab. Deploy will reinstall boxes that are not yet Talos."
      }</div>
      ${stepHtml}
    </div>
    ${err}
    <h3 style="margin-top:.15rem">vRacks on this account</h3>
    <p class="muted" style="font-size:.75rem;margin:.2rem 0 .35rem">
      Pick the vRack these dedicated servers share. IDs are OVH <code>pn-…</code> service names;
      the label is the name set in OVH (or suggested from this cluster).
      ${
        suggested
          ? `Suggested: <strong>${esc(vrackOptionLabel(suggested))}</strong> (${esc(suggested.id)})${
              suggested.reason ? ` — ${esc(suggested.reason)}` : ""
            }.`
          : ""
      }
    </p>
    <div id="srv-vrack-choices" style="margin:.2rem 0 .5rem">${vrackList}</div>
    ${vrackControl}
    <div class="row" style="flex-wrap:wrap;gap:.5rem;align-items:flex-end">
      <label class="field" style="margin:0;width:auto">
        <span>VLAN tag on private NIC</span>
        <input id="srv-vrack-vlan" type="number" min="0" max="4000" step="1" value="${esc(
          String(vlanId)
        )}" style="width:6rem" ${gate(canRun(), "operator")} />
      </label>
      <label class="field" style="margin:0;width:auto">
        <span>Private CIDR</span>
        <input id="srv-vrack-cidr" type="text" value="${esc(cidr)}" placeholder="10.10.0.0/24" style="width:10rem" ${gate(
          canRun(),
          "operator"
        )} />
      </label>
      <button class="secondary btn-sm" type="button" id="srv-vrack-save" ${gate(canRun(), "operator")}>Save fabric</button>
      <button class="btn-sm" type="button" id="srv-vrack-apply-attach" ${gate(canAdmin(), "admin")}>Apply VLAN + IPs + attach</button>
      <button class="secondary btn-sm" type="button" id="srv-vrack-apply" ${gate(canRun(), "operator")}>Apply defaults + private IPs</button>
      <button class="secondary btn-sm" type="button" id="srv-vrack-attach" ${gate(canAdmin(), "admin")}>Attach all to vRack</button>
      ${
        data.interconnect === "ok"
          ? `<button class="btn-sm" type="button" id="srv-vrack-deploy" ${gate(canAdmin(), "admin")}>Deploy cluster</button>`
          : ""
      }
      <button class="secondary btn-sm" type="button" id="srv-vrack-refresh">Refresh from OVH</button>
    </div>
    <p class="muted" style="font-size:.72rem;margin:.4rem 0 .2rem">
      Greenfield defaults: VLAN ${esc(String(defaults.vlan_id ?? 100))} (0 = untagged, 100+ = 802.1q) · CIDR ${esc(
        defaults.private_cidr || "10.10.0.0/24"
      )} · private IPs start at .11.
      Apply writes those onto every OVH server (skips hosts that already have a private IP). Then Attach plugs the private NIC into the selected vRack.
    </p>
    ${
      vlanChips
        ? `<div class="row" style="margin-top:.25rem;font-size:.75rem;gap:.35rem;flex-wrap:wrap;align-items:center">
            <span class="muted">VLANs looked up on this vRack:</span>${vlanChips}
          </div>`
        : ""
    }
    ${blockHint ? `<div class="muted" style="font-size:.72rem;margin-top:.25rem">IP blocks: ${esc(blockHint)}</div>` : ""}
    <h3 style="margin-top:.65rem">NICs</h3>
    <table style="margin-top:.25rem;width:100%">
      <thead><tr>
        <th>Server</th><th>Public IP</th><th>Public MAC</th><th>Private IP</th><th>Private MAC</th><th>vRack VNI</th><th>VLAN</th><th>vRack</th><th></th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div id="srv-vrack-msg" class="muted" style="font-size:.75rem;margin-top:.35rem"></div>
  </div>`;

  const markDirty = () => {
    box.dataset.dirty = "1";
  };
  box.querySelectorAll("input, select").forEach((el) => el.addEventListener("input", markDirty));
  box.querySelectorAll("input, select").forEach((el) => el.addEventListener("change", markDirty));
  box.querySelectorAll("[data-vlan]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const input = document.getElementById("srv-vrack-vlan");
      if (input) input.value = btn.dataset.vlan;
      markDirty();
    });
  });
  const saveBtn = document.getElementById("srv-vrack-save");
  if (saveBtn) saveBtn.addEventListener("click", () => saveVrackFabric(envId));
  const applyAttachBtn = document.getElementById("srv-vrack-apply-attach");
  if (applyAttachBtn) applyAttachBtn.addEventListener("click", () => provisionVrackFabric(envId, true));
  const applyBtn = document.getElementById("srv-vrack-apply");
  if (applyBtn) applyBtn.addEventListener("click", () => provisionVrackFabric(envId, false));
  const attachBtn = document.getElementById("srv-vrack-attach");
  if (attachBtn) attachBtn.addEventListener("click", () => attachVrackFabric(envId));
  const deployBtn = document.getElementById("srv-vrack-deploy");
  if (deployBtn) deployBtn.addEventListener("click", () => startFabricDeploy(envId));
  const refreshBtn = document.getElementById("srv-vrack-refresh");
  if (refreshBtn) refreshBtn.addEventListener("click", () => refreshVrackLive(envId));
  box.querySelectorAll('input[name="srv-vrack-choice"]').forEach((el) => {
    el.addEventListener("change", () => {
      const hidden = document.getElementById("srv-vrack-id");
      if (hidden) hidden.value = el.value;
      markDirty();
    });
  });
  box.querySelectorAll("[data-nic-save]").forEach((btn) => {
    btn.addEventListener("click", () => saveNicRow(envId, btn.closest("tr")));
  });
  box.querySelectorAll("[data-nic-attach]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const host = btn.closest("tr") && btn.closest("tr").dataset.host;
      attachVrackFabric(envId, host ? [host] : null);
    });
  });
  setVrackChecking(checking);
}

function readVrackForm() {
  const sel = document.getElementById("srv-vrack-id");
  const vlanEl = document.getElementById("srv-vrack-vlan");
  const cidrEl = document.getElementById("srv-vrack-cidr");
  const vlanRaw = vlanEl ? vlanEl.value.trim() : "";
  const vlan = vlanRaw === "" ? NaN : Number(vlanRaw);
  return {
    vrack: sel ? sel.value.trim() : "",
    vlan_id: Number.isFinite(vlan) ? vlan : 100,
    private_cidr: cidrEl ? cidrEl.value.trim() : "",
  };
}

async function startFabricDeploy(envId) {
  if (!envId) return;
  if (
    !confirm(
      "Deploy the cluster?\n\n" +
        "OVH + Talos will BYOI-reinstall any node that is not answering on :50000, then run talosctl bootstrap, then the genestack pipeline.\n\n" +
        "This wipes boxes that are not yet Talos. Deploy stops at the first failing stage; there is no automatic rollback."
    )
  ) {
    return;
  }
  const msg = document.getElementById("srv-vrack-msg");
  const btn = document.getElementById("srv-vrack-deploy");
  if (msg) msg.textContent = "Creating deploy job…";
  if (btn) btn.disabled = true;
  try {
    const job = await api(`/api/v1/environments/${encodeURIComponent(envId)}/jobs`, {
      method: "POST",
      body: JSON.stringify({ operation: "genestack.deploy", params: {} }),
    });
    const id = job && job.id != null ? String(job.id) : "";
    toast(id ? `Deploy job ${id.slice(0, 8)}… created` : "Deploy job created", "ok");
    if (msg) {
      msg.innerHTML = id
        ? `deploy job created — <a href="#/activity?tab=jobs&job=${esc(id)}">view job ${esc(id.slice(0, 8))}…</a>`
        : "deploy job created";
    }
    window.dispatchEvent(new CustomEvent("deploy-job-started", { detail: { envId, jobId: id } }));
  } catch (e) {
    if (msg) msg.textContent = "";
    if (e.status === 409) toast("Deploy blocked by a running mutating job", "bad");
    else toast(`Deploy failed: ${e.message}`, "error");
    if (e.status === 403) toast("Insufficient role: admin required", "bad");
  } finally {
    if (btn) btn.disabled = !canAdmin();
  }
}

async function provisionVrackFabric(envId, attach) {
  const msg = document.getElementById("srv-vrack-msg");
  const body = readVrackForm();
  if (!body.vrack) {
    toast("Pick a vRack first", "warn");
    return;
  }
  if (body.vlan_id < 0 || body.vlan_id > 4000) {
    toast("VLAN must be 0 (untagged) or 1–4000", "warn");
    return;
  }
  const who = attach ? "and attach every private NIC" : "and assign private IPs (.11, .12, …)";
  if (
    !confirm(
      `Apply fabric on ${body.vrack}?\n\nVLAN ${body.vlan_id} · ${body.private_cidr || "10.10.0.0/24"}\nThis ${who}. Existing private IPs are left alone.`
    )
  ) {
    return;
  }
  if (msg) msg.textContent = attach ? "Provisioning and attaching…" : "Applying fabric defaults…";
  try {
    const res = await api(`/api/v1/environments/${encodeURIComponent(envId)}/ovh/vrack/provision`, {
      method: "POST",
      body: JSON.stringify({
        vrack: body.vrack,
        vlan_id: body.vlan_id,
        private_cidr: body.private_cidr || "10.10.0.0/24",
        assign_ips: true,
        attach: !!attach,
      }),
    });
    toast(res.message || "Fabric defaults applied", "ok");
    const box = document.getElementById("srv-vrack");
    if (box) box.dataset.dirty = "";
    const jobId = res && (res.job_id != null || res.id != null) ? String(res.job_id || res.id) : "";
    if (jobId) {
      pollVrackJob(envId, jobId);
      return;
    }
    await loadServersCard(envId);
  } catch (e) {
    if (msg) msg.textContent = "";
    toast(`Provision failed: ${e.message}`, "bad");
  }
}

async function saveVrackFabric(envId) {
  const msg = document.getElementById("srv-vrack-msg");
  const body = readVrackForm();
  if (body.vlan_id < 0 || body.vlan_id > 4000) {
    toast("VLAN must be 0 (untagged) or 1–4000", "warn");
    return;
  }
  if (msg) msg.textContent = "Saving…";
  try {
    await api(`/api/v1/environments/${encodeURIComponent(envId)}/ovh/vrack`, {
      method: "PUT",
      body: JSON.stringify({
        vrack: body.vrack || null,
        vlan_id: body.vlan_id,
        private_cidr: body.private_cidr || null,
      }),
    });
    toast("vRack fabric saved", "ok");
    const box = document.getElementById("srv-vrack");
    if (box) box.dataset.dirty = "";
    await refreshVrackLive(envId);
  } catch (e) {
    if (msg) msg.textContent = "";
    toast(`Save fabric failed: ${e.message}`, "bad");
  }
}

async function attachVrackFabric(envId, hostnames) {
  const msg = document.getElementById("srv-vrack-msg");
  const body = readVrackForm();
  const vrack = body.vrack;
  if (!vrack) {
    toast("Select a vRack and Save fabric first", "warn");
    return;
  }
  const who = hostnames && hostnames.length ? hostnames.join(", ") : "every OVH server in this environment";
  if (
    !confirm(
      `Attach ${who} to ${vrack}?\n\nDedicated servers plug the private NIC into the vRack. VLAN ${body.vlan_id} is applied later at Talos apply-config.`
    )
  ) {
    return;
  }
  const btn = document.getElementById("srv-vrack-attach");
  if (btn) btn.disabled = true;
  if (msg) msg.textContent = "Creating attach job…";
  const payload = { vrack };
  if (hostnames && hostnames.length) payload.server_hostnames = hostnames;
  try {
    const job = await api(`/api/v1/environments/${encodeURIComponent(envId)}/ovh/vrack/attach`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const id = job && (job.job_id != null || job.id != null) ? String(job.job_id || job.id) : "";
    if (!id) {
      if (msg) msg.textContent = "attach job created (no job ID)";
      if (btn) btn.disabled = !canAdmin();
      return;
    }
    toast(`vRack attach job ${id.slice(0, 8)}… created`, "ok");
    pollVrackJob(envId, id);
  } catch (e) {
    if (msg) msg.textContent = "";
    toast(`vRack attach failed: ${e.message}`, "bad");
    if (e.status === 403) toast("Insufficient role: admin required", "bad");
    if (btn) btn.disabled = !canAdmin();
  }
}

async function pollVrackJob(envId, jobId) {
  const msg = document.getElementById("srv-vrack-msg");
  const btn = document.getElementById("srv-vrack-attach");
  if (!document.getElementById("srv-vrack") || serversLoadedEnvId !== envId) return;
  let job;
  try {
    job = await api(`/api/v1/jobs/${encodeURIComponent(jobId)}`);
  } catch {
    vrackPollTimer = setTimeout(() => pollVrackJob(envId, jobId), 5000);
    return;
  }
  if (!document.getElementById("srv-vrack") || serversLoadedEnvId !== envId) return;
  const status = String((job && job.status) || "").toLowerCase();
  if (status === "queued" || status === "running") {
    if (msg) {
      msg.innerHTML = `${esc(status)}… <a href="#/activity?tab=jobs&job=${esc(jobId)}">job ${esc(
        jobId.slice(0, 8)
      )}…</a>`;
    }
    vrackPollTimer = setTimeout(() => pollVrackJob(envId, jobId), 5000);
    return;
  }
  if (btn) btn.disabled = !canAdmin();
  toast(
    `vRack attach ${status === "success" ? "succeeded" : status || "finished"}`,
    status === "success" ? "ok" : "bad"
  );
  await refreshVrackLive(envId);
}

async function saveNicRow(envId, tr) {
  if (!tr) return;
  const hostname = tr.dataset.host;
  if (!hostname) return;
  const val = (name) => {
    const el = tr.querySelector(`[data-nic="${name}"]`);
    return el ? el.value.trim() : "";
  };
  const msg = document.getElementById("srv-vrack-msg");
  if (msg) msg.textContent = `Saving NICs for ${hostname}…`;
  try {
    await api(`/api/v1/environments/${encodeURIComponent(envId)}/ovh/vrack/nics`, {
      method: "PUT",
      body: JSON.stringify({
        hostname,
        public_ip: val("public_ip") || null,
        public_mac: val("public_mac") || null,
        private_ip: val("private_ip") || null,
        private_mac: val("private_mac") || null,
        vrack_vni: val("vrack_vni") || null,
      }),
    });
    toast(`${hostname}: NIC saved`, "ok");
    const box = document.getElementById("srv-vrack");
    if (box) box.dataset.dirty = "";
    if (msg) msg.textContent = "";
  } catch (e) {
    if (msg) msg.textContent = "";
    toast(`${hostname}: ${e.message}`, "bad");
  }
}

let becomeTargets = [];
let savedMetalPath = "";

function selectedBecome() {
  const picks = [];
  document.querySelectorAll("#srv-card input[data-srv-pick]:checked").forEach((cb) => {
    const row = becomeTargets[Number(cb.getAttribute("data-srv-pick"))];
    if (row) picks.push(row);
  });
  return picks;
}

function syncBecomeBar() {
  const bar = document.getElementById("srv-become");
  if (!bar) return;
  const picks = selectedBecome();
  const count = document.getElementById("srv-become-count");
  if (count) count.textContent = picks.length === 1 ? "1 server" : `${picks.length} servers`;
  bar.classList.toggle("hidden", picks.length === 0);
  const boxes = document.querySelectorAll("#srv-card input[data-srv-pick]");
  const master = document.getElementById("srv-select-all");
  if (master) {
    master.checked = boxes.length > 0 && picks.length === boxes.length;
    master.indeterminate = picks.length > 0 && picks.length < boxes.length;
  }
}

function resetBecome() {
  becomeTargets = [];
  document.querySelectorAll("#srv-card input[data-srv-pick]").forEach((cb) => {
    cb.checked = false;
  });
  const master = document.getElementById("srv-select-all");
  if (master) {
    master.checked = false;
    master.indeterminate = false;
  }
  const msg = document.getElementById("srv-become-msg");
  if (msg) msg.textContent = "";
  document.querySelectorAll("#srv-become input[name='srv-become']").forEach((el) => {
    el.checked = false;
  });
  syncBecomeBar();
}

function gateBecome() {
  const allowed = canRun();
  for (const id of ["srv-become-apply", "srv-adapt", "srv-adapt-clear"]) {
    const btn = document.getElementById(id);
    if (!btn) continue;
    btn.disabled = !allowed;
    if (!allowed) btn.title = "Requires operator role";
  }
  gateInstalled();
}

function gateInstalled() {
  const btn = document.getElementById("srv-talos-installed");
  if (!btn) return;
  btn.disabled = !canAdmin();
  if (!canAdmin()) btn.title = "Requires admin role";
  else btn.title = "Talos is already running, for example from an ISO. Does not power the machines.";
}

function syncPathSwitch() {
  const save = document.getElementById("srv-path-save");
  if (!save) return;
  const chosen = document.querySelector("#srv-path-switch input[name='srv-path-choice']:checked");
  const value = chosen ? chosen.value : "";
  save.disabled = !canRun() || !value || value === savedMetalPath;
  if (!canRun()) save.title = "Requires operator role";
}

function paintPathSwitch(provider) {
  savedMetalPath = provider === "kubespray" || provider === "talos" ? provider : "";
  document.querySelectorAll("#srv-path-switch input[name='srv-path-choice']").forEach((el) => {
    el.checked = el.value === savedMetalPath;
    el.disabled = !canRun();
  });
  syncPathSwitch();
}

async function saveMetalPath(envId) {
  const msg = document.getElementById("srv-path-msg");
  const chosen = document.querySelector("#srv-path-switch input[name='srv-path-choice']:checked");
  if (!chosen || !chosen.value || chosen.value === savedMetalPath) return;
  const label = chosen.value === "kubespray" ? "Kubespray" : "Talos";
  if (!window.confirm(
    `Save this environment's metal path as ${label}?\n\nThis does not reboot any machine and does not start a playbook.`
  )) return;
  const btn = document.getElementById("srv-path-save");
  if (btn) btn.disabled = true;
  try {
    await api(`/api/v1/environments/${encodeURIComponent(envId)}/config/provider`, {
      method: "PUT",
      body: JSON.stringify({ provider: chosen.value }),
    });
    if (msg) msg.textContent = `Saved ${label}. Nothing rebooted.`;
    toast(`Metal path saved as ${label}`, "ok");
    window.dispatchEvent(new CustomEvent("gsc-metal-path", { detail: { provider: chosen.value } }));
    await loadServersCard(envId);
  } catch (e) {
    if (msg) msg.textContent = e.message;
    toast(e.message, "bad");
    syncPathSwitch();
  }
}

function becomeConfirmText(choice, ready, skipped) {
  const names = ready.map((row) => row.hostname).join("\n");
  const skipLine = skipped.length
    ? `\n\nNo management port, left alone:\n${skipped.map((row) => row.hostname).join("\n")}`
    : "";
  if (!ready.length) {
    return `None of these servers have a management port, so nothing will reboot:\n${skipped.map((row) => row.hostname).join("\n")}\n\nTalos is already installed does not power them. Already have an OS only records them.`;
  }
  if (choice === "talos") {
    return (
      `Reboot these servers onto the Talos cluster?\n\n${names}\n\n` +
      "Talos only proceeds on a machine that already has an accepted commission wipe. " +
      "Otherwise that job fails and the disks stay as they are." +
      skipLine
    );
  }
  if (choice === "kubespray") {
    return (
      `Install Ubuntu on these servers for a Kubespray set, not Talos?\n\n${names}\n\n` +
      "They reboot into the Ubuntu installer. This does not start the Kubespray playbook " +
      "and does not change this environment's provider." +
      skipLine
    );
  }
  return (
    `Install Ubuntu on these servers and leave them out of the Talos cluster?\n\n${names}\n\n` +
    "They reboot into the Ubuntu installer." +
    skipLine
  );
}

async function applyAdapt(envId, adopt) {
  const msg = document.getElementById("srv-become-msg");
  const picks = selectedBecome();
  if (!picks.length) return;
  const ready = picks.filter((row) => !row.inCluster);
  const skipped = picks.filter((row) => row.inCluster);
  const names = ready.map((row) => row.hostname).join("\n");
  const skipLine = skipped.length
    ? `\n\nAlready in the cluster, left alone:\n${skipped.map((row) => row.hostname).join("\n")}`
    : "";
  const text = !ready.length
    ? `Every selected machine is already in the cluster, so nothing will be recorded:\n${skipped.map((row) => row.hostname).join("\n")}`
    : adopt
      ? `Record that these machines already have an OS?\n\n${names}\n\nThis does not reboot them, install anything, or start a playbook. The row shows Kubespray recorded. Roles stay.${skipLine}`
      : `Clear that record on these machines?\n\n${names}\n\nThis does not reboot them or change their roles.${skipLine}`;
  if (!window.confirm(text)) return;
  if (!ready.length) {
    if (msg) msg.textContent = "Nothing recorded. Those machines are already in the cluster.";
    return;
  }
  const adaptBtn = document.getElementById("srv-adapt");
  const clearBtn = document.getElementById("srv-adapt-clear");
  if (adaptBtn) adaptBtn.disabled = true;
  if (clearBtn) clearBtn.disabled = true;
  try {
    const res = await api(`/api/v1/environments/${encodeURIComponent(envId)}/servers/adopt`, {
      method: "POST",
      body: JSON.stringify({
        hostnames: ready.map((row) => row.hostname),
        adopt,
      }),
    });
    const warn = res && Array.isArray(res.warnings) && res.warnings.length ? ` ${res.warnings.join(" ")}` : "";
    if (msg) msg.textContent = (adopt ? "Recorded. They already have an OS." : "Cleared that record.") + warn;
    toast(adopt ? "Recorded. They already have an OS" : "Cleared that record", "ok");
    await loadServersCard(envId);
  } catch (e) {
    if (msg) msg.textContent = e.message;
    toast(e.message, "bad");
    gateBecome();
  }
}

function ubuntuNowText(staying, leaving) {
  const stay = staying.length
    ? staying.map((row) => row.hostname).join("\n")
    : "(none)";
  const leaveBlock = leaving.length
    ? `\n\nThese are in the cluster. Confirming reinstalls them and removes them from Kubernetes:\n${leaving.map((row) => row.hostname).join("\n")}`
    : "\n\nMachines already in the cluster were not selected.";
  return (
    `Install Ubuntu on these hosts and bring them up? There is no second step.\n\n` +
    `If a host already answers, it stays on disk and leaves the Talos plan:\n${stay}` +
    leaveBlock +
    `\n\nA host that does not answer reboots into the Ubuntu installer. This job waits until it answers. ` +
    `No OpenStack and no Kubernetes are installed.`
  );
}

function installedOs(server, bootByName, inCluster, metalPath) {
  const name = String((server && server.hostname) || "");
  const boot = (bootByName && bootByName.get(name)) || {};
  const next = String(boot.next_boot || "");
  const stage = String(boot.boot_stage || "");
  if (next === "ubuntu" || stage === "ubuntu") return "ubuntu";
  if (next === "talos" || stage === "talos") return "talos";
  if (inCluster && metalPath === "talos") return "talos";
  return "";
}

function osVerb(choice, installed) {
  return choice && choice === installed ? "Reinstall" : "Install";
}

function paintOsButton(btn, choice) {
  const installed = btn.dataset.installed || "";
  const inCluster = btn.dataset.inCluster === "1";
  const hasNode = btn.dataset.node === "1";
  if (choice === "talos" && !hasNode) {
    btn.textContent = "Already installed";
    btn.className = `${inCluster ? "secondary " : ""}btn-sm`;
    btn.disabled = !canAdmin();
    btn.title = canAdmin()
      ? "Talos is already running. Apply a config to every saved host. Does not power this machine."
      : "Requires admin role";
    return;
  }
  const verb = choice ? osVerb(choice, installed) : "Install";
  btn.textContent = verb;
  const quiet = !choice || inCluster || verb === "Reinstall";
  btn.className = `${quiet ? "secondary " : ""}btn-sm`;
  btn.disabled = !canRun() || !choice;
  if (!choice) btn.title = "Pick Ubuntu or Talos";
  else if (choice === "ubuntu" && inCluster) btn.title = `${verb} Ubuntu and remove this machine from the cluster`;
  else if (choice === "ubuntu") btn.title = `${verb} Ubuntu on this machine and leave it out of the cluster`;
  else btn.title = `${verb} Talos on this machine`;
}

function osControlHtml(i, server, bootByName, row, metalPath) {
  const inCluster = !!(row && row.inCluster);
  const hasNode = !!(row && row.nodeId);
  const installed = installedOs(server, bootByName, inCluster, metalPath);
  const selected = installed === "ubuntu" || installed === "talos" ? installed : "";
  const pointAtTalos = selected === "talos" && !hasNode;
  const verb = pointAtTalos ? "Already installed" : selected ? "Reinstall" : "Install";
  const quiet = inCluster || verb === "Reinstall";
  const host = String((server && server.hostname) || "host");
  const title = !selected
    ? "Pick Ubuntu or Talos"
    : pointAtTalos
      ? "Talos is already running. Apply a config to every saved host. Does not power this machine."
      : selected === "ubuntu" && inCluster
      ? "Reinstall Ubuntu and remove this machine from the cluster"
      : selected === "ubuntu"
        ? "Reinstall Ubuntu on this machine and leave it out of the cluster"
        : "Reinstall Talos on this machine";
  const choice = (value, label) =>
    `<label class="srv-os-choice${selected === value ? " on" : ""}">` +
    `<input type="radio" name="srv-os-${i}" value="${value}" data-os="${i}"${selected === value ? " checked" : ""} ${gate(canRun(), "operator")} />` +
    `${osNameHtml(value, label)}</label>`;
  return (
    `<span class="srv-os-choices" role="radiogroup" aria-label="Operating system for ${esc(host)}">` +
    `${choice("ubuntu", "Ubuntu")}${choice("talos", "Talos")}` +
    `</span> ` +
    `<button class="${quiet ? "secondary " : ""}btn-sm" type="button" data-reconfigure="${i}" data-installed="${esc(installed)}" data-in-cluster="${inCluster ? "1" : "0"}" data-node="${hasNode ? "1" : "0"}" title="${esc(title)}" ${gate(pointAtTalos ? canAdmin() : canRun(), pointAtTalos ? "admin" : "operator")}${selected ? "" : " disabled"}>${verb}</button>`
  );
}

async function queueUbuntuBringup(envId, picks) {
  const msg = document.getElementById("srv-become-msg");
  const staying = picks.filter((row) => !row.inCluster);
  const leaving = picks.filter((row) => row.inCluster);
  if (!staying.length && !leaving.length) return;
  const applyBtn = document.getElementById("srv-become-apply");
  if (applyBtn) applyBtn.disabled = true;
  try {
    const job = await api(`/api/v1/environments/${encodeURIComponent(envId)}/jobs`, {
      method: "POST",
      body: JSON.stringify({
        operation: "hosts.ubuntu.bringup",
        params: {
          hostnames: staying.map((row) => row.hostname),
          leave_hostnames: leaving.map((row) => row.hostname),
        },
      }),
    });
    const id = job && job.id != null ? String(job.id) : "";
    if (msg) msg.textContent = id ? `Ubuntu bring-up queued ${id.slice(0, 8)}` : "Ubuntu bring-up queued";
    toast(id ? `Ubuntu bring-up queued ${id.slice(0, 8)}` : "Ubuntu bring-up queued", "ok");
  } catch (e) {
    if (msg) msg.textContent = e.message;
    toast(e.message, "bad");
  } finally {
    gateBecome();
  }
}

async function bringUpUbuntu(envId, picks) {
  const staying = picks.filter((row) => !row.inCluster);
  const leaving = picks.filter((row) => row.inCluster);
  if (!window.confirm(ubuntuNowText(staying, leaving))) return;
  await queueUbuntuBringup(envId, picks);
}

async function pointAtInstalledTalos(envId) {
  const msg = document.getElementById("srv-become-msg");
  if (!canAdmin()) {
    const text = "An admin points the console at Talos that is already installed.";
    if (msg) msg.textContent = text;
    toast(text, "bad");
    return;
  }
  if (!window.confirm(
    "Point this console at Talos that is already installed?\n\n" +
    "This uses every saved host, not one row. Each machine must already be running Talos and waiting for a config. That is what an ISO install does, on hardware or a virtual machine.\n\n" +
    "The console does not power the machines and does not use a management port. It applies a Talos config to each address, bootstraps etcd once, and fetches the kubeconfig.\n\n" +
    "One host needs the control plane role. The install disk defaults to /dev/sda. Confirm that disk. Dry run only logs the commands."
  )) return;
  const btn = document.getElementById("srv-talos-installed");
  if (btn) btn.disabled = true;
  try {
    const job = await api(`/api/v1/environments/${encodeURIComponent(envId)}/jobs`, {
      method: "POST",
      body: JSON.stringify({ operation: "genestack.talos.bootstrap", params: {} }),
    });
    const id = job && job.id != null ? String(job.id) : "";
    const text = id ? `Talos bootstrap queued ${id.slice(0, 8)}` : "Talos bootstrap queued";
    if (msg) msg.textContent = text;
    toast(text, "ok");
  } catch (e) {
    if (msg) msg.textContent = e.message;
    toast(e.message, "bad");
  } finally {
    gateInstalled();
  }
}

async function reconfigureHost(envId, row, choice, installed) {
  const host = row.hostname || "this machine";
  if (choice === "ubuntu") {
    const verb = osVerb(choice, installed);
    const where = row.inCluster
      ? "This machine is in the cluster. Confirming removes it from Kubernetes."
      : "It stays out of the cluster.";
    const how = "If it already answers, it stays on disk. If it does not answer, it reboots into the Ubuntu installer and this job waits.";
    if (!window.confirm(`${verb} Ubuntu on ${host}?\n\n${where}\n\n${how}\n\nNo OpenStack and no Kubernetes are installed.`)) return;
    await queueUbuntuBringup(envId, [row]);
    return;
  }
  if (choice !== "talos") return;
  if (!row.nodeId) {
    await pointAtInstalledTalos(envId);
    return;
  }
  const verb = osVerb(choice, installed);
  if (!window.confirm(
    `${verb} Talos on ${host}?\n\nThis reboots the machine onto Talos. Talos only proceeds after an accepted commission wipe. Otherwise the job fails and the disks stay as they are.`
  )) return;
  const msg = document.getElementById("srv-become-msg");
  const applyBtn = document.getElementById("srv-become-apply");
  if (applyBtn) applyBtn.disabled = true;
  try {
    const job = await api(`/api/v1/environments/${encodeURIComponent(envId)}/jobs`, {
      method: "POST",
      body: JSON.stringify({
        operation: "baremetal.node.next_boot",
        params: {
          node_id: row.nodeId,
          next_boot: "talos",
          boot_now: true,
        },
      }),
    });
    const id = job && job.id != null ? String(job.id) : "";
    if (msg) msg.textContent = id ? `${host}: queued ${id.slice(0, 8)}` : `${host}: queued`;
    toast(id ? `Talos boot queued ${id.slice(0, 8)}` : "Talos boot queued", "ok");
  } catch (e) {
    if (msg) msg.textContent = e.message;
    toast(e.message, "bad");
  } finally {
    gateBecome();
  }
}

async function applyBecome(envId) {
  const msg = document.getElementById("srv-become-msg");
  const picks = selectedBecome();
  if (!picks.length) return;
  const choiceEl = document.querySelector("#srv-become input[name='srv-become']:checked");
  if (!choiceEl) {
    if (msg) msg.textContent = "Pick Ubuntu server, Talos cluster, or Kubespray.";
    return;
  }
  if (choiceEl.value === "ubuntu") {
    await bringUpUbuntu(envId, picks);
    return;
  }
  const ready = picks.filter((row) => row.nodeId);
  const skipped = picks.filter((row) => !row.nodeId);
  if (!window.confirm(becomeConfirmText(choiceEl.value, ready, skipped))) return;
  if (!ready.length) {
    if (msg) msg.textContent = "Nothing queued. Those servers have no management port. Talos is already installed does not power them.";
    return;
  }
  const nextBoot = choiceEl.value === "talos" ? "talos" : "ubuntu";
  const applyBtn = document.getElementById("srv-become-apply");
  if (applyBtn) applyBtn.disabled = true;
  const lines = [];
  let failed = false;
  try {
    for (const row of ready) {
      try {
        const job = await api(`/api/v1/environments/${encodeURIComponent(envId)}/jobs`, {
          method: "POST",
          body: JSON.stringify({
            operation: "baremetal.node.next_boot",
            params: {
              node_id: row.nodeId,
              next_boot: nextBoot,
              boot_now: true,
            },
          }),
        });
        const id = job && job.id != null ? String(job.id) : "";
        lines.push(id ? `${row.hostname}: queued ${id.slice(0, 8)}` : `${row.hostname}: queued`);
      } catch (e) {
        failed = true;
        lines.push(`${row.hostname}: ${e.message}`);
      }
    }
    if (skipped.length) lines.push(`Left alone: ${skipped.map((row) => row.hostname).join(", ")}`);
    if (msg) msg.textContent = lines.join(" · ");
    const n = lines.filter((line) => line.includes(": queued")).length;
    toast(failed ? "Some servers were not queued" : `Queued ${n} server${n === 1 ? "" : "s"}`, failed ? "bad" : "ok");
  } finally {
    gateBecome();
  }
}

export async function loadServersCard(envId) {
  const tbody = document.getElementById("srv-tbody");
  if (!tbody) return;
  resetBecome();
  if (envId !== serversLoadedEnvId) {
    if (vrackPollTimer) {
      clearTimeout(vrackPollTimer);
      vrackPollTimer = null;
    }
  }
  serversLoadedEnvId = envId || "";
  const err = document.getElementById("srv-err");
  const msg = document.getElementById("srv-msg");
  const discovered = document.getElementById("srv-discovered");
  err.innerHTML = "";
  discovered.innerHTML = "";
  if (!envId) {
    msg.textContent = "";
    renderBmcWall("", [], "Select an environment.");
    tbody.innerHTML = `<tr><td colspan="8" class="muted">Select an environment.</td></tr>`;
    return;
  }
  msg.textContent = "Loading…";

  let data;
  const bootByName = new Map();
  const clusterIps = new Set();
  let bmNodes = [];
  let metalPath = "";
  try {
    const [serversRes, bmRes, k8sRes, pathName] = await Promise.all([
      api(`/api/v1/environments/${encodeURIComponent(envId)}/servers`),
      api(`/api/v1/environments/${encodeURIComponent(envId)}/baremetal`).catch(() => null),
      api(`/api/v1/environments/${encodeURIComponent(envId)}/k8s/nodes`).catch(() => null),
      fetchMetalPath(api, envId),
    ]);
    metalPath = pathName;
    data = serversRes;
    bmNodes = (bmRes && bmRes.nodes) || [];
    for (const n of bmNodes) {
      if (n && n.name) bootByName.set(String(n.name), n);
    }
    for (const n of (k8sRes && k8sRes.nodes) || []) {
      const ip = String((n && n.internal_ip) || "");
      if (ip && String((n && n.status) || "").toLowerCase() === "ready") clusterIps.add(ip);
    }
  } catch (e) {
    msg.textContent = "";
    renderBmcWall(envId, [], "BMC list unavailable");
    tbody.innerHTML = `<tr><td colspan="8" class="muted">Unavailable</td></tr>`;
    err.innerHTML = `<div class="error">${esc(e.message)}</div>`;
    return;
  }
  renderBmcWall(envId, bmNodes);

  // Contract is an object envelope ({servers, ovh_bound, ...}); tolerate a bare array.
  const servers = Array.isArray(data) ? data : data && Array.isArray(data.servers) ? data.servers : [];
  const ovhBound = !!(data && !Array.isArray(data) && data.ovh_bound);
  const title = document.getElementById("srv-title");
  const kicker = document.getElementById("srv-kicker");
  const hint = document.getElementById("srv-hint");
  if (title) title.textContent = ovhBound ? "OVH infrastructure" : "Metal hosts";
  if (kicker) {
    kicker.textContent = ovhBound
      ? "Dedicated · vRack · private fabric"
      : "Hostname and IP are enough. A management port is optional.";
  }
  if (hint) {
    hint.textContent = ovhBound
      ? "Dedicated servers use a public NIC for management and a private NIC on the vRack. Roles here are the saved cluster plan."
      : "Add a host by hostname and IP. Talos is already installed applies a config to every saved address and does not power the machines. Already have an OS records Ubuntu that is already there. A role only saves the cluster plan.";
  }
  const pathLine = document.getElementById("srv-path");
  if (pathLine) pathLine.textContent = metalPathSentence(metalPath);
  paintPathSwitch(metalPath);
  msg.textContent = servers.length ? `${servers.length} server(s)` : "No servers in inventory yet.";
  const ovhBanner = document.getElementById("srv-ovh-banner");
  if (ovhBanner) {
    ovhBanner.innerHTML = ovhBound
      ? `<div class="hint-row" style="color:var(--ok,#4caf50)">OVH environment — dedicated servers from the bound account. ${metalPath === "kubespray" ? "Kubespray" : "Talos"}, Kubernetes, and Genestack use the <strong>private NIC</strong> (vRack). The public NIC stays up but the host firewall default-denies the internet edge.</div>`
      : "";
  }
  const vrackBox = document.getElementById("srv-vrack");
  if (vrackBox && !ovhBound) vrackBox.innerHTML = "";

  // OVH-bound env: persist source:ovh on static hosts that match live inventory
  // so Talos BYOI and the YAML agree with the bound account.
  if (ovhBound && canRun() && !adoptingOvh) {
    const needsAdopt = servers.some(
      (s) =>
        s &&
        s.assigned &&
        s.source !== "ovh" &&
        s.source !== "maas" &&
        s.source !== "baremetal" &&
        s.source !== "terraform"
    );
    if (needsAdopt) {
      adoptingOvh = true;
      try {
        const adopted = await api(`/api/v1/environments/${encodeURIComponent(envId)}/ovh/adopt`, {
          method: "POST",
          body: "{}",
        });
        const n = adopted && Array.isArray(adopted.adopted) ? adopted.adopted.length : 0;
        if (n) {
          toast(`${n} server(s) tagged as OVH`, "ok");
          adoptingOvh = false;
          return loadServersCard(envId);
        }
      } catch {
        // 403/503/unbound — inventory still renders; operator can Import from OVH.
      }
      adoptingOvh = false;
    }
  }

  const invRows = servers.filter((s) => s && typeof s === "object" && s.assigned);
  becomeTargets = invRows.map((s) => {
    const boot = bootByName.get(String(s.hostname || "")) || {};
    const ip = String((s && (s.private_ip || s.ip)) || "");
    return {
      hostname: String(s.hostname || "?"),
      nodeId: boot.id ? String(boot.id) : "",
      inCluster: !!(ip && clusterIps.has(ip)),
    };
  });
  if (ovhBound) {
    const fabric = Object.assign({}, data.fabric || {}, data.ovh || {});
    if (!Array.isArray(fabric.servers) || !fabric.servers.length) {
      fabric.servers = invRows
        .filter((s) => s.source === "ovh" || s.service_name)
        .map((s) => ({
          hostname: s.hostname,
          service_name: s.service_name,
          private_ip: s.private_ip,
          public_ip: s.public_ip,
          private_mac: s.private_mac,
          public_mac: s.public_mac,
          vrack_vni: s.vrack_vni,
          nics: s.nics || [],
          attached_to: s.attached_to,
        }));
    }
    loadVrackPanel(envId, { seed: fabric });
  }
  tbody.innerHTML = invRows.length
    ? invRows
        .map((s, i) => {
          const roles = Array.isArray(s.roles) ? s.roles : [];
          const source = s.source || "static";
          const sub = s.system_id
            ? `<div class="muted" style="font-size:.75rem">${esc(s.system_id)}</div>`
            : source === "ovh" && s.service_name
              ? `<div class="muted" style="font-size:.75rem">${esc(s.service_name)}</div>`
              : "";
          const removeBtn = `<button class="secondary btn-sm" type="button" data-remove="${i}" ${gate(canRun(), "operator")}>Remove</button>`;
          const sourceCls = source === "ovh" || source === "static" || source === "terraform" ? "ok" : "";
          const ipCell = s.private_ip
            ? `${esc(s.private_ip)} <span style="font-size:.7rem">priv</span>` +
              (s.public_ip ? `<div style="font-size:.7rem">${esc(s.public_ip)} pub</div>` : "") +
              (s.private_mac ? `<div style="font-size:.7rem">${esc(s.private_mac)}</div>` : "")
            : esc(s.ip || "—");
          const boot = bootByName.get(String(s.hostname || "")) || {};
          const ilo = boot.bmc_host
            ? `<div style="font-size:.7rem;color:var(--fg-muted,#b7d4c4)">iLO ${esc(boot.bmc_host)}</div>`
            : "";
          return `<tr data-row="${i}">
            <td><input type="checkbox" class="srv-pick" data-srv-pick="${i}" aria-label="Select ${esc(s.hostname || "host")}"></td>
            <td><strong>${esc(s.hostname || "?")}</strong>${sub}${ilo}</td>
            <td class="muted">${ipCell}</td>
            <td class="muted">${esc(s.ssh_user || "—")}</td>
            <td><span class="pill ${sourceCls}">${esc(source)}</span></td>
            <td>${hostOsLabel(s, bootByName, clusterIps, metalPath)}</td>
            <td>${roleEditor("inv", i, roles)}</td>
            <td class="srv-os-cell">
              ${osControlHtml(i, s, bootByName, becomeTargets[i], metalPath)}
              <button class="secondary btn-sm" type="button" data-save="${i}" ${gate(canRun(), "operator")}>Save</button>
              ${removeBtn}
              <span class="muted" style="font-size:.75rem" data-save-msg="${i}"></span>
            </td>
          </tr>`;
        })
        .join("")
    : `<tr><td colspan="8" class="muted">No servers in inventory yet.</td></tr>`;

  tbody.querySelectorAll("input[data-os]").forEach((input) => {
    input.addEventListener("change", () => {
      const cell = input.closest(".srv-os-cell");
      const btn = cell && cell.querySelector("button[data-reconfigure]");
      if (cell) {
        cell.querySelectorAll(".srv-os-choice").forEach((label) => {
          const radio = label.querySelector("input");
          label.classList.toggle("on", !!(radio && radio.checked));
        });
      }
      if (btn) paintOsButton(btn, input.value);
    });
  });
  tbody.querySelectorAll("button[data-reconfigure]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const row = becomeTargets[Number(btn.dataset.reconfigure)];
      const picked = btn.parentElement && btn.parentElement.querySelector("input[data-os]:checked");
      const choice = picked ? picked.value : "";
      if (!row || !choice || btn.disabled) return;
      btn.disabled = true;
      try {
        await reconfigureHost(envId, row, choice, btn.dataset.installed || "");
      } finally {
        if (btn.isConnected) paintOsButton(btn, choice);
      }
    })
  );
  tbody.querySelectorAll("button[data-save]").forEach((btn) =>
    btn.addEventListener("click", () => saveInventoryRow(envId, invRows[Number(btn.dataset.save)], btn.dataset.save))
  );
  tbody.querySelectorAll("button[data-remove]").forEach((btn) =>
    btn.addEventListener("click", () => removeRow(envId, invRows[Number(btn.dataset.remove)]))
  );
  syncBecomeBar();
  gateBecome();

  // Always render the Add host form at top of discovered section.
  // Compute current cluster status across all assigned hosts.
  const clusterStatus = REQUIRED_ROLE_SETS.map((req) => {
    const count = invRows.filter((s) => {
      const roles = Array.isArray(s.roles) ? s.roles : [];
      return roles.includes(req.role);
    }).length;
    return { ...req, count };
  });

  const addHostHtml = `
    <div id="srv-add-host">
      <h3>Add host</h3>
      <div id="cluster-status-bar" style="margin:.3rem 0 .5rem"></div>
      <div class="sshkey-auth-section" style="margin-bottom:.4rem">
        <label style="font-size:.8rem;display:flex;align-items:center;gap:.4rem">
          <input type="radio" name="srv-auth-method" value="key" checked /> SSH Key (environment default)
        </label>
        <label style="font-size:.8rem;display:flex;align-items:center;gap:.4rem;margin-top:.2rem">
          <input type="radio" name="srv-auth-method" value="password" /> Username &amp; Password
        </label>
      </div>
      <div id="srv-add-fields">
        <div class="row" style="flex-wrap:wrap;gap:.5rem;align-items:center">
          <input id="srv-add-hostname" type="text" placeholder="hostname" ${gate(canRun(), "operator")} />
          <input id="srv-add-ip" type="text" placeholder="IP address" ${gate(canRun(), "operator")} />
          <input id="srv-add-ssh-user" type="text" placeholder="SSH user (default: root)" ${gate(canRun(), "operator")} />
          <input id="srv-add-ssh-pass" type="password" placeholder="SSH password" style="display:none" ${gate(canRun(), "operator")} />
        </div>
        <div class="srv-key-hint" style="font-size:.72rem;color:var(--fg-muted,#666);margin:.25rem 0">
          <span>Auth: uses the environment's SSH key.</span>
          <button type="button" data-srv-copy-key style="background:none;border:none;color:var(--accent,#4a9eff);cursor:pointer;font-size:.72rem;padding:0">Copy public key</button>
          <span style="margin-left:.3rem">View on</span>
          <button type="button" data-srv-view-key style="background:none;border:none;color:var(--accent,#4a9eff);cursor:pointer;font-size:.72rem;padding:0">Config tab → SSH Keys</button>
        </div>
        <p class="muted" style="margin:.35rem 0 .2rem">Saving a host records it. It does not boot Talos or Ubuntu.</p>
        <div style="margin-top:.35rem;font-size:.78rem;color:var(--fg-muted,#888)">Topology</div>
        <div id="srv-topo-select" style="margin-top:.3rem"></div>
        <div id="srv-add-roles" style="margin-top:.5rem;display:none">
          <div class="row" style="flex-wrap:wrap;gap:.5rem;align-items:center">
            ${roleBoxes("add", 0, [])}
            <button class="secondary btn-sm" id="srv-add-btn-custom" type="button" ${gate(canRun(), "operator")}>Add</button>
          </div>
        </div>
        <div id="srv-add-quick" style="margin-top:.5rem">
          <button class="secondary btn-sm" id="srv-add-btn-quick" type="button" ${gate(canRun(), "operator")}>Add</button>
        </div>
      </div>
    </div>`;
  document.getElementById("srv-add-host").innerHTML = addHostHtml;
  // Render cluster status bar
  renderClusterStatus(clusterStatus);
  // Render topology preset selector
  renderTopologySelector();
  // Toggle password field visibility based on auth method
  const authRadios = document.querySelectorAll('input[name="srv-auth-method"]');
  authRadios.forEach(r => r.addEventListener('change', () => {
    const passField = document.getElementById('srv-add-ssh-pass');
    if (passField) passField.style.display = r.value === 'password' && r.checked ? '' : 'none';
  }));
  for (const addBtnId of ["srv-add-btn-custom", "srv-add-btn-quick"]) {
    const b = document.getElementById(addBtnId);
    if (b) b.addEventListener("click", () => addHost(envId, b));
  }

  // Optional OVH dedicated-server import.
  const ovhSection = document.createElement("details");
  ovhSection.id = "srv-ovh-import";
  ovhSection.style.marginTop = ".9rem";
  ovhSection.innerHTML = `
    <summary>Import from OVH</summary>
    <p class="muted" style="font-size:.78rem;margin:.4rem 0">
      List this OVH account's dedicated servers and add selected ones to the
      inventory as <strong>source: ovh</strong>. Roles are suggested from each
      server's specs. Talos BYOI uses this identity.
    </p>
    <div id="srv-ovh-box"></div>`;
  if (ovhBound && !invRows.length) ovhSection.open = true;
  if (ovhBound) document.getElementById("srv-add-host").appendChild(ovhSection);
  ovhSection.addEventListener("toggle", async () => {
    if (!ovhSection.open) return;
    const box = document.getElementById("srv-ovh-box");
    if (box.dataset.loaded) return;
    box.dataset.loaded = "1";
    await import("../ovh.js").then(async ({ ovhAccountPicker, ovhServerTable, ovhEnvStatus }) => {
      const status = await ovhEnvStatus(envId);
      if (!status.account_id) {
        await new Promise((resolve) =>
          ovhAccountPicker({ envId, box, onPicked: () => { box.innerHTML = ""; ovhServerTable({ envId, box, onUse: importOvh }); resolve(); } })
        );
        return;
      }
      if (!status.has_consumer_key) {
        // Bound, but the account's key is missing: picker lets the operator
        // switch/unbind; a platform admin must Connect the account in Admin.
        box.innerHTML = "";
        await ovhAccountPicker({ envId, box, onPicked: () => mountOvhServers(box) });
        return;
      }
      async function mountOvhServers(b) {
        b.innerHTML = "";
        await ovhServerTable({ envId, box: b, onUse: importOvh });
      }
      await mountOvhServers(box);
    });
  });
  function importOvh(chosen) {
    // Feed selected servers through the same /servers/static path the Add host
    // form uses, then refresh the inventory table.
    (async () => {
      let ok = 0;
      for (const s of chosen) {
        try {
          await api(`/api/v1/environments/${encodeURIComponent(envId)}/servers/static`, {
            method: "POST",
            body: JSON.stringify({
              hostname: s.hostname || s.server_id,
              ip: s.ip || s.private_ip || s.public_ip || null,
              roles: s.roles || [],
              source: "ovh",
              service_name: s.server_id || s.hostname || null,
              public_ip: s.public_ip || null,
              private_ip: s.private_ip || null,
              private_mac: s.private_mac || null,
              vrack_vni: s.vrack_vni || null,
            }),
          });
          ok += 1;
        } catch (e) {
          toast(`${s.hostname || s.server_id}: ${e.message}`, "bad");
        }
      }
      if (ok) {
        toast(`${ok} OVH server(s) imported (source: ovh)`, "ok");
        loadServersCard(envId);
      }
    })();
  }

  discovered.innerHTML = ovhBound
    ? `<p class="muted" style="margin:.3rem 0">Import dedicated servers from the bound OVH account, or add a host by hostname and IP.</p>`
    : `<p class="muted" style="margin:.3rem 0">Add a host by hostname and IP. This only saves the host. It does not boot it. Use this for a Talos ISO, or for Ubuntu that is already installed.</p>`;
}

async function saveInventoryRow(envId, server, rowIdx) {
  const info = server && typeof server === "object" ? server : {};
  const err = document.getElementById("srv-err");
  const msgSpan = document.querySelector(`[data-save-msg="${rowIdx}"]`);
  err.innerHTML = "";
  const roles = checkedRoles("inv", rowIdx);
  if (msgSpan) msgSpan.textContent = "saving…";
  const url = `/api/v1/environments/${encodeURIComponent(envId)}/servers/static`;
  const body = {
    hostname: info.hostname || "",
    ip: info.ip || null,
    ssh_user: info.ssh_user || null,
    roles,
    source: info.source || "static",
    service_name: info.service_name || null,
  };
  try {
    await api(url, { method: "POST", body: JSON.stringify(body) });
    toast(`${info.hostname || "server"}: assignment saved`, "ok");
    await loadServersCard(envId);
  } catch (e) {
    if (msgSpan) msgSpan.textContent = "";
    err.innerHTML = `<div class="error">${esc(info.hostname || "server")}: ${esc(e.message)}</div>`;
    if (e.status === 403) toast("Insufficient role: operator required", "bad");
  }
}

async function saveDiscoveredRow(envId, server, rowIdx) {
  const info = server && typeof server === "object" ? server : {};
  const err = document.getElementById("srv-err");
  const msgSpan = document.querySelector(`[data-disc-save-msg="${rowIdx}"]`);
  err.innerHTML = "";
  const roles = checkedRoles("disc", rowIdx);
  if (msgSpan) msgSpan.textContent = "saving…";
  try {
    await api(`/api/v1/environments/${encodeURIComponent(envId)}/servers/assign`, {
      method: "POST",
      body: JSON.stringify({
        system_id: info.system_id || "",
        hostname: info.hostname || "",
        roles,
        ip: info.ip || "",
      }),
    });
    toast(`${info.hostname || info.system_id || "server"}: assignment saved`, "ok");
    await loadServersCard(envId);
  } catch (e) {
    if (msgSpan) msgSpan.textContent = "";
    err.innerHTML = `<div class="error">${esc(info.hostname || info.system_id || "server")}: ${esc(e.message)}</div>`;
    if (e.status === 403) toast("Insufficient role: operator required", "bad");
  }
}

async function removeRow(envId, server) {
  const info = server && typeof server === "object" ? server : {};
  const err = document.getElementById("srv-err");
  err.innerHTML = "";
  try {
    await api(`/api/v1/environments/${encodeURIComponent(envId)}/servers/remove`, {
      method: "POST",
      body: JSON.stringify({ hostname: info.hostname || "" }),
    });
    toast(`${info.hostname || "server"}: removed from inventory`, "ok");
    await loadServersCard(envId);
  } catch (e) {
    err.innerHTML = `<div class="error">${esc(info.hostname || "server")}: ${esc(e.message)}</div>`;
    if (e.status === 403) toast("Insufficient role: operator required", "bad");
  }
}

function renderClusterStatus(statuses) {
  const bar = document.getElementById("cluster-status-bar");
  if (!bar) return;
  bar.innerHTML = statuses
    .map((s) => {
      const ok = s.count >= s.min;
      const icon = ok ? "\u2705" : "\u26A0";
      const cls = ok ? "ok" : "warn";
      return `<span class="pill ${cls}" style="margin:0 .2rem">${icon} ${esc(s.label)}: ${s.count}/${s.min}</span>`;
    })
    .join("");
}

function renderTopologySelector() {
  const container = document.getElementById("srv-topo-select");
  if (!container) return;
  const gateAttr = gate(canRun(), "operator");
  container.innerHTML = Object.entries(TOPOLOGY_PRESETS)
    .map(([key, preset]) =>
      `<label style="display:flex;align-items:flex-start;gap:.4rem;margin-bottom:.2rem;cursor:pointer;font-size:.78rem">
        <input type="radio" name="srv-topology" value="${key}"${key === "control" ? " checked" : ""} style="margin-top:.15rem" ${gateAttr} />
        <div>
          <strong>${esc(preset.label)}</strong>
          <div class="muted" style="font-size:.72rem">${esc(preset.desc)}</div>
        </div>
      </label>`
    )
    .join("");
  // Wire change: show/hide custom role checkboxes, or set roles on the quick-add button.
  container.querySelectorAll('input[name="srv-topology"]').forEach((radio) => {
    radio.addEventListener("change", () => {
      const preset = TOPOLOGY_PRESETS[radio.value];
      const rolesArea = document.getElementById("srv-add-roles");
      const quickArea = document.getElementById("srv-add-quick");
      if (radio.value === "custom") {
        rolesArea.style.display = "";
        quickArea.style.display = "none";
      } else {
        rolesArea.style.display = "none";
        quickArea.style.display = "";
      }
    });
  });
}

export function destroyServersCard() {
  clearOvhPoll();
  adoptingOvh = false;
}

async function addHost(envId, addBtn = null) {
  const card = document.getElementById("srv-card");
  const authMethod = card?.querySelector('input[name="srv-auth-method"]:checked')?.value || "key";
  const err = document.getElementById("srv-err");
  err.innerHTML = "";
  const hostname = (document.getElementById("srv-add-hostname") || {}).value || "";
  const ip = (document.getElementById("srv-add-ip") || {}).value || "";
  const sshUser = (document.getElementById("srv-add-ssh-user") || {}).value || "";
  const sshPass = (document.getElementById("srv-add-ssh-pass") || {}).value || "";
  // Resolve roles from topology preset or manual checkboxes.
  const topoVal = card?.querySelector('input[name="srv-topology"]:checked')?.value || "control";
  let roles;
  if (topoVal === "custom") {
    roles = checkedRoles("add", 0);
  } else {
    const preset = TOPOLOGY_PRESETS[topoVal];
    roles = preset ? preset.roles : ["k8s_control_plane", "etcd", "control"];
  }
  if (!hostname.trim()) {
    err.innerHTML = `<div class="error">Hostname is required. Make sure it's unique and valid.</div>`;
    return;
  }
  if (addBtn) { addBtn.disabled = true; addBtn.textContent = "Adding…"; }
  const body = {
    hostname: hostname.trim(),
    ip: ip.trim() || null,
    ssh_user: sshUser.trim() || null,
    roles,
    ssh_auth_method: authMethod,
  };
  if (authMethod === "password" && sshPass) {
    body.ssh_password = sshPass;
  }
  try {
    await api(`/api/v1/environments/${encodeURIComponent(envId)}/servers/static`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    toast(`${hostname.trim()}: added to inventory`, "ok");
    const passField = document.getElementById("srv-add-ssh-pass");
    if (passField) passField.value = "";
    await loadServersCard(envId);
  } catch (e) {
    let msg = e.message;
    if (e.status === 409) msg = `Hostname "${esc(hostname.trim())}" already exists. Choose a unique name.`;
    else if (e.isNetwork || e.isTimeout) msg = `Failed to add server. Check your connection and try again.`;
    else msg = `Failed to add server: ${msg}`;
    err.innerHTML = `<div class="error">${esc(msg)}</div>`;
    toast(`Failed to add server: ${e.message}`, "error");
    if (e.status === 403) toast("Insufficient role: operator required", "bad");
  } finally {
    if (addBtn) { addBtn.disabled = false; addBtn.textContent = "Add"; }
  }
}
