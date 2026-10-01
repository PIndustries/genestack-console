// pages/environment_progress.js — "Deploy in progress" bar for the environment detail
// page. Shows the 8-stage genestack pipeline as a horizontal strip parsed from the
// deploy job's log: completed (green ✓), current (pulsing amber), upcoming (grey),
// failed (red, stops the strip). A structured log renderer parses both deploy and
// push operations into human-readable steps with checkmarks and color coding.
// Polls GET /jobs/{id} every 3s while the job is queued/running; subscribes to the
// "jobs" SSE topic for immediate status changes (falls back to polling if SSE drops).
// Inert when the workflow's deploy step has no active job.
import { api, esc, statusPill } from "../api.js";
import { connect } from "../stream.js";

const POLL_MS = 3000;
const IDLE_RECHECK_MS = 15000; // picks up a deploy started from the Config card
const SUCCESS_HIDE_MS = 30000;
const RECENT_DONE_MS = 60000; // show the final strip for jobs that just finished
const ACTIVE = new Set(["queued", "running"]);

let timer = null;
let stages = null; // cached /genestack/pipeline catalog
let activeEnvId = ""; // guards against stale DOM after navigation/env switch
let jobsStreamHandle = null;
let sseLive = false;

// ---------- log parsing ----------

// Lines look like:
//   [deploy] === stage 3/8: operators (Operators & Platform Base) — 2 item(s) ===
//   [deploy] stage hosts complete (1/8)
//   [deploy] FAILED at <stage>/<item> rc=N — stopping pipeline
const RE_STAGE_START = /===\s*stage\s+(\d+)\s*\/\s*(\d+)\s*:\s*([\w-]+)\s*\(([^)]*)\)/g;
const RE_STAGE_DONE = /stage\s+([\w-]+)\s+complete\s*\(\s*(\d+)\s*\/\s*(\d+)\s*\)/g;
const RE_FAILED = /FAILED at .*$/gm;

function parseLog(logText) {
  const text = String(logText || "");
  const completed = new Set();
  const seen = [];
  let current = null;
  let m;
  RE_STAGE_START.lastIndex = 0;
  while ((m = RE_STAGE_START.exec(text))) {
    const id = m[3];
    if (!seen.includes(id)) seen.push(id);
    current = { id, name: m[4] || "", index: Number(m[1]), total: Number(m[2]) };
  }
  RE_STAGE_DONE.lastIndex = 0;
  while ((m = RE_STAGE_DONE.exec(text))) completed.add(m[1]);
  if (current && completed.has(current.id)) current = null;
  let failed = null;
  RE_FAILED.lastIndex = 0;
  while ((m = RE_FAILED.exec(text))) failed = m[0];
  return { completed, current, failed, seen };
}

// ---------- structured log rendering ----------
// Parses both deploy and push logs into human-readable steps with ✓/✕ marks.
// Deploy log patterns:
//   [deploy] === stage 1/8: hosts (Host Setup) — 1 item(s) ===
//   [deploy] stage hosts complete (1/8)
//   [deploy] FAILED at hosts/talos rc=1 — stopping pipeline
//   [deploy] phase 1: push config version=3 dry_run=false
//   [deploy] pushed config version=3 files=12 bytes=8421
// Push log patterns:
//   [config.push] version=3 dry_run=false
//   [dry-run] would write /etc/genestack/.../inventory.yaml (2341 bytes)
//   wrote /etc/genestack/.../inventory.yaml (2341 bytes)
//   merged kubesecrets.yaml — 3 updated, 2 preserved
// Generic patterns:
//   [pipeline] skipping tempest (disabled in config doc)
//   [warn] ...
//   [ssh] deploy host: user@host

