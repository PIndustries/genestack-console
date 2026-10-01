// pages/environment_baremetal.js — "Bare metal (console-managed)" card on the
// environment detail page. Zero-touch bare-metal provisioning without MAAS: nodes
// are registered by BMC credentials (baremetal.node.register), then driven per row
// — power on/off/restart (baremetal.node.power), PXE boot (baremetal.node.pxe_boot),
// and Provision (baremetal.node.provision, admin-gated: PXE boot → wait for the
// talos API → land the node in the env inventory doc). Data comes from
// GET /api/v1/environments/{id}/baremetal; every action is a job (confirm → POST →
// amber running pill + #/activity?tab=jobs&job=<id> link → 5s poll → reload on terminal), mirroring
// the verify/maas patterns in environment_workflow.js / environment_servers.js.
// Defensive: the endpoint may 404 (backend not deployed yet) or return partial
// payloads — every path degrades to a muted note, never a page break.
import { api, esc, fmtTime, toast } from "../api.js";
import { canAdmin, canRun, gate } from "../store.js";

const BM_ACTIVE = new Set(["queued", "running"]);
const BM_POLL_MS = 5000;

let bmLoadedEnvId = ""; // env the card last rendered — guards against env switches
const bmRowJobs = new Map(); // node_id -> active job id (row buttons disabled while set)
const bmTimers = new Map(); // node_id|"register" -> pending poll timeout
const bmDoneIds = new Set(); // terminal jobs already toasted/reloaded for

function clearBmTimers() {
  bmTimers.forEach((t) => clearTimeout(t));
  bmTimers.clear();
}

function unavail(note) {
  return `<div class="muted">Unavailable${note ? " — " + esc(note) : ""}.</div>`;
}

// state pill: registered grey / booting amber / talos-ready green / failed red.
function statePillHtml(state) {
  const s = String(state || "").toLowerCase();
  if (s === "registered") return '<span class="pill">registered</span>';
  if (s === "booting") return '<span class="pill warn">booting</span>';
  if (s === "talos-ready") return '<span class="pill ok">talos-ready</span>';
  if (s === "failed") return '<span class="pill bad">failed</span>';
  return `<span class="pill">${esc(state || "unknown")}</span>`;
}

function bmRunningHtml(label, jobId, status) {
  const id = String(jobId || "");
  return (
    `<span class="pill warn">${esc(label)} ${esc(status)}…</span> ` +
    `<a href="#/activity?tab=jobs&job=${esc(id)}">job ${esc(id.slice(0, 8))}…</a>`
  );
}

function setBmJobLine(nodeId, html) {
  const el = document.querySelector(`#bm-card [data-bm-job="${nodeId}"]`);
  if (el) el.innerHTML = html;
}

function setBmRowBusy(nodeId, busy) {
  document.querySelectorAll(`#bm-card tr[data-node="${nodeId}"] button`).forEach((b) => {
    b.disabled = busy || !canRun();
  });
}

// Poll a baremetal.node.* job every 5s (mirrors pollMaasJob in
// environment_servers.js): update the row's job line while queued/running; on a
// terminal state toast once and reload the card so the node's new state shows.
async function pollBmJob(envId, nodeId, jobId, label) {
  if (!document.getElementById("bm-card")) return; // navigated away
  let job;
  try {
    job = await api(`/api/v1/jobs/${encodeURIComponent(jobId)}`);
  } catch {
    // Transient fetch failure: keep the line as-is and retry on the next tick.
    bmTimers.set(nodeId, setTimeout(() => pollBmJob(envId, nodeId, jobId, label), BM_POLL_MS));
    return;
  }
  if (!document.getElementById("bm-card") || bmLoadedEnvId !== envId) return;
  const status = String(job && job.status ? job.status : "").toLowerCase();
  if (BM_ACTIVE.has(status)) {
    setBmJobLine(nodeId, bmRunningHtml(label, jobId, status));
    bmTimers.set(nodeId, setTimeout(() => pollBmJob(envId, nodeId, jobId, label), BM_POLL_MS));
    return;
  }
  // Terminal state: stop polling, re-enable the row, reload the card once.
  bmRowJobs.delete(nodeId);
  if (bmDoneIds.has(jobId)) return;
  bmDoneIds.add(jobId);
  toast(
    `${label} ${status === "success" ? "succeeded" : status || "finished"}`,
    status === "success" ? "ok" : "bad"
  );
  await loadBaremetalCard(envId);
}

