// pages/operations.js — grouped catalog + typed run forms.
import { api, esc, toast } from "../api.js";
import { store, loadEnvs, envOptionsHtml, roleAtLeast, gate } from "../store.js";

export const title = "Operations";

const GROUP_ORDER = ["internal", "host", "genestack", "ansible", "baremetal"];

// Allowlists verified against app/services/catalog.py.
// Verified against ansible/roles/basic_ops/tasks/main.yml (ping|facts|disk_check|all).
const BASIC_OPS_ACTIONS = ["ping", "facts", "disk_check", "all"];
const PLAYBOOK_ALLOWLIST = [
  "host_preflight.yml",
  "basic_ops.yml",
  "provision_bridge.yml",
  "host-setup.yml",
];

let ops = [];
// Dynamically discovered per page-load; null = fetch failed → free-text fallback.
let serviceNames = null;
let stageIds = null;

export async function render(root, { param }) {
  root.innerHTML = `
  <div class="grid">
    <div class="card span-5">
      <h2>Catalog</h2>
      <div id="ops-list" class="muted">Loading…</div>
    </div>
    <div class="card span-7">
      <h2>Run operation</h2>
      <div id="op-run" class="muted">Select an operation from the catalog.</div>
    </div>
  </div>`;

  const listed = await api("/api/v1/operations");
  ops = (Array.isArray(listed) ? listed : []).filter((op) => !String(op.id || "").startsWith("maas."));
  if (!store.envs.length) await loadEnvs().catch(() => {});
  // Cache per page-load; failures degrade the dropdowns to free-text inputs.
  const [svcRes, pipeRes] = await Promise.allSettled([
    api("/api/v1/genestack/services"),
    api("/api/v1/genestack/pipeline"),
  ]);
  serviceNames =
    svcRes.status === "fulfilled" && Array.isArray(svcRes.value.services)
      ? svcRes.value.services.map((s) => s.name).filter(Boolean)
      : null;
  stageIds =
    pipeRes.status === "fulfilled" && Array.isArray(pipeRes.value.stages)
      ? pipeRes.value.stages.map((s) => s.id).filter(Boolean)
      : null;
  renderCatalog();
  if (param && ops.some((o) => o.id === param)) selectOp(param);
}

function renderCatalog() {
  const groups = {};
  for (const op of ops) {
    const g = op.id.split(".")[0];
    (groups[g] = groups[g] || []).push(op);
  }
  const order = Object.keys(groups).sort((a, b) => {
    const ia = GROUP_ORDER.indexOf(a);
    const ib = GROUP_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b);
  });
  document.getElementById("ops-list").innerHTML =
    order
      .map(
        (g) => `
      <div class="ops-group">
        <div class="ops-group-title">${esc(g)}</div>
        ${groups[g]
          .map(
            (op) => `
          <button type="button" class="ops-item" data-op="${esc(op.id)}">
            <div class="ops-item-head"><code>${esc(op.id)}</code><span class="badge role-${esc(op.required_role)}">${esc(op.required_role)}</span></div>
            <div class="muted ops-desc">${esc(op.description || op.name)}</div>
          </button>`
          )
          .join("")}
      </div>`
      )
      .join("") || '<div class="muted">No operations available for your role.</div>';

  document.querySelectorAll(".ops-item").forEach((b) =>
    b.addEventListener("click", () => selectOp(b.dataset.op))
  );
}

function selectOp(id) {
  document.querySelectorAll(".ops-item").forEach((b) =>
    b.classList.toggle("active", b.dataset.op === id)
  );
  history.replaceState(null, "", "#/operations/" + id);
  const op = ops.find((o) => o.id === id);
  const runEl = document.getElementById("op-run");
  runEl.classList.remove("muted");

  const paramHints = (op.params || []).length
    ? `<ul class="param-hints">${op.params
        .map(
          (p) =>
            `<li><code>${esc(p.name)}</code>${p.required ? " (required)" : ""} — ${esc(p.description || p.type)}</li>`
        )
        .join("")}</ul>`
    : "";

  runEl.innerHTML = `
    <div class="op-meta">
      <div class="row">
        <code class="op-title">${esc(op.id)}</code>
        <span class="badge role-${esc(op.required_role)}">${esc(op.required_role)}</span>
        <span class="badge">${esc(op.backend)}</span>
      </div>
      <p class="muted">${esc(op.description)}</p>
      ${paramHints}
    </div>
    <label class="field" style="margin-bottom:.75rem"><span>Environment</span>
      <select id="run-env">${envOptionsHtml(null, { includeNone: true, noneLabel: "(no environment)" })}</select>
    </label>
    ${typedFormHtml(op)}
    <label class="check"><input type="checkbox" id="run-sync" /> Run synchronously (run_sync)</label>
    <div class="hint muted">Mutating or long-running ops (deploy, tempest, pipeline) default to queued — the job runs in the worker and live logs stream on the activity page. Only enable this for quick read-only ops.</div>
    <div class="row" style="margin-top:.75rem">
      <button id="btn-run-op" type="button" ${gate(roleAtLeast(op.required_role), op.required_role)}>Run</button>
      <span id="run-msg" class="muted"></span>
    </div>`;

  wireKvEditors(runEl);
  document.getElementById("btn-run-op").addEventListener("click", () => runOp(op));
}

