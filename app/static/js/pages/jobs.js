// pages/jobs.js — job list with filters, auto-poll, detail with log, retry.
// Job status changes also arrive over the "jobs" SSE topic; while the stream is
// connected the 3s auto-poll is suspended and rows update in place.
import { api, esc, fmtTime, statusPill, dryRunPill, toast } from "../api.js";
import { store, loadEnvs, envName, envOptionsHtml, canRun, canAdmin, gate } from "../store.js";
import { connect } from "../stream.js";

export const title = "Jobs";

const POLL_MS = 3000;
const ACTIVE = new Set(["queued", "running"]);

// Pipeline stage ids in order (mirrors app/services/service_registry.py) —
// deploy resumes happen from one of these.
const PIPELINE_STAGES = [
  "hosts",
  "infrastructure",
  "operators",
  "core",
  "compute-network",
  "platform-extras",
  "observability",
  "testing",
];

// The deploy job records its failure point in the log ("[deploy] FAILED at
// <stage>/<item>") and in the error text ("deploy failed at stage '<stage>'").
// No structured field exists on the job row, so the failed stage is derived.
function failedDeployStage(job) {
  const hay = `${String(job.log_text || "")}\n${String(job.error || "")}`;
  // Log line: "[deploy] FAILED at <stage>/<item>" — capture stops at "/".
  const m1 = hay.match(/FAILED at ([a-z0-9_-]+)/i);
  if (m1 && PIPELINE_STAGES.includes(m1[1])) return m1[1];
  // Error text: "deploy failed at stage '<stage>' item '<name>' (rc=…)".
  const m2 = hay.match(/deploy failed at stage '([a-z0-9_-]+)'/i);
  if (m2 && PIPELINE_STAGES.includes(m2[1])) return m2[1];
  return null;
}

let pollTimer = null;
let selectedId = null;
let streamHandle = null;
let sseLive = false;

// Transport hint derived from the job log. The backend records no structured
// transport field, so v1 reads the markers the executor already writes:
//   agent-routed lines  → "[agent] ..." or "via agent"
//   ssh-routed commands → "$ ssh -o BatchMode ..."
//   local execution     → "cwd=..." lines (or no markers at all)
// Returns "agent" | "ssh" | "local", or null when the log is missing/empty —
// no pill is rendered then.
export function transportFromLog(logText) {
  const log = String(logText || "");
  if (!log.trim()) return null;
  if (log.includes("[agent]") || log.includes("via agent")) return "agent";
  if (log.includes("ssh -o BatchMode")) return "ssh";
  return "local";
}

// Small pill next to the status pill. The dry-run pill stays dominant: a
// rehearsal executed nothing, so no transport pill is shown for dry-run jobs.
function transportPill(logText, dryRun) {
  if (dryRun === true) return "";
  const t = transportFromLog(logText);
  if (!t) return "";
  return ` <span class="pill transport-${t}" title="Transport that carried this op (derived from the job log)">${t}</span>`;
}

export function destroy() {
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
  if (streamHandle) {
    streamHandle.close();
    streamHandle = null;
  }
  sseLive = false;
}

export async function render(root, { param }) {
  selectedId = param || null;
  if (!store.envs.length) await loadEnvs().catch(() => {});
  root.innerHTML = `
  <div class="card">
    <div class="toolbar">
      <h2>Jobs</h2>
      <select id="f-status">
        <option value="">all statuses</option>
        <option value="queued">queued</option>
        <option value="running">running</option>
        <option value="success">success</option>
        <option value="failed">failed</option>
      </select>
      <select id="f-env" data-gsc-env-select data-gsc-env-none="all environments">${envOptionsHtml(null, { includeNone: true, noneLabel: "all environments" })}</select>
      <button class="secondary btn-sm" id="btn-jobs-refresh" type="button">Refresh</button>
      <span id="jobs-msg" class="muted"></span>
    </div>
    <div id="jobs-err"></div>
    <table>
      <thead><tr><th>When</th><th>Operation</th><th>Status</th><th>Environment</th><th>By</th></tr></thead>
      <tbody id="jobs-tbody"><tr><td colspan="5" class="muted">Loading…</td></tr></tbody>
    </table>
  </div>
  <div class="card" style="margin-top:1rem">
    <h2>Job detail</h2>
    <div class="muted" id="job-detail-body">Select a job.</div>
  </div>`;

  document.getElementById("f-status").addEventListener("change", () => loadJobs());
  document.getElementById("f-env").addEventListener("change", () => loadJobs());
  document.getElementById("btn-jobs-refresh").addEventListener("click", () => loadJobs());

  await loadJobs();
  if (selectedId) showJob(selectedId);

  streamHandle = connect(["jobs"], {
    jobs: onJobEvent,
    onState: (state) => {
      sseLive = state === "open";
      if (sseLive && pollTimer) {
        // Stream is authoritative now; suspend the fallback poll.
        clearTimeout(pollTimer);
        pollTimer = null;
      }
      if (!sseLive) loadJobs(); // resumes polling if jobs are still active
    },
  });
}

