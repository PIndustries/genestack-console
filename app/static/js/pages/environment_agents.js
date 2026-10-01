// pages/environment_agents.js — Agents card for the Inventory tab.
// Two flows:
//  1) Auto-install: enter host + SSH creds → console SSH-pushes the agent
//  2) Manual: one copy-paste command to run on the target host
// Then lists enrolled agents with connection status.
import { api, esc, fmtTime, toast } from "../api.js";

const AGENT_POLL_MS = 15000;
let agentTimer = null;
let agentEnvId = "";
let installJobId = "";
let installTimer = null;
const INSTALL_POLL_MS = 5000;

export function agentsCardHtml() {
  return `<div class="card" id="agents-card">
    <style>
      #agents-card .agents-toolbar { display: flex; align-items: center; gap: .5rem; justify-content: space-between; }
      #agents-card .agents-toolbar h2 { margin: 0; font-size: .95rem; }
      #agents-card .agents-body { margin-top: .5rem; }
      #agents-card .agent-row {
        display: flex; align-items: center; gap: .5rem;
        padding: .4rem 0; border-bottom: 1px solid var(--border, #2a2a2a);
        font-size: .8rem;
      }
      #agents-card .agent-row:last-child { border-bottom: none; }
      #agents-card .agent-name { font-weight: 600; min-width: 7rem; }
      #agents-card .agent-host { color: var(--fg-muted, #666); min-width: 6rem; }
      #agents-card .agent-ver { font-size: .72rem; color: var(--fg-muted, #666); }
      #agents-card .agent-seen { font-size: .72rem; color: var(--fg-muted, #666); margin-left: auto; }
      #agents-card .install-form { display: flex; flex-wrap: wrap; gap: .4rem; align-items: center; margin-top: .4rem; }
      #agents-card .install-form input { font-size: .8rem; padding: .3rem .5rem; border: 1px solid var(--border,#333); border-radius: .3rem; background: rgba(0,0,0,.2); color: var(--fg,#ccc); }
      #agents-card .install-form input::placeholder { color: var(--fg-muted,#555); }
      #agents-card .install-form input:focus { outline: 1px solid var(--accent,#4a9eff); }
      #agents-card .install-status { margin-top: .3rem; font-size: .78rem; }
      #agents-card .section-label { font-size: .75rem; color: var(--fg-muted,#666); margin-top: .6rem; margin-bottom: .2rem; }
      #agents-card .pxe-panel { margin-top: .3rem; padding: .5rem; background: rgba(0,0,0,.15); border-radius: .3rem; font-size: .78rem; }
      #agents-card .pxe-panel summary { cursor: pointer; font-weight: 600; font-size: .8rem; }
      #agents-card .pxe-panel .pxe-fields { display: grid; grid-template-columns: auto 1fr; gap: .3rem .5rem; margin-top: .3rem; align-items: center; }
      #agents-card .pxe-panel label { color: var(--fg-muted, #888); }
      #agents-card .pxe-panel input { font-size: .78rem; padding: .25rem .4rem; border: 1px solid var(--border,#333); border-radius: .3rem; background: rgba(0,0,0,.2); color: var(--fg,#ccc); }
      #agents-card .pxe-panel input:focus { outline: 1px solid var(--accent,#4a9eff); }
      #agents-card .pxe-panel .pxe-actions { grid-column: 1 / -1; display: flex; gap: .3rem; margin-top: .2rem; }
      #agents-card .pxe-panel .pxe-hint { font-size: .72rem; color: var(--fg-muted, #555); grid-column: 1 / -1; margin-top: .1rem; }
    </style>
    <div class="agents-toolbar">
      <h2>Agents</h2>
      <span class="muted" id="agents-msg"></span>
    </div>
    <div class="agents-body" id="agents-body">
      <div class="card-empty">Loading…</div>
    </div>
  </div>`;
}