const RE_PUSH_VERSION = /\[config\.push\]\s+version=(\d+)/g;
const RE_PUSH_WOULD_WRITE = /\[dry-run\]\s+would\s+write\s+([^\s]+)\s+\((\d+)\s+bytes\)/g;
const RE_PUSH_WROTE = /\b([a-zA-Z])\wrote\s+([^\s]+)\s+\((\d+)\s+bytes\)/g;
const RE_PUSH_MERGED = /merged\s+(\S+)\s*—\s*(.+)$/gm;
const RE_DEPLOY_PHASE = /\[deploy\]\s+phase\s+\d+:\s*(.+)$/gm;
const RE_DEPLOY_PUSHED = /\[deploy\]\s+pushed\s+config\s+version=(\d+)\s+files=(\d+)\s+bytes=(\d+)/g;
const RE_SKIP_DISABLED = /\[pipeline\]\s+skipping\s+(\S+)\s+\(disabled in config doc\)/g;
const RE_WARN = /\[warn\]\s+(.+)$/gm;
const RE_SSH_TARGET = /\[ssh\]\s+deploy\s+host:\s*(.+)$/gm;
const RE_AGENT_EXEC = /\[agent\]\s+executor:\s*(.+)$/gm;
const RE_ITEM_RC = /\[pipeline\]\s+stopping\s+at\s+(\S+)\s+rc=(\d+)/gm;
const RE_COMPLETE = /Completed\s+successfully:\s*(.+)$/gm;
const RE_FAILED_MSG = /Failed:\s*(.+)$/gm;
const RE_EXCEPTION = /Exception:\s*(.+)$/gm;

function classifyLine(line) {
  const s = line.trim();
  if (!s) return null;
  // Stage markers
  const mStart = /===\s*stage\s+(\d+)\s*\/\s*(\d+)\s*:\s*([\w-]+)\s*\(([^)]*)\)/.exec(s);
  if (mStart) return { type: "stage-start", id: mStart[3], name: mStart[4], index: mStart[1], total: mStart[2] };
  const mDone = /stage\s+([\w-]+)\s+complete\s*\(\s*(\d+)\s*\/\s*(\d+)\s*\)/.exec(s);
  if (mDone) return { type: "stage-done", id: mDone[1], done: mDone[2], total: mDone[3] };
  // Failure
  const mFail = /FAILED at (.+?)\s*(rc=\d+)?\s*(?:— stopping pipeline)?/.exec(s);
  if (mFail) return { type: "error", message: `FAILED at ${mFail[1]}${mFail[2] ? " " + mFail[2] : ""}` };
  // Push file operations
  const mWould = /\[dry-run\]\s+would\s+write\s+([^\s]+)\s+\((\d+)\s+bytes\)/.exec(s);
  if (mWould) return { type: "file-skip", path: mWould[1].split("/").pop(), bytes: mWould[2] };
  const mWrote = /\bwrote\s+([^\s]+)\s+\((\d+)\s+bytes\)/.exec(s);
  if (mWrote) return { type: "file-written", path: mWrote[1].split("/").pop(), bytes: mWrote[2] };
  // Merged files
  const mMerged = /merged\s+(\S+)\s*—\s*(.+)/.exec(s);
  if (mMerged) return { type: "info", message: `Merged ${mMerged[1]} — ${mMerged[2]}` };
  // Deploy phases
  const mPhase = /\[deploy\]\s+phase\s+(\d+):\s*(.+)/.exec(s);
  if (mPhase) return { type: "phase", number: mPhase[1], message: mPhase[2] };
  // Deploy push summary
  const mPushed = /\[deploy\]\s+pushed\s+config\s+version=(\d+)\s+files=(\d+)\s+bytes=(\d+)/.exec(s);
  if (mPushed) return { type: "summary", message: `Pushed config v${mPushed[1]}: ${mPushed[2]} files, ${mPushed[3]} bytes` };
  // Skipped items
  const mSkip = /\[pipeline\]\s+skipping\s+(\S+)\s+\(disabled in config doc\)/.exec(s);
  if (mSkip) return { type: "skip", service: mSkip[1] };
  // Warnings
  const mWarn = /\[warn\]\s+(.+)/.exec(s);
  if (mWarn) return { type: "warn", message: mWarn[1] };
  // SSH/agent target
  const mSsh = /\[ssh\]\s+deploy\s+host:\s*(.+)/.exec(s);
  if (mSsh) return { type: "info", message: `SSH target: ${mSsh[1]}` };
  const mAgent = /\[agent\]\s+executor:\s*(.+)/.exec(s);
  if (mAgent) return { type: "info", message: `Agent: ${mAgent[1]}` };
  // Completed/Failed/Exception
  const mComplete = /Completed\s+successfully:\s*(.+)/.exec(s);
  if (mComplete) return { type: "success", message: mComplete[1] };
  const mFailed = /Failed:\s*(.+)/.exec(s);
  if (mFailed) return { type: "error", message: mFailed[1] };
  const mExcept = /Exception:\s*(.+)/.exec(s);
  if (mExcept) return { type: "error", message: `Exception: ${mExcept[1]}` };
  // Config push version
  const mCv = /\[config\.push\]\s+version=(\d+)/.exec(s);
  if (mCv) return { type: "info", message: `Config push v${mCv[1]}` };
  // Timestamped raw log lines — strip timestamp for display
  const mTs = /^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\]\s+(.+)/.exec(s);
  if (mTs) return classifyLine(mTs[1]) || { type: "raw", message: mTs[1] };
  return { type: "raw", message: s };
}

