// pages/environment_workflow.js — guided workflow spine for the environment detail page.
// Six ordered steps (connect → inventory → config → push → deploy → operate) rendered as
// full-width rows from GET /api/v1/environments/{id}/workflow. The first step whose state
// isn't "done" is the current one: expanded, with its action emphasized. Steps before it
// render compact (green ✓ + summary); steps after it render collapsed and de-emphasized.
// Every row has a "details ▸" toggle revealing that step's details/verify/day-2/info
// content. The card modules for each step live in the detail page's tab panels
// (environment_detail.js); the stepper links to them.
// The endpoint is being built in parallel, so all rendering is defensive: a
// 404/error degrades to the step skeleton plus a retry note, and every step tolerates
// missing/renamed keys in its details.
// Job tracking (verify/prepare/agent-install/day-2) rides the "jobs" SSE topic: a
// status event for a tracked job kicks a single re-fetch; the 5s setTimeout polls
// are the fallback while the stream is down (same pattern as environment_components.js).
import { api, esc, fmtAge, fmtTime, toast } from "../api.js";
import { canAdmin, canRun, gate, store, loadEnvs } from "../store.js";
import { connect } from "../stream.js";
import { loadClusterCard } from "./environment_cluster.js";
import { loadOpenstackCard } from "./environment_openstack.js";
import { ROLES } from "../roles.js";

// Static per-step copy: order, display name, the numbered "How do I do this?" guide,
// and the "What is this / why" explanation. State, summary, and details always come
// from the backend.
const STEPS = [
  {
    id: "connect",
    name: "Connect",
    howto: [
      "Jobs run on this console — it is the fleet hub.",
      "An agent is optional and advanced: only if jobs must execute inside the environment.",
    ],
    info:
      "Where jobs run. For OVH Rise + Talos, this console host is the operator. " +
      "An agent is optional (advanced): it dials out over WebSocket when you need in-environment execution.",
  },
  {
    id: "inventory",
    name: "Inventory",
    howto: [
      "Import Rise boxes from the bound OVH account.",
      "Pick the vRack, then apply VLAN 100 / 10.10.0.0/24 / .11+ and attach private NICs.",
      "Every environment needs at least 1 k8s_control_plane, 1 etcd, and 1 control node.",
    ],
    info:
      "The machines that become the cloud. Import dedicated Rise servers, plug the private NIC into the vRack, " +
      "tag VLAN 100, and assign private IPs (.11+). Kubernetes and OpenStack use the private fabric. " +
      "k8s_control_plane + etcd form the Kubernetes core; control/compute/network/storage are OpenStack roles.",
  },
  {
    id: "config",
    name: "Config",
    howto: [
      "Set the cluster domain and ACME email.",
      "The Talos image is the console default — leave it unless you have a custom factory image.",
      "YAML is expert: use it for components, network, and secrets (secrets stay masked).",
    ],
    info:
      "One versioned YAML document describing the whole environment — provider, servers, " +
      "network, which OpenStack components are enabled, helm overrides. Domain + ACME email " +
      "are the first-run fields; the rest is expert. The portal is the source of truth; every save is a new version.",
  },
  {
    id: "push",
    name: "Push",
    howto: [
      "Deploy already pushes config — you do not need a separate Push for first run.",
      "You can still push alone from the Config tab if you want to write files without deploying.",
    ],
    info:
      "Renders the config document into real files in the env's config directory " +
      "(openstack-components.yaml, inventory/inventory.yaml, helm-configs/*, kustomize " +
      "overlays) — exactly what genestack's install scripts read. Existing files are " +
      "backed up first. Deploy includes this step.",
  },
  {
    id: "deploy",
    name: "Deploy",
    howto: [
      "One action: Deploy cluster. Confirm the wipe.",
      "Pushes config, BYOI-reinstalls any node not answering Talos :50000, runs talosctl bootstrap, then the genestack pipeline.",
      "Stops at the first failing stage. There is no automatic rollback.",
    ],
    info:
      "One action for OVH Rise + Talos. Deploy pushes config, BYOI-reinstalls nodes that are not " +
      "answering on :50000, bootstraps Kubernetes with talosctl, then runs genestack's pipeline " +
      "(kube-ovn, longhorn, mariadb, rabbitmq, ovn, then OpenStack). Confirm wipe before you start.",
  },
  {
    id: "operate",
    name: "Operate",
    howto: [
      "Start with Verify (quick, then standard).",
      "Then open Machines, Kubernetes, and OpenStack.",
      "Download kubeconfig and talosconfig from Machines. Do not use Lens.",
    ],
    info:
      "Day 2: cluster reachability, helm releases, pod health, and drift. Verify still " +
      "runs genestack's test suite (scripts/tests): k8s health → infra → services → a " +
      "boot-a-VM smoke test. Machines, Kubernetes, and OpenStack are the operator " +
      "screens. Horizon remains at " +
      "horizon.<gateway_domain> as a fallback. From a VPN, point that hostname " +
      "at the hub address the installer printed. " +
      "kubeconfig and talosconfig download from Machines; do not use Lens.",
  },
];

const ACTION_LABELS = {
  inventory: "Prepare fabric",
  config: "Edit config ↓",
  push: "Push from the Config card ↓",
  deploy: "Deploy cluster",
  operate: "Machines",
};

// Card modules live in the detail page's tab panels; this stepper links to them.

// A deploy job reached a terminal state: refresh the stepper and the operate step's
// cluster/OpenStack cards so the reachability views catch up.
function onDeploySettled(envId) {
  loadWorkflowCard(envId);
  loadClusterCard(envId);
  loadOpenstackCard(envId);
}

const ROLE_ORDER = ROLES;

// ---------- verify (genestack.verify job) ----------

const VERIFY_LEVELS = [
  { id: "quick", label: "Quick (~30s)", title: "k8s + infra health checks (~30s)" },
  { id: "standard", label: "Standard (~2m)", title: "adds OpenStack service checks (~2m)" },
  {
    id: "full",
    label: "Full (~10m · test VM)",
    title: "creates and deletes a real test server/network/volume (~10m)",
  },
];
const VERIFY_ACTIVE = new Set(["queued", "running"]);
const VERIFY_POLL_MS = 5000;

let verifyTimer = null;
let verifyJobId = ""; // job currently being polled ("" = idle)
let verifyEnvId = ""; // env the polled job belongs to
let verifyLevel = "verify"; // level of the polled job (for the result line)
let loadedEnvId = ""; // env the stepper last rendered — guards against env switches
// Manual "details ▸" overrides: step id → open bool. Rows not in here follow the
// computed default (current step expanded, everything else collapsed). Cleared on
// env switch so stale toggles don't leak across environments.
const userToggled = new Map();
const verifyDoneIds = new Set(); // terminal jobs already toasted/refreshed for

function clearVerifyTimer() {
  if (verifyTimer) {
    clearTimeout(verifyTimer);
    verifyTimer = null;
  }
}

function verifyRunningHtml(level, jobId, status) {
  const id = String(jobId || "");
  return (
    `<span class="pill warn">verify ${esc(level)} ${esc(status)}…</span> ` +
    `<a href="#/activity?tab=jobs&job=${esc(id)}">job ${esc(id.slice(0, 8))}…</a>`
  );
}

// Result line for the operate step. `verify` is operate.details.verify, which may be
// null/absent on older backends — then the buttons still render and the line reads
// "no verify runs yet".
function verifyLineHtml(verify) {
  const v = verify && typeof verify === "object" ? verify : null;
  if (!v) return '<span class="muted">no verify runs yet</span>';
  const level = String(v.level || "verify");
  const status = String(v.status || "").toLowerCase();
  const id = v.job_id != null ? String(v.job_id) : "";
  const time = v.finished_at ? ` · ${esc(fmtTime(v.finished_at))}` : "";
  if (VERIFY_ACTIVE.has(status)) return verifyRunningHtml(level, id, status);
  if (status === "success" && id) {
    return `<a class="pill ok" href="#/activity?tab=jobs&job=${esc(id)}">verify ${esc(level)} passed${time}</a>`;
  }
  if ((status === "failed" || status === "error") && id) {
    return `<a class="pill warn" href="#/activity?tab=jobs&job=${esc(id)}">verify ${esc(level)} failed${time}</a>`;
  }
  const link = id ? ` <a href="#/activity?tab=jobs&job=${esc(id)}">job ${esc(id.slice(0, 8))}…</a>` : "";
  return `<span class="pill">verify ${esc(level)} ${esc(v.status || "unknown")}</span>${link}`;
}