export function wireAgentsCard(getEnvId) {
  const card = document.getElementById("agents-card");
  if (!card) return;

  card.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-agents-action]");
    if (!btn) return;
    const envId = getEnvId();
    if (!envId) return;

    if (btn.dataset.agentsAction === "auto-install") {
      doAutoInstall(envId);
    } else if (btn.dataset.agentsAction === "manual-token") {
      doManualToken(envId);
    } else if (btn.dataset.agentsAction === "pxe-save") {
      doPxeSave(envId, btn.dataset.agentId);
    } else if (btn.dataset.agentsAction === "pxe-clear") {
      doPxeClear(envId, btn.dataset.agentId);
    }
  });

  card.addEventListener("submit", (e) => {
    if (e.target.closest("#agents-install-form")) {
      e.preventDefault();
      doAutoInstall(getEnvId());
    }
  });
}

export function destroyAgentsCard() {
  clearAgentTimer();
  clearInstallTimer();
}

export async function loadAgentsCard(envId) {
  const body = document.getElementById("agents-body");
  const msg = document.getElementById("agents-msg");
  if (!body) return;

  if (!envId) {
    body.innerHTML = `<div class="card-empty">Select an environment.</div>`;
    clearAgentTimer();
    return;
  }

  agentEnvId = envId;

  let status = null;
  try {
    status = await api(`/api/v1/environments/${encodeURIComponent(envId)}/agent/status`);
  } catch {
    status = null;
  }

  let agents = [];
  try {
    agents = await api(`/api/v1/environments/${encodeURIComponent(envId)}/agents`);
  } catch {
    agents = [];
  }

  if (msg) msg.textContent = "";
  render({ status: status || {}, agents: Array.isArray(agents) ? agents : [] });
  startAgentTimer(envId);
}

function render(data) {
  const body = document.getElementById("agents-body");
  if (!body) return;

  const status = data.status || {};
  const agents = data.agents || [];
  const connectedCount = status.connected_count || 0;
  const total = agents.length;

  let html = `
    <div class="section-label">Install agent on a host</div>
    <form id="agents-install-form" onsubmit="return false">
      <div class="install-form">
        <input id="ag-host" type="text" placeholder="host (IP or hostname)" required style="flex:2;min-width:10rem" />
        <input id="ag-user" type="text" placeholder="ssh user" value="root" style="width:5rem" />
        <input id="ag-port" type="number" placeholder="port" value="22" min="1" max="65535" style="width:4rem" />
        <input id="ag-name" type="text" placeholder="agent name (optional)" style="flex:1.5;min-width:8rem" />
        <button class="btn-sm" type="submit" data-agents-action="auto-install">Install</button>
      </div>
    </form>
    <div id="ag-install-status" class="install-status"></div>
    <div class="section-label">Or run manually on the target host</div>
    <div style="display:flex;gap:.4rem;align-items:center">
      <button class="btn-sm secondary" data-agents-action="manual-token">Get one-liner command</button>
    </div>`;

  if (total) {
    html += `
      <div class="section-label">Enrolled agents (${connectedCount}/${total} connected)</div>`;
    agents.forEach((a, idx) => {
      const isOnline = a.connected === true;
      const dotClass = isOnline ? "ok" : "off";
      const name = a.name || a.agent_id?.slice(0, 8) || "(unnamed)";
      const host = a.hostname || "—";
      const ver = a.version || "";
      const seen = a.last_seen ? fmtTime(a.last_seen) : "";
      const agentId = a.agent_id || "";
      const hasPxe = !!a.pxe_config;

      html += `
        <div class="agent-row">
          <span class="fl-agent-dot ${dotClass}" title="${isOnline ? 'connected' : 'offline'}"></span>
          <span class="agent-name">${esc(name)}</span>
          <span class="agent-host">${esc(host)}</span>
          ${ver ? `<span class="agent-ver">${esc(ver)}</span>` : ""}
          ${seen ? `<span class="agent-seen">${seen}</span>` : ""}
        </div>
        <details class="pxe-panel">
          <summary>⚡ PXE/DHCP${hasPxe ? " (configured)" : ""}</summary>
          <div class="pxe-fields" data-agent-id="${esc(agentId)}">
            <label>Network interface</label>
            <input type="text" data-pxe-field="interface" placeholder="e.g. eth1" value="${esc(String(a.pxe_config?.interface || ""))}" />

            <label>DHCP range start</label>
            <input type="text" data-pxe-field="range_start" placeholder="e.g. 192.168.1.100" value="${esc(String(a.pxe_config?.range_start || ""))}" />

            <label>DHCP range end</label>
            <input type="text" data-pxe-field="range_end" placeholder="e.g. 192.168.1.200" value="${esc(String(a.pxe_config?.range_end || ""))}" />

            <label>Netmask</label>
            <input type="text" data-pxe-field="netmask" placeholder="255.255.255.0" value="${esc(String(a.pxe_config?.netmask || ""))}" />

            <label>Gateway</label>
            <input type="text" data-pxe-field="gateway" placeholder="e.g. 192.168.1.1" value="${esc(String(a.pxe_config?.gateway || ""))}" />

            <label>DNS</label>
            <input type="text" data-pxe-field="dns" placeholder="e.g. 8.8.8.8" value="${esc(String(a.pxe_config?.dns || ""))}" />

            <label>TFTP server</label>
            <input type="text" data-pxe-field="next_server" placeholder="defaults to gateway" value="${esc(String(a.pxe_config?.next_server || ""))}" />

            <label>Boot image URL</label>
            <input type="text" data-pxe-field="image_url" placeholder="Talos initramfs URL" value="${esc(String(a.pxe_config?.image_url || ""))}" />

            <span class="pxe-hint">This agent will serve DHCP + PXE on the provisioning network. Talos nodes boot via iPXE → initramfs.</span>
            <div class="pxe-actions">
              <button class="btn-sm secondary" data-agents-action="pxe-save" data-agent-id="${esc(agentId)}">Save PXE config</button>
              ${hasPxe ? `<button class="btn-sm secondary" data-agents-action="pxe-clear" data-agent-id="${esc(agentId)}">Clear</button>` : ""}
            </div>
            <span class="pxe-status" data-pxe-status data-agent-id="${esc(agentId)}"></span>
          </div>
        </details>`;
    });
  }

  body.innerHTML = html;
}

