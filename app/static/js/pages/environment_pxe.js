// pages/environment_pxe.js — "PXE Control" card on the environment detail page.
// Per-agent PXE configs: each enrolled agent can have its own PXE provisioning
// settings.  Actions: enable PXE on an agent, prep & render, restart sidecar,
// edit config, clear PXE.  Data comes from
// GET  /api/v1/environments/{id}/agents
// GET  /api/v1/environments/{id}/pxe
// PATCH /api/v1/environments/{id}/agent/pxe-config
// POST /api/v1/environments/{id}/pxe/agent
// Defensive: every fetch path degrades to a muted note, never a page break.
import { api, esc, toast } from "../api.js";
import { canRun, gate } from "../store.js";

let pxeLoadedEnvId = "";

// ---------- helpers ----------

function fmtSize(bytes) {
  if (bytes == null || bytes <= 0) return "—";
  return (bytes / 1024 / 1024).toFixed(1) + " MB";
}

function statusPill(ok) {
  return ok
    ? '<span class="pill ok">✓</span>'
    : '<span class="pill bad">✗</span>';
}

function connectedPill(connected) {
  return connected
    ? '<span class="pill ok">connected</span>'
    : '<span class="pill bad">offline</span>';
}

function sidecarPill(running) {
  return running
    ? '<span class="pill ok">Running</span>'
    : '<span class="pill bad">Stopped</span>';
}

function setMsg(html) {
  const el = document.getElementById("pxe-msg");
  if (el) el.innerHTML = html;
}

// Build a map {agent_id: agent_config_block} from /pxe response
function agentStatusMap(pxeData) {
  const m = {};
  const list = pxeData && Array.isArray(pxeData.agent_configs) ? pxeData.agent_configs : [];
  for (const ac of list) {
    m[ac.agent_id] = ac;
  }
  return m;
}

// ---------- per-agent block rendering ----------

function agentWithConfigHtml(envId, agent, ac) {
  const cfg = agent.pxe_config || {};
  const assets = ac && ac.assets ? ac.assets : {};
  const agentId = esc(agent.agent_id);
  const name = esc(agent.name);
  const hostname = esc(agent.hostname);
  const sidecarRunning = ac && ac.sidecar_running === true;
  const kernelOk = !!assets.kernel_ready;
  const initrdOk = !!assets.initramfs_ready;
  const dnsmasq = ac && ac.dnsmasq_conf;
  const bootIpXE = ac && ac.boot_ipxe;

  const configItems = [
    ["Interface", cfg.interface || "—"],
    ["Range Start", cfg.range_start || "—"],
    ["Range End", cfg.range_end || "—"],
    ["Gateway", cfg.gateway || "—"],
    ["Kernel", kernelOk ? "✓ ready" : "✗ missing"],
    ["Initramfs", initrdOk ? "✓ ready" : "✗ missing"],
  ];

  let configGrid = `<div class="pxe-config-grid">`;
  for (const [k, v] of configItems) {
    configGrid += `<div class="pxe-config-item"><span class="pxe-config-key">${esc(k)}</span><span class="pxe-config-val">${esc(v)}</span></div>`;
  }
  configGrid += `</div>`;

  let configDetails = "";
  if (dnsmasq || bootIpXE) {
    let inner = "";
    if (dnsmasq) inner += `<details><summary style="cursor:pointer;font-size:.72rem">dnsmasq.conf</summary><pre class="log-inline" style="font-size:.68rem;max-height:8rem;overflow:auto">${esc(dnsmasq)}</pre></details>`;
    if (bootIpXE) inner += `<details><summary style="cursor:pointer;font-size:.72rem">boot.ipxe</summary><pre class="log-inline" style="font-size:.68rem;max-height:8rem;overflow:auto">${esc(bootIpXE)}</pre></details>`;
    configDetails = `<details style="margin-top:.4rem"><summary style="cursor:pointer;font-size:.75rem">Rendered configs</summary>${inner}</details>`;
  }

  return `
  <div class="pxe-agent-block">
    <div class="pxe-agent-header">
      <span class="pxe-agent-name">${name}</span>
      <span class="pxe-agent-hostname">${hostname}</span>
      ${connectedPill(agent.connected)}
      ${sidecarPill(sidecarRunning)}
    </div>
    ${configGrid}
    ${configDetails}
    <div class="pxe-action-bar">
      <button class="btn-sm secondary" data-action="prep" data-agent-id="${agentId}" type="button">Prep &amp; Render</button>
      <button class="btn-sm secondary" data-action="restart" type="button">Restart DHCP</button>
      <button class="btn-sm secondary" data-action="edit" data-agent-id="${agentId}" type="button">Edit</button>
      <button class="btn-sm secondary" data-action="clear" data-agent-id="${agentId}" type="button">Clear</button>
    </div>
  </div>`;
}