function selectHtml(id, options) {
  return `<select id="${id}">${options
    .map((o) => `<option value="${esc(o)}">${esc(o)}</option>`)
    .join("")}</select>`;
}

function kvEditorHtml(id) {
  return `<div class="field" style="margin-bottom:.75rem"><span>extra_vars (key/value; values accept JSON)</span>
    <div class="kv-editor" id="${id}">
      <div class="kv-row"><input class="kv-key" placeholder="key" /><input class="kv-val" placeholder='value (JSON or string)' /><span></span></div>
    </div>
    <div><button type="button" class="secondary btn-sm kv-add" data-kv="${id}">+ add var</button></div>
  </div>`;
}

function typedFormHtml(op) {
  const textField = (id, label, placeholder = "") =>
    `<label class="field" style="margin-bottom:.75rem"><span>${label}</span><input id="${id}" type="text" placeholder="${esc(placeholder)}" /></label>`;
  switch (op.id) {
    case "maas.machine.power_status":
      return textField("p-system_id", "system_id (required)", "e.g. abc123");
    case "host.preflight":
      return textField("p-limit", "limit (ansible --limit pattern)") + kvEditorHtml("p-extra-vars");
    case "host.basic_ops":
      return (
        `<label class="field" style="margin-bottom:.75rem"><span>action (required)</span>${selectHtml("p-action", BASIC_OPS_ACTIONS)}</label>` +
        textField("p-limit", "limit (ansible --limit pattern)") +
        kvEditorHtml("p-extra-vars")
      );
    case "genestack.service.enable":
      return serviceNames && serviceNames.length
        ? `<label class="field" style="margin-bottom:.75rem"><span>service (discovered)</span>${selectHtml("p-service", serviceNames)}</label>`
        : textField("p-service", "service (required)", "e.g. keystone");
    case "genestack.pipeline.run":
      return stageIds && stageIds.length
        ? `<label class="field" style="margin-bottom:.75rem"><span>stage (required)</span>${selectHtml("p-stage", stageIds)}</label>`
        : textField("p-stage", "stage (required)", "e.g. operators");
    case "ansible.playbook.run":
      return (
        `<label class="field" style="margin-bottom:.75rem"><span>playbook (allowlist)</span>${selectHtml("p-playbook", PLAYBOOK_ALLOWLIST)}</label>` +
        textField("p-limit", "limit (ansible --limit pattern)") +
        textField("p-tags", "tags (comma-separated)") +
        kvEditorHtml("p-extra-vars")
      );
    case "genestack.host_setup":
      return (
        `<label class="check" style="margin:0 0 .75rem"><input type="checkbox" id="p-check" /> check mode (ansible --check)</label>` +
        textField("p-limit", "limit (ansible --limit pattern)")
      );
    case "genestack.deploy":
      return (
        textField("p-parallelism", "parallelism (1..16, default 1 — hosts deployed in parallel; keystone always first)") +
        `<label class="check" style="margin:0 0 .75rem"><input type="checkbox" id="p-dry-run" /> dry_run (force rehearsal regardless of env setting)</label>` +
        `<label class="check" style="margin:0 0 .75rem"><input type="checkbox" id="p-skip-push" /> skip_push (run pipeline without pushing config first)</label>` +
        textField("p-from-stage", "from_stage (start at this pipeline stage, e.g. core)")
      );
    case "genestack.tempest":
      return (
        `<label class="field" style="margin-bottom:.75rem"><span>action</span>${selectHtml("p-action", ["install-run", "install", "run"])}</label>` +
        textField("p-suite", "suite (tempest test regex; 'full' or omit = chart default)")
      );
    case "genestack.k8s_upgrade":
      return textField("p-kube_version", "kube_version (e.g. 1.31.4; omit = keep inventory value)");
    case "genestack.hyperconverged_lab":
      return (
        `<label class="field" style="margin-bottom:.75rem"><span>platform (required)</span>${selectHtml("p-platform", ["kubespray", "talos"])}</label>` +
        textField("p-include", "include (comma-separated OpenStack services)") +
        textField("p-extra_args", "extra_args (advanced: extra script flags)") +
        `<label class="check" style="margin:0 0 .75rem"><input type="checkbox" id="p-dry-run" /> dry_run (force rehearsal)</label>`
      );
    // Ops with no params:
    case "internal.health":
    case "maas.machines.list":
    case "genestack.components.desired":
    case "genestack.components.list":
    case "genestack.scripts.list":
    case "genestack.services.list":
    case "genestack.cluster.status":
    case "genestack.smoke":
      return '<div class="hint muted">This operation takes no parameters.</div>';
    // Fallback for anything unrecognized:
    default:
      return `<label class="field" style="margin-bottom:.75rem"><span>Params (raw JSON)</span>
        <textarea id="p-raw" rows="5" placeholder='{"key": "value"}'></textarea>
      </label>`;
  }
}