function formatBytes(n) {
  const num = parseInt(n, 10);
  if (isNaN(num)) return n;
  if (num < 1024) return num + " B";
  if (num < 1024 * 1024) return (num / 1024).toFixed(1) + " KB";
  return (num / (1024 * 1024)).toFixed(1) + " MB";
}

function renderStructuredLog(logText, job) {
  const text = String(logText || "");
  const lines = text.split("\n").filter(Boolean);
  if (!lines.length) return '<div class="dp-log-empty muted">Waiting for output…</div>';

  const entries = [];
  let lastErrorLine = null;
  const errorContext = [];

  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i].trim();
    if (!raw) continue;
    const cls = classifyLine(raw);
    if (!cls) continue;

    if (cls.type === "error") {
      lastErrorLine = i;
      errorContext.length = 0;
    }
    if (lastErrorLine != null && Math.abs(i - lastErrorLine) <= 3) {
      errorContext.push({ line: raw, cls });
    }

    entries.push({ raw, cls, idx: i });
  }

  // Build HTML — only show classified lines, skip raw timestamps clutter
  const htmlLines = entries.map(({ raw, cls }) => {
    switch (cls.type) {
      case "stage-start":
        return `<div class="dp-log-stage">Stage ${cls.index}/${cls.total}: <strong>${esc(cls.name)}</strong></div>`;
      case "stage-done":
        return `<div class="dp-log-line ok">✓ ${esc(cls.id)} complete (${cls.done}/${cls.total})</div>`;
      case "file-written":
        return `<div class="dp-log-line ok">✓ Wrote ${esc(cls.path)} (${formatBytes(cls.bytes)})</div>`;
      case "file-skip":
        return `<div class="dp-log-line muted">○ Would write ${esc(cls.path)} (${formatBytes(cls.bytes)})</div>`;
      case "error":
        return `<div class="dp-log-line error">✕ ${esc(cls.message)}</div>`;
      case "warn":
        return `<div class="dp-log-line warn">⚠ ${esc(cls.message)}</div>`;
      case "skip":
        return `<div class="dp-log-line muted">○ ${esc(cls.service)} (disabled)</div>`;
      case "phase":
        return `<div class="dp-log-phase">Phase ${cls.number}: ${esc(cls.message)}</div>`;
      case "summary":
        return `<div class="dp-log-line ok">✓ ${esc(cls.message)}</div>`;
      case "success":
        return `<div class="dp-log-line ok">✓ ${esc(cls.message)}</div>`;
      case "info":
        return `<div class="dp-log-line muted">${esc(cls.message)}</div>`;
      default:
        // Skip unclassified raw lines — they're timestamps and internal noise
        return "";
    }
  }).filter(Boolean).join("");

  // Error context: when the job failed, show a focused error block
  let errorBlock = "";
  if (lastErrorLine != null && errorContext.length && (job.status === "failed" || job.status === "error")) {
    const ctxHtml = errorContext.map(({ line, cls }) => {
      if (cls.type === "error") return `<div class="dp-log-line error">✕ ${esc(classifyLine(line).message)}</div>`;
      return `<div class="dp-log-line muted" style="opacity:0.6">${esc(line.slice(0, 120))}</div>`;
    }).join("");
    const suggestion = getSuggestion(errorContext);
    errorBlock = `<div class="dp-error-block">
      <strong>Failure context:</strong>
      <pre class="dp-error-ctx">${ctxHtml}</pre>
      ${suggestion ? `<div class="dp-suggestion">💡 ${esc(suggestion)}</div>` : ""}
    </div>`;
  }

  return htmlLines + errorBlock;
}

