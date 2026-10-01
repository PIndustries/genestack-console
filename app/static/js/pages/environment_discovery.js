// pages/environment_discovery.js — "Hardware discovery" card on the environment
// detail page, embedded right after the bare metal card. Two tables fed by
// GET /api/v1/environments/{id}/discovery: "Discovered hosts (PXE/DHCP)" — machines
// that booted against the env's agent and announced themselves (state discovered →
// a per-row Claim… inline form: name + role checkboxes → POST /discovery/claim →
// reload; claimed rows render a green pill) — and "Discovered BMCs (Redfish)" —
// BMCs found by the agent (state new → a per-row Credentials… inline form: name,
// username, password → POST /discovery/bmc-creds → reload; registered rows render a
// green pill). A "Scan subnet for BMCs" form on top starts a baremetal.bmc_scan job
// (confirm → POST → amber running pill + #/activity?tab=jobs&job=<id> link → 5s poll → reload on
// terminal), mirroring the register-node pattern in environment_baremetal.js.
// Defensive: the endpoints may 404 (backend not deployed yet) or the scan op may be
// unknown on older backends — every path degrades to a muted note, never a page break.
import { api, esc, fmtTime, toast } from "../api.js";
import { canRun, gate } from "../store.js";
import { ROLES, ROLE_LABELS } from "../roles.js";

const SCAN_ACTIVE = new Set(["queued", "running"]);
const SCAN_POLL_MS = 5000;

let discLoadedEnvId = ""; // env the card last rendered — guards against env switches
let scanTimer = null; // pending scan-job poll timeout
let scanJobId = ""; // scan job currently being polled ("" = idle)
const scanDoneIds = new Set(); // terminal scan jobs already toasted/reloaded for

function clearScanTimer() {
  if (scanTimer) {
    clearTimeout(scanTimer);
    scanTimer = null;
  }
}

function unavail(note) {
  return `<div class="muted">Unavailable${note ? " — " + esc(note) : ""}.</div>`;
}

// state pill: host — discovered grey / claimed green; BMC — new amber / registered green.
function hostStatePillHtml(state) {
  const s = String(state || "").toLowerCase();
  if (s === "claimed") return '<span class="pill ok">claimed</span>';
  if (s === "discovered") return '<span class="pill">discovered</span>';
  return `<span class="pill">${esc(state || "unknown")}</span>`;
}

function bmcStatePillHtml(state) {
  const s = String(state || "").toLowerCase();
  if (s === "registered") return '<span class="pill ok">registered</span>';
  if (s === "new") return '<span class="pill warn">new</span>';
  return `<span class="pill">${esc(state || "unknown")}</span>`;
}

function scanRunningHtml(jobId, status) {
  const id = String(jobId || "");
  return (
    `<span class="pill warn">bmc scan ${esc(status)}…</span> ` +
    `<a href="#/activity?tab=jobs&job=${esc(id)}">job ${esc(id.slice(0, 8))}…</a>`
  );
}

function setScanLine(html) {
  const el = document.getElementById("disc-scan-line");
  if (el) el.innerHTML = html;
}

// Poll the baremetal.bmc_scan job every 5s (mirrors pollRegisterJob in
// environment_baremetal.js): update the scan line while queued/running; on a
// terminal state toast once and reload the card so newly found BMCs appear.
async function pollScanJob(envId, jobId) {
  clearScanTimer();
  scanJobId = jobId;
  if (!document.getElementById("disc-card")) return; // navigated away
  let job;
  try {
    job = await api(`/api/v1/jobs/${encodeURIComponent(jobId)}`);
  } catch {
    // Transient fetch failure: keep the line as-is and retry on the next tick.
    scanTimer = setTimeout(() => pollScanJob(envId, jobId), SCAN_POLL_MS);
    return;
  }
  if (!document.getElementById("disc-card") || discLoadedEnvId !== envId) return;
  const status = String(job && job.status ? job.status : "").toLowerCase();
  if (SCAN_ACTIVE.has(status)) {
    setScanLine(scanRunningHtml(jobId, status));
    scanTimer = setTimeout(() => pollScanJob(envId, jobId), SCAN_POLL_MS);
    return;
  }
  // Terminal state: stop polling, toast once, reload the card once.
  scanJobId = "";
  if (scanDoneIds.has(jobId)) return;
  scanDoneIds.add(jobId);
  toast(
    `bmc scan ${status === "success" ? "succeeded" : status || "finished"}`,
    status === "success" ? "ok" : "bad"
  );
  await loadDiscoveryCard(envId);
}