function agentEditFormHtml(envId, agent, ac) {
  const cfg = agent.pxe_config || {};
  const agentId = esc(agent.agent_id);
  const name = esc(agent.name);
  const hostname = esc(agent.hostname);
  const g = gate(canRun(), "operator");

  return `
  <div class="pxe-agent-block">
    <div class="pxe-agent-header">
      <span class="pxe-agent-name">${name}</span>
      <span class="pxe-agent-hostname">${hostname}</span>
      ${connectedPill(agent.connected)}
      <span class="pill" style="background:rgba(255,214,0,.15);color:#ffd600;font-size:.68rem">editing</span>
    </div>
    <form id="pxe-edit-${agentId}" class="pxe-form-grid">
      <div class="pxe-form-field">
        <label>interface</label>
        <input name="interface" type="text" value="${esc(cfg.interface || "")}" placeholder="eth1" ${g}/>
      </div>
      <div class="pxe-form-field">
        <label>range_start</label>
        <input name="range_start" type="text" value="${esc(cfg.range_start || "")}" placeholder="10.10.0.100" ${g}/>
      </div>
      <div class="pxe-form-field">
        <label>range_end</label>
        <input name="range_end" type="text" value="${esc(cfg.range_end || "")}" placeholder="10.10.0.199" ${g}/>
      </div>
      <div class="pxe-form-field">
        <label>gateway</label>
        <input name="gateway" type="text" value="${esc(cfg.gateway || "")}" placeholder="10.10.0.1" ${g}/>
      </div>
      <div class="pxe-form-field">
        <label>image_url (optional)</label>
        <input name="image_url" type="text" value="${esc(cfg.image_url || "")}" placeholder="Talos image URL" ${g}/>
      </div>
    </form>
    <div class="pxe-form-actions">
      <button class="btn-sm" data-action="save-edit" data-agent-id="${agentId}" type="button" ${g}>Save</button>
      <button class="btn-sm secondary" data-action="cancel-edit" data-agent-id="${agentId}" type="button">Cancel</button>
    </div>
  </div>`;
}

function agentWithoutConfigHtml(envId, agent) {
  const agentId = esc(agent.agent_id);
  const name = esc(agent.name);
  const hostname = esc(agent.hostname);
  const g = gate(canRun(), "operator");

  return `
  <div class="pxe-agent-block">
    <div class="pxe-agent-header">
      <span class="pxe-agent-name">${name}</span>
      <span class="pxe-agent-hostname">${hostname}</span>
      ${connectedPill(agent.connected)}
      <span class="pill" style="background:rgba(255,255,255,.05);color:var(--fg-muted,#666);font-size:.68rem">PXE not configured</span>
    </div>
    <p style="font-size:.72rem;color:var(--fg-muted,#555);margin:.2rem 0 .3rem">Configure the DHCP pool and boot image for this agent's L2 network.</p>
    <form id="pxe-agent-${agentId}" class="pxe-form-grid">
      <input type="hidden" name="agent_id" value="${agentId}">
      <div class="pxe-form-field">
        <label>interface</label>
        <input name="interface" type="text" value="eth1" placeholder="eth1" ${g}/>
      </div>
      <div class="pxe-form-field">
        <label>range_start</label>
        <input name="range_start" type="text" value="" placeholder="10.10.0.100" ${g}/>
      </div>
      <div class="pxe-form-field">
        <label>range_end</label>
        <input name="range_end" type="text" value="" placeholder="10.10.0.199" ${g}/>
      </div>
      <div class="pxe-form-field">
        <label>gateway</label>
        <input name="gateway" type="text" value="" placeholder="10.10.0.1" ${g}/>
      </div>
      <div class="pxe-form-field">
        <label>image_url (optional)</label>
        <input name="image_url" type="text" placeholder="Talos image URL" ${g}/>
      </div>
    </form>
    <div class="pxe-form-actions">
      <button class="btn-sm" data-action="enable" data-agent-id="${agentId}" type="button" ${g}>Enable PXE</button>
    </div>
  </div>`;
}