function setVerifyLine(html) {
  const el = document.getElementById("wf-verify-line");
  if (el) el.innerHTML = html;
}

// Track the verify job: update the result line in place while queued/running; on a
// terminal state refresh the whole stepper so the operate step shows the recorded
// result. Kicked by "jobs" SSE events while the stream is live; the 5s setTimeout
// is the fallback while it is down (mirrors environment_progress.js).
async function pollVerifyJob(envId, jobId, level) {
  clearVerifyTimer();
  verifyJobId = jobId;
  verifyEnvId = envId;
  verifyLevel = level;
  if (!document.getElementById("wf-card")) return; // navigated away
  let job;
  try {
    job = await api(`/api/v1/jobs/${encodeURIComponent(jobId)}`);
  } catch {
    // Transient fetch failure: keep the line as-is and retry on the next tick
    // (while SSE is live the next job event re-kicks the poll instead).
    if (!jobsSseLive) verifyTimer = setTimeout(() => pollVerifyJob(envId, jobId, level), VERIFY_POLL_MS);
    return;
  }
  if (!document.getElementById("wf-card") || loadedEnvId !== verifyEnvId) return;
  const status = String(job && job.status ? job.status : "").toLowerCase();
  if (VERIFY_ACTIVE.has(status)) {
    setVerifyLine(verifyRunningHtml(level, jobId, status));
    if (!jobsSseLive) verifyTimer = setTimeout(() => pollVerifyJob(envId, jobId, level), VERIFY_POLL_MS);
    return;
  }
  // Terminal state: stop polling, show the outcome, refresh the step once.
  verifyJobId = "";
  if (verifyDoneIds.has(jobId)) {
    setVerifyLine(verifyLineHtml({ level, status, job_id: jobId, finished_at: job && job.finished_at }));
    return;
  }
  verifyDoneIds.add(jobId);
  toast(
    `Verify ${level} ${status === "success" ? "passed" : status || "finished"}`,
    status === "success" ? "ok" : "bad"
  );
  loadWorkflowCard(envId);
}

async function startVerify(envId, level) {
  if (!envId) return;
  const lvl = VERIFY_LEVELS.find((l) => l.id === level);
  if (!lvl) return;
  const warn =
    lvl.id === "full"
      ? "\n\nFull verify provisions and deletes a REAL server, network, and volume in this cloud."
      : "";
  if (!confirm(`Run genestack ${lvl.id} verify against this environment?${warn}`)) return;
  setVerifyLine(`<span class="muted">creating ${esc(lvl.id)} verify job…</span>`);
  try {
    const job = await api(`/api/v1/environments/${encodeURIComponent(envId)}/jobs`, {
      method: "POST",
      body: JSON.stringify({ operation: "genestack.verify", params: { level: lvl.id } }),
    });
    const id = job && job.id != null ? String(job.id) : "";
    if (!id) {
      loadWorkflowCard(envId);
      return;
    }
    toast(`Verify ${lvl.id} job ${id.slice(0, 8)}… created`, "ok");
    setVerifyLine(verifyRunningHtml(lvl.id, id, "queued"));
    pollVerifyJob(envId, id, lvl.id);
  } catch (e) {
    let msg = e.message;
    if (e.isNetwork || e.isTimeout) msg = "Verify failed to start: server unreachable. Check your connection.";
    setVerifyLine(`<span class="error">verify failed to start: ${esc(msg)}</span>`);
    toast(`Verify ${lvl.id} failed: ${e.message}`, "error");
    if (e.status === 403) toast("Insufficient role: operator required", "bad");
  }
}

function verifyActionsHtml() {
  const buttons = VERIFY_LEVELS.map(
    (l) =>
      `<button class="secondary btn-sm" type="button" data-wf-verify="${esc(l.id)}" ` +
      `title="${esc(l.title)}" ${gate(canRun(), "operator")}>${esc(l.label)}</button>`
  ).join("");
  return `<div class="wf-verify-actions"><span class="k">verify:</span>${buttons}</div>`;
}

// ---------- prepare host (genestack.host_prepare job) ----------

const PREPARE_ACTIVE = new Set(["queued", "running"]);
const PREPARE_POLL_MS = 5000;

let prepareTimer = null;
let prepareJobId = ""; // job currently being polled ("" = idle)
let prepareEnvId = ""; // env the polled job belongs to
const prepareDoneIds = new Set(); // terminal jobs already toasted/refreshed for

function clearPrepareTimer() {
  if (prepareTimer) {
    clearTimeout(prepareTimer);
    prepareTimer = null;
  }
}

function prepareRunningHtml(jobId, status) {
  const id = String(jobId || "");
  return (
    `<span class="pill warn">prepare ${esc(status)}…</span> ` +
    `<a href="#/activity?tab=jobs&job=${esc(id)}">job ${esc(id.slice(0, 8))}…</a>`
  );
}

// Result line for the connect step. `prepared` is connect.details.prepared, which may be
// null/absent on older backends — then the line reads "not prepared yet".
function prepareLineHtml(prepared, jobId) {
  const id = jobId != null ? String(jobId) : "";
  if (prepared === true) {
    return id
      ? `<a class="pill ok" href="#/activity?tab=jobs&job=${esc(id)}">host prepared ✓</a>`
      : '<span class="pill ok">host prepared ✓</span>';
  }
  if (prepared === false) {
    return id
      ? `<a class="pill warn" href="#/activity?tab=jobs&job=${esc(id)}">prepare failed</a>`
      : '<span class="pill warn">prepare failed</span>';
  }
  return '<span class="muted">not prepared yet</span>';
}

function setPrepareLine(html) {
  const el = document.getElementById("wf-prepare-line");
  if (el) el.innerHTML = html;
}

// Track the prepare job (mirrors pollVerifyJob): update the result line in
// place while queued/running; on a terminal state refresh the whole stepper so the
// connect step shows the recorded result. SSE-kicked, 5s poll as stream-down fallback.
async function pollPrepareJob(envId, jobId) {
  clearPrepareTimer();
  prepareJobId = jobId;
  prepareEnvId = envId;
  if (!document.getElementById("wf-card")) return; // navigated away
  let job;
  try {
    job = await api(`/api/v1/jobs/${encodeURIComponent(jobId)}`);
  } catch {
    // Transient fetch failure: keep the line as-is and retry on the next tick
    // (while SSE is live the next job event re-kicks the poll instead).
    if (!jobsSseLive) prepareTimer = setTimeout(() => pollPrepareJob(envId, jobId), PREPARE_POLL_MS);
    return;
  }
  if (!document.getElementById("wf-card") || loadedEnvId !== prepareEnvId) return;
  const status = String(job && job.status ? job.status : "").toLowerCase();
  if (PREPARE_ACTIVE.has(status)) {
    setPrepareLine(prepareRunningHtml(jobId, status));
    if (!jobsSseLive) prepareTimer = setTimeout(() => pollPrepareJob(envId, jobId), PREPARE_POLL_MS);
    return;
  }
  // Terminal state: stop polling, show the outcome, refresh the step once.
  prepareJobId = "";
  if (prepareDoneIds.has(jobId)) {
    setPrepareLine(prepareLineHtml(status === "success", jobId));
    return;
  }
  prepareDoneIds.add(jobId);
  toast(
    `Host prepare ${status === "success" ? "succeeded" : status || "finished"}`,
    status === "success" ? "ok" : "bad"
  );
  loadWorkflowCard(envId);
}

async function startPrepare(envId, target) {
  if (!envId) return;
  if (
    !confirm(
      `Prepare host ${target}? This clones the genestack repo (if absent) and runs ` +
        "bootstrap.sh to build the config skeleton. Runs with the env's dry_run setting."
    )
  )
    return;
  setPrepareLine('<span class="muted">creating host prepare job…</span>');
  try {
    const job = await api(`/api/v1/environments/${encodeURIComponent(envId)}/jobs`, {
      method: "POST",
      body: JSON.stringify({ operation: "genestack.host_prepare", params: {} }),
    });
    const id = job && job.id != null ? String(job.id) : "";
    if (!id) {
      loadWorkflowCard(envId);
      return;
    }
    toast(`Host prepare job ${id.slice(0, 8)}… created`, "ok");
    setPrepareLine(prepareRunningHtml(id, "queued"));
    pollPrepareJob(envId, id);
  } catch (e) {
    setPrepareLine(`<span class="error">prepare failed to start: ${esc(e.message)}</span>`);
    if (e.status === 403) toast("Insufficient role: admin required", "bad");
  }
}

