// pages/environment_components.js — "Components" card on the environment detail
// page. Desired-state toggles saved via PUT /api/v1/genestack/components (which
// versions the env config doc and returns {components, version, note} — a push is
// still required to apply), a drift badge fed by GET /api/v1/environments/{id}/drift
// (re-checked on env:{id} snapshot events and fleet-topic drift events), and a
// two-step reconcile: "Preview plan" runs a genestack.components.reconcile job with
// {apply:false} and, once the plan succeeds, reveals "Apply reconcile" ({apply:true},
// confirm() lists the planned changes from the plan job's log). Job completion
// arrives over the "jobs" SSE topic with a 5s poll fallback while the stream is
// down (same pattern as environment_servers.js). All rendering is defensive: a
// missing drifted field or an absent drift endpoint degrades to a grey "drift
// unknown" badge, never an exception.
import { api, esc, fmtAge, toast } from "../api.js";
import { canRun, gate } from "../store.js";
import { connect } from "../stream.js";

const JOB_ACTIVE = new Set(["queued", "running"]);
const JOB_POLL_MS = 5000;

let activeEnvId = ""; // guards against stale DOM after navigation/env switch
let streamHandle = null;
let subscribedEnvId = "";
let sseLive = false;
let drift = null; // last /drift payload; null = never checked (or check unavailable)
let planJob = null; // { id, status } — in-flight reconcile plan (apply:false)
let applyJob = null; // { id, status } — in-flight reconcile apply (apply:true)
let planSummary = ""; // planned-change lines extracted from the finished plan job log
let pollTimer = null;

function el(id) {
  return document.getElementById(id);
}

// ---------- drift badge ----------

// artifacts may arrive as a list of {artifact, status, ...} (/drift endpoint) or a
// name → status map (fleet SSE drift event). Returns [[name, status], ...].
function artifactEntries(d) {
  if (!d || typeof d !== "object") return [];
  if (Array.isArray(d.artifacts)) {
    return d.artifacts
      .filter((a) => a && typeof a === "object")
      .map((a) => [String(a.artifact || "?"), String(a.status || "unknown")]);
  }
  if (d.artifacts && typeof d.artifacts === "object") {
    return Object.entries(d.artifacts).map(([name, status]) => [String(name), String(status)]);
  }
  return [];
}

function paintDrift() {
  const badge = el("comp-drift");
  if (!badge) return;
  // Defensive: missing drifted field / never checked → grey "drift unknown".
  if (!drift || drift.drifted === undefined || drift.drifted === null) {
    const note = drift && drift.error ? ` title="drift check failed: ${esc(drift.error)}"` : ' title="drift never checked"';
    badge.innerHTML = `<span class="pill"${note}>drift unknown</span>`;
    return;
  }
  if (drift.drifted === true) {
    const nonMatch = artifactEntries(drift).filter(([, status]) => status !== "match");
    const tip = nonMatch.length
      ? nonMatch.map(([name, status]) => `${name}: ${status}`).join("\n")
      : "one or more artifacts differ from the expected state";
    badge.innerHTML = `<span class="pill warn" title="${esc(tip)}">drift</span>`;
    return;
  }
  const age = fmtAge(drift.checked_at);
  badge.innerHTML = `<span class="pill ok" title="all artifacts match${age ? " · checked " + esc(age) : ""}">in sync</span>`;
}

async function loadDrift(envId) {
  if (!envId || !el("comp-drift")) return;
  try {
    const d = await api(`/api/v1/environments/${encodeURIComponent(envId)}/drift`);
    if (envId !== activeEnvId || !el("comp-drift")) return;
    drift = d && typeof d === "object" ? d : null;
  } catch (e) {
    if (envId !== activeEnvId || !el("comp-drift")) return;
    // 404 = never checked; anything else = check unavailable. Both render grey.
    drift = e && e.status === 404 ? null : { error: e.message };
  }
  paintDrift();
}

// ---------- toggles ----------

function toggleCellHtml(name, value) {
  if (value !== true && value !== false) {
    return `<span class="muted">${esc(value)}</span>`;
  }
  return `<label class="switch" title="Toggle desired state for ${esc(name)}">
    <input type="checkbox" data-comp-toggle="${esc(name)}"${value ? " checked" : ""} ${gate(canRun(), "operator")} />
    <span class="track"></span>
  </label>`;
}