// ---------- auto-install (SSH push) ----------

async function doAutoInstall(envId) {
  const hostEl = document.getElementById("ag-host");
  const userEl = document.getElementById("ag-user");
  const portEl = document.getElementById("ag-port");
  const nameEl = document.getElementById("ag-name");
  const statusEl = document.getElementById("ag-install-status");
  const installBtn = document.querySelector("#agents-install-form [data-agents-action=\"auto-install\"]");

  const host = hostEl?.value.trim();
  if (!host) {
    setStatus(statusEl, "error", "Host is required");
    hostEl?.focus();
    return;
  }

  const sshUser = userEl?.value.trim() || "root";
  const sshPort = parseInt(portEl?.value, 10) || 22;
  const name = nameEl?.value.trim();

  // Disable button during request
  if (installBtn) {
    installBtn.disabled = true;
    installBtn.textContent = "Installing…";
  }

  installJobId = "";
  setStatus(statusEl, "info", "Creating install job…");

  try {
    const params = { host, ssh_user: sshUser, ssh_port: sshPort };
    if (name) params.name = name;

    const job = await api(`/api/v1/environments/${encodeURIComponent(envId)}/jobs`, {
      method: "POST",
      body: JSON.stringify({ operation: "agent.install", params }),
    });

    const id = job?.id;
    if (!id) {
      setStatus(statusEl, "warn", "Install job created but no job ID returned");
      return;
    }

    installJobId = id;
    setStatus(statusEl, "info", `Installing on ${esc(host)}… (${id.slice(0, 8)})`);
    pollInstallJob(envId, id, statusEl);

  } catch (e) {
    let msg = e.message;
    if (e.isNetwork || e.isTimeout) {
      msg = `Could not SSH to ${esc(host)}. Check IP, SSH user, port, and firewall rules. (${e.message})`;
    } else if (e.status === 400 && /unknown operation/i.test(String(e.message || ""))) {
      setStatus(statusEl, "warn", "Agent push-install not available on this backend — use Get one-liner command instead");
      return;
    }
    setStatus(statusEl, "error", `Install failed: ${esc(msg)}`);
  } finally {
    if (installBtn) {
      installBtn.disabled = false;
      installBtn.textContent = "Install";
    }
  }
}