// jobs topic event: {type:"job", id, environment_id, operation, status, dry_run}.
function onJobEvent(payload) {
  if (!payload || payload.id == null) return;
  const id = String(payload.id);
  const row = document.querySelector(`#jobs-tbody tr[data-job="${CSS.escape(id)}"]`);
  if (!row) {
    // Not in the current list (filters) — refresh once to pick it up.
    loadJobs();
    return;
  }
  if (payload.status) {
    const cell = row.children[2]; // Status column
    if (cell) {
      cell.innerHTML =
        statusPill(payload.status) + (payload.dry_run === true ? " " + dryRunPill() : "");
    }
  }
  if (selectedId === id && payload.status && !ACTIVE.has(String(payload.status))) {
    showJob(id, { silent: true });
  }
}

async function loadJobs() {
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
  const tbody = document.getElementById("jobs-tbody");
  const err = document.getElementById("jobs-err");
  err.innerHTML = "";

  const qs = new URLSearchParams();
  const status = document.getElementById("f-status").value;
  const envId = document.getElementById("f-env").value;
  if (status) qs.set("status", status);
  if (envId) qs.set("environment_id", envId);
  qs.set("limit", "100");

  let jobs;
  try {
    jobs = (await api("/api/v1/jobs?" + qs.toString())) || [];
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" class="muted">Unavailable</td></tr>`;
    err.innerHTML = `<div class="error">${esc(e.message)}</div>`;
    return;
  }

  document.getElementById("jobs-msg").textContent = `${jobs.length} job(s)`;
  tbody.innerHTML =
    jobs
      .map(
        (j) => `<tr class="clickable${j.id === selectedId ? " selected" : ""}" data-job="${esc(j.id)}">
          <td class="muted">${esc(fmtTime(j.created_at || j.started_at))}</td>
          <td><code>${esc(j.operation)}</code></td>
          <td>${statusPill(j.status)}${j.dry_run === true ? " " + dryRunPill() : ""}${transportPill(j.log_text, j.dry_run)}</td>
          <td class="muted">${esc(envName(j.environment_id))}</td>
          <td class="muted">${esc(j.created_by || "")}</td>
        </tr>`
      )
      .join("") || `<tr><td colspan="5" class="muted">No jobs</td></tr>`;
  tbody.querySelectorAll("tr[data-job]").forEach((tr) =>
    tr.addEventListener("click", () => showJob(tr.dataset.job))
  );

  // Auto-poll while any visible job is still active — only as fallback when the
  // SSE stream is down; live updates arrive via onJobEvent otherwise.
  const activeIds = jobs.filter((j) => ACTIVE.has(j.status)).map((j) => j.id);
  if (activeIds.length && !sseLive) {
    pollTimer = setTimeout(async () => {
      await loadJobs();
      if (selectedId && activeIds.includes(selectedId)) showJob(selectedId, { silent: true });
    }, POLL_MS);
  }
}