async function startScan(envId) {
  if (!envId) return;
  const subnetEl = document.getElementById("disc-subnet");
  const subnet = subnetEl ? subnetEl.value.trim() : "";
  if (!subnet) {
    setScanLine('<span class="error">subnet is required (e.g. 10.0.0.0/24)</span>');
    if (subnetEl) subnetEl.focus();
    return;
  }
  if (!confirm(`Scan subnet ${subnet} for Redfish BMCs? The in-environment agent sweeps the subnet.`)) return;
  setScanLine('<span class="muted">creating bmc scan job…</span>');
  try {
    const job = await api(`/api/v1/environments/${encodeURIComponent(envId)}/jobs`, {
      method: "POST",
      body: JSON.stringify({ operation: "baremetal.bmc_scan", params: { subnet } }),
    });
    const id = job && job.id != null ? String(job.id) : "";
    if (!id) {
      await loadDiscoveryCard(envId);
      return;
    }
    toast(`bmc scan job ${id.slice(0, 8)}… created`, "ok");
    setScanLine(scanRunningHtml(id, "queued"));
    pollScanJob(envId, id);
  } catch (e) {
    // Older backends reject baremetal.bmc_scan with 400 "Unknown operation" — note
    // it inline and leave the two tables as the source of truth. No breakage.
    if (e.status === 400 && /unknown operation/i.test(String(e.message || ""))) {
      setScanLine('<span class="muted">BMC scan is not available on this backend</span>');
      return;
    }
    setScanLine(`<span class="error">bmc scan failed to start: ${esc(e.message)}</span>`);
    toast(`bmc scan failed to start: ${e.message}`, "bad");
    if (e.status === 403) toast("Insufficient role: operator required", "bad");
  }
}

// ---------- claim a discovered host (POST /discovery/claim) ----------

function claimRoleBoxes(mac, i) {
  return ROLES.map(
    (r) => `<label class="check" style="margin:0 .6rem 0 0">
      <input type="checkbox" data-disc-claim-role="${esc(mac)}" data-row="${i}" value="${r}" ${gate(canRun(), "operator")} /> ${ROLE_LABELS[r] || r}
    </label>`
  ).join("");
}

function checkedClaimRoles(mac) {
  const roles = [];
  document
    .querySelectorAll(`#disc-card input[data-disc-claim-role="${CSS.escape(mac)}"]:checked`)
    .forEach((cb) => roles.push(cb.value));
  return roles;
}

function setClaimLine(mac, html) {
  const el = document.querySelector(`#disc-card [data-disc-claim-line="${CSS.escape(mac)}"]`);
  if (el) el.innerHTML = html;
}