// ---------- body builder ----------

function bodyHtml(envId, agents, pxeData) {
  const agentStatus = agentStatusMap(pxeData);
  const agentList = Array.isArray(agents) ? agents : [];

  if (agentList.length === 0) {
    return `
      <div class="pxe-empty">
        <div class="pxe-empty-icon">⚡</div>
        <p><strong>No agents enrolled yet</strong></p>
        <p>Each agent runs PXE/DHCP on its L2 network to boot bare-metal nodes.</p>
        <div class="pxe-hint">
          Go to the <strong>Inventory tab → Agents</strong> card to install an agent
          on a host on your provisioning network.
        </div>
      </div>`;
  }

  let html = "";
  for (const agent of agentList) {
    if (agent.pxe_config) {
      html += agentWithConfigHtml(envId, agent, agentStatus[agent.agent_id]);
    } else {
      html += agentWithoutConfigHtml(envId, agent);
    }
  }
  return html;
}

// ---------- actions ----------

async function pxePostAction(envId, path, label) {
  try {
    const res = await api(`/api/v1/environments/${encodeURIComponent(envId)}/pxe${path}`, {
      method: "POST",
    });
    const detail = res && res.detail ? res.detail : (typeof res === "string" ? res : "done");
    toast(`${label}: ${esc(detail)}`, "ok");
    return true;
  } catch (e) {
    toast(`${label} failed: ${e.message}`, "bad");
    return false;
  }
}

async function patchAgentPxeConfig(envId, agentId, pxeConfig) {
  return api(`/api/v1/environments/${encodeURIComponent(envId)}/agent/pxe-config`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ agent_id: agentId, pxe_config: pxeConfig }),
  });
}

function readFormFields(formEl) {
  const fd = new FormData(formEl);
  const obj = {};
  for (const [k, v] of fd.entries()) {
    obj[k] = v;
  }
  return obj;
}

// ---------- public API ----------

export function pxeCardHtml() {
  return `
  <style>
    /* PXE card styles */
    #pxe-card .pxe-empty {
      text-align: center;
      padding: 2rem 1rem;
      color: var(--fg-muted, #666);
    }
    #pxe-card .pxe-empty .pxe-empty-icon {
      font-size: 2rem;
      opacity: .3;
      margin-bottom: .5rem;
    }
    #pxe-card .pxe-empty p {
      font-size: .8rem;
      margin: .2rem 0;
      line-height: 1.4;
    }
    #pxe-card .pxe-empty .pxe-hint {
      font-size: .72rem;
      color: var(--fg-muted, #555);
      margin-top: .5rem;
    }

    .pxe-agent-block {
      border: 1px solid var(--border, #2a2a2a);
      border-radius: .4rem;
      padding: .6rem .75rem;
      margin-bottom: .5rem;
      background: rgba(255,255,255,.02);
    }
    .pxe-agent-header {
      display: flex;
      align-items: center;
      gap: .4rem;
      margin-bottom: .4rem;
      padding-bottom: .3rem;
      border-bottom: 1px solid var(--border, #222);
    }
    .pxe-agent-name {
      font-size: .82rem;
      font-weight: 600;
      color: var(--fg, #ccc);
    }
    .pxe-agent-hostname {
      font-size: .72rem;
      color: var(--fg-muted, #555);
    }

    .pxe-config-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(10rem, 1fr));
      gap: .3rem .5rem;
      margin: .3rem 0;
    }
    .pxe-config-item {
      font-size: .72rem;
    }
    .pxe-config-key {
      color: var(--fg-muted, #555);
      display: block;
      margin-bottom: .05rem;
    }
    .pxe-config-val {
      color: var(--fg, #bbb);
      font-family: monospace;
      font-size: .72rem;
    }

    .pxe-action-bar {
      display: flex;
      align-items: center;
      gap: .3rem;
      flex-wrap: wrap;
      margin-top: .4rem;
    }

    /* PXE enable/edit form */
    .pxe-form-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(12rem, 1fr));
      gap: .4rem;
      margin: .4rem 0;
    }
    .pxe-form-field {
      display: flex;
      flex-direction: column;
      gap: .15rem;
    }
    .pxe-form-field label {
      font-size: .7rem;
      color: var(--fg-muted, #666);
      font-weight: 500;
    }
    .pxe-form-field input {
      padding: .25rem .4rem;
      font-size: .75rem;
      border: 1px solid var(--border, #333);
      border-radius: .2rem;
      background: var(--bg, #111);
      color: var(--fg, #ccc);
      font-family: monospace;
    }
    .pxe-form-field input:focus {
      outline: none;
      border-color: #555;
    }
    .pxe-form-actions {
      display: flex;
      gap: .3rem;
      margin-top: .3rem;
    }
  </style>
  <div class="card" id="pxe-card">
    <div class="toolbar">
      <h2>PXE &amp; DHCP</h2>
      <button class="secondary btn-sm" id="pxe-refresh" type="button">Refresh</button>
      <select id="pxe-agent-list" class="btn-sm secondary" style="font-size:.75rem;padding:2px 6px">
        <option value="__all__">All Agents</option>
      </select>
      <span id="pxe-msg" class="muted"></span>
    </div>
    <div id="pxe-err"></div>
    <div id="pxe-body" class="pxe-empty">
      <div class="pxe-empty-icon">⚡</div>
      <p>Select an environment to view PXE &amp; DHCP settings.</p>
    </div>
  </div>`;
}