function prepareActionsHtml(target) {
  return (
    `<div class="wf-prepare-actions"><span class="k">prepare:</span>` +
    `<button class="secondary btn-sm" type="button" data-wf-prepare ` +
    `data-target="${esc(target)}" title="Clone genestack + run bootstrap.sh on the deploy host" ` +
    `${gate(canAdmin(), "admin")}>Prepare host</button></div>`
  );
}

// ---------- deploy cluster (genestack.deploy job) ----------

let workflowDryRun = false;

async function startDeploy(envId) {
  if (!envId) return;
  const env = store.envs.find((x) => x.id === envId) || {};
  const name = env.name || envId;
  const dry = workflowDryRun === true || env.dry_run === true;
  const dryNote = dry
    ? "\n\nDry-run is on — this will only rehearse. Turn it off in environment settings / config.yaml before touching servers."
    : "";
  if (
    !confirm(
      `Deploy environment "${name}"?\n\n` +
        "OVH + Talos: Deploy will BYOI-reinstall any node that is not answering on :50000, then run talosctl bootstrap, then the genestack pipeline.\n\n" +
        "This wipes boxes that are not yet Talos. Deploy stops at the first failing stage; there is no automatic rollback." +
        dryNote
    )
  ) {
    return;
  }
  try {
    const job = await api(`/api/v1/environments/${encodeURIComponent(envId)}/jobs`, {
      method: "POST",
      body: JSON.stringify({ operation: "genestack.deploy", params: {} }),
    });
    const id = job && job.id != null ? String(job.id) : "";
    toast(id ? `Deploy job ${id.slice(0, 8)}… created` : "Deploy job created", "ok");
    window.dispatchEvent(new CustomEvent("deploy-job-started", { detail: { envId, jobId: id } }));
    loadWorkflowCard(envId);
  } catch (e) {
    if (e.status === 409) {
      const blockingId = e.detail && e.detail.conflicting_job_id ? String(e.detail.conflicting_job_id) : "";
      toast(
        blockingId
          ? `Deploy blocked by a running mutating job ${blockingId.slice(0, 8)}…`
          : "Deploy blocked by a running mutating job",
        "bad"
      );
    } else {
      toast(`Deploy failed: ${e.message}`, "error");
    }
    if (e.status === 403) toast("Insufficient role: admin required", "bad");
  }
}

// ---------- agent channel (dial-out agent per env) ----------

const AGENT_POLL_MS = 15000;

let agentTimer = null;
let agentEnvId = ""; // env whose status is being polled

function clearAgentTimer() {
  if (agentTimer) {
    clearTimeout(agentTimer);
    agentTimer = null;
  }
}

// Status line for the connect step. `status` is GET .../agent/status
// ({enrolled, connected, last_seen, version, hostname, credential_name/name}
// — nulls when never connected); null means the fetch failed (endpoint not on
// this backend yet, or a transient error).
function agentLineHtml(status) {
  if (!status || typeof status !== "object") {
    return '<span class="muted">agent: status unavailable</span>';
  }
  if (status.connected === true) {
    const bits = ["connected"];
    if (status.hostname) bits.push(String(status.hostname));
    if (status.version) bits.push(String(status.version));
    bits.push("execution prefers this channel");
    return (
      `<span class="pill ok">agent ${esc(bits.join(" · "))}</span> ` +
      '<span class="muted">ops run via agent when connected, then ssh, then local.</span>'
    );
  }
  if (status.last_seen) {
    const ago = fmtAge(status.last_seen);
    return `<span class="pill">agent offline${ago ? ` · last seen ${esc(ago)}` : ""}</span>`;
  }
  const enrolled = status.enrolled === true || !!(status.name || status.credential_name);
  if (enrolled) return '<span class="pill">agent offline · never connected</span>';
  return '<span class="muted">no agent</span>';
}

function setAgentLine(html) {
  const el = document.getElementById("wf-agent-line");
  if (el) el.innerHTML = html;
}

function connectStepOpen() {
  const body = document.getElementById("wf-body-connect");
  return !!body && !body.hasAttribute("hidden");
}

// Poll the agent status every 15s while the connect step is expanded. The
// timer stops when the step collapses, the env changes, or the page unloads
// (wf-body-connect disappears on navigation).
async function pollAgentStatus(envId) {
  clearAgentTimer();
  agentEnvId = envId;
  if (!connectStepOpen()) return;
  let status = null;
  try {
    status = await api(`/api/v1/environments/${encodeURIComponent(envId)}/agent/status`);
  } catch {
    status = null; // older backends without the agent endpoints land here
  }
  if (!connectStepOpen() || loadedEnvId !== agentEnvId) return;
  setAgentLine(agentLineHtml(status));
  agentTimer = setTimeout(() => pollAgentStatus(envId), AGENT_POLL_MS);
}

// Start polling when the connect step is expanded for a real env; stop otherwise.
function syncAgentPolling(envId) {
  if (envId && connectStepOpen()) {
    if (agentEnvId !== envId || !agentTimer) pollAgentStatus(envId);
  } else {
    clearAgentTimer();
    agentEnvId = "";
  }
}

// Listen for env-state-changed from agents card — refresh immediately
window.addEventListener("env-state-changed", (e) => {
  if (e.detail.envId === loadedEnvId) {
    pollAgentStatus(loadedEnvId);
  }
});

function agentActionsHtml() {
  return (
    `<div class="wf-agent-actions"><span class="k">agent:</span>` +
    `<button class="secondary btn-sm" type="button" data-wf-gotab="inventory" ` +
    `title="Go to Inventory tab to install or manage agents">Install agent →</button></div>`
  );
}

function closeAgentTokenModal() {
  const el = document.getElementById("wf-agent-modal");
  if (el) el.remove(); // dropping the node also drops the raw token from the DOM
}

function copyAgentText(text, btn) {
  const label = btn.textContent;
  const done = () => {
    btn.textContent = "Copied ✓";
    setTimeout(() => {
      btn.textContent = label;
    }, 1500);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, () => {
      toast("Copy failed — select the text manually", "bad");
    });
    return;
  }
  toast("Clipboard unavailable — select the text manually", "info");
}

// One-time overlay with the raw token + run instructions (mirrors the
// gs-welcome overlay in app.js). Accepts both the planned shape
// ({token, instructions, agent:{name}}) and the one landing in schemas.py
// ({token, docker_run, name}). Dismiss — the Done button or a backdrop
// click — removes the overlay, clearing the token from the DOM; the hub only
// stores the token's hash, so it cannot be shown again.
function showAgentTokenModal(payload) {
  closeAgentTokenModal();
  const p = payload && typeof payload === "object" ? payload : {};
  const token = typeof p.token === "string" ? p.token : "";
  const instructions =
    (typeof p.instructions === "string" && p.instructions) ||
    (typeof p.docker_run === "string" && p.docker_run) ||
    "";
  const agent = p.agent && typeof p.agent === "object" ? p.agent : {};
  const agentName = agent.name || p.name || "";
  const overlay = document.createElement("div");
  overlay.id = "wf-agent-modal";
  overlay.className = "wf-agent-modal";
  overlay.innerHTML = `
    <div class="wf-agent-modal-card" role="dialog" aria-modal="true" aria-label="Agent enrollment token">
      <h2>Agent token${agentName ? ` — ${esc(agentName)}` : ""}</h2>
      <p class="wf-agent-warn">Shown once — store it now. The hub keeps only a hash of this token; it cannot be recovered later.</p>
      <div class="wf-agent-field">
        <span class="k">token</span>
        <pre class="wf-agent-mono">${esc(token)}</pre>
        <button class="secondary btn-sm" type="button" data-wf-agent-copy-token>Copy token</button>
      </div>
      ${
        instructions
          ? `<div class="wf-agent-field">
              <span class="k">run the agent inside the environment</span>
              <pre class="wf-agent-mono">${esc(instructions)}</pre>
              <button class="secondary btn-sm" type="button" data-wf-agent-copy-instructions>Copy instructions</button>
            </div>`
          : ""
      }
      <div class="wf-agent-modal-actions">
        <button type="button" data-wf-agent-close>Done — I stored it</button>
      </div>
    </div>`;
  overlay.addEventListener("click", (e) => {
    if (e.target.closest("[data-wf-agent-copy-token]")) {
      copyAgentText(token, e.target.closest("[data-wf-agent-copy-token]"));
      return;
    }
    if (e.target.closest("[data-wf-agent-copy-instructions]")) {
      copyAgentText(instructions, e.target.closest("[data-wf-agent-copy-instructions]"));
      return;
    }
    if (e.target === overlay || e.target.closest("[data-wf-agent-close]")) closeAgentTokenModal();
  });
  document.body.appendChild(overlay);
}