async function startBmJob(envId, nodeId, nodeName, operation, label, params, role) {
  setBmJobLine(nodeId, `<span class="muted">creating ${esc(label)} job…</span>`);
  try {
    const job = await api(`/api/v1/environments/${encodeURIComponent(envId)}/jobs`, {
      method: "POST",
      body: JSON.stringify({ operation, params }),
    });
    const id = job && job.id != null ? String(job.id) : "";
    if (!id) {
      await loadBaremetalCard(envId);
      return;
    }
    bmRowJobs.set(nodeId, id);
    toast(`${nodeName}: ${label} job ${id.slice(0, 8)}… created`, "ok");
    setBmJobLine(nodeId, bmRunningHtml(label, id, "queued"));
    setBmRowBusy(nodeId, true);
    pollBmJob(envId, nodeId, id, label);
  } catch (e) {
    setBmJobLine(nodeId, `<span class="error">${esc(label)} failed to start: ${esc(e.message)}</span>`);
    toast(`${label} failed to start: ${e.message}`, "bad");
    if (e.status === 403) toast(`Insufficient role: ${role} required`, "bad");
  }
}

function bmPower(envId, node, action) {
  const name = (node && node.name) || "?";
  const nodeId = String((node && node.id) || "");
  if (!confirm(`Power ${action} ${name}? This acts on the real hardware via its BMC.`)) return;
  startBmJob(envId, nodeId, name, "baremetal.node.power", `power ${action}`, { node_id: nodeId, action }, "operator");
}

let bmConsoleEscBound = false;

function ensureBmConsoleModal() {
  let el = document.getElementById("bm-console-modal");
  if (el) return el;
  el = document.createElement("div");
  el.id = "bm-console-modal";
  el.className = "os-console-modal";
  el.hidden = true;
  el.innerHTML = `<div class="os-console-panel" role="dialog" aria-modal="true" aria-labelledby="bm-console-title">
      <div class="os-console-head">
        <h2 id="bm-console-title">iLO console</h2>
        <button type="button" class="secondary btn-sm" data-bm-console-close>Close</button>
      </div>
      <iframe id="bm-console-frame" title="iLO remote console" referrerpolicy="no-referrer"></iframe>
      <p class="muted os-console-help">iLO HTML5 KVM via Console proxy. BMC credentials stay on the server.</p>
    </div>`;
  document.body.appendChild(el);
  el.addEventListener("click", (e) => {
    if (e.target === el || e.target.closest("[data-bm-console-close]")) closeBmConsoleModal();
  });
  return el;
}

function onBmConsoleKey(e) {
  if (e.key === "Escape") {
    e.preventDefault();
    closeBmConsoleModal();
  }
}

function closeBmConsoleModal() {
  const frame = document.getElementById("bm-console-frame");
  if (frame) frame.src = "about:blank";
  const modal = document.getElementById("bm-console-modal");
  if (modal) modal.hidden = true;
  if (bmConsoleEscBound) {
    document.removeEventListener("keydown", onBmConsoleKey);
    bmConsoleEscBound = false;
  }
}

async function bmOpenConsole(envId, node) {
  const name = (node && node.name) || "node";
  const nodeId = String((node && node.id) || "");
  if (!nodeId) return;
  const modal = ensureBmConsoleModal();
  const title = document.getElementById("bm-console-title");
  const frame = document.getElementById("bm-console-frame");
  if (title) title.textContent = `iLO console — ${name}`;
  if (frame) frame.src = "about:blank";
  modal.hidden = true;
  try {
    const d = await api(
      `/api/v1/environments/${encodeURIComponent(envId)}/baremetal/nodes/${encodeURIComponent(nodeId)}/console/session`,
      { method: "POST", timeout: 25000 }
    );
    if (!d || !d.ok || !d.embed_url) {
      toast((d && (d.error || d.message)) || "iLO console unavailable", "bad");
      return;
    }
    const embed = String(d.embed_url);
    if (!embed.startsWith("/") || embed.startsWith("//")) {
      toast("iLO console unavailable", "bad");
      return;
    }
    if (frame) frame.src = embed;
    modal.hidden = false;
    if (!bmConsoleEscBound) {
      document.addEventListener("keydown", onBmConsoleKey);
      bmConsoleEscBound = true;
    }
  } catch (err) {
    toast(err && err.message ? err.message : "iLO console failed", "bad");
  }
}