export function wirePxeCard(getEnvId) {
  const refresh = document.getElementById("pxe-refresh");
  if (refresh) refresh.addEventListener("click", () => loadPxeCard(getEnvId()));

  const agentSelect = document.getElementById("pxe-agent-list");
  if (agentSelect) {
    agentSelect.addEventListener("change", () => loadPxeCard(getEnvId()));
  }

  // Delegate actions inside the card body
  const card = document.getElementById("pxe-card");
  if (!card) return;

  card.addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-action]");
    if (!btn) return;
    const envId = getEnvId();
    if (!envId) return;

    const action = btn.dataset.action;
    const agentId = btn.dataset.agentId;

    if (action === "enable" && agentId) {
      await handleEnablePxe(envId, agentId);
    } else if (action === "prep" && agentId) {
      await handlePrepAgent(envId, agentId);
    } else if (action === "restart") {
      await handleRestart(envId);
    } else if (action === "edit" && agentId) {
      await handleEditStart(envId, agentId);
    } else if (action === "save-edit" && agentId) {
      await handleEditSave(envId, agentId);
    } else if (action === "cancel-edit" && agentId) {
      await loadPxeCard(envId);
    } else if (action === "clear" && agentId) {
      await handleClearPxe(envId, agentId);
    }
  });
}

export async function loadPxeCard(envId) {
  const body = document.getElementById("pxe-body");
  if (!body) return;
  const err = document.getElementById("pxe-err");
  if (err) err.innerHTML = "";

  if (!envId) {
    setMsg("");
    body.classList.add("muted");
    body.innerHTML = "Select an environment.";
    return;
  }

  if (envId !== pxeLoadedEnvId) {
    // env switched — reset
  }
  pxeLoadedEnvId = envId;
  setMsg("Loading…");

  // Fetch agents and PXE status in parallel
  let agents, pxeData;
  let agentsErr, pxeErr;

  const agentsP = api(`/api/v1/environments/${encodeURIComponent(envId)}/agents`).then(
    (d) => { agents = d; },
    (e) => { agentsErr = e; agents = []; }
  );

  const pxeP = api(`/api/v1/environments/${encodeURIComponent(envId)}/pxe`).then(
    (d) => { pxeData = d; },
    (e) => { pxeErr = e; pxeData = {}; }
  );

  await Promise.all([agentsP, pxeP]);

  if (pxeLoadedEnvId !== envId || !document.getElementById("pxe-body")) return;

  // If both failed bail
  if (agentsErr && pxeErr) {
    setMsg("");
    body.classList.remove("muted");
    body.innerHTML = `<div class="muted">Failed to load PXE data.</div>`;
    if (err) err.innerHTML = `<div class="error">${esc(pxeErr.message)}</div>`;
    return;
  }

  // Update agent dropdown
  const agentListEl = document.getElementById("pxe-agent-list");
  if (agentListEl && Array.isArray(agents)) {
    const currentVal = agentListEl.value;
    agentListEl.innerHTML = '<option value="__all__">All Agents</option>';
    for (const a of agents) {
      const opt = document.createElement("option");
      opt.value = a.agent_id;
      opt.textContent = `${a.name} (${a.hostname})`;
      agentListEl.appendChild(opt);
    }
    // Restore selection if still valid
    if (currentVal && [...agentListEl.options].some((o) => o.value === currentVal)) {
      agentListEl.value = currentVal;
    }
  }

  // Filter agents by dropdown selection
  let filteredAgents = Array.isArray(agents) ? agents : [];
  if (agentListEl && agentListEl.value !== "__all__") {
    filteredAgents = filteredAgents.filter((a) => a.agent_id === agentListEl.value);
  }

  setMsg("");
  body.classList.remove("muted");
  body.innerHTML = bodyHtml(envId, filteredAgents, pxeData);
}