async function createAgentToken(envId) {
  if (!envId) return;
  if (
    !confirm(
      "Create a new agent enrollment token for this environment? The raw token is shown once, right after creation."
    )
  )
    return;
  setAgentLine('<span class="muted">creating agent token…</span>');
  try {
    const payload = await api(`/api/v1/environments/${encodeURIComponent(envId)}/agent/token`, {
      method: "POST",
      body: JSON.stringify({}), // schema defaults the credential name to "default"
    });
    showAgentTokenModal(payload);
    toast("Agent token created — shown once, store it now", "ok");
    pollAgentStatus(envId);
  } catch (e) {
    setAgentLine(`<span class="error">token creation failed: ${esc(e.message)}</span>`);
    toast(`Token creation failed: ${e.message}`, "error");
    if (e.status === 403) toast("Insufficient role: admin required", "bad");
  }
}

// ---------- day-2 ops (backup mariadb / k8s upgrade jobs) ----------

const DAY2_OPS = [
  {
    id: "backup_mariadb",
    label: "Backup MariaDB",
    operation: "genestack.backup_mariadb",
    role: "operator",
    title: "Runs genestack's scripts/backup-mariadb.sh on the deploy host",
    confirm: "Run genestack's scripts/backup-mariadb.sh on the deploy host?",
  },
  {
    id: "k8s_upgrade",
    label: "Upgrade Kubernetes",
    operation: "genestack.k8s_upgrade",
    role: "admin",
    title: "Runs kubespray upgrade-cluster.yml on the deploy host",
    confirm:
      "Runs kubespray upgrade-cluster.yml on the deploy host — this upgrades the live cluster. Continue?",
  },
];
const DAY2_ACTIVE = new Set(["queued", "running"]);
const DAY2_POLL_MS = 5000;

let day2Timer = null;
let day2JobId = ""; // job currently being polled ("" = idle)
let day2EnvId = ""; // env the polled job belongs to
let day2Label = ""; // display label of the polled op (for the result line)
const day2DoneIds = new Set(); // terminal jobs already toasted for

function clearDay2Timer() {
  if (day2Timer) {
    clearTimeout(day2Timer);
    day2Timer = null;
  }
}

function day2RunningHtml(label, jobId, status) {
  const id = String(jobId || "");
  return (
    `<span class="pill warn">${esc(label)} ${esc(status)}…</span> ` +
    `<a href="#/activity?tab=jobs&job=${esc(id)}">job ${esc(id.slice(0, 8))}…</a>`
  );
}

function day2ResultHtml(label, status, jobId, finishedAt) {
  const id = String(jobId || "");
  const time = finishedAt ? ` · ${esc(fmtTime(finishedAt))}` : "";
  const outcome = status === "success" ? "succeeded" : status || "finished";
  const cls = status === "success" ? "ok" : "warn";
  return `<a class="pill ${cls}" href="#/activity?tab=jobs&job=${esc(id)}">${esc(label)} ${esc(outcome)}${time}</a>`;
}

function setDay2Line(html) {
  const el = document.getElementById("wf-day2-line");
  if (el) el.innerHTML = html;
}

// Track a day-2 job (mirrors pollVerifyJob): update the result line in place
// while queued/running; on a terminal state show the outcome pill. The workflow
// endpoint doesn't report day-2 runs, so the line itself is the record — no reload.
// SSE-kicked, 5s poll as stream-down fallback.
async function pollDay2Job(envId, jobId, label) {
  clearDay2Timer();
  day2JobId = jobId;
  day2EnvId = envId;
  day2Label = label;
  if (!document.getElementById("wf-card")) return; // navigated away
  let job;
  try {
    job = await api(`/api/v1/jobs/${encodeURIComponent(jobId)}`);
  } catch {
    // Transient fetch failure: keep the line as-is and retry on the next tick
    // (while SSE is live the next job event re-kicks the poll instead).
    if (!jobsSseLive) day2Timer = setTimeout(() => pollDay2Job(envId, jobId, label), DAY2_POLL_MS);
    return;
  }
  if (!document.getElementById("wf-card") || loadedEnvId !== day2EnvId) return;
  const status = String(job && job.status ? job.status : "").toLowerCase();
  if (DAY2_ACTIVE.has(status)) {
    setDay2Line(day2RunningHtml(label, jobId, status));
    if (!jobsSseLive) day2Timer = setTimeout(() => pollDay2Job(envId, jobId, label), DAY2_POLL_MS);
    return;
  }
  // Terminal state: stop polling, show the outcome, toast once.
  day2JobId = "";
  setDay2Line(day2ResultHtml(label, status, jobId, job && job.finished_at));
  if (day2DoneIds.has(jobId)) return;
  day2DoneIds.add(jobId);
  toast(
    `${label} ${status === "success" ? "succeeded" : status || "finished"}`,
    status === "success" ? "ok" : "bad"
  );
}

async function startDay2(envId, opId) {
  if (!envId) return;
  const op = DAY2_OPS.find((o) => o.id === opId);
  if (!op) return;
  if (!confirm(op.confirm)) return;
  setDay2Line(`<span class="muted">creating ${esc(op.label)} job…</span>`);
  try {
    const job = await api(`/api/v1/environments/${encodeURIComponent(envId)}/jobs`, {
      method: "POST",
      body: JSON.stringify({ operation: op.operation, params: {} }),
    });
    const id = job && job.id != null ? String(job.id) : "";
    if (!id) {
      loadWorkflowCard(envId);
      return;
    }
    toast(`${op.label} job ${id.slice(0, 8)}… created`, "ok");
    setDay2Line(day2RunningHtml(op.label, id, "queued"));
    pollDay2Job(envId, id, op.label);
  } catch (e) {
    let msg = e.message;
    if (e.isNetwork || e.isTimeout) msg = "Server unreachable. Check your connection and try again.";
    setDay2Line(`<span class="error">${esc(op.label)} failed: ${esc(msg)}</span>`);
    toast(`${op.label} failed: ${e.message}`, "error");
    if (e.status === 403) toast(`Insufficient role: ${op.role} required`, "bad");
  }
}

// `deployed` gates the whole group: when the env doesn't look deployed yet the
// buttons still render but greyed out with an explanatory tooltip.
function day2ActionsHtml(deployed) {
  const buttons = DAY2_OPS.map((op) => {
    const roleGate = op.role === "admin" ? gate(canAdmin(), "admin") : gate(canRun(), "operator");
    const gated = deployed
      ? roleGate
      : 'disabled title="Environment does not look deployed yet"';
    const titleAttr = gated ? "" : ` title="${esc(op.title)}"`;
    return (
      `<button class="secondary btn-sm" type="button" data-wf-day2="${esc(op.id)}"` +
      `${titleAttr} ${gated}>${esc(op.label)}</button>`
    );
  }).join("");
  return `<div class="wf-verify-actions wf-day2-actions"><span class="k">day 2:</span>${buttons}</div>`;
}

// ---------- jobs stream (SSE "jobs" topic) ----------

let jobsStreamHandle = null;
let jobsStreamEnvId = ""; // env the stream was opened for ("" = closed)
let jobsSseLive = false;

function anyJobTracked() {
  return !!(verifyJobId || prepareJobId || day2JobId);
}

// jobs topic event: {type:"job", id, environment_id, operation, status, dry_run}.
// A tracked job changed state — kick its tracker once so it re-fetches the job
// (for log/finished_at) and applies; the poll timers stay suspended while the
// stream is live. Untracked ids (other pages' jobs) are ignored.
function onJobsEvent(payload) {
  if (!payload || payload.id == null) return;
  const id = String(payload.id);
  if (id === verifyJobId && verifyEnvId) pollVerifyJob(verifyEnvId, id, verifyLevel);
  else if (id === prepareJobId && prepareEnvId) pollPrepareJob(prepareEnvId, id);
  else if (id === day2JobId && day2EnvId) pollDay2Job(day2EnvId, id, day2Label);
}