function wireKvEditors(scope) {
  scope.querySelectorAll(".kv-add").forEach((btn) =>
    btn.addEventListener("click", () => {
      const ed = document.getElementById(btn.dataset.kv);
      const row = document.createElement("div");
      row.className = "kv-row";
      row.innerHTML = `<input class="kv-key" placeholder="key" /><input class="kv-val" placeholder='value (JSON or string)' /><button type="button" class="secondary btn-sm kv-del">×</button>`;
      row.querySelector(".kv-del").addEventListener("click", () => row.remove());
      ed.appendChild(row);
    })
  );
}

function collectKv(id) {
  const obj = {};
  document.querySelectorAll(`#${id} .kv-row`).forEach((r) => {
    const k = r.querySelector(".kv-key").value.trim();
    if (!k) return;
    const raw = r.querySelector(".kv-val").value.trim();
    if (raw === "") {
      obj[k] = "";
      return;
    }
    try {
      obj[k] = JSON.parse(raw);
    } catch {
      obj[k] = raw;
    }
  });
  return obj;
}

function collectParams(op) {
  const val = (id) => document.getElementById(id).value.trim();
  const params = {};
  const addExtraVars = () => {
    const ev = collectKv("p-extra-vars");
    if (Object.keys(ev).length) params.extra_vars = ev;
  };
  switch (op.id) {
    case "maas.machine.power_status":
      if (val("p-system_id")) params.system_id = val("p-system_id");
      break;
    case "host.preflight":
      if (val("p-limit")) params.limit = val("p-limit");
      addExtraVars();
      break;
    case "host.basic_ops":
      params.action = val("p-action");
      if (val("p-limit")) params.limit = val("p-limit");
      addExtraVars();
      break;
    case "genestack.service.enable":
      params.service = val("p-service");
      break;
    case "genestack.pipeline.run":
      params.stage = val("p-stage");
      break;
    case "ansible.playbook.run":
      params.playbook = val("p-playbook");
      if (val("p-limit")) params.limit = val("p-limit");
      if (val("p-tags")) params.tags = val("p-tags");
      addExtraVars();
      break;
    case "genestack.host_setup":
      params.check = document.getElementById("p-check").checked;
      if (val("p-limit")) params.limit = val("p-limit");
      break;
    case "genestack.deploy":
      {
        const n = parseInt(val("p-parallelism"), 10);
        if (Number.isFinite(n) && n > 0) params.parallelism = n;
        if (document.getElementById("p-dry-run").checked) params.dry_run = true;
        if (document.getElementById("p-skip-push").checked) params.skip_push = true;
        if (val("p-from-stage")) params.from_stage = val("p-from-stage");
      }
      break;
    case "genestack.tempest":
      params.action = val("p-action");
      if (val("p-suite")) params.suite = val("p-suite");
      break;
    case "genestack.k8s_upgrade":
      if (val("p-kube_version")) params.kube_version = val("p-kube_version");
      break;
    case "genestack.hyperconverged_lab":
      params.platform = val("p-platform");
      if (val("p-include")) params.include = val("p-include");
      if (val("p-extra_args")) params.extra_args = val("p-extra_args");
      if (document.getElementById("p-dry-run").checked) params.dry_run = true;
      break;
    case "internal.health":
    case "maas.machines.list":
    case "genestack.components.desired":
    case "genestack.components.list":
    case "genestack.scripts.list":
    case "genestack.services.list":
    case "genestack.cluster.status":
    case "genestack.smoke":
      break;
    default: {
      const raw = val("p-raw");
      if (raw) {
        try {
          return JSON.parse(raw);
        } catch (e) {
          throw new Error("Params are not valid JSON: " + e.message);
        }
      }
    }
  }
  return params;
}

async function runOp(op) {
  const msg = document.getElementById("run-msg");
  let params;
  try {
    params = collectParams(op);
  } catch (e) {
    msg.textContent = e.message;
    return;
  }
  const envId = document.getElementById("run-env").value;
  const runSync = document.getElementById("run-sync").checked;
  msg.textContent = "Submitting…";
  try {
    let job;
    if (envId) {
      job = await api(`/api/v1/environments/${encodeURIComponent(envId)}/jobs`, {
        method: "POST",
        body: JSON.stringify({ operation: op.id, params, environment_id: envId, run_sync: runSync }),
      });
    } else {
      job = await api("/api/v1/jobs", {
        method: "POST",
        body: JSON.stringify({ operation: op.id, params, run_sync: runSync }),
      });
    }
    toast(`Job ${String(job.id).slice(0, 8)}… created (${job.status})`, "ok");
    location.hash = "#/activity?tab=jobs&job=" + encodeURIComponent(job.id);
  } catch (e) {
    msg.textContent = e.message;
    if (e.status === 403) toast("Insufficient role for this operation", "bad");
  }
}