function getSuggestion(ctx) {
  if (!ctx.length) return "";
  const text = ctx.map(c => c.line.toLowerCase()).join(" ");
  if (/ssh|connection refused|no route|host key/.test(text))
    return "Check SSH connectivity to the deploy host and verify firewall rules.";
  if (/kubeconfig|admin.conf/.test(text))
    return "Ensure the Kubernetes control plane is accessible and admin.conf exists on the first control-plane node.";
  if (/permission denied|chmod/.test(text))
    return "Check file permissions on the deploy host — try running 'Prepare host' again.";
  if (/timeout|deadline/.test(text))
    return "The job timed out — check network connectivity and try again.";
  if (/not found|no config|missing/.test(text))
    return "Save your environment config and Push before deploying.";
  if (/talos/.test(text))
    return "Verify Talos config and that nodes are reachable. Check the Talos bootstrap logs.";
  return "Check the full job log for details and review the deploy host's SSH connectivity.";
}

function stageList(parsed) {
  if (Array.isArray(stages) && stages.length) return stages;
  // Fallback when the pipeline catalog is unavailable: stages seen in the log.
  return parsed.seen.map((id) => ({ id, name: id }));
}

function stripHtml(parsed, job) {
  const list = stageList(parsed);
  const failedStage = parsed.failed && parsed.current ? parsed.current.id : null;
  const segs = list
    .map((stage, i) => {
      const id = String(stage.id || "");
      let cls = "upcoming";
      let mark = String(i + 1);
      let title = "pending";
      if (parsed.completed.has(id)) {
        cls = "done";
        mark = "✓";
        title = "complete";
      } else if (parsed.failed && parsed.current && id === parsed.current.id) {
        cls = "failed";
        mark = "✕";
        title = "failed";
      } else if (parsed.failed && failedStage && list.findIndex((s) => String(s.id) === failedStage) > i) {
        cls = "done";
        mark = "✓";
        title = "complete";
      } else if (parsed.current && id === parsed.current.id && !parsed.failed) {
        cls = "current";
        mark = "●";
        title = "in progress";
      } else if (parsed.failed) {
        cls = "stopped";
        title = "not reached";
      }
      return `<div class="dp-seg ${cls}" title="${esc(stage.name || id)} — ${title}">
        <span class="dp-seg-mark">${mark}</span>
        <span class="dp-seg-name">${esc(stage.name || id)}</span>
      </div>`;
    })
    .join("");
  const total = list.length || 8;
  let statusLine = "";
  if (parsed.failed) {
    statusLine = `<div class="dp-status bad">${esc(parsed.failed)}</div>`;
  } else if (parsed.current && ACTIVE.has(String(job.status || "").toLowerCase())) {
    const c = parsed.current;
    statusLine = `<div class="dp-status">Stage ${c.index || "?"}/${c.total || total}: <strong>${esc(c.name || c.id)}</strong></div>`;
  } else if (String(job.status || "").toLowerCase() === "queued") {
    statusLine = '<div class="dp-status muted">Queued — waiting for a worker…</div>';
  }
  return `<div class="dp-strip">${segs}</div>${statusLine}`;
}

function finalHtml(job, parsed) {
  const ok = String(job.status || "").toLowerCase() === "success";
  const id = String(job.id || "");
  if (ok) {
    return `<div class="dp-final ok">
      <strong>Deploy complete</strong> — all stages finished.
      <a href="#/activity?tab=jobs&job=${esc(id)}">view job log →</a>
    </div>`;
  }
  return `<div class="dp-final bad">
    <strong>Deploy failed.</strong>
    ${parsed.failed ? `<div class="dp-status bad">${esc(parsed.failed)}</div>` : job.error ? `<div class="dp-status bad">${esc(job.error)}</div>` : ""}
    <a href="#/activity?tab=jobs&job=${esc(id)}">view full job log →</a>
  </div>`;
}