// Stream dropped: resume the 5s fallback polls for every job still tracked.
function resumeJobPolling() {
  if (verifyJobId && verifyEnvId) pollVerifyJob(verifyEnvId, verifyJobId, verifyLevel);
  if (prepareJobId && prepareEnvId) pollPrepareJob(prepareEnvId, prepareJobId);
  if (day2JobId && day2EnvId) pollDay2Job(day2EnvId, day2JobId, day2Label);
}

function resubscribeJobs(envId) {
  if (jobsStreamHandle && jobsStreamEnvId === envId) return;
  if (jobsStreamHandle) {
    jobsStreamHandle.close();
    jobsStreamHandle = null;
  }
  jobsStreamEnvId = envId;
  jobsSseLive = false;
  if (!envId) return;
  jobsStreamHandle = connect(["jobs"], {
    jobs: onJobsEvent,
    onState: (state) => {
      jobsSseLive = state === "open";
      if (jobsSseLive) {
        // The stream is authoritative now — suspend the fallback timers. An
        // in-flight poll won't reschedule itself while jobsSseLive is true.
        clearVerifyTimer();
        clearPrepareTimer();
        clearDay2Timer();
      } else if (anyJobTracked()) {
        resumeJobPolling();
      }
    },
  });
}

// Page teardown (router): close the stream and stop every tracker. Card modules
// mounted into steps own their own destroy; this covers the workflow card itself.
export function destroyWorkflowCard() {
  if (jobsStreamHandle) {
    jobsStreamHandle.close();
    jobsStreamHandle = null;
  }
  jobsStreamEnvId = "";
  jobsSseLive = false;
  clearVerifyTimer();
  verifyJobId = "";
  verifyEnvId = "";
  clearPrepareTimer();
  prepareJobId = "";
  prepareEnvId = "";
  clearDay2Timer();
  day2JobId = "";
  day2EnvId = "";
  clearAgentTimer();
  agentEnvId = "";
  loadedEnvId = "";
}

function statePill(state) {
  const s = String(state || "").toLowerCase();
  if (s === "done") return '<span class="pill ok">done</span>';
  if (s === "attention") return '<span class="pill warn">attention</span>';
  if (s === "pending") return "<span class=\"pill\">pending</span>";
  return '<span class="pill">unknown</span>';
}

function jobLine(details, label) {
  const d = details && typeof details === "object" ? details : {};
  const bits = [];
  if (d.job_id != null && d.job_id !== "") {
    const id = String(d.job_id);
    bits.push(`${label} <a href="#/activity?tab=jobs&job=${esc(id)}">job ${esc(id.slice(0, 8))}…</a>`);
  }
  if (d.status) bits.push(`status <strong>${esc(d.status)}</strong>`);
  if (d.finished_at) bits.push(`finished ${esc(fmtTime(d.finished_at))}`);
  if (d.dry_run === true) bits.push('<span class="pill warn">dry-run</span>');
  return bits.length ? `<div>${bits.join(" · ")}</div>` : '<div class="muted">no job yet</div>';
}

function kv(k, v, { code = false } = {}) {
  if (v == null || v === "") return "";
  return `<div><span class="k">${esc(k)}</span> ${code ? `<code>${esc(v)}</code>` : esc(v)}</div>`;
}

// Deploy-host label for the connect step: "user@host", or null when jobs run here.
function connectTarget(d) {
  const dd = d && typeof d === "object" ? d : {};
  return dd.deployer_ssh_host
    ? `${dd.deployer_ssh_user ? dd.deployer_ssh_user + "@" : ""}${dd.deployer_ssh_host}`
    : null;
}

function connectAdvancedHtml(details, envId) {
  const dd = details && typeof details === "object" ? details : {};
  const ssh = connectTarget(dd);
  const env = store.envs.find((x) => x.id === envId) || {};
  const deployerForm = ssh
    ? ""
    : `<div class="wf-deployer-form" style="margin-top:.4rem">
        <div style="font-size:.78rem;color:var(--fg-muted,#666);margin-bottom:.25rem">Set deploy host (optional — only if jobs must SSH off this console)</div>
        <div style="display:flex;gap:.35rem;align-items:center;flex-wrap:wrap">
          <input id="wf-deployer-host" type="text" placeholder="user@hostname" style="font-size:.78rem;padding:.25rem .5rem;border:1px solid var(--border,#333);border-radius:.25rem;background:rgba(0,0,0,.2);color:var(--fg,#ccc);flex:1;min-width:10rem" />
          <button class="secondary btn-sm" type="button" id="wf-deployer-save">Save</button>
        </div>
        <div id="wf-deployer-msg" style="margin-top:.25rem;font-size:.75rem"></div>
      </div>`;
  return (
    deployerForm +
    `<div class="wf-state-form" style="margin-top:.4rem">
      <div style="font-size:.78rem;color:var(--fg-muted,#666);margin-bottom:.25rem">State export (git-backed)</div>
      <div class="form-grid" style="margin-top:0">
        <label class="field"><span>State repo path</span>
          <input id="wf-state-path" type="text" value="${esc(env.state_repo_path || "")}" placeholder="/path/to/genestack (on console host)" autocomplete="off" style="font-size:.78rem;padding:.25rem .5rem;border:1px solid var(--border,#333);border-radius:.25rem;background:rgba(0,0,0,.2);color:var(--fg,#ccc)" />
        </label>
        <label class="field"><span>State repo remote</span>
          <input id="wf-state-remote" type="text" value="${esc(env.state_repo_remote || "")}" placeholder="origin (optional — commit only when empty)" autocomplete="off" style="font-size:.78rem;padding:.25rem .5rem;border:1px solid var(--border,#333);border-radius:.25rem;background:rgba(0,0,0,.2);color:var(--fg,#ccc)" />
        </label>
      </div>
      <div class="row" style="margin-top:.35rem;gap:.4rem">
        <button class="secondary btn-sm" type="button" id="wf-state-save" ${gate(canRun(), "operator")}>Save</button>
        <span id="wf-state-msg" style="font-size:.75rem"></span>
      </div>
    </div>` +
    `<div id="wf-prepare-line">${prepareLineHtml(dd.prepared, dd.prepare_job_id)}</div>` +
    `<div id="wf-agent-line"><span class="muted">agent: checking…</span></div>` +
    prepareActionsHtml(ssh || "local") +
    agentActionsHtml()
  );
}

// Per-step detail renderers. Each receives the step's `details` object and the env id.
const DETAIL_RENDERERS = {
  connect(d) {
    const dd = d && typeof d === "object" ? d : {};
    const ssh = connectTarget(dd);
    return (
      (dd.local_hub === true || !ssh
        ? '<div><span class="pill ok">this console is the fleet hub</span></div>'
        : "") +
      kv("config dir", dd.genestack_config_dir, { code: true }) +
      (ssh ? kv("deploy host", ssh, { code: true }) : "") +
      kv("kubeconfig", dd.kubeconfig_source) +
      (dd.dry_run === true
        ? '<div><span class="pill warn">dry_run on — actions only rehearse</span></div>'
        : "")
    );
  },
  inventory(d) {
    const dd = d && typeof d === "object" ? d : {};
    const roles = dd.roles && typeof dd.roles === "object" ? dd.roles : {};
    const chips = ROLE_ORDER.filter((r) => roles[r])
      .map((r) => `<span class="chip">${esc(r)} ${esc(roles[r])}</span>`)
      .join("");
    const missing = Array.isArray(dd.hosts_missing_private_ip) ? dd.hosts_missing_private_ip : [];
    const vlan =
      dd.vlan_id != null && dd.vlan_id !== ""
        ? Number(dd.vlan_id) === 0
          ? "0 (untagged)"
          : String(dd.vlan_id)
        : null;
    return (
      kv("hosts", dd.host_count != null ? String(dd.host_count) : null) +
      (chips ? `<div class="row" style="gap:.25rem">${chips}</div>` : '<div class="muted">no role counts</div>') +
      kv("vRack", dd.vrack, { code: true }) +
      kv("VLAN", vlan) +
      (dd.private_ips_assigned != null ? kv("private IPs", String(dd.private_ips_assigned)) : "") +
      (missing.length ? kv("missing private IP", missing.join(", ")) : "")
    );
  },
  config(d) {
    const dd = d && typeof d === "object" ? d : {};
    return (
      (dd.version != null
        ? `<div><span class="pill ok">v${esc(dd.version)}</span></div>`
        : '<div><span class="pill">not configured</span></div>') +
      kv("updated", dd.updated_at ? fmtTime(dd.updated_at) : null) +
      kv("by", dd.updated_by)
    );
  },
  push(d) {
    return jobLine(d, "last push:");
  },
  deploy(d) {
    return jobLine(d, "last deploy:");
  },
  operate(d) {
    const dd = d && typeof d === "object" ? d : {};
    const reach =
      dd.reachable === true
        ? '<span class="pill ok">reachable</span>'
        : dd.reachable === false
          ? '<span class="pill bad">unreachable</span>'
          : '<span class="pill">unknown</span>';
    const skyline =
      dd.skyline_url && typeof dd.skyline_url === "string"
        ? `<a class="chip wf-link-chip" href="${esc(dd.skyline_url)}" target="_blank" rel="noopener noreferrer">skyline ↗</a>`
        : "";
    const horizon =
      dd.horizon_url && typeof dd.horizon_url === "string"
        ? `<a class="chip wf-link-chip" href="${esc(dd.horizon_url)}" target="_blank" rel="noopener noreferrer">horizon ↗</a>`
        : "";
    const dash = skyline || horizon ? `<div>${horizon}${skyline ? " " + skyline : ""}</div>` : "";
    return (
      `<div>${reach}</div>` +
      kv("nodes", dd.node_count != null ? String(dd.node_count) : null) +
      kv("helm releases", dd.release_count != null ? String(dd.release_count) : null) +
      `<div id="wf-verify-line">${verifyLineHtml(dd.verify)}</div>` +
      `<div id="wf-day2-line"></div>` +
      dash +
      (dd.error ? `<div class="error" style="margin-top:.25rem">${esc(dd.error)}</div>` : "")
    );
  },
};