function bmPxeBoot(envId, node) {
  const name = (node && node.name) || "?";
  const nodeId = String((node && node.id) || "");
  if (!confirm(`PXE boot ${name}? The node reboots and boots talos from this console.`)) return;
  startBmJob(envId, nodeId, name, "baremetal.node.pxe_boot", "pxe boot", { node_id: nodeId }, "operator");
}

function bmProvision(envId, node) {
  const name = (node && node.name) || "?";
  const nodeId = String((node && node.id) || "");
  if (
    !confirm(
      `Provision ${name}? Zero-touch: PXE boot → wait for the talos API → add the node ` +
        "to this environment's inventory. This acts on the real hardware."
    )
  )
    return;
  startBmJob(envId, nodeId, name, "baremetal.node.provision", "provision", { node_id: nodeId }, "admin");
}

// Actions cell for a node row: Power ▾ dropdown (on/off/restart), PXE boot, and
// Provision (zero-touch, admin-gated) plus a per-row job line.
function bmActionsCellHtml(node, i) {
  const nodeId = String((node && node.id) || "");
  const opGate = bmRowJobs.has(nodeId)
    ? 'disabled title="job running for this node"'
    : gate(canRun(), "operator");
  const adminGate = bmRowJobs.has(nodeId)
    ? 'disabled title="job running for this node"'
    : gate(canAdmin(), "admin");
  return (
    `<td style="white-space:nowrap">` +
    `<details class="bm-power"><summary>Power ▾</summary>` +
    `<div class="bm-power-menu">` +
    `<button class="secondary btn-sm" type="button" data-bm-power="on" data-row="${i}" title="Power on via the BMC" ${opGate}>Power on</button>` +
    `<button class="secondary btn-sm" type="button" data-bm-power="off" data-row="${i}" title="Force power off via the BMC" ${opGate}>Power off</button>` +
    `<button class="secondary btn-sm" type="button" data-bm-power="restart" data-row="${i}" title="Force restart via the BMC" ${opGate}>Restart</button>` +
    `</div></details> ` +
    `<button class="secondary btn-sm" type="button" data-bm-console="${i}" title="iLO HTML5 remote console via Console proxy" ${opGate}>Console</button> ` +
    `<button class="secondary btn-sm" type="button" data-bm-pxe="${i}" title="PXE-boot into talos (boot once + restart)" ${opGate}>PXE boot</button> ` +
    `<button class="secondary btn-sm" type="button" data-bm-provision="${i}" title="Zero-touch: PXE boot → talos API → inventory" ${adminGate}>Provision</button>` +
    `<div class="bm-job-line" data-bm-job="${esc(nodeId)}"></div></td>`
  );
}