async function showJob(id, { silent = false } = {}) {
  selectedId = id;
  history.replaceState(null, "", "#/activity?tab=jobs&job=" + encodeURIComponent(id));
  document.querySelectorAll("#jobs-tbody tr[data-job]").forEach((tr) =>
    tr.classList.toggle("selected", tr.dataset.job === id)
  );
  const body = document.getElementById("job-detail-body");
  if (!silent) body.innerHTML = "Loading…";

  let job;
  try {
    job = await api(`/api/v1/jobs/${encodeURIComponent(id)}`);
  } catch (e) {
    body.innerHTML = `<div class="error">${esc(e.message)}</div>`;
    return;
  }

  body.innerHTML = `
    <div class="detail-grid">
      <div><div class="k">ID</div><code>${esc(job.id)}</code></div>
      <div><div class="k">Operation</div><code>${esc(job.operation)}</code></div>
      <div><div class="k">Status</div>${statusPill(job.status)}${job.dry_run === true ? " " + dryRunPill() : ""}${transportPill(job.log_text, job.dry_run)}</div>
      <div><div class="k">Environment</div>${esc(envName(job.environment_id))}</div>
      <div><div class="k">Created by</div>${esc(job.created_by || "—")}</div>
      <div><div class="k">Created</div>${esc(fmtTime(job.created_at))}</div>
      <div><div class="k">Started</div>${esc(fmtTime(job.started_at)) || "—"}</div>
      <div><div class="k">Finished</div>${esc(fmtTime(job.finished_at)) || "—"}</div>
    </div>
    ${job.error ? `<div class="error">${esc(job.error)}${job.dry_run === true ? " (rehearsal — nothing was executed)" : ""}</div>` : ""}
    ${!job.error && job.dry_run === true ? `<div class="dryrun-note">Rehearsal — nothing was executed.</div>` : ""}
    <div class="row" style="margin-bottom:.75rem">
      <button class="secondary btn-sm" id="btn-job-retry" type="button" ${gate(canRun(), "operator")}>Retry</button>
      ${resumeBtnHtml(job)}
      ${ACTIVE.has(job.status) ? `<button class="danger btn-sm" id="btn-job-cancel" type="button" ${gate(canRun(), "operator")}>Cancel</button>` : ""}
      <span id="job-retry-msg" class="muted"></span>
    </div>
    <h2>Params</h2>
    <pre>${esc(JSON.stringify(job.params || {}, null, 2))}</pre>
    <h2 style="margin-top:1rem">Log</h2>
    <pre class="log-tall">${esc(job.log_text || "(no log)")}</pre>`;

  document.getElementById("btn-job-retry").addEventListener("click", () => retryJob(job));
  const resumeBtn = document.getElementById("btn-job-resume");
  if (resumeBtn) {
    const stage = failedDeployStage(job);
    resumeBtn.disabled = !stage;
    if (!stage) resumeBtn.title = "Failed stage could not be determined from the job log";
    resumeBtn.addEventListener("click", () => resumeDeployJob(job, stage));
  }
  const cancelBtn = document.getElementById("btn-job-cancel");
  if (cancelBtn) cancelBtn.addEventListener("click", () => cancelJob(job));
}

// "Resume from failed stage" — only offered on failed genestack.deploy jobs.
// genestack.deploy requires the admin role server-side, so gate at admin.
function resumeBtnHtml(job) {
  if (job.operation !== "genestack.deploy" || job.status !== "failed") return "";
  return `<button class="secondary btn-sm" id="btn-job-resume" type="button" ${gate(canAdmin(), "admin")}>Resume from failed stage</button>`;
}

async function resumeDeployJob(job, stage) {
  const msg = document.getElementById("job-retry-msg");
  msg.textContent = "Creating resume job…";
  try {
    const body = { operation: job.operation, params: { ...(job.params || {}) } };
    if (stage) body.params.from_stage = stage;
    if (job.environment_id) body.environment_id = job.environment_id;
    body.run_sync = false;
    const newJob = await api(
      job.environment_id
        ? `/api/v1/environments/${encodeURIComponent(job.environment_id)}/jobs`
        : "/api/v1/jobs",
      { method: "POST", body: JSON.stringify(body) }
    );
    toast(`Resume job ${String(newJob.id).slice(0, 8)}… created (from stage ${esc(stage)})`, "ok");
    await loadJobs();
    showJob(newJob.id);
  } catch (e) {
    msg.textContent = e.message;
    if (e.status === 403) toast("Insufficient role: admin required", "bad");
  }
}

async function cancelJob(job) {
  const msg = document.getElementById("job-retry-msg");
  msg.textContent = "Cancelling…";
  try {
    const updated = await api(`/api/v1/jobs/${encodeURIComponent(job.id)}/cancel`, {
      method: "POST",
    });
    toast(
      updated.status === "failed"
        ? "Job cancelled"
        : "Cancellation requested — stops at the next command boundary",
      "ok"
    );
    await loadJobs();
    showJob(job.id, { silent: true });
  } catch (e) {
    msg.textContent = e.message;
    if (e.status === 403) toast("Insufficient role: operator required", "bad");
    if (e.status === 409) toast("Job already finished — cannot cancel", "bad");
  }
}

async function retryJob(job) {
  const msg = document.getElementById("job-retry-msg");
  msg.textContent = "Retrying…";
  try {
    const newJob = await api(`/api/v1/jobs/${encodeURIComponent(job.id)}/retry`, {
      method: "POST",
      body: JSON.stringify({ run_sync: false }),
    });
    toast(`Retry created job ${String(newJob.id).slice(0, 8)}…`, "ok");
    await loadJobs();
    showJob(newJob.id);
  } catch (e) {
    msg.textContent = e.message;
    if (e.status === 403) toast("Insufficient role: operator required", "bad");
  }
}