// Numbered "How do I do this?" guide rendered at the top of each step's expandable
// body, above the "What is this / why" explainer.
function howtoHtml(step) {
  if (!Array.isArray(step.howto) || !step.howto.length) return "";
  const items = step.howto.map((t) => `<li>${esc(t)}</li>`).join("");
  return `<div class="wf-howto"><span class="k">How do I do this?</span><ol>${items}</ol></div>`;
}

// The step's ONE primary action. Deploy fires the job; other steps navigate to
// the tab that owns their cards. On the current step the control is primary.
function stepActionHtml(stepId, envId, current) {
  if (stepId === "deploy") {
    const cls = current ? "btn-sm wf-action primary" : "secondary btn-sm";
    return (
      `<button class="${cls}" type="button" data-wf-deploy ${gate(canAdmin(), "admin")}>Deploy cluster</button>` +
      ` <button class="secondary btn-sm" type="button" data-wf-gotab="config" title="Start stage / dry-run on the Config tab">Open Config →</button>`
    );
  }
  const tabMap = { connect: "inventory", inventory: "platform", config: "config", push: "config", operate: "platform" };
  const ptabMap = { inventory: "ovh", operate: "machines" };
  const tabName = tabMap[stepId];
  if (!tabName) return "";
  const actionLabels = {
    connect: "Set up connection",
    inventory: "Prepare fabric",
    config: "Edit Config",
    push: "Push Files",
    operate: "Machines",
  };
  const label = actionLabels[stepId] || tabName.charAt(0).toUpperCase() + tabName.slice(1);
  const cls = current ? "btn-sm wf-action primary" : "secondary btn-sm";
  const ptab = ptabMap[stepId];
  const ptabAttr = ptab ? ` data-wf-ptab="${esc(ptab)}"` : "";
  return `<button class="${cls}" type="button" data-wf-gotab="${esc(tabName)}"${ptabAttr}>${esc(label)} →</button>`;
}

// One spine row: progress-bar step, detail row, and collapsible body.
// rowPos is "done" | "current" | "future".  Returns {bar, row, body}.
function stepHtml(step, idx, data, envId, ctx, rowPos) {
  const s = data && typeof data === "object" ? data : {};
  const state = s.state || "pending";
  const summary = s.summary || "—";
  const details = DETAIL_RENDERERS[step.id] ? DETAIL_RENDERERS[step.id](s.details) : "";
  const deployed = !!(ctx && ctx.deployed);
  const isCurrent = rowPos === "current";
  const open = userToggled.has(step.id) ? userToggled.get(step.id) : isCurrent;  // current step auto-expanded
  const sid = esc(step.id);
  const bodyOpen = open ? " open" : "";

  const dotClass = rowPos === "done" ? "done" : rowPos === "current" ? "current" : state === "attention" ? "attention" : "";

  const bar = `
    <div class="wf-progress-step ${dotClass}" data-step="${sid}">
      <div class="wf-progress-dot">${rowPos === "done" ? "✓" : idx + 1}</div>
      <div class="wf-progress-label">${esc(step.name)}</div>
      <div class="wf-progress-state">${statePill(state)}</div>
    </div>`;

  const row = `
    <div class="wf-step-detail ${dotClass}" data-step="${sid}">
      <div class="wf-num">${rowPos === "done" ? "✓" : idx + 1}</div>
      <div class="wf-step-info">
        <div class="wf-step-name">${esc(step.name)} ${statePill(state)}</div>
        <div class="wf-step-summary">${esc(summary)}</div>
      </div>
      <div class="wf-step-actions">
        ${stepActionHtml(step.id, envId, isCurrent)}
        <button class="wf-step-expand" type="button" data-wf-toggle="${sid}" aria-expanded="${open}">${open ? "▾" : "▸"}</button>
      </div>
    </div>`;

  const dryWarn =
    step.id === "deploy" && ctx && ctx.dryRun
      ? '<div class="error" style="margin:.25rem 0">Dry-run is on — Deploy will only rehearse. Turn it off in environment settings / config.yaml before touching servers.</div>'
      : "";
  const connectAdvanced =
    step.id === "connect"
      ? `<details class="wf-info"><summary>Advanced — remote agent, deploy host, git state</summary>
          ${connectAdvancedHtml(s.details, envId)}
        </details>`
      : "";
  const body = `
    <div class="wf-step-body${bodyOpen}" id="wf-body-${sid}">
      ${howtoHtml(step)}
      ${dryWarn}
      <div class="wf-details">${details || '<div class="muted">—</div>'}</div>
      ${step.id === "operate" ? verifyActionsHtml() : ""}
      ${step.id === "operate" ? day2ActionsHtml(deployed) : ""}
      ${connectAdvanced}
      <details class="wf-info">
        <summary>What is this / why</summary>
        <p>${esc(step.info)}</p>
      </details>
    </div>`;

  return { bar, row, body };
}

// Skeleton rendered when no env is selected or the workflow endpoint is unavailable:
// horizontal progress bar + compact detail rows with the note; how-to and
// "What is this / why" stay reachable via the details toggle.
function skeletonHtml(note, withRetry) {
  const steps = STEPS.map((step, idx) => {
    const sid = `sk-${esc(step.id)}`;
    return (
      `<div class="wf-progress-step" data-step="${sid}">` +
      `<div class="wf-progress-dot">${idx + 1}</div>` +
      `<div class="wf-progress-label">${esc(step.name)}</div>` +
      `<div class="wf-progress-state">unknown</div>` +
      `</div>`
    );
  }).join("");

  const detailRows = STEPS.map((step, idx) => {
    const sid = `sk-${esc(step.id)}`;
    return (
      `<div class="wf-step-detail" data-step="${sid}">` +
      `<div class="wf-num">${idx + 1}</div>` +
      `<div class="wf-step-info">` +
      `<div class="wf-step-name">${esc(step.name)}</div>` +
      `<div class="wf-step-summary">${esc(note)}</div>` +
      `</div>` +
      `<button class="wf-step-expand" type="button" data-wf-toggle="${sid}">▸</button>` +
      `</div>` +
      `<div class="wf-step-body" id="wf-body-${sid}">` +
      howtoHtml(step) +
      `<details class="wf-info"><summary>What is this / why</summary><p>${esc(step.info)}</p></details>` +
      `</div>`
    );
  }).join("");

  return `
    <div class="wf-progress-bar">${steps}</div>
    <div>${detailRows}</div>
    ${withRetry ? `<div style="margin-top:.5rem"><span class="hint muted">Live step states unavailable — showing the workflow skeleton.</span> <button class="secondary btn-sm" id="wf-retry" type="button">Retry</button></div>` : ""}`;
}