function showCard(job, parsed, { final = false } = {}) {
  const card = document.getElementById("dp-card");
  const body = document.getElementById("dp-body");
  if (!card || !body) return false;
  const head = document.getElementById("dp-head");
  const dryRun = job.params && job.params.dry_run === true ? ' <span class="pill warn">dry-run</span>' : "";
  if (head) {
    head.innerHTML = final
      ? "Deploy finished"
      : `Deploy in progress — job <a href="#/activity?tab=jobs&job=${esc(String(job.id || ""))}">${esc(String(job.id || "").slice(0, 8))}…</a> ${statusPill(job.status)}${dryRun}`;
  }
  card.classList.remove("hidden");
  const strip = stripHtml(parsed, job);
  const logHtml = renderStructuredLog(job.log_text, job);
  const finalSection = final ? finalHtml(job, parsed) : "";
  body.innerHTML = strip + `<div class="dp-log-wrap" id="dp-log">${logHtml}</div>` + finalSection;
  // Auto-scroll log to bottom
  const logEl = document.getElementById("dp-log");
  if (logEl) logEl.scrollTop = logEl.scrollHeight;
  return true;
}

function hideCard() {
  const card = document.getElementById("dp-card");
  if (card) card.classList.add("hidden");
}

// ---------- SSE integration ----------

function resubscribeSse(envId) {
  if (jobsStreamHandle) {
    jobsStreamHandle.close();
    jobsStreamHandle = null;
  }
  sseLive = false;
  if (!envId) return;
  jobsStreamHandle = connect(["jobs"], {
    jobs: (payload) => {
      if (!payload || payload.id == null) return;
      // If we're actively polling a deploy job, SSE event triggers immediate re-fetch
      if (pollingJobId && String(payload.id) === pollingJobId) {
        pollJobOnce(pollingJobId, pollOnDone);
      }
    },
    onState: (state) => {
      sseLive = state === "open";
      if (sseLive) {
        // SSE is authoritative — extend poll interval since events come in real-time
        clearTimer();
        if (pollingJobId) {
          timer = setTimeout(() => pollJob(pollingJobId, pollOnDone), POLL_MS * 3);
        }
      }
    },
  });
}

// ---------- polling ----------

let pollingJobId = ""; // current job being polled ("" = idle)
let pollOnDone = null; // callback when polling job reaches terminal state

function clearTimer() {
  if (timer) {
    clearTimeout(timer);
    timer = null;
  }
}

// Fetch once without scheduling the next tick. Used by SSE handler.
async function pollJobOnce(jobId, onDone) {
  if (!document.getElementById("dp-card")) return;
  let job;
  try {
    job = await api(`/api/v1/jobs/${encodeURIComponent(jobId)}`);
  } catch {
    return;
  }
  if (!document.getElementById("dp-card")) return;
  const parsed = parseLog(job.log_text);
  const status = String(job.status || "").toLowerCase();
  if (ACTIVE.has(status)) {
    showCard(job, parsed);
  } else {
    // Terminal state
    showCard(job, parsed, { final: true });
    if (typeof onDone === "function") onDone(job);
    if (status === "success") timer = setTimeout(hideCard, SUCCESS_HIDE_MS);
  }
}

async function ensureStages() {
  if (Array.isArray(stages) && stages.length) return;
  try {
    const data = await api("/api/v1/genestack/pipeline");
    stages = Array.isArray(data && data.stages) ? data.stages : [];
  } catch {
    stages = []; // fall back to ids seen in the log
  }
}