// ---------- action handlers ----------

async function handleEnablePxe(envId, agentId) {
  const form = document.getElementById("pxe-agent-" + agentId);
  if (!form) return;

  const fd = readFormFields(form);
  const cfg = {};
  if (fd.interface) cfg.interface = fd.interface;
  if (fd.range_start) cfg.range_start = fd.range_start;
  if (fd.range_end) cfg.range_end = fd.range_end;
  if (fd.gateway) cfg.gateway = fd.gateway;
  if (fd.image_url) cfg.image_url = fd.image_url;

  try {
    await patchAgentPxeConfig(envId, agentId, cfg);
    toast("PXE enabled for agent", "ok");
    await loadPxeCard(envId);
  } catch (e) {
    toast("Enable PXE failed: " + e.message, "bad");
  }
}

async function handlePrepAgent(envId, agentId) {
  try {
    const res = await api(
      `/api/v1/environments/${encodeURIComponent(envId)}/pxe/agent`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent_id: agentId }),
      }
    );
    const detail = res && res.detail ? res.detail : "done";
    toast("Prep & Render: " + esc(detail), "ok");
    await loadPxeCard(envId);
  } catch (e) {
    toast("Prep & Render failed: " + e.message, "bad");
  }
}

async function handleRestart(envId) {
  await pxePostAction(envId, "/restart", "Restart sidecar");
}

async function handleEditStart(envId, agentId) {
  let agents;
  try {
    agents = await api(`/api/v1/environments/${encodeURIComponent(envId)}/agents`);
  } catch {
    toast("Could not reload agent data for editing", "bad");
    return;
  }
  const agent = (Array.isArray(agents) ? agents : []).find((a) => a.agent_id === agentId);
  if (!agent || !agent.pxe_config) {
    toast("Agent config not found", "bad");
    return;
  }

  let pxeData = {};
  try { pxeData = await api(`/api/v1/environments/${encodeURIComponent(envId)}/pxe`); } catch {}

  const body = document.getElementById("pxe-body");
  if (!body) return;

  const agentListEl = document.getElementById("pxe-agent-list");
  let filteredAgents = Array.isArray(agents) ? agents : [];
  if (agentListEl && agentListEl.value !== "__all__") {
    filteredAgents = filteredAgents.filter((a) => a.agent_id === agentListEl.value);
  }

  const agentStatus = agentStatusMap(pxeData);
  let html = "";
  for (const ag of filteredAgents) {
    if (ag.agent_id === agentId && ag.pxe_config) {
      html += agentEditFormHtml(envId, ag, agentStatus[ag.agent_id]);
    } else if (ag.pxe_config) {
      html += agentWithConfigHtml(envId, ag, agentStatus[ag.agent_id]);
    } else {
      html += agentWithoutConfigHtml(envId, ag);
    }
  }
  body.innerHTML = html;
}

async function handleEditSave(envId, agentId) {
  const form = document.getElementById("pxe-edit-" + agentId);
  if (!form) return;

  const fd = readFormFields(form);
  const cfg = {};
  if (fd.interface) cfg.interface = fd.interface;
  if (fd.range_start) cfg.range_start = fd.range_start;
  if (fd.range_end) cfg.range_end = fd.range_end;
  if (fd.gateway) cfg.gateway = fd.gateway;
  if (fd.image_url) cfg.image_url = fd.image_url;

  try {
    await patchAgentPxeConfig(envId, agentId, cfg);
    toast("Config updated", "ok");
    await loadPxeCard(envId);
  } catch (e) {
    toast("Save failed: " + e.message, "bad");
  }
}

async function handleClearPxe(envId, agentId) {
  if (!confirm("Clear PXE config for this agent?")) return;
  try {
    await patchAgentPxeConfig(envId, agentId, null);
    toast("PXE config cleared", "ok");
    await loadPxeCard(envId);
  } catch (e) {
    toast("Clear PXE failed: " + e.message, "bad");
  }
}

export function destroyPxeCard() {}