// Renders the descriptor's components payload (same shape the old read-only card
// consumed) as toggle rows + chart versions.
function paintComponents(comp) {
  const body = el("comp-body");
  if (!body) return;
  body.classList.remove("muted");
  if (!comp || typeof comp !== "object") {
    body.classList.add("muted");
    body.innerHTML = "Unavailable.";
    return;
  }
  if (comp.error) {
    body.innerHTML = `<div class="error">${esc(comp.error)}</div>`;
    return;
  }
  const components = comp.components && typeof comp.components === "object" ? comp.components : {};
  // chart_versions is {"charts": {...}} | null; tolerate a flat map too.
  let versions = {};
  const cv = comp.chart_versions;
  if (cv && typeof cv === "object") {
    versions = cv.charts && typeof cv.charts === "object" ? cv.charts : cv;
  }
  const names = Object.keys(components).sort();
  const srcNote = comp.chart_versions_source ? ` · versions: ${esc(comp.chart_versions_source)}` : "";
  const meta = `<div class="muted" style="font-size:.8rem;margin-bottom:.5rem">scope: <code>${esc(comp.scope || "—")}</code> · path: <code>${esc(comp.path || "—")}</code>${srcNote}</div>`;
  const versionsErr = comp.chart_versions_error
    ? `<div class="hint muted">chart versions: ${esc(comp.chart_versions_error)}</div>`
    : "";
  if (!names.length) {
    body.innerHTML = meta + versionsErr + '<div class="muted">Unavailable — no components listed.</div>';
    return;
  }
  const rows = names
    .map(
      (name) => `<tr>
        <td><strong>${esc(name)}</strong></td>
        <td>${toggleCellHtml(name, components[name])}</td>
        <td><code>${esc(versions[name] || "—")}</code></td>
      </tr>`
    )
    .join("");
  body.innerHTML = `${meta}${versionsErr}<table>
    <thead><tr><th>Service</th><th>Enabled</th><th>Chart version</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

// Saving versions the env config doc; the returned note says so ("saved to config
// vN — push to apply"). Show it inline and refresh the config card's version pill.
async function saveComponents() {
  const envId = activeEnvId;
  const msg = el("comp-msg");
  const note = el("comp-note");
  const body = el("comp-body");
  if (!envId || !body) return;
  const components = {};
  body.querySelectorAll("input[data-comp-toggle]").forEach((input) => {
    components[input.dataset.compToggle] = input.checked;
  });
  if (!Object.keys(components).length) {
    toast("Nothing to save — the environment lists no components", "warn");
    return;
  }
  if (msg) msg.textContent = "Saving…";
  try {
    const res = await api(
      `/api/v1/genestack/components?environment_id=${encodeURIComponent(envId)}`,
      {
        method: "PUT",
        body: JSON.stringify({ components }),
      }
    );
    if (envId !== activeEnvId) return;
    if (msg) msg.textContent = "";
    const text = res && res.note ? String(res.note) : "saved — push to apply";
    if (note) note.textContent = text;
    // Refresh the config version display on the Config card, if it is rendered.
    const version = res && res.version != null ? res.version : null;
    const cfgPill = el("cfg-version");
    if (version != null && cfgPill) {
      cfgPill.textContent = "v" + version;
      cfgPill.className = "pill ok";
    }
    toast(`Components ${text}`, "ok");
  } catch (e) {
    if (msg) msg.textContent = "";
    if (note) note.innerHTML = `<span class="error">${esc(e.message)}</span>`;
    toast(`Save failed: ${e.message}`, "bad");
    if (e.status === 403) toast("Insufficient role: operator required", "bad");
  }
}

// ---------- reconcile (genestack.components.reconcile jobs) ----------

function jobLineHtml(job, label) {
  const id = String(job.id || "");
  const link = id ? ` <a href="#/activity?tab=jobs&job=${esc(id)}">job ${esc(id.slice(0, 8))}…</a>` : "";
  return `<span class="pill warn">${esc(label)} ${esc(job.status || "queued")}…</span>${link}`;
}

function paintReconcile() {
  const box = el("comp-job");
  const applyBtn = el("comp-btn-apply");
  const planBtn = el("comp-btn-plan");
  if (planBtn) planBtn.disabled = !canRun() || !!planJob || !!applyJob;
  if (applyBtn) {
    // Revealed only after a plan job succeeded and no apply is in flight.
    applyBtn.classList.toggle("hidden", !planSummary || !!applyJob);
    applyBtn.disabled = !canRun() || !!applyJob;
  }
  if (!box) return;
  if (applyJob) {
    box.innerHTML = jobLineHtml(applyJob, "reconcile apply");
  } else if (planJob) {
    box.innerHTML = jobLineHtml(planJob, "reconcile plan");
  } else if (planSummary) {
    box.innerHTML = `<div class="muted" style="font-size:.8rem">plan ready — review the job log, then Apply reconcile.</div>`;
  } else {
    box.innerHTML = "";
  }
}

async function startReconcileJob(apply) {
  const envId = activeEnvId;
  if (!envId) return null;
  try {
    const job = await api(`/api/v1/environments/${encodeURIComponent(envId)}/jobs`, {
      method: "POST",
      body: JSON.stringify({ operation: "genestack.components.reconcile", params: { apply: apply === true } }),
    });
    return job && job.id != null ? String(job.id) : null;
  } catch (e) {
    toast(`Reconcile failed to start: ${e.message}`, "bad");
    if (e.status === 403) toast("Insufficient role: operator required", "bad");
    return null;
  }
}

async function previewPlan() {
  if (planJob || applyJob) return;
  planSummary = "";
  planJob = { id: "", status: "queued" };
  paintReconcile();
  const id = await startReconcileJob(false);
  if (!id) {
    planJob = null;
    paintReconcile();
    return;
  }
  planJob = { id, status: "queued" };
  toast(`Reconcile plan job ${id.slice(0, 8)}… created`, "ok");
  paintReconcile();
  schedulePolling();
}

// Pull the finished plan job's log and keep the lines that describe planned
// changes (enable/disable), so the apply confirm() can list them.
async function summarizePlan(jobId) {
  try {
    const job = await api(`/api/v1/jobs/${encodeURIComponent(jobId)}`);
    const log = job && typeof job.log_text === "string" ? job.log_text : "";
    const lines = log
      .split("\n")
      .map((l) => l.trim())
      .filter((l) => /\b(enable|disable|reconcile|change|apply)\b/i.test(l))
      .filter((l) => !/^\[?(debug|info)\]?$/i.test(l));
    planSummary = lines.slice(0, 30).join("\n") || "(no per-component changes found in the plan log — review the job log)";
  } catch {
    planSummary = "(could not load the plan log — review the job before applying)";
  }
}

async function applyReconcile() {
  if (!planSummary || applyJob || planJob) return;
  if (
    !confirm(
      `Apply reconcile for this environment?\n\nPlanned changes (from the preview plan):\n${planSummary}\n\nThis executes the enables/disables on the deploy host.`
    )
  ) {
    return;
  }
  applyJob = { id: "", status: "queued" };
  paintReconcile();
  const id = await startReconcileJob(true);
  if (!id) {
    applyJob = null;
    paintReconcile();
    return;
  }
  applyJob = { id, status: "queued" };
  toast(`Reconcile apply job ${id.slice(0, 8)}… created`, "ok");
  paintReconcile();
  schedulePolling();
}

function trackedKind(jobId) {
  const id = String(jobId);
  if (planJob && String(planJob.id) === id) return "plan";
  if (applyJob && String(applyJob.id) === id) return "apply";
  return null;
}

// jobs topic event: {type:"job", id, environment_id, operation, status}.
function onJobEvent(payload) {
  if (!payload || payload.id == null) return;
  const kind = trackedKind(payload.id);
  if (!kind) return;
  const status = String(payload.status || "").toLowerCase();
  const job = kind === "plan" ? planJob : applyJob;
  if (JOB_ACTIVE.has(status)) {
    if (job.status !== status) {
      job.status = status;
      paintReconcile();
    }
    return;
  }
  finishReconcileJob(kind, String(payload.id), status);
}

async function finishReconcileJob(kind, jobId, status) {
  const ok = status === "success";
  if (kind === "plan") {
    planJob = null;
    if (ok) {
      toast("Reconcile plan finished — review and apply", "ok");
      await summarizePlan(jobId);
    } else {
      planSummary = "";
      toast(`Reconcile plan ${status || "failed"}`, "bad");
    }
  } else {
    applyJob = null;
    planSummary = ""; // plan consumed — a new apply needs a fresh preview
    toast(`Reconcile apply ${ok ? "succeeded" : status || "failed"}`, ok ? "ok" : "bad");
    if (activeEnvId) loadDrift(activeEnvId); // reconcile re-checks drift server-side
  }
  paintReconcile();
}

function schedulePolling() {
  if (pollTimer || sseLive) return;
  pollTimer = setTimeout(() => pollReconcileJobs(), JOB_POLL_MS);
}

// Fallback for when the jobs stream is down.
async function pollReconcileJobs() {
  pollTimer = null;
  if (!el("comp-card")) return;
  for (const kind of ["plan", "apply"]) {
    const job = kind === "plan" ? planJob : applyJob;
    if (!job || !job.id) continue;
    let data;
    try {
      data = await api(`/api/v1/jobs/${encodeURIComponent(job.id)}`);
    } catch {
      continue; // transient fetch failure — retry on the next tick
    }
    const status = String(data && data.status ? data.status : "").toLowerCase();
    if (JOB_ACTIVE.has(status)) {
      if (status !== job.status) {
        job.status = status;
        paintReconcile();
      }
    } else {
      await finishReconcileJob(kind, String(job.id), status);
    }
  }
  if ((planJob || applyJob) && !sseLive) schedulePolling();
}

// ---------- streaming ----------

function resubscribe(envId) {
  if (streamHandle && subscribedEnvId === envId) return;
  if (streamHandle) {
    streamHandle.close();
    streamHandle = null;
  }
  subscribedEnvId = envId;
  sseLive = false;
  if (!envId) return;
  streamHandle = connect([`env:${envId}`, "fleet", "jobs"], {
    // New telemetry snapshot — re-check drift (cheap, keeps the badge fresh).
    [`env:${envId}`]: (payload) => {
      if (activeEnvId !== envId) return;
      if (payload && payload.type === "drift" && String(payload.environment_id || envId) === envId) {
        drift = payload;
        paintDrift();
        return;
      }
      loadDrift(envId);
    },
    // Fleet-wide drift event for this environment carries the result inline.
    fleet: (payload) => {
      if (!payload || payload.type !== "drift") return;
      if (String(payload.environment_id || "") !== activeEnvId) return;
      drift = payload;
      paintDrift();
    },
    jobs: onJobEvent,
    onState: (state) => {
      sseLive = state === "open";
      if (sseLive && pollTimer) {
        clearTimeout(pollTimer);
        pollTimer = null;
      }
      if (!sseLive && (planJob || applyJob)) schedulePolling();
    },
  });
}

// ---------- public API ----------

export function componentsCardHtml() {
  return `
  <div class="card span-12" id="comp-card">
    <div class="toolbar">
      <h2>Components</h2>
      <span id="comp-drift"></span>
      <button class="secondary btn-sm" id="comp-btn-save" type="button" ${gate(canRun(), "operator")}>Save</button>
      <button class="secondary btn-sm" id="comp-btn-plan" type="button" ${gate(canRun(), "operator")}>Preview plan</button>
      <button class="secondary btn-sm hidden" id="comp-btn-apply" type="button" ${gate(canRun(), "operator")}>Apply reconcile</button>
      <span id="comp-msg" class="muted"></span>
    </div>
    <div id="comp-note" class="muted" style="font-size:.8rem"></div>
    <div id="comp-body" class="muted">—</div>
    <div id="comp-job" style="margin-top:.5rem"></div>
  </div>`;
}

export function wireComponentsCard(getEnvId) {
  const save = el("comp-btn-save");
  if (save) save.addEventListener("click", () => saveComponents());
  const plan = el("comp-btn-plan");
  if (plan) plan.addEventListener("click", () => previewPlan());
  const apply = el("comp-btn-apply");
  if (apply) apply.addEventListener("click", () => applyReconcile());
  paintReconcile();
  paintDrift();
}

// Called by environment_detail after the descriptor fetch resolves: renders the
// toggles from the descriptor's components payload and kicks off the drift check.
export async function loadComponentsCard(envId, comp, { keepStream = false } = {}) {
  if (envId !== activeEnvId) {
    // Env switched: abandon the previous env's drift/reconcile state.
    drift = null;
    planJob = null;
    applyJob = null;
    planSummary = "";
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  }
  activeEnvId = envId || "";
  if (!keepStream) resubscribe(activeEnvId);
  const body = el("comp-body");
  if (!body) return;
  if (!activeEnvId) {
    drift = null;
    paintDrift();
    paintReconcile();
    body.classList.add("muted");
    body.innerHTML = "Select an environment.";
    return;
  }
  paintReconcile();
  paintDrift();
  paintComponents(comp);
  loadDrift(activeEnvId);
}

export function destroyComponentsCard() {
  if (streamHandle) {
    streamHandle.close();
    streamHandle = null;
  }
  subscribedEnvId = "";
  activeEnvId = "";
  sseLive = false;
  drift = null;
  planJob = null;
  applyJob = null;
  planSummary = "";
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}
