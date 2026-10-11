// pages/hosts.js — "Host VMs" page. Lists hypervisor-local VMs from
// GET /api/v1/hostvms (re-discovered on every call) with operator-gated
// power actions fired as env-less jobs via POST /api/v1/jobs
// (operations hostvm.start/stop/restart, params {vm_id}). Job completion
// arrives over the "jobs" SSE topic with a 5s poll fallback while the stream
// is down — the same tracking pattern as environment_servers.js. A 15s
// auto-poll keeps the table fresh; the serial console is an expandable panel
// that tails GET /api/v1/hostvms/{id}/serial.
import { api, esc, toast } from "../api.js";
import { canRun, gate } from "../store.js";
import { connect } from "../stream.js";
import { bindLiveLog, resetLiveLog, stickLiveLog } from "../logview.js";

export const title = "Hosts";

const REFRESH_MS = 15000;
const JOB_POLL_MS = 5000;
const JOB_ACTIVE = new Set(["queued", "running"]);

let mounted = false; // guards against stale DOM after navigation
let refreshTimer = null;
let lastVms = []; // last rendered list — rows re-render from this on job updates
const vmJobs = new Map(); // vm_id -> { id, label, status } (buttons disabled while set)
const vmDoneIds = new Set(); // terminal jobs already toasted/refreshed for
let labJob = null; // { id, status } while Create lab is queued or running
let streamHandle = null;
let sseLive = false;
let jobPollTimer = null;
let serialVmId = null; // vm whose serial panel is open
let serialLines = 200;

// ---------- formatting ----------

function fmtMem(bytes) {
  if (bytes == null) return "—";
  const n = Number(bytes);
  if (!Number.isFinite(n) || n < 0) return "—";
  const gib = n / 1024 ** 3;
  if (gib >= 1) return gib.toFixed(1) + " GiB";
  return Math.max(1, Math.round(n / 1024 ** 2)) + " MiB";
}

// Humanized uptime: "3d 4h", "5h 12m", "42m", "<1m".
function fmtUptime(seconds) {
  if (seconds == null) return "—";
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m`;
  return "<1m";
}

// ---------- rendering ----------

function statePillHtml(vm) {
  if (vm.running) {
    const pid = vm.pid != null ? ` · pid ${esc(vm.pid)}` : "";
    return `<span class="pill ok">running${pid}</span>`;
  }
  return '<span class="pill">stopped</span>';
}

function sourcePillHtml(source) {
  const s = String(source || "discovered");
  return `<span class="chip">${esc(s)}</span>`;
}

function vmJobLineHtml(vmId) {
  const job = vmJobs.get(vmId);
  if (!job) return "";
  const id = String(job.id || "");
  const link = id ? ` <a href="#/activity?tab=jobs&job=${esc(id)}">job ${esc(id.slice(0, 8))}…</a>` : "";
  return `<span class="pill warn">${esc(job.label)} ${esc(job.status || "queued")}…</span>${link}`;
}

function consoleCellHtml(vm) {
  const id = String(vm.id || "");
  const bits = [];
  if (vm.serial_log_path) {
    const active = serialVmId === id;
    bits.push(
      `<button class="secondary btn-sm" type="button" data-hv-serial="${esc(id)}">` +
        `${active ? "Hide serial" : "Serial"}</button>`
    );
  }
  if (vm.vnc_port != null) {
    bits.push(`<span class="muted">VNC :${esc(vm.vnc_port)}</span>`);
  }
  if (vm.novnc_port != null) {
    const host = vm.vnc_host || window.location.hostname;
    const url = `http://${host}:${vm.novnc_port}`;
    bits.push(
      `<a class="hv-ext" href="${esc(url)}" target="_blank" rel="noopener noreferrer">noVNC ↗</a>`
    );
  }
  return bits.length ? `<div class="hv-console">${bits.join("")}</div>` : '<span class="muted">—</span>';
}