async function pollInstallJob(envId, jobId, statusEl) {
  clearInstallTimer();

  try {
    const job = await api(`/api/v1/jobs/${encodeURIComponent(jobId)}`);
    const st = String(job?.status || "").toLowerCase();

    if (st === "queued" || st === "running") {
      setStatus(statusEl, "info", `Installing… (${st})`);
      installTimer = setTimeout(() => pollInstallJob(envId, jobId, statusEl), INSTALL_POLL_MS);
      return;
    }

    if (st === "success") {
      setStatus(statusEl, "ok", "Agent installed successfully");
      installJobId = "";
      loadAgentsCard(envId);
      // Notify workflow stepper of potential state change
      const evt = new CustomEvent("env-state-changed", { detail: { envId, type: "agent-installed" } });
      window.dispatchEvent(evt);
      return;
    }

    const errMsg = job?.error || job?.log_text?.slice(-100) || st;
    setStatus(statusEl, "error", `Install failed: ${esc(String(errMsg))}`);
    installJobId = "";

  } catch {
    // If we can't poll, just stop
    installJobId = "";
  }
}

function clearInstallTimer() {
  if (installTimer) {
    clearTimeout(installTimer);
    installTimer = null;
  }
}

// ---------- manual token (one-liner) ----------

async function doManualToken(envId) {
  const msg = document.getElementById("agents-msg");
  const tokenBtn = document.querySelector('#agents-card [data-agents-action="manual-token"]');
  if (msg) msg.textContent = "Creating…";
  if (tokenBtn) { tokenBtn.disabled = true; tokenBtn.textContent = "Generating…"; }

  try {
    const result = await api(`/api/v1/environments/${encodeURIComponent(envId)}/agent/token`, {
      method: "POST",
    });

    if (result && result.token) {
      showOneLinerModal(result);
      if (msg) msg.textContent = "";
      loadAgentsCard(envId);
    } else {
      toast("Token creation returned no token", "warn");
      if (msg) msg.textContent = "";
    }
  } catch (e) {
    let msgText = e.message;
    if (e.isNetwork || e.isTimeout) msgText = "Failed to reach server. Check your connection and try again.";
    toast(`Token creation failed: ${msgText}`, "error");
    if (msg) msg.textContent = "";
  } finally {
    if (tokenBtn) { tokenBtn.disabled = false; tokenBtn.textContent = "Get one-liner command"; }
  }
}