async function claimHost(envId, host) {
  const info = host && typeof host === "object" ? host : {};
  const mac = String(info.mac || "");
  const nameEl = document.querySelector(`#disc-card [data-disc-claim-name="${CSS.escape(mac)}"]`);
  const name = nameEl ? nameEl.value.trim() : "";
  if (!name) {
    setClaimLine(mac, '<span class="error">name is required</span>');
    if (nameEl) nameEl.focus();
    return;
  }
  const roles = checkedClaimRoles(mac);
  const body = { mac, name };
  if (roles.length) body.roles = roles;
  setClaimLine(mac, '<span class="muted">claiming…</span>');
  try {
    await api(`/api/v1/environments/${encodeURIComponent(envId)}/discovery/claim`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    toast(`${name}: claimed into inventory`, "ok");
    await loadDiscoveryCard(envId);
  } catch (e) {
    if (e && e.status === 404) {
      setClaimLine(mac, '<span class="muted">claim is not available on this backend</span>');
      return;
    }
    setClaimLine(mac, `<span class="error">claim failed: ${esc(e.message)}</span>`);
    toast(`claim failed: ${e.message}`, "bad");
    if (e.status === 403) toast("Insufficient role: operator required", "bad");
  }
}

// ---------- register a discovered BMC (POST /discovery/bmc-creds) ----------

function setCredsLine(bmcId, html) {
  const el = document.querySelector(`#disc-card [data-disc-creds-line="${CSS.escape(bmcId)}"]`);
  if (el) el.innerHTML = html;
}

async function registerBmc(envId, bmc) {
  const info = bmc && typeof bmc === "object" ? bmc : {};
  const bmcId = String(info.id || "");
  const val = (attr) => {
    const el = document.querySelector(`#disc-card [${attr}="${CSS.escape(bmcId)}"]`);
    return el ? el.value.trim() : "";
  };
  const name = val("data-disc-creds-name");
  const username = val("data-disc-creds-user");
  const password = val("data-disc-creds-pass");
  if (!name || !username || !password) {
    setCredsLine(bmcId, '<span class="error">name, username, and password are required</span>');
    return;
  }
  setCredsLine(bmcId, '<span class="muted">registering…</span>');
  try {
    await api(`/api/v1/environments/${encodeURIComponent(envId)}/discovery/bmc-creds`, {
      method: "POST",
      body: JSON.stringify({ bmc_id: bmcId, name, username, password }),
    });
    toast(`${name}: BMC registered as a bare-metal node`, "ok");
    await loadDiscoveryCard(envId);
  } catch (e) {
    if (e && e.status === 404) {
      setCredsLine(bmcId, '<span class="muted">BMC registration is not available on this backend</span>');
      return;
    }
    setCredsLine(bmcId, `<span class="error">registration failed: ${esc(e.message)}</span>`);
    toast(`BMC registration failed: ${e.message}`, "bad");
    if (e.status === 403) toast("Insufficient role: operator required", "bad");
  }
}

// ---------- tables ----------

function hostsTableHtml(hosts) {
  const rows = hosts
    .map((h, i) => {
      const info = h && typeof h === "object" ? h : {};
      const mac = String(info.mac || "");
      const isDiscovered = String(info.state || "").toLowerCase() === "discovered";
      const claimCell = isDiscovered
        ? `<details class="disc-claim"><summary>Claim…</summary>` +
          `<div class="disc-claim-form">` +
          `<input type="text" data-disc-claim-name="${esc(mac)}" value="${esc(info.hostname || "")}" placeholder="name" autocomplete="off" ${gate(canRun(), "operator")} />` +
          `<div class="row" style="gap:0">${claimRoleBoxes(mac, i)}</div>` +
          `<div class="row" style="gap:.4rem;align-items:center">` +
          `<button class="secondary btn-sm" type="button" data-disc-claim="${i}" title="Claim into this environment's inventory doc" ${gate(canRun(), "operator")}>Claim</button>` +
          `<span class="muted" style="font-size:.72rem">lands in the inventory doc servers</span>` +
          `</div></div></details>` +
          `<div class="disc-job-line" data-disc-claim-line="${esc(mac)}"></div>`
        : "";
      return `<tr data-row="${i}">
        <td><code>${esc(mac || "—")}</code></td>
        <td class="muted">${esc(info.ip || "—")}</td>
        <td>${esc(info.hostname || "—")}</td>
        <td>${hostStatePillHtml(info.state)}</td>
        <td class="muted">${esc(fmtTime(info.last_seen)) || "—"}</td>
        <td style="white-space:nowrap">${claimCell}</td>
      </tr>`;
    })
    .join("");
  return `<h3>Discovered hosts (PXE/DHCP)</h3>
    <table>
      <thead><tr>
        <th>MAC</th><th>IP</th><th>Hostname</th><th>State</th><th>Last seen</th><th></th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function bmcsTableHtml(bmcs) {
  const rows = bmcs
    .map((b, i) => {
      const info = b && typeof b === "object" ? b : {};
      const bmcId = String(info.id || "");
      const isNew = String(info.state || "").toLowerCase() === "new";
      const credsCell = isNew
        ? `<details class="disc-claim"><summary>Credentials…</summary>` +
          `<div class="disc-claim-form">` +
          `<input type="text" data-disc-creds-name="${esc(bmcId)}" placeholder="name" autocomplete="off" ${gate(canRun(), "operator")} />` +
          `<input type="text" data-disc-creds-user="${esc(bmcId)}" placeholder="username" autocomplete="off" ${gate(canRun(), "operator")} />` +
          `<input type="password" data-disc-creds-pass="${esc(bmcId)}" placeholder="password" autocomplete="off" ${gate(canRun(), "operator")} />` +
          `<div class="row" style="gap:.4rem;align-items:center">` +
          `<button class="secondary btn-sm" type="button" data-disc-creds="${i}" title="Register as a managed bare-metal node" ${gate(canRun(), "operator")}>Register</button>` +
          `<span class="muted" style="font-size:.72rem">registers the BMC as a managed bare-metal node</span>` +
          `</div></div></details>` +
          `<div class="disc-job-line" data-disc-creds-line="${esc(bmcId)}"></div>`
        : "";
      return `<tr data-row="${i}">
        <td><code>${esc(info.ip || "—")}</code></td>
        <td class="muted">${esc(info.vendor || "—")}</td>
        <td class="muted">${esc(info.model || "—")}</td>
        <td>${bmcStatePillHtml(info.state)}</td>
        <td class="muted">${esc(fmtTime(info.last_seen)) || "—"}</td>
        <td style="white-space:nowrap">${credsCell}</td>
      </tr>`;
    })
    .join("");
  return `<h3>Discovered BMCs (Redfish)</h3>
    <table>
      <thead><tr>
        <th>IP</th><th>Vendor</th><th>Model</th><th>State</th><th>Last seen</th><th></th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// ---------- public API ----------

export function discoveryCardHtml() {
  return `
<style>
#disc-card .toolbar { display:flex; align-items:center; gap:.5rem; padding-bottom:.4rem; margin-bottom:.3rem; border-bottom:1px solid var(--border, #222); flex-wrap:wrap; }
#disc-card .toolbar h2 { margin:0; font-size:.95rem; font-weight:600; }
#disc-card .card-empty { text-align:center; padding:1.5rem 1rem; color:var(--fg-muted, #666); }
#disc-card .card-empty .empty-icon { font-size:1.5rem; opacity:.3; margin-bottom:.4rem; }
#disc-card .card-empty p { font-size:.78rem; margin:.15rem 0; }
#disc-card .card-empty .empty-hint { font-size:.7rem; color:var(--fg-muted, #555); margin-top:.3rem; }
#disc-card .hint-row { display:flex; align-items:center; gap:.5rem; padding:.3rem 0; margin-bottom:.3rem; font-size:.72rem; color:var(--fg-muted, #666); }
#disc-card .pill { display:inline-block; padding:.15rem .45rem; border-radius:.2rem; font-size:.72rem; font-weight:500; }
#disc-card .pill.ok { background:#1a3a1a; color:#4caf50; }
#disc-card .pill.bad { background:#3a1a1a; color:#ef5350; }
#disc-card .pill.warn { background:#3a3a1a; color:#ffc107; }
#disc-card .disc-scan-form { display:flex; align-items:center; gap:.5rem; flex-wrap:wrap; padding:.3rem 0; margin-bottom:.3rem; border-bottom:1px solid var(--border, #222); }
#disc-card h3 { font-size:.82rem; font-weight:600; margin:.5rem 0 .3rem; }
</style>
<div class="card span-12" id="disc-card">
  <div class="toolbar">
    <h2>Inventory — PXE &amp; BMC (Redfish)</h2>
    <button class="secondary btn-sm" id="disc-refresh" type="button">Refresh</button>
    <span id="disc-msg" class="muted"></span>
  </div>
  <div class="hint-row">machines that PXE/DHCP against this environment's agent announce themselves; scan a subnet to find their BMCs</div>
  <div id="disc-err"></div>
  <div class="disc-scan-form">
    <input id="disc-subnet" type="text" placeholder="10.0.0.0/24" autocomplete="off" ${gate(canRun(), "operator")} />
    <button class="secondary btn-sm" id="disc-scan" type="button" title="Sweep the subnet for Redfish BMCs via the in-environment agent" ${gate(canRun(), "operator")}>Scan subnet for BMCs</button>
    <span class="muted" style="font-size:.72rem">agent sweeps the subnet for Redfish BMCs — results appear below</span>
  </div>
  <div id="disc-scan-line" class="disc-job-line"></div>
  <div id="disc-body" class="muted">
    <div class="card-empty">
      <div class="empty-icon">⬡</div>
      <p>Select an environment to discover hardware</p>
    </div>
  </div>
</div>`;
}

export function wireDiscoveryCard(getEnvId) {
  const refresh = document.getElementById("disc-refresh");
  if (refresh) refresh.addEventListener("click", () => loadDiscoveryCard(getEnvId()));
  const scan = document.getElementById("disc-scan");
  if (scan) scan.addEventListener("click", () => startScan(getEnvId()));
}

export async function loadDiscoveryCard(envId) {
  const body = document.getElementById("disc-body");
  if (!body) return;
  if (envId !== discLoadedEnvId) {
    // Env switched: abandon polling for the previous env's scan job.
    clearScanTimer();
    scanJobId = "";
  }
  discLoadedEnvId = envId || "";
  const msg = document.getElementById("disc-msg");
  const err = document.getElementById("disc-err");
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
    d = await api(`/api/v1/environments/${encodeURIComponent(envId)}/discovery`);
  } catch (e) {
    if (discLoadedEnvId !== envId || !document.getElementById("disc-body")) return;
    if (msg) msg.textContent = "";
    body.classList.remove("muted");
    if (e && e.status === 404) {
      // Backend doesn't serve discovery yet — stay quiet, never a page break.
      body.innerHTML = unavail("hardware discovery is not available on this backend");
    } else {
      body.innerHTML = unavail("discovery fetch failed");
      if (err) err.innerHTML = `<div class="error">${esc(e.message)}</div>`;
    }
    return;
  }
  if (discLoadedEnvId !== envId || !document.getElementById("disc-body")) return;

  // Contract is {hosts: [...], bmcs: [...]}; tolerate missing keys.
  const hosts = d && Array.isArray(d.hosts) ? d.hosts : [];
  const bmcs = d && Array.isArray(d.bmcs) ? d.bmcs : [];
  if (msg) msg.textContent = `${hosts.length} host(s) · ${bmcs.length} bmc(s)`;
  body.classList.remove("muted");

  const hostsHtml = hosts.length
    ? hostsTableHtml(hosts)
    : `<h3>Discovered hosts (PXE/DHCP)</h3>
      <div class="muted">No discovered hosts yet — nodes that PXE/DHCP against this environment's agent appear here automatically.</div>`;
  const bmcsHtml = bmcs.length
    ? bmcsTableHtml(bmcs)
    : `<h3>Discovered BMCs (Redfish)</h3>
      <div class="muted">No BMCs found yet — scan a subnet above; nodes that PXE/DHCP against this environment's agent appear here automatically.</div>`;
  body.innerHTML = hostsHtml + bmcsHtml;

  body.querySelectorAll("button[data-disc-claim]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const details = btn.closest("details");
      if (details) details.open = false;
      claimHost(envId, hosts[Number(btn.dataset.discClaim)]);
    })
  );
  body.querySelectorAll("button[data-disc-creds]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const details = btn.closest("details");
      if (details) details.open = false;
      registerBmc(envId, bmcs[Number(btn.dataset.discCreds)]);
    })
  );
}

export function destroyDiscoveryCard() {
  clearScanTimer();
  scanJobId = "";
}