export function workflowCardHtml() {
  return `
  <style>
    /* Progress bar: horizontal step indicators */
    .wf-progress-bar {
      display: flex;
      align-items: stretch;
      gap: 0;
      padding: .5rem 0;
      margin-bottom: .5rem;
    }
    .wf-progress-step {
      flex: 1;
      display: flex;
      flex-direction: column;
      align-items: center;
      text-align: center;
      position: relative;
      padding: .25rem .15rem;
    }
    .wf-progress-step::after {
      content: '';
      position: absolute;
      top: 1rem;
      right: -1rem;
      width: calc(1rem + 2px);
      height: 2px;
      background: var(--border, #333);
    }
    .wf-progress-step:last-child::after { display: none; }
    .wf-progress-step.done::after { background: #4caf50; }

    .wf-progress-dot {
      width: 1.6rem;
      height: 1.6rem;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: .7rem;
      font-weight: 700;
      border: 2px solid var(--border, #333);
      background: var(--bg, #111);
      color: var(--fg-muted, #555);
      flex-shrink: 0;
      margin-bottom: .3rem;
    }
    .wf-progress-step.done .wf-progress-dot {
      background: #4caf50;
      border-color: #4caf50;
      color: #fff;
    }
    .wf-progress-step.current .wf-progress-dot {
      border-color: #ff9800;
      color: #ff9800;
      box-shadow: 0 0 0 3px rgba(255,152,0,.15);
    }
    .wf-progress-step.attention .wf-progress-dot {
      border-color: #f44336;
      color: #f44336;
    }

    .wf-progress-label {
      font-size: .7rem;
      color: var(--fg-muted, #666);
      font-weight: 500;
      line-height: 1.2;
    }
    .wf-progress-step.done .wf-progress-label { color: #4caf50; }
    .wf-progress-step.current .wf-progress-label { color: #ff9800; font-weight: 600; }
    .wf-progress-step.attention .wf-progress-label { color: #f44336; }

    .wf-progress-state {
      font-size: .6rem;
      color: var(--fg-muted, #555);
      margin-top: .1rem;
    }

    /* Compact step detail rows (below progress bar) */
    .wf-step-detail {
      display: flex;
      align-items: center;
      gap: .5rem;
      padding: .4rem .5rem;
      border-radius: .3rem;
      border: 1px solid transparent;
      margin-bottom: .25rem;
    }
    .wf-step-detail.current {
      background: rgba(255,152,0,.05);
      border-color: rgba(255,152,0,.15);
    }
    .wf-step-detail .wf-num {
      width: 1.4rem;
      height: 1.4rem;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: .65rem;
      font-weight: 700;
      flex-shrink: 0;
    }
    .wf-step-detail.done .wf-num {
      background: #4caf50;
      color: #fff;
    }
    .wf-step-detail:not(.done) .wf-num {
      background: var(--bg-card, #1e1e1e);
      color: var(--fg-muted, #666);
    }
    .wf-step-detail.current .wf-num {
      background: #ff9800;
      color: #fff;
    }

    .wf-step-info {
      flex: 1;
      min-width: 0;
    }
    .wf-step-name {
      font-size: .8rem;
      font-weight: 600;
      color: var(--fg, #ccc);
    }
    .wf-step-summary {
      font-size: .7rem;
      color: var(--fg-muted, #666);
      margin-top: .05rem;
    }

    .wf-step-actions {
      display: flex;
      align-items: center;
      gap: .3rem;
      flex-shrink: 0;
    }

    /* Collapsible details per step */
    .wf-step-expand {
      font-size: .72rem;
      color: var(--fg-muted, #555);
      cursor: pointer;
      padding: .2rem;
      background: none;
      border: none;
      font-family: inherit;
    }
    .wf-step-expand:hover { color: var(--fg, #999); }

    .wf-step-body {
      display: none;
      padding: .5rem .75rem;
      margin-bottom: .3rem;
      background: rgba(255,255,255,.02);
      border-radius: .3rem;
      font-size: .78rem;
      color: var(--fg-muted, #888);
      line-height: 1.5;
    }
    .wf-step-body.open { display: block; }

    .wf-howto { margin-bottom: .4rem; }
    .wf-howto ol { margin: .2rem 0; padding-left: 1.2rem; }
    .wf-howto li { margin-bottom: .15rem; }
    .wf-details { margin-bottom: .3rem; }
    .wf-info { font-size: .72rem; color: var(--fg-muted, #555); margin-top: .3rem; }
    .wf-info p { margin: .2rem 0 0; }

    .wf-verify-actions, .wf-prepare-actions, .wf-day2-actions {
      display: flex;
      align-items: center;
      gap: .3rem;
      margin-top: .3rem;
      flex-wrap: wrap;
    }
    .wf-verify-actions .k, .wf-prepare-actions .k, .wf-day2-actions .k {
      font-size: .72rem;
      color: var(--fg-muted, #555);
      margin-right: .2rem;
     }

    .wf-agent-actions { margin-top: .3rem; }

    .wf-link-chip {
      display: inline-block;
      padding: .1rem .35rem;
      border-radius: .2rem;
      font-size: .68rem;
      background: var(--bg-card, #1e1e1e);
      border: 1px solid var(--border, #333);
      color: var(--fg-muted, #888);
      text-decoration: none;
    }
    .wf-link-chip:hover { border-color: var(--fg-muted, #666); color: var(--fg, #ccc); }

    /* Agent token modal */
    .wf-agent-modal {
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,.7);
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 1000;
    }
    .wf-agent-modal-card {
      background: var(--bg-card, #161616);
      border: 1px solid var(--border, #333);
      border-radius: .5rem;
      padding: 1.5rem;
      max-width: 48rem;
      width: 90%;
      max-height: 80vh;
      overflow-y: auto;
    }
    .wf-agent-warn {
      color: #ff9800;
      font-size: .78rem;
      margin: .3rem 0 .5rem;
    }
    .wf-agent-field {
      margin-bottom: .5rem;
    }
    .wf-agent-mono {
      font-family: monospace;
      font-size: .72rem;
      word-break: break-all;
      background: rgba(255,255,255,.04);
      padding: .4rem .5rem;
      border-radius: .2rem;
      border: 1px solid var(--border, #333);
      margin: .2rem 0;
      max-height: 6rem;
      overflow-y: auto;
    }
    .wf-agent-modal-actions {
      display: flex;
      justify-content: flex-end;
      margin-top: .75rem;
    }
  </style>
  <div class="card span-12" id="wf-card">
    <div class="toolbar">
      <h2>Workflow</h2>
      <span id="wf-msg" class="muted"></span>
    </div>
    <div id="wf-steps">${skeletonHtml("Select an environment.", false)}</div>
  </div>`;
}

export function wireWorkflowCard(getEnvId) {
  const card = document.getElementById("wf-card");
  if (!card) return;
  card.addEventListener("click", (e) => {
    const toggleBtn = e.target.closest("[data-wf-toggle]");
    if (toggleBtn) {
      const body = document.getElementById("wf-body-" + toggleBtn.dataset.wfToggle);
      if (body) {
        const open = body.classList.toggle("open");
        toggleBtn.textContent = open ? "▾" : "▸";
        toggleBtn.setAttribute("aria-expanded", String(open));
        userToggled.set(toggleBtn.dataset.wfToggle, open);
        syncAgentPolling(getEnvId());
      }
      return;
    }
    const verifyBtn = e.target.closest("[data-wf-verify]");
    if (verifyBtn) {
      if (!verifyBtn.disabled) startVerify(getEnvId(), verifyBtn.dataset.wfVerify);
      return;
    }
    const prepareBtn = e.target.closest("[data-wf-prepare]");
    if (prepareBtn) {
      if (!prepareBtn.disabled) startPrepare(getEnvId(), prepareBtn.dataset.target || "local");
      return;
    }
    const deployBtn = e.target.closest("[data-wf-deploy]");
    if (deployBtn) {
      if (!deployBtn.disabled) startDeploy(getEnvId());
      return;
    }
    const agentBtn = e.target.closest("[data-wf-agent-token]");
    if (agentBtn) {
      if (!agentBtn.disabled) createAgentToken(getEnvId());
      return;
    }
    const day2Btn = e.target.closest("[data-wf-day2]");
    if (day2Btn) {
      if (!day2Btn.disabled) startDay2(getEnvId(), day2Btn.dataset.wfDay2);
      return;
    }
    const goToTab = e.target.closest("[data-wf-gotab]");
    if (goToTab) {
      const tabName = goToTab.dataset.wfGotab;
      const tabBtn = document.querySelector(`.tab[data-tab="${tabName}"]`);
      if (tabBtn) tabBtn.click();
      const ptab = goToTab.dataset.wfPtab;
      if (ptab) {
        const ptabBtn = document.querySelector(`.ptab[data-ptab="${ptab}"]`);
        if (ptabBtn) ptabBtn.click();
      }
      return;
    }
    if (e.target.closest("#wf-retry")) loadWorkflowCard(getEnvId());
    // Deployer SSH host form
    const deployerSave = e.target.closest("#wf-deployer-save");
    if (deployerSave) {
      saveDeployerHost(getEnvId(), deployerSave);
      return;
    }
    // State export repo form
    const stateSave = e.target.closest("#wf-state-save");
    if (stateSave) {
      saveStateRepo(getEnvId(), stateSave);
      return;
    }
  });
  // Auto-expand the Deploy step when a deploy job starts from the Config card
  window.addEventListener("deploy-job-started", (e) => {
    if (e.detail.envId !== loadedEnvId) return;
    userToggled.set("deploy", true);
    const body = document.getElementById("wf-body-deploy");
    if (body) {
      body.classList.add("open");
      const btn = body.previousElementSibling?.querySelector("[data-wf-toggle='deploy']")
        || document.querySelector("[data-wf-toggle='deploy']");
      if (btn) { btn.textContent = "▾"; btn.setAttribute("aria-expanded", "true"); }
    }
  });
}