function showOneLinerModal(payload) {
  const p = payload && typeof payload === "object" ? payload : {};
  const token = typeof p.token === "string" ? p.token : "";
  const name = p.name || p.agent?.name || "";
  // Build one-liner: grab install.sh from console, pipe to bash with TOKEN
  const consoleUrl = window.location.origin;
  const oneLiner = `curl -fsSL ${consoleUrl}/api/v1/agent/install.sh | TOKEN=${esc(token)} bash`;

  const overlay = document.createElement("div");
  overlay.style.cssText = `
    position: fixed; top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,.6); z-index: 1000;
    display: flex; align-items: center; justify-content: center;
  `;
  overlay.innerHTML = `
    <div style="
      background: var(--bg, #1a1a1a); border: 1px solid var(--border, #333);
      border-radius: .5rem; padding: 1.5rem; max-width: 50rem; width: 90%;
      color: var(--fg, #ccc);
    ">
      <h2 style="margin:0 0 .5rem;font-size:1rem">Run this on the target host</h2>
      <p style="font-size:.78rem;color:var(--fg-muted,#666);margin:.3rem 0 .6rem">
        One command — downloads the agent install script and enrolls it with this environment.
      </p>
      <div style="display:flex;align-items:center;gap:.5rem;margin:.4rem 0">
        <code style="
          background:rgba(0,0,0,.3);padding:.4rem .6rem;border-radius:.3rem;
          font-size:.78rem;word-break:break-all;flex:1;
        " class="one-liner-val">${esc(oneLiner)}</code>
        <button class="btn-sm copy-btn">Copy</button>
      </div>
      ${p.docker_run ? `<details style="margin:.5rem 0"><summary style="font-size:.8rem;cursor:pointer">Docker alternative</summary><pre style="font-size:.72rem;overflow-x:auto;white-space:pre-wrap">${esc(p.docker_run)}</pre></details>` : ""}
      <p style="font-size:.72rem;color:var(--fg-muted,#555);margin:.8rem 0 0">
        Token shown once. The console stores only a hash.
      </p>
      <div style="display:flex;justify-content:flex-end;margin-top:1rem">
        <button class="btn-sm close-btn">Done</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  const close = () => overlay.remove();
  overlay.querySelector(".close-btn").addEventListener("click", close);
  overlay.querySelector(".copy-btn").addEventListener("click", () => {
    const val = overlay.querySelector(".one-liner-val").textContent;
    navigator.clipboard.writeText(val).then(() => {
      overlay.querySelector(".copy-btn").textContent = "Copied ✓";
      setTimeout(() => { overlay.querySelector(".copy-btn").textContent = "Copy"; }, 1500);
    }).catch(() => {
      overlay.querySelector(".copy-btn").textContent = "Failed";
      setTimeout(() => { overlay.querySelector(".copy-btn").textContent = "Copy"; }, 1500);
    });
  });
  overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
}

// ---------- per-agent PXE/DHCP config ----------

function readPxeFields(agentId) {
  const container = document.querySelector(`.pxe-fields[data-agent-id="${agentId}"]`);
  if (!container) return null;
  const cfg = {};
  container.querySelectorAll("[data-pxe-field]").forEach((inp) => {
    const val = inp.value.trim();
    if (val) cfg[inp.dataset.pxeField] = val;
  });
  return cfg;
}

function setPxeStatus(agentId, type, msg) {
  const el = document.querySelector(`[data-pxe-status][data-agent-id="${agentId}"]`);
  if (!el) return;
  const cls = type === "ok" ? "ok" : type === "error" ? "error" : "warn";
  el.innerHTML = `<span class="${cls}">${msg}</span>`;
}

async function patchAgentPxeConfig(envId, agentId, pxeConfig) {
  return api(`/api/v1/environments/${encodeURIComponent(envId)}/agent/pxe-config`, {
    method: "PATCH",
    body: JSON.stringify({ agent_id: agentId, pxe_config: pxeConfig }),
  });
}

async function doPxeSave(envId, agentId) {
  const cfg = readPxeFields(agentId);
  if (!cfg) {
    toast("PXE form not found", "error");
    return;
  }
  const missing = ["interface", "range_start", "range_end"].filter((k) => !cfg[k]);
  if (missing.length) {
    setPxeStatus(agentId, "error", `Missing: ${missing.join(", ")}`);
    return;
  }
  setPxeStatus(agentId, "info", "Saving…");
  try {
    await patchAgentPxeConfig(envId, agentId, cfg);
    setPxeStatus(agentId, "ok", "Saved ✓ — agent will apply on next sync");
    toast("PXE config saved", "ok");
    setTimeout(() => loadAgentsCard(envId), 1500);
  } catch (e) {
    setPxeStatus(agentId, "error", `Save failed: ${esc(e.message)}`);
    toast(`PXE save failed: ${e.message}`, "error");
  }
}

async function doPxeClear(envId, agentId) {
  if (!confirm("Clear PXE/DHCP config for this agent?")) return;
  setPxeStatus(agentId, "info", "Clearing…");
  try {
    await patchAgentPxeConfig(envId, agentId, null);
    setPxeStatus(agentId, "ok", "Cleared ✓");
    toast("PXE config cleared", "ok");
    setTimeout(() => loadAgentsCard(envId), 1000);
  } catch (e) {
    setPxeStatus(agentId, "error", `Clear failed: ${esc(e.message)}`);
    toast(`PXE clear failed: ${e.message}`, "error");
  }
}

// ---------- helpers ----------

function startAgentTimer(envId) {
  clearAgentTimer();
  agentEnvId = envId;
  agentTimer = setTimeout(() => {
    if (agentEnvId === envId) loadAgentsCard(envId);
  }, AGENT_POLL_MS);
}

function clearAgentTimer() {
  if (agentTimer) {
    clearTimeout(agentTimer);
    agentTimer = null;
  }
}

function setStatus(el, type, msg) {
  if (!el) return;
  const cls = type === "ok" ? "ok" : type === "error" ? "error" : type === "warn" ? "warn" : "muted";
  el.innerHTML = `<span class="${cls}">${msg}</span>`;
}

// Listen for env-state-changed to refresh the agents card immediately
window.addEventListener("env-state-changed", (e) => {
  if (e.detail.envId === agentEnvId) {
    loadAgentsCard(agentEnvId);
  }
});