// Actions cell: Start only when stopped; Stop/Restart only when running —
// operator-gated, plus the inline job line while a job is queued/running.
function actionsCellHtml(vm) {
  const id = String(vm.id || "");
  const job = vmJobs.get(id);
  const gateAttr = job
    ? 'disabled title="job running for this VM"'
    : gate(canRun(), "operator");
  const titleAttr = (t) => (gateAttr ? "" : ` title="${esc(t)}"`);
  const btn = (op, label, title) =>
    `<button class="secondary btn-sm" type="button" data-hv-op="${op}" data-vid="${esc(id)}"` +
    `${titleAttr(title)} ${gateAttr}>${label}</button>`;
  const bits = [];
  if (!vm.running) bits.push(btn("start", "Start", "Power on this VM"));
  if (vm.running) {
    bits.push(btn("stop", "Stop", "Shut down this VM"));
    bits.push(btn("restart", "Restart", "Restart this VM"));
  }
  const jobLine = job ? `<div class="vm-job-line">${vmJobLineHtml(id)}</div>` : "";
  return `<div class="vm-actions">${bits.join("")}</div>${jobLine}`;
}

function tableHtml(vms) {
  const rows = vms
    .map(
      (vm) => `<tr data-vid="${esc(vm.id || "")}">
        <td><strong>${esc(vm.name || "(unnamed)")}</strong> ${sourcePillHtml(vm.source)}</td>
        <td>${statePillHtml(vm)}</td>
        <td class="muted">${
          vm.running && vm.cpu_percent != null ? esc(Number(vm.cpu_percent).toFixed(1)) + "%" : "—"
        }</td>
        <td class="muted">${esc(fmtMem(vm.running ? vm.rss_bytes : null))}</td>
        <td class="muted">${esc(fmtUptime(vm.running ? vm.uptime_seconds : null))}</td>
        <td class="muted hv-workdir" title="${esc(vm.workdir || "")}">${esc(vm.workdir || "—")}</td>
        <td>${consoleCellHtml(vm)}</td>
        <td>${actionsCellHtml(vm)}</td>
      </tr>`
    )
    .join("");
  return `<table>
    <thead><tr><th>Name</th><th>State</th><th>CPU</th><th>Memory</th><th>Uptime</th><th>Workdir</th><th>Console</th><th>Actions</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

function wireRows() {
  const body = document.getElementById("hv-body");
  if (!body) return;
  body.querySelectorAll("button[data-hv-op]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const vm = lastVms.find((v) => String(v.id || "") === btn.dataset.vid);
      if (vm) vmAction(vm, btn.dataset.hvOp);
    })
  );
  body.querySelectorAll("button[data-hv-serial]").forEach((btn) =>
    btn.addEventListener("click", () => toggleSerial(btn.dataset.hvSerial))
  );
}

// Re-render just the table from the last fetch (job state changed).
function paintRows() {
  const body = document.getElementById("hv-body");
  if (!body || !lastVms.length) return;
  body.innerHTML = tableHtml(lastVms);
  wireRows();
}

function paint(data) {
  const body = document.getElementById("hv-body");
  if (!body) return;
  const count = document.getElementById("hv-count");
  body.classList.remove("muted");
  const vms = Array.isArray(data && data.vms) ? data.vms : [];
  lastVms = vms;
  if (!vms.length) {
    if (count) count.textContent = "";
    body.classList.add("muted");
    body.innerHTML = `<div class="hv-empty">No VMs discovered.
      <div style="font-size:.82rem;margin-top:.35rem">Create lab starts Ubuntu guests on this computer. A VM that is already here shows up when its discovery root is configured.</div>
    </div>`;
    return;
  }
  if (count) count.textContent = `${vms.length} VM(s)`;
  body.innerHTML = tableHtml(vms);
  wireRows();
}

// ---------- power actions (hostvm.* jobs via the global jobs endpoint) ----------

const VM_OPS = {
  start: { operation: "hostvm.start", label: "start", confirm: (n) => `Start VM '${n}'?` },
  stop: { operation: "hostvm.stop", label: "stop", confirm: (n) => `Stop VM '${n}'?` },
  restart: { operation: "hostvm.restart", label: "restart", confirm: (n) => `Restart VM '${n}'?` },
};

const LAB_ROLES = {
  3: "lab-1 is control, lab-2 is compute and network, lab-3 is storage.",
  4: "lab-1 is control, lab-2 is compute and network, lab-3 is storage, lab-4 is a worker.",
  5: "lab-1 is control, lab-2 is compute and network, lab-3 is storage, lab-4 is a worker, lab-5 is a second compute.",
};

function paintLabLine() {
  const line = document.getElementById("hv-lab-line");
  const btn = document.getElementById("btn-hv-lab");
  const active = !!(labJob && JOB_ACTIVE.has(String(labJob.status || "queued")));
  if (btn && canRun()) btn.disabled = active;
  if (!line) return;
  if (!labJob || !labJob.id) {
    line.innerHTML = "";
    return;
  }
  const id = String(labJob.id);
  line.innerHTML =
    `<span class="pill warn">lab ${esc(labJob.status || "queued")}</span> ` +
    `<a href="#/activity?tab=jobs&job=${esc(id)}">job ${esc(id.slice(0, 8))}…</a>`;
}

async function startLab() {
  const select = document.getElementById("hv-lab-count");
  const count = Number(select && select.value) || 3;
  const message =
    `Create a ${count}-node lab on this console host?\n\n` +
    "This is not a production workload. The console stays outside the cluster.\n" +
    "Each guest is Ubuntu, 4 vCPU, 16 GiB, and a thin 80 GB disk.\n" +
    `${LAB_ROLES[count] || ""}\n` +
    "OpenStack does not install until you Apply on the environment.";
  if (!confirm(message)) return;
  labJob = { id: "", status: "queued" };
  paintLabLine();
  try {
    const job = await api("/api/v1/jobs", {
      method: "POST",
      body: JSON.stringify({
        operation: "hostvm.lab",
        params: { count, confirm: true },
      }),
    });
    const id = job && job.id != null ? String(job.id) : "";
    if (!id) {
      labJob = null;
      paintLabLine();
      return;
    }
    labJob = { id, status: "queued" };
    toast(`Lab job ${id.slice(0, 8)}… created`, "ok");
    paintLabLine();
    scheduleJobPolling();
  } catch (e) {
    labJob = null;
    paintLabLine();
    toast(`Create lab failed to start: ${e.message}`, "bad");
    if (e.status === 403) toast("Insufficient role: operator required", "bad");
  }
}

function vmAction(vm, op) {
  const spec = VM_OPS[op];
  if (!spec) return;
  const name = vm.name || vm.id || "?";
  if (!confirm(spec.confirm(name))) return;
  startVmJob(vm, spec);
}

async function startVmJob(vm, spec) {
  const vmId = String(vm.id || "");
  const name = vm.name || vmId || "?";
  if (!vmId) {
    toast(`${name}: VM has no id`, "bad");
    return;
  }
  vmJobs.set(vmId, { id: "", label: spec.label, status: "queued" });
  paintRows();
  try {
    // Host VMs have no environment — the env-less global jobs endpoint.
    const job = await api("/api/v1/jobs", {
      method: "POST",
      body: JSON.stringify({ operation: spec.operation, params: { vm_id: vmId } }),
    });
    const id = job && job.id != null ? String(job.id) : "";
    if (!id) {
      vmJobs.delete(vmId);
      paintRows();
      return;
    }
    vmJobs.set(vmId, { id, label: spec.label, status: "queued" });
    toast(`${name}: ${spec.label} job ${id.slice(0, 8)}… created`, "ok");
    paintRows();
    scheduleJobPolling(); // no-op while the jobs stream is live
  } catch (e) {
    vmJobs.delete(vmId);
    paintRows();
    toast(`${spec.label} failed to start: ${e.message}`, "bad");
    if (e.status === 403) toast("Insufficient role: operator required", "bad");
  }
}

// ---------- job tracking (jobs SSE topic + 5s poll fallback) ----------

function trackedVmId(jobId) {
  const id = String(jobId);
  for (const [vmId, j] of vmJobs) {
    if (String(j.id) === id) return vmId;
  }
  return null;
}

// jobs topic event: {type:"job", id, status}.
function onJobEvent(payload) {
  if (!payload || payload.id == null) return;
  if (labJob && String(labJob.id) === String(payload.id)) {
    const status = String(payload.status || "").toLowerCase();
    if (JOB_ACTIVE.has(status)) {
      labJob = { id: String(payload.id), status };
      paintLabLine();
      return;
    }
    finishLabJob(String(payload.id), status);
    return;
  }
  const vmId = trackedVmId(payload.id);
  if (vmId == null) return;
  const status = String(payload.status || "").toLowerCase();
  if (JOB_ACTIVE.has(status)) {
    const job = vmJobs.get(vmId);
    if (job.status !== status) {
      vmJobs.set(vmId, Object.assign({}, job, { status }));
      paintRows();
    }
    return;
  }
  finishVmJob(vmId, String(payload.id), status);
}

// Terminal state: drop the row lock, toast once, reload so the new power
// state shows.
async function finishLabJob(jobId, status) {
  labJob = null;
  paintLabLine();
  if (!vmDoneIds.has(jobId)) {
    vmDoneIds.add(jobId);
    toast(
      `lab ${status === "success" ? "succeeded" : status || "finished"}`,
      status === "success" ? "ok" : "bad"
    );
  }
  await loadVms({ background: true });
}

async function finishVmJob(vmId, jobId, status) {
  const job = vmJobs.get(vmId);
  vmJobs.delete(vmId);
  if (!vmDoneIds.has(jobId)) {
    vmDoneIds.add(jobId);
    toast(
      `${job ? job.label : "job"} ${status === "success" ? "succeeded" : status || "finished"}`,
      status === "success" ? "ok" : "bad"
    );
  }
  await loadVms({ background: true });
}

function scheduleJobPolling() {
  if (jobPollTimer || sseLive) return;
  jobPollTimer = setTimeout(pollVmJobs, JOB_POLL_MS);
}

// Fallback for when the jobs stream is down: poll each tracked job every 5s.
async function pollVmJobs() {
  jobPollTimer = null;
  if (!mounted || !document.getElementById("hv-card")) return;
  if (labJob && labJob.id) {
    try {
      const job = await api(`/api/v1/jobs/${encodeURIComponent(labJob.id)}`);
      const status = String(job && job.status ? job.status : "").toLowerCase();
      if (JOB_ACTIVE.has(status)) {
        labJob = { id: String(labJob.id), status };
        paintLabLine();
      } else {
        await finishLabJob(String(labJob.id), status);
      }
    } catch {
      // transient fetch failure — retry on the next tick
    }
  }
  for (const [vmId, j] of [...vmJobs]) {
    if (!j.id) continue;
    let job;
    try {
      job = await api(`/api/v1/jobs/${encodeURIComponent(j.id)}`);
    } catch {
      continue; // transient fetch failure — retry on the next tick
    }
    const status = String(job && job.status ? job.status : "").toLowerCase();
    if (JOB_ACTIVE.has(status)) {
      if (status !== j.status) {
        vmJobs.set(vmId, Object.assign({}, j, { status }));
        paintRows();
      }
    } else {
      await finishVmJob(vmId, String(j.id), status);
    }
  }
  if ((vmJobs.size || (labJob && labJob.id)) && !sseLive) scheduleJobPolling();
}

// ---------- serial console panel ----------

function toggleSerial(vmId) {
  const id = String(vmId || "");
  if (serialVmId === id) {
    closeSerial();
  } else {
    serialVmId = id;
    paintRows(); // flips the button label to "Hide serial"
    openSerial(id);
  }
}

function openSerial(vmId) {
  const card = document.getElementById("hv-serial-card");
  if (!card) return;
  const vm = lastVms.find((v) => String(v.id || "") === vmId);
  resetLiveLog("hv-serial-pre");
  card.classList.remove("hidden");
  const titleEl = document.getElementById("hv-serial-title");
  if (titleEl) titleEl.textContent = `Serial console — ${(vm && vm.name) || vmId}`;
  const pathEl = document.getElementById("hv-serial-path");
  if (pathEl) pathEl.textContent = (vm && vm.serial_log_path) || "";
  loadSerial();
  card.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function closeSerial() {
  serialVmId = null;
  const card = document.getElementById("hv-serial-card");
  if (card) card.classList.add("hidden");
  paintRows(); // restores the "Serial" button label
}

async function loadSerial() {
  const vmId = serialVmId;
  const pre = document.getElementById("hv-serial-pre");
  if (!vmId || !pre) return;
  pre.textContent = "Loading…";
  let data;
  try {
    data = await api(`/api/v1/hostvms/${encodeURIComponent(vmId)}/serial?lines=${serialLines}`);
  } catch (e) {
    if (serialVmId !== vmId) return;
    pre.textContent = `Serial log unavailable: ${e.message}`;
    return;
  }
  if (serialVmId !== vmId) return; // panel switched/closed mid-flight
  const lines = Array.isArray(data && data.lines) ? data.lines : [];
  pre.textContent = lines.length ? lines.join("\n") : "(serial log is empty)";
  stickLiveLog(pre);
}

// ---------- public API ----------

export function destroy() {
  mounted = false;
  if (refreshTimer) {
    clearInterval(refreshTimer);
    refreshTimer = null;
  }
  if (jobPollTimer) {
    clearTimeout(jobPollTimer);
    jobPollTimer = null;
  }
  if (streamHandle) {
    streamHandle.close();
    streamHandle = null;
  }
  sseLive = false;
  lastVms = [];
  vmJobs.clear();
  labJob = null;
  serialVmId = null;
}

export async function render(root) {
  destroy(); // clear any stale timer/stream from a previous mount
  mounted = true;
  root.innerHTML = `
  <div class="card" id="hv-card">
    <div class="toolbar">
      <h2>Host VMs</h2>
      <span id="hv-count" class="muted"></span>
      <button class="secondary btn-sm" id="btn-hv-refresh" type="button">Refresh</button>
      <label class="muted hv-lines-label">lab
        <select id="hv-lab-count" ${gate(canRun(), "operator")}>
          <option value="3" selected>3 nodes</option>
          <option value="4">4 nodes</option>
          <option value="5">5 nodes</option>
        </select>
      </label>
      <button class="secondary btn-sm" id="btn-hv-lab" type="button" ${gate(canRun(), "operator")}>Create lab</button>
      <span class="muted" id="hv-updated" style="font-size:.78rem"></span>
    </div>
    <p class="muted hv-sub">VMs discovered on this hypervisor. Create lab starts a 3 to 5 node Ubuntu cluster on this computer. It is not a production workload.</p>
    <div id="hv-lab-line"></div>
    <div id="hv-err"></div>
    <div id="hv-body" class="muted">—</div>
  </div>
  <div class="card hv-serial-card hidden" id="hv-serial-card">
    <div class="toolbar">
      <h2 id="hv-serial-title">Serial console</h2>
      <label class="muted hv-lines-label">lines
        <select id="hv-serial-lines">
          <option value="100">100</option>
          <option value="200" selected>200</option>
          <option value="500">500</option>
        </select>
      </label>
      <button class="secondary btn-sm" id="btn-hv-serial-latest" type="button">Latest</button>
      <button class="secondary btn-sm" id="btn-hv-serial-refresh" type="button">Refresh</button>
      <button class="secondary btn-sm" id="btn-hv-serial-close" type="button">Close</button>
    </div>
    <div class="muted" id="hv-serial-path" style="font-size:.78rem"></div>
    <pre class="hv-serial" id="hv-serial-pre"></pre>
  </div>`;

  bindLiveLog(
    document.getElementById("hv-serial-pre"),
    document.getElementById("btn-hv-serial-latest")
  );
  document.getElementById("btn-hv-refresh").addEventListener("click", () => loadVms());
  document.getElementById("btn-hv-lab").addEventListener("click", () => startLab());
  paintLabLine();
  document.getElementById("btn-hv-serial-refresh").addEventListener("click", () => loadSerial());
  document.getElementById("btn-hv-serial-close").addEventListener("click", () => closeSerial());
  document.getElementById("hv-serial-lines").addEventListener("change", (e) => {
    serialLines = Number(e.target.value) || 200;
    loadSerial();
  });

  await loadVms();

  // Job events arrive on the "jobs" topic; polling below covers stream gaps.
  streamHandle = connect(["jobs"], {
    jobs: onJobEvent,
    onState: (state) => {
      sseLive = state === "open";
      if (sseLive && jobPollTimer) {
        clearTimeout(jobPollTimer);
        jobPollTimer = null;
      }
      if (!sseLive && vmJobs.size) scheduleJobPolling();
    },
  });

  // Auto-poll keeps the discovered list fresh; skip ticks while hidden.
  refreshTimer = setInterval(() => {
    if (!document.hidden) loadVms({ background: true });
  }, REFRESH_MS);
}

async function loadVms({ background = false } = {}) {
  const body = document.getElementById("hv-body");
  const err = document.getElementById("hv-err");
  if (!mounted || !body || !err) return; // page was unloaded mid-flight
  if (!background) err.innerHTML = "";
  let data;
  try {
    data = await api("/api/v1/hostvms");
  } catch (e) {
    if (!mounted || !document.getElementById("hv-body")) return;
    if (background && lastVms.length) {
      // Keep the last good table; just note the refresh failure.
      err.innerHTML = `<div class="error">Refresh failed: ${esc(e.message)}</div>`;
      return;
    }
    lastVms = [];
    body.classList.remove("muted");
    body.innerHTML = `<div class="error">${esc(e.message)}</div>`;
    return;
  }
  if (!mounted || !document.getElementById("hv-body")) return;
  err.innerHTML = "";
  paint(data);
  const updated = document.getElementById("hv-updated");
  if (updated) updated.textContent = "updated " + new Date().toLocaleTimeString();
}