// ---------- deployer host form ----------

async function saveDeployerHost(envId, btn) {
  const hostInput = document.getElementById("wf-deployer-host");
  const msgEl = document.getElementById("wf-deployer-msg");
  const val = hostInput?.value.trim();
  if (!val) {
    if (msgEl) msgEl.innerHTML = '<span class="error">Enter a hostname or user@hostname</span>';
    hostInput?.focus();
    return;
  }
  const [userPart, ...hostParts] = val.split("@");
  const host = hostParts.join("@") || userPart;
  const user = hostParts.length ? userPart : null;
  btn.disabled = true;
  btn.textContent = "Saving…";
  if (msgEl) msgEl.innerHTML = '';
  try {
    await api(`/api/v1/environments/${encodeURIComponent(envId)}`, {
      method: "PATCH",
      body: JSON.stringify({
        deployer_ssh_host: host,
        deployer_ssh_user: user,
      }),
    });
    toast("Deploy host saved", "ok");
    if (msgEl) msgEl.innerHTML = '<span class="ok">Saved</span>';
    loadWorkflowCard(envId);
  } catch (e) {
    let msg = e.message;
    if (e.isNetwork || e.isTimeout) msg = "Server unreachable. Check your connection and try again.";
    toast(`Deploy host save failed: ${msg}`, "error");
    if (msgEl) msgEl.innerHTML = `<span class="error">${esc(msg)}</span>`;
    btn.disabled = false;
    btn.textContent = "Save";
  }
}

// ---------- state export repo form ----------

async function saveStateRepo(envId, btn) {
  const pathInput = document.getElementById("wf-state-path");
  const remoteInput = document.getElementById("wf-state-remote");
  const msgEl = document.getElementById("wf-state-msg");
  const orNull = (s) => (s && s.trim() ? s.trim() : null);
  btn.disabled = true;
  btn.textContent = "Saving…";
  if (msgEl) msgEl.innerHTML = '';
  try {
    await api(`/api/v1/environments/${encodeURIComponent(envId)}`, {
      method: "PATCH",
      body: JSON.stringify({
        state_repo_path: orNull(pathInput?.value),
        state_repo_remote: orNull(remoteInput?.value),
      }),
    });
    toast("State repo saved", "ok");
    if (msgEl) msgEl.innerHTML = '<span class="ok">Saved</span>';
    try {
      await loadEnvs();
    } catch {
      // envs refresh is best-effort; re-render the card below either way
    }
    loadWorkflowCard(envId);
  } catch (e) {
    let msg = e.message;
    if (e.isNetwork || e.isTimeout) msg = "Server unreachable. Check your connection and try again.";
    toast(`State repo save failed: ${msg}`, "error");
    if (msgEl) msgEl.innerHTML = `<span class="error">${esc(msg)}</span>`;
    btn.disabled = false;
    btn.textContent = "Save";
  }
}

export async function loadWorkflowCard(envId) {
  const box = document.getElementById("wf-steps");
  if (!box) return;
  if ((envId || "") !== loadedEnvId) {
    userToggled.clear();
  }
  loadedEnvId = envId || "";
  resubscribeJobs(loadedEnvId); // jobs SSE for verify/prepare/install/day-2 tracking
  const msg = document.getElementById("wf-msg");
  if (!envId) {
    clearVerifyTimer();
    verifyJobId = "";
    clearPrepareTimer();
    prepareJobId = "";
    clearDay2Timer();
    day2JobId = "";
    clearAgentTimer();
    agentEnvId = "";
    if (msg) msg.textContent = "";
    box.innerHTML = skeletonHtml("Select an environment.", false);
    return;
  }
  if (msg) msg.textContent = "Loading…";
  let data;
  try {
    data = await api(`/api/v1/environments/${encodeURIComponent(envId)}/workflow`);
  } catch (e) {
    if (msg) msg.textContent = "";
    clearAgentTimer();
    agentEnvId = "";
    let errMsg = e.message || "workflow fetch failed";
    if (e.isNetwork || e.isTimeout) errMsg = "Console server unreachable. Check that the backend is running.";
    box.innerHTML =
      `<div class="error">${esc(errMsg)}</div>` +
      skeletonHtml("State unavailable.", true);
    return;
  }
  if (msg) msg.textContent = "";
  const steps = Array.isArray(data && data.steps) ? data.steps : [];
  const byId = {};
  steps.forEach((s) => {
    if (s && typeof s === "object" && s.id) byId[s.id] = s;
  });
  // Day-2 actions only make sense on a deployed env: reachable now, or a successful
  // deploy/verify on record. Defensive about missing/renamed detail keys.
  const operate0 = byId.operate && byId.operate.details && typeof byId.operate.details === "object" ? byId.operate.details : {};
  const deploy0 = byId.deploy && byId.deploy.details && typeof byId.deploy.details === "object" ? byId.deploy.details : {};
  const verify0 = operate0.verify && typeof operate0.verify === "object" ? operate0.verify : {};
  const envDry =
    data && data.environment && typeof data.environment === "object" ? data.environment.dry_run : null;
  const connectDry = byId.connect && byId.connect.details && byId.connect.details.dry_run;
  const deployDry = deploy0.dry_run;
  workflowDryRun = envDry === true || connectDry === true || deployDry === true;
  const ctx = {
    deployed:
      operate0.reachable === true ||
      String(verify0.status || "").toLowerCase() === "success" ||
      String(deploy0.status || "").toLowerCase() === "success",
    dryRun: workflowDryRun,
  };
  // Row position: the first step whose state isn't "done" is current (expanded,
  // emphasized); steps before it are done (compact), steps after are future (collapsed,
  // de-emphasized). All steps done → the whole spine renders compact.
  const firstOpen = STEPS.findIndex((step) => {
    const st = byId[step.id] && byId[step.id].state;
    return String(st || "pending").toLowerCase() !== "done";
  });
  const rendered = STEPS.map((step, idx) => {
    const pos = firstOpen === -1 || idx < firstOpen ? "done" : idx === firstOpen ? "current" : "future";
    return stepHtml(step, idx, byId[step.id], envId, ctx, pos);
  });
  box.innerHTML =
    `<div class="wf-progress-bar">${rendered.map(r => r.bar).join("")}</div>` +
    `<div>${rendered.map(r => r.row + r.body).join("")}</div>`;
  // If the backend reports a verify job still queued/running (e.g. the page was
  // reloaded mid-verify), resume polling it so the result lands on its own.
  const operate = byId.operate;
  const od = operate && operate.details && typeof operate.details === "object" ? operate.details : {};
  const v = od.verify && typeof od.verify === "object" ? od.verify : null;
  const vId = v && v.job_id != null ? String(v.job_id) : "";
  if (vId && VERIFY_ACTIVE.has(String(v.status || "").toLowerCase()) && verifyJobId !== vId) {
    pollVerifyJob(envId, vId, String(v.level || "verify"));
  }
  // Start (or stop) the 15s agent status poll based on whether the connect
  // step ended up expanded.
  syncAgentPolling(envId);
}