function nodesTableHtml(nodes) {
  const rows = nodes
    .map((n, i) => {
      const info = n && typeof n === "object" ? n : {};
      return `<tr data-row="${i}" data-node="${esc(String(info.id || ""))}">
        <td><strong>${esc(info.name || "?")}</strong></td>
        <td><code>${esc(info.bmc_host || "—")}</code></td>
        <td class="muted">${esc(info.pxe_mac || "—")}</td>
        <td class="muted">${esc(info.expected_ip || "—")}</td>
        <td>${statePillHtml(info.state)}</td>
        <td class="muted">${esc(fmtTime(info.last_seen)) || "—"}</td>
        ${bmActionsCellHtml(info, i)}
      </tr>`;
    })
    .join("");
  return `<table>
      <thead><tr>
        <th>Name</th><th>BMC host</th><th>PXE MAC</th><th>Expected IP</th><th>State</th><th>Last seen</th><th>Actions</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// ---------- register node (baremetal.node.register job) ----------

function setRegLine(html) {
  const el = document.getElementById("bm-reg-line");
  if (el) el.innerHTML = html;
}

// Poll the register job every 5s (mirrors pollBmJob, keyed "register"): update the
// register line while queued/running; on a terminal state toast once, collapse the
// form, and reload the card so the new node row appears.
async function pollRegisterJob(envId, jobId) {
  if (!document.getElementById("bm-card")) return; // navigated away
  let job;
  try {
    job = await api(`/api/v1/jobs/${encodeURIComponent(jobId)}`);
  } catch {
    // Transient fetch failure: keep the line as-is and retry on the next tick.
    bmTimers.set("register", setTimeout(() => pollRegisterJob(envId, jobId), BM_POLL_MS));
    return;
  }
  if (!document.getElementById("bm-card") || bmLoadedEnvId !== envId) return;
  const status = String(job && job.status ? job.status : "").toLowerCase();
  if (BM_ACTIVE.has(status)) {
    setRegLine(bmRunningHtml("register", jobId, status));
    bmTimers.set("register", setTimeout(() => pollRegisterJob(envId, jobId), BM_POLL_MS));
    return;
  }
  // Terminal state: stop polling, toast once, reload the card once.
  if (bmDoneIds.has(jobId)) return;
  bmDoneIds.add(jobId);
  toast(
    `register ${status === "success" ? "succeeded" : status || "finished"}`,
    status === "success" ? "ok" : "bad"
  );
  await loadBaremetalCard(envId);
}

async function registerNode(envId) {
  const val = (id) => {
    const el = document.getElementById(id);
    return el ? el.value.trim() : "";
  };
  const name = val("bm-reg-name");
  const bmcHost = val("bm-reg-bmc-host");
  const bmcUsername = val("bm-reg-bmc-user");
  const bmcPassword = val("bm-reg-bmc-pass");
  const pxeMac = val("bm-reg-pxe-mac");
  if (!name || !bmcHost || !bmcUsername || !bmcPassword) {
    setRegLine('<span class="error">name, BMC host, username, and password are required</span>');
    return;
  }
  const params = { name, bmc_host: bmcHost, bmc_username: bmcUsername, bmc_password: bmcPassword };
  if (pxeMac) params.pxe_mac = pxeMac; // absent → the job auto-reads it from the BMC
  setRegLine('<span class="muted">creating register job…</span>');
  try {
    const job = await api(`/api/v1/environments/${encodeURIComponent(envId)}/jobs`, {
      method: "POST",
      body: JSON.stringify({ operation: "baremetal.node.register", params }),
    });
    const id = job && job.id != null ? String(job.id) : "";
    if (!id) {
      await loadBaremetalCard(envId);
      return;
    }
    toast(`${name}: register job ${id.slice(0, 8)}… created`, "ok");
    setRegLine(bmRunningHtml("register", id, "queued"));
    pollRegisterJob(envId, id);
  } catch (e) {
    setRegLine(`<span class="error">register failed to start: ${esc(e.message)}</span>`);
    toast(`register failed to start: ${e.message}`, "bad");
    if (e.status === 403) toast("Insufficient role: operator required", "bad");
  }
}

// ---------- public API ----------

export function baremetalCardHtml() {
  return `
<style>
#bm-card .toolbar { display:flex; align-items:center; gap:.5rem; padding-bottom:.4rem; margin-bottom:.3rem; border-bottom:1px solid var(--border, #222); flex-wrap:wrap; }
#bm-card .toolbar h2 { margin:0; font-size:.95rem; font-weight:600; }
#bm-card .card-empty { text-align:center; padding:1.5rem 1rem; color:var(--fg-muted, #666); }
#bm-card .card-empty .empty-icon { font-size:1.5rem; opacity:.3; margin-bottom:.4rem; }
#bm-card .card-empty p { font-size:.78rem; margin:.15rem 0; }
#bm-card .card-empty .empty-hint { font-size:.7rem; color:var(--fg-muted, #555); margin-top:.3rem; }
#bm-card .hint-row { display:flex; align-items:center; gap:.5rem; padding:.3rem 0; margin-bottom:.3rem; font-size:.72rem; color:var(--fg-muted, #666); }
#bm-card .pill { display:inline-block; padding:.15rem .45rem; border-radius:.2rem; font-size:.72rem; font-weight:500; }
#bm-card .pill.ok { background:#1a3a1a; color:#4caf50; }
#bm-card .pill.bad { background:#3a1a1a; color:#ef5350; }
#bm-card .pill.warn { background:#3a3a1a; color:#ffc107; }
#bm-card .bm-register { margin-top:.5rem; padding-top:.4rem; border-top:1px solid var(--border, #222); }
</style>
<div class="card span-12" id="bm-card">
  <div class="toolbar">
    <h2>Bare Metal</h2>
    <button class="secondary btn-sm" id="bm-refresh" type="button">Refresh</button>
    <span id="bm-msg" class="muted"></span>
  </div>
  <div class="hint-row">Redfish BMC: iLO console (HTML5 KVM proxied here), power, PXE-boot into Talos, provision</div>
  <div id="bm-err"></div>
  <div id="bm-body" class="muted">
    <div class="card-empty">
      <div class="empty-icon">⬡</div>
      <p>Select an environment to view bare-metal nodes</p>
    </div>
  </div>
  <details class="bm-register">
    <summary>Register node</summary>
    <div class="bm-register-form">
      <input id="bm-reg-name" type="text" placeholder="name" autocomplete="off" ${gate(canRun(), "operator")} />
      <input id="bm-reg-bmc-host" type="text" placeholder="bmc host" autocomplete="off" ${gate(canRun(), "operator")} />
      <input id="bm-reg-bmc-user" type="text" placeholder="bmc username" autocomplete="off" ${gate(canRun(), "operator")} />
      <input id="bm-reg-bmc-pass" type="password" placeholder="bmc password" autocomplete="off" ${gate(canRun(), "operator")} />
      <input id="bm-reg-pxe-mac" type="text" placeholder="pxe mac (optional)" autocomplete="off" ${gate(canRun(), "operator")} />
      <button class="secondary btn-sm" id="bm-reg-submit" type="button" ${gate(canRun(), "operator")}>Register</button>
      <span class="muted" style="font-size:.72rem">pxe mac: leave empty to auto-read from the BMC</span>
    </div>
    <div id="bm-reg-line" class="bm-job-line"></div>
  </details>
</div>`;
}

export function wireBaremetalCard(getEnvId) {
  const refresh = document.getElementById("bm-refresh");
  if (refresh) refresh.addEventListener("click", () => loadBaremetalCard(getEnvId()));
  const submit = document.getElementById("bm-reg-submit");
  if (submit) submit.addEventListener("click", () => registerNode(getEnvId()));
}

export async function loadBaremetalCard(envId) {
  const body = document.getElementById("bm-body");
  if (!body) return;
  if (envId !== bmLoadedEnvId) {
    // Env switched: abandon polling for the previous env's node jobs.
    clearBmTimers();
    bmRowJobs.clear();
  }
  bmLoadedEnvId = envId || "";
  const msg = document.getElementById("bm-msg");
  const err = document.getElementById("bm-err");
  if (err) err.innerHTML = "";
  if (!envId) {
    if (msg) msg.textContent = "";
    body.classList.add("muted");
    body.innerHTML = "Select an environment.";
    return;
  }
  if (msg) msg.textContent = "Loading…";

  let d;
  try {
    d = await api(`/api/v1/environments/${encodeURIComponent(envId)}/baremetal`);
  } catch (e) {
    if (bmLoadedEnvId !== envId || !document.getElementById("bm-body")) return;
    if (msg) msg.textContent = "";
    body.classList.remove("muted");
    if (e && e.status === 404) {
      // Backend doesn't serve bare-metal yet — stay quiet, never a page break.
      body.innerHTML = unavail("bare-metal provisioning is not available on this backend");
    } else {
      body.innerHTML = unavail("bare-metal fetch failed");
      if (err) err.innerHTML = `<div class="error">${esc(e.message)}</div>`;
    }
    return;
  }
  if (bmLoadedEnvId !== envId || !document.getElementById("bm-body")) return;
  if (msg) msg.textContent = "";

  // Contract is {nodes: [...]}; tolerate a bare array.
  const nodes = Array.isArray(d) ? d : d && Array.isArray(d.nodes) ? d.nodes : [];
  if (msg) msg.textContent = `${nodes.length} node(s)`;
  body.classList.remove("muted");
  if (!nodes.length) {
    body.innerHTML =
      '<div class="muted">No bare-metal nodes registered — register a node\'s BMC, PXE-boot it into talos, provision — zero touch.</div>';
    return;
  }
  body.innerHTML = nodesTableHtml(nodes);
  body.querySelectorAll("button[data-bm-power]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const details = btn.closest("details");
      if (details) details.open = false;
      bmPower(envId, nodes[Number(btn.dataset.row)], btn.dataset.bmPower);
    })
  );
  body.querySelectorAll("button[data-bm-console]").forEach((btn) =>
    btn.addEventListener("click", () => bmOpenConsole(envId, nodes[Number(btn.dataset.bmConsole)]))
  );
  body.querySelectorAll("button[data-bm-pxe]").forEach((btn) =>
    btn.addEventListener("click", () => bmPxeBoot(envId, nodes[Number(btn.dataset.bmPxe)]))
  );
  body.querySelectorAll("button[data-bm-provision]").forEach((btn) =>
    btn.addEventListener("click", () => bmProvision(envId, nodes[Number(btn.dataset.bmProvision)]))
  );
}

export function destroyBaremetalCard() {
  clearBmTimers();
  bmRowJobs.clear();
}