async function pollJob(jobId, onDone) {
  clearTimer();
  pollingJobId = jobId;
  pollOnDone = onDone;
  if (!document.getElementById("dp-card")) return; // navigated away
  let job;
  try {
    job = await api(`/api/v1/jobs/${encodeURIComponent(jobId)}`);
  } catch {
    // Transient fetch failure: keep the strip as-is and retry on the next tick.
    // Don't reschedule if SSE is live — it will kick us instead.
    if (!sseLive) timer = setTimeout(() => pollJob(jobId, onDone), POLL_MS);
    return;
  }
  if (!document.getElementById("dp-card")) return;
  const parsed = parseLog(job.log_text);
  const status = String(job.status || "").toLowerCase();
  if (ACTIVE.has(status)) {
    if (!showCard(job, parsed)) return;
    // Only schedule next poll if SSE is not live
    if (!sseLive) timer = setTimeout(() => pollJob(jobId, onDone), POLL_MS);
    return;
  }
  // Terminal state: show the final strip, notify the page, stop polling.
  pollingJobId = "";
  pollOnDone = null;
  if (!showCard(job, parsed, { final: true })) return;
  if (typeof onDone === "function") onDone(job);
  if (status === "success") timer = setTimeout(hideCard, SUCCESS_HIDE_MS);
}

// ---------- public API ----------

export function progressCardHtml() {
  return `
  <div class="card span-12 hidden" id="dp-card">
    <div class="toolbar">
      <h2 id="dp-head">Deploy in progress</h2>
      <span id="dp-msg" class="muted"></span>
    </div>
    <div id="dp-body"></div>
  </div>`;
}

export function wireProgressCard() {
  // No interactive elements of its own; the job log link uses the hash router.
}

// finished_at from the API is naive UTC (no designator) — parse it as UTC.
function finishedRecently(finishedAt, windowMs) {
  if (!finishedAt) return false;
  const t = String(finishedAt);
  const ts = Date.parse(/Z$|[+-]\d{2}:?\d{2}$/.test(t) ? t : t + "Z");
  if (Number.isNaN(ts)) return false;
  const age = Date.now() - ts;
  return age >= 0 && age < windowMs;
}

// Load the workflow's deploy step; only poll the job endpoint when a deploy job is
// queued/running. A job that finished in the last minute (e.g. the user just clicked
// Deploy on a fast dry-run) shows its final strip once. When idle, re-check
// occasionally so a deploy kicked off from the Config card appears without a manual
// refresh.
export async function loadProgressCard(envId, { onDone } = {}) {
  clearTimer();
  activeEnvId = envId || "";
  if (!envId) {
    hideCard();
    return;
  }
  let data;
  try {
    data = await api(`/api/v1/environments/${encodeURIComponent(envId)}/workflow`);
  } catch {
    hideCard(); // workflow unavailable — stay inert, never break the page
    return;
  }
  if (activeEnvId !== envId || !document.getElementById("dp-card")) return;
  const steps = Array.isArray(data && data.steps) ? data.steps : [];
  const deploy = steps.find((s) => s && s.id === "deploy");
  const d = deploy && deploy.details && typeof deploy.details === "object" ? deploy.details : {};
  const jobId = d.job_id != null ? String(d.job_id) : "";
  const status = String(d.status || "").toLowerCase();

  if (jobId && ACTIVE.has(status)) {
    await ensureStages();
    if (activeEnvId !== envId) return;
    resubscribeSse(envId);
    pollJob(jobId, onDone);
    return;
  }
  if (jobId && finishedRecently(d.finished_at, RECENT_DONE_MS)) {
    // Just finished: render the final strip once, no polling.
    await ensureStages();
    try {
      const job = await api(`/api/v1/jobs/${encodeURIComponent(jobId)}`);
      if (activeEnvId !== envId) return;
      showCard(job, parseLog(job.log_text), { final: true });
    } catch {
      hideCard();
    }
  } else {
    hideCard();
  }
  // Idle: one slow re-check so a freshly clicked Deploy shows up on its own.
  timer = setTimeout(() => {
    if (activeEnvId === envId && document.getElementById("dp-card")) loadProgressCard(envId, { onDone });
  }, IDLE_RECHECK_MS);
}

// Cancel timers when the page is torn down.
export function destroyProgressCard() {
  clearTimer();
  activeEnvId = "";
  pollingJobId = "";
  pollOnDone = null;
  if (jobsStreamHandle) {
    jobsStreamHandle.close();
    jobsStreamHandle = null;
  }
  sseLive = false;
}
