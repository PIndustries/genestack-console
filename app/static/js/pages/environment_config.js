// pages/environment_config.js — env-config editor card for the environment detail page.
// Current YAML + version history (read-only view of old versions), save (server-validated),
// render preview, and push (creates a genestack.config.push job). Also renders a compact
// Deployment provider card (GET/PUT /config/provider) and a BYOI reinstall panel for
// talos + OVH environments (POST /ovh/byoi, 5s job poll). All rendering is defensive:
// the env-config backend may be absent or partially implemented, so every section degrades
// to a muted "unavailable" note instead of throwing.
import { api, esc, fmtTime, toast } from "../api.js";
import { canAdmin, canRun, gate, store } from "../store.js";

// Shown when the environment has never been configured (GET /config → version: null).
const STARTER_TEMPLATE = `# Genestack environment configuration
# The server validates this document on save; unknown keys are reported as warnings.
provider: talos     # OVH Rise production path
# provider: kubespray     # lab path, when the machines are already installed

# The console drives talosctl from this host — no ssh to the nodes.
# Nodes should boot a Talos factory image that includes the iscsi-tools and
# util-linux-tools system extensions (longhorn needs both). Blank image_url =
# console default.
# talos:
#   cluster_name: my-cluster
#   install_disk: /dev/nvme0n1    # Rise often NVMe, not /dev/sda
#   image_url: ""    # factory metal-amd64.qcow2 (iscsi-tools + util-linux-tools)
#   cluster_cidrs: [10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16]  # private fabric
#   public_ingress_ports: []   # extra public ports (80/443). 6443+50000 already open on dual-NIC.
#   public_management_cidrs: []  # after public firewall lockdown, console CIDR must be listed here

ovh:                         # dedicated/Rise fabric (Inventory tab can set these)
  # vrack: pn-xxxxxx           # vRack service name
  vlan_id: 100               # 0 = untagged; 1-4000 = 802.1q on the private NIC (greenfield = 100)
  private_cidr: 10.10.0.0/24

deploy:
  # dry_run: false
  # git_ref: main

servers: {}             # e.g. node1: {ip: 10.10.0.11, roles: [control, network], source: ovh}
                        # roles: k8s_control_plane, etcd, control, compute, network, storage

network:
  # gateway_domain: cluster.example.com  # → GATEWAY_DOMAIN for setup-infrastructure.sh / envoy gateway
  # acme_email: ops@example.com          # → ACME_EMAIL for setup-envoy-gateway.sh (ACME/Let's Encrypt)
  # hyperconverged: false                # → HYPERCONVERGED: every openstack role on each node (single-node = AIO topology)
  # container_interface: enp3934127.100  # → CONTAINER_INTERFACE (kube-ovn IFACE; Talos = VLAN iface with the private IP, not the untagged parent)
  # compute_interface: bond1             # → COMPUTE_INTERFACE (must NOT be the public / default-route NIC)
  # ovn:                                 # each key → OVN_<KEY> for setup-infrastructure.sh
  #   external_interface: bond0.126      # → OVN_EXTERNAL_INTERFACE (br-ex uplink; never the public NIC; empty does not fall back to compute_interface)
  #   vlans: "vlan10:bond0:10:1500"      # → OVN_VLANS (OVN-created VLAN interfaces)

# Ansible inventory group_vars → rendered to inventory/group_vars/<group>/console-rendered.yml
# group_vars:
#   all:                # e.g. cloud_provider for kubespray
#     cloud_provider: openstack
#   k8s_cluster:        # e.g. kube_version / kube_ovn_iface
#     kube_version: v1.34.3

components:             # community starter — enable/disable per cloud
  keystone: true
  glance: true
  nova: true
  neutron: true
  horizon: true
  cinder: true

# Kubernetes secrets → rendered to kubesecrets.yaml (merged on push — existing
# entries from bin/create-secrets.sh are preserved; no mass rotation).
# WARNING: values are encrypted at rest and masked on read — a saved document
# shows "***" per value; leave "***" in place to keep the stored value.
# secrets:
#   netapp-cinder-backend:      # secret name (dns-1123)
#     namespace: openstack      # optional, default "openstack"
#     data:
#       username: admin
#       password: s3cret
`;

// Pipeline stage ids in order (mirrors PIPELINE_STAGES in
// app/services/service_registry.py) — the deploy "start stage" select.
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

export function configCardHtml() {
  return `
  <div class="card span-12" id="cfg-card">
    <div class="toolbar">
      <h2>Config</h2>
      <span id="cfg-version" class="pill">…</span>
      <select id="cfg-history" title="Version history"><option value="">current</option></select>
      <button class="secondary btn-sm" id="cfg-btn-render" type="button">Render preview</button>
      <button class="secondary btn-sm" id="cfg-btn-push" type="button" ${gate(canRun(), "operator")}>Push</button>
      <button class="secondary btn-sm" id="cfg-btn-export-state" type="button" title="Renders the portal config doc into the state repo at state/&lt;env&gt;/, commits it, and pushes to the configured remote" ${gate(canRun(), "operator")}>Export State</button>
      <button class="secondary btn-sm" id="cfg-btn-deploy" type="button" ${gate(canAdmin(), "admin")}>Deploy</button>
      <button class="secondary btn-sm" id="cfg-btn-deploy-dry" type="button" ${gate(canAdmin(), "admin")}>Deploy (dry-run)</button>
      <details id="cfg-adv-deploy">
        <summary style="font-size:.75rem;cursor:pointer">Start stage</summary>
        <select id="cfg-from-stage" title="Resume a previous deploy from a later pipeline stage (earlier stages are skipped)">
          <option value="">hosts (full pipeline)</option>
          ${PIPELINE_STAGES.slice(1).map((s) => `<option value="${s}">${s}</option>`).join("")}
        </select>
      </details>
      <button class="secondary btn-sm" id="cfg-btn-preflight" type="button" title="Run host.preflight (ansible) against the inventory hosts before deploying" ${gate(canRun(), "operator")}>Run preflight</button>
      <span id="cfg-preflight-badge"></span>
      <span id="cfg-msg" class="muted"></span>
    </div>
    <div id="cfg-err"></div>
    <div id="cfg-banner"></div>
    <label class="field" style="margin-top:.25rem"><span>Environment config (YAML — validated server-side on save)</span>
      <textarea id="cfg-yaml" rows="18" spellcheck="false" placeholder="(no config loaded)"></textarea>
    </label>
    <div class="row" style="margin-top:.5rem">
      <button id="cfg-btn-save" type="button" ${gate(canRun(), "operator")}>Save</button>
      <span id="cfg-save-msg" class="muted"></span>
    </div>
    <div id="cfg-warnings"></div>
    <div id="cfg-preview" style="margin-top:.75rem"></div>
  </div>
  <div class="card span-12" id="dep-card" style="margin-top:.75rem">
    <div class="toolbar">
      <h2>Deployment</h2>
      <span id="dep-provider" class="pill">…</span>
      <span id="dep-msg" class="muted"></span>
    </div>
    <div id="dep-err"></div>
    <div id="dep-body" class="muted">Loading…</div>
  </div>
  <div class="card span-12" id="byoi-card" style="margin-top:.75rem;display:none">
    <div class="toolbar">
      <h2>Advanced: wipe &amp; reinstall now</h2>
      <button class="secondary btn-sm" id="byoi-reinstall" type="button" ${gate(canAdmin(), "admin")}>Reinstall selected</button>
      <span id="byoi-msg" class="muted"></span>
    </div>
    <div id="byoi-err"></div>
    <div class="hint" style="color:var(--warn);font-size:.78rem">Deploy already reinstalls nodes that are not answering Talos :50000. Use this only to force a wipe.</div>
    <div id="byoi-body" class="muted">Loading…</div>
  </div>`;
}

export function wireConfigCard(getEnvId) {
  document.getElementById("cfg-history").addEventListener("change", (e) => {
    const v = e.target.value;
    if (v === "") loadConfigCard(getEnvId());
    else loadConfigVersion(getEnvId(), v);
  });
  document.getElementById("cfg-btn-save").addEventListener("click", () => saveConfig(getEnvId()));
  document.getElementById("cfg-btn-render").addEventListener("click", () => renderPreview(getEnvId()));
  document.getElementById("cfg-btn-push").addEventListener("click", () => pushConfig(getEnvId()));
  document.getElementById("cfg-btn-export-state").addEventListener("click", () => exportState(getEnvId()));
  document.getElementById("cfg-btn-deploy").addEventListener("click", () => deployEnv(getEnvId(), {}));
  document.getElementById("cfg-btn-deploy-dry").addEventListener("click", () => deployEnv(getEnvId(), { dry_run: true }));
  document.getElementById("cfg-btn-preflight").addEventListener("click", () => runPreflight(getEnvId()));
  document.getElementById("byoi-reinstall").addEventListener("click", () => startByoiReinstall(getEnvId()));
}

function el(id) {
  return document.getElementById(id);
}

function setVersionPill(version) {
  const pill = el("cfg-version");
  if (version == null) {
    pill.textContent = "not configured";
    pill.className = "pill";
  } else {
    pill.textContent = "v" + version;
    pill.className = "pill ok";
  }
}

function setBanner(html) {
  el("cfg-banner").innerHTML = html || "";
}

function setEditing({ readonly }) {
  el("cfg-yaml").readOnly = readonly;
  const save = el("cfg-btn-save");
  // Re-apply role gate on top of the viewing-state gate.
  save.disabled = readonly || !canRun();
  if (readonly) save.title = "Viewing an old version — switch back to current to edit";
  else if (!canRun()) save.title = "Requires operator role";
  else save.title = "";
}

function resetCard(note) {
  setVersionPill(null);
  el("cfg-version").textContent = "—";
  el("cfg-msg").textContent = note || "";
  el("cfg-err").innerHTML = "";
  setPreflightBadge("");
  setBanner("");
  el("cfg-yaml").value = "";
  el("cfg-yaml").readOnly = true;
  el("cfg-save-msg").textContent = "";
  el("cfg-warnings").innerHTML = "";
  el("cfg-preview").innerHTML = "";
  el("cfg-history").innerHTML = '<option value="">current</option>';
  hideDeploymentSections();
}

export async function loadConfigCard(envId) {
  if (!el("cfg-card")) return;
  if (!envId) {
    resetCard("Select an environment.");
    return;
  }
  el("cfg-err").innerHTML = "";
  el("cfg-msg").textContent = "Loading…";
  el("cfg-preview").innerHTML = "";

  let data;
  try {
    data = await api(`/api/v1/environments/${encodeURIComponent(envId)}/config`);
  } catch (e) {
    el("cfg-msg").textContent = "";
    el("cfg-err").innerHTML = `<div class="error">${esc(e.message)}</div>`;
    setVersionPill(null);
    el("cfg-version").textContent = "unavailable";
    el("cfg-yaml").value = "";
    el("cfg-yaml").readOnly = true;
    setBanner('<div class="muted">Unavailable — config backend did not respond.</div>');
    hideDeploymentSections();
    return;
  }

  const version = data && data.version != null ? data.version : null;
  setVersionPill(version);
  el("cfg-msg").textContent =
    version != null
      ? `updated ${fmtTime(data.updated_at) || "—"}${data.updated_by ? " by " + data.updated_by : ""}`
      : "no saved config — starter template shown";
  el("cfg-yaml").value = typeof data.yaml === "string" && data.yaml ? data.yaml : STARTER_TEMPLATE;
  setBanner("");
  setEditing({ readonly: false });
  el("cfg-save-msg").textContent = "";
  el("cfg-warnings").innerHTML = "";

  await loadVersions(envId, version);
  loadDeploymentSection(envId);
}

async function loadVersions(envId, currentVersion) {
  const sel = el("cfg-history");
  let versions = [];
  try {
    const data = await api(`/api/v1/environments/${encodeURIComponent(envId)}/config/versions`);
    if (Array.isArray(data)) versions = data;
  } catch {
    // History is best-effort; leave just the "current" option.
  }
  const opts = [`<option value="">current${currentVersion != null ? " (v" + esc(currentVersion) + ")" : ""}</option>`];
  versions
    .slice()
    .sort((a, b) => (b.version || 0) - (a.version || 0))
    .forEach((v) => {
      const label = `v${v.version} — ${fmtTime(v.created_at) || "?"}${v.created_by ? " · " + v.created_by : ""}`;
      opts.push(`<option value="${esc(v.version)}">${esc(label)}</option>`);
    });
  sel.innerHTML = opts.join("");
  sel.value = "";
}

async function loadConfigVersion(envId, version) {
  if (!envId) return;
  el("cfg-err").innerHTML = "";
  let data;
  try {
    data = await api(
      `/api/v1/environments/${encodeURIComponent(envId)}/config/versions/${encodeURIComponent(version)}`
    );
  } catch (e) {
    el("cfg-err").innerHTML = `<div class="error">${esc(e.message)}</div>`;
    el("cfg-history").value = "";
    return;
  }
  el("cfg-yaml").value = typeof data.yaml === "string" ? data.yaml : "";
  setEditing({ readonly: true });
  setBanner(`
    <div class="row" style="margin:.25rem 0 .5rem">
      <span class="pill warn">viewing old version v${esc(data.version != null ? data.version : version)} (read-only)</span>
      <button class="secondary btn-sm" id="cfg-back-current" type="button">Back to current</button>
    </div>`);
  document.getElementById("cfg-back-current").addEventListener("click", () => {
    el("cfg-history").value = "";
    loadConfigCard(envId);
  });
}

async function saveConfig(envId) {
  if (!envId) return;
  const err = el("cfg-err");
  const msg = el("cfg-save-msg");
  const btn = el("cfg-btn-save");
  err.innerHTML = "";
  el("cfg-warnings").innerHTML = "";
  msg.textContent = "Saving…";
  btn.disabled = true;
  btn.textContent = "Saving…";
  try {
    const res = await api(`/api/v1/environments/${encodeURIComponent(envId)}/config`, {
      method: "PUT",
      body: JSON.stringify({ yaml_text: el("cfg-yaml").value }),
    });
    msg.textContent = res && res.version != null ? `saved → v${res.version}` : "saved";
    const warnings = res && Array.isArray(res.warnings) ? res.warnings : [];
    if (warnings.length) {
      el("cfg-warnings").innerHTML = `<div class="hint" style="color:var(--warn)">warnings:<ul style="margin:.25rem 0 0">${warnings
        .map((w) => `<li>${esc(w)}</li>`)
        .join("")}</ul></div>`;
    }
    toast(msg.textContent, "ok");
    await loadConfigCard(envId);
    el("cfg-save-msg").textContent = msg.textContent;
    if (warnings.length) {
      el("cfg-warnings").innerHTML = `<div class="hint" style="color:var(--warn)">warnings:<ul style="margin:.25rem 0 0">${warnings
        .map((w) => `<li>${esc(w)}</li>`)
        .join("")}</ul></div>`;
    }
  } catch (e) {
    msg.textContent = "";
    err.innerHTML = `<div class="error">Config save failed: ${esc(e.message)}</div>`;
    toast(`Config save failed: ${e.message}`, "error");
    if (e.status === 403) toast("Insufficient role: operator required", "bad");
  } finally {
    btn.disabled = false;
    btn.textContent = "Save";
  }
}

async function renderPreview(envId) {
  if (!envId) return;
  const panel = el("cfg-preview");
  const err = el("cfg-err");
  err.innerHTML = "";
  panel.innerHTML = '<div class="muted">Rendering…</div>';
  try {
    const data = await api(`/api/v1/environments/${encodeURIComponent(envId)}/config/render`);
    const files = data && data.files && typeof data.files === "object" ? data.files : {};
    const paths = Object.keys(files).sort();
    panel.innerHTML = paths.length
      ? `<details open>
          <summary style="cursor:pointer">Rendered files (${paths.length}) — what a push would write</summary>
          ${paths
            .map(
              (p) => `<h3 style="font-size:.85rem;margin:.6rem 0 .2rem"><code>${esc(p)}</code></h3>
              <pre class="log-inline">${esc(files[p])}</pre>`
            )
            .join("")}
        </details>`
      : '<div class="muted">Render returned no files.</div>';
  } catch (e) {
    panel.innerHTML = "";
    err.innerHTML = `<div class="error">Render preview failed: ${esc(e.message)}</div>`;
  }
}

async function pushConfig(envId) {
  if (!envId) return;
  const msg = el("cfg-msg");
  const err = el("cfg-err");
  const btn = el("cfg-btn-push");
  const pushPanel = getOrCreateProgressPanel("cfg-push-progress");
  err.innerHTML = "";
  msg.textContent = "Creating push job…";
  btn.disabled = true;
  btn.textContent = "Pushing…";
  pushPanel.innerHTML = '<div class="muted">Starting push…</div>';
  pushPanel.classList.remove("hidden");
  try {
    const job = await api(`/api/v1/environments/${encodeURIComponent(envId)}/jobs`, {
      method: "POST",
      body: JSON.stringify({ operation: "genestack.config.push", params: {} }),
    });
    const id = job && job.id != null ? String(job.id) : "";
    msg.innerHTML = id
      ? `push job created — <a href="#/activity?tab=jobs&job=${esc(id)}">view job ${esc(id.slice(0, 8))}…</a>`
      : "push job created";
    toast(`Push job ${id.slice(0, 8)}… created`, "ok");
    pollPushJob(id, pushPanel);
  } catch (e) {
    msg.textContent = "";
    err.innerHTML = `<div class="error">Push failed: ${esc(e.message)}. Check server logs for details.</div>`;
    toast(`Push failed: ${e.message}`, "error");
    if (e.status === 403) toast("Insufficient role: operator required", "bad");
  } finally {
    btn.disabled = false;
    btn.textContent = "Push";
  }
}

async function exportState(envId) {
  if (!envId) return;
  const msg = el("cfg-msg");
  const err = el("cfg-err");
  const btn = el("cfg-btn-export-state");
  const statePanel = getOrCreateProgressPanel("cfg-state-export-progress");
  err.innerHTML = "";
  msg.textContent = "Creating export state job…";
  btn.disabled = true;
  btn.textContent = "Exporting…";
  statePanel.innerHTML = '<div class="muted">Starting state export…</div>';
  statePanel.classList.remove("hidden");
  try {
    const job = await api(`/api/v1/environments/${encodeURIComponent(envId)}/jobs`, {
      method: "POST",
      body: JSON.stringify({ operation: "genestack.state.export", params: {} }),
    });
    const id = job && job.id != null ? String(job.id) : "";
    msg.innerHTML = id
      ? `export state job created — <a href="#/activity?tab=jobs&job=${esc(id)}">view job ${esc(id.slice(0, 8))}…</a>`
      : "export state job created";
    toast(`Export state job ${id.slice(0, 8)}… created`, "ok");
    pollPushJob(id, statePanel);
  } catch (e) {
    msg.textContent = "";
    err.innerHTML = `<div class="error">Export state failed: ${esc(e.message)}. Check server logs for details.</div>`;
    toast(`Export state failed: ${e.message}`, "error");
    if (e.status === 403) toast("Insufficient role: operator required", "bad");
  } finally {
    btn.disabled = false;
    btn.textContent = "Export State";
  }
}

const POLL_MS = 3000;
const pollTimers = new Map();
const ACTIVE_STATUS = new Set(["queued", "running"]);

function clearPoll(jobId) {
  const t = pollTimers.get(jobId);
  if (t) {
    clearTimeout(t);
    pollTimers.delete(jobId);
  }
}

function getOrCreateProgressPanel(panelId) {
  let panel = el(panelId);
  if (!panel) {
    panel = document.createElement("div");
    panel.id = panelId;
    panel.className = "hidden dp-push-panel";
    const banner = el("cfg-banner");
    if (banner && banner.parentElement) {
      banner.parentElement.insertBefore(panel, banner.nextSibling);
    }
  }
  return panel;
}

async function pollPushJob(jobId, container) {
  clearPoll(jobId);
  if (!container.parentElement) return;
  let job;
  try {
    job = await api(`/api/v1/jobs/${encodeURIComponent(jobId)}`);
  } catch {
    clearPoll(jobId);
    pollTimers.set(jobId, setTimeout(() => pollPushJob(jobId, container), POLL_MS));
    return;
  }
  if (!container.parentElement) return;
  const status = String(job.status || "").toLowerCase();
  container.innerHTML = `
    <div class="dp-push-header">
      <span class="pill ${status === "success" ? "ok" : status === "failed" ? "bad" : "warn"}">${esc(status)}</span>
      <a href="#/activity?tab=jobs&job=${esc(jobId)}" style="margin-left:.5rem">job ${esc(jobId.slice(0, 8))}…</a>
    </div>
    <div class="dp-push-log">${renderPushLog(job.log_text, job)}</div>`;
  if (ACTIVE_STATUS.has(status)) {
    clearPoll(jobId);
    pollTimers.set(jobId, setTimeout(() => pollPushJob(jobId, container), POLL_MS));
  } else if (status === "success") {
    setTimeout(() => {
      if (container.parentElement) { container.classList.add("hidden"); container.innerHTML = ""; }
    }, 15000);
  }
}

function formatBytes(n) {
  const num = parseInt(n, 10);
  if (isNaN(num)) return n;
  if (num < 1024) return num + " B";
  if (num < 1024 * 1024) return (num / 1024).toFixed(1) + " KB";
  return (num / (1024 * 1024)).toFixed(1) + " MB";
}

function renderPushLog(logText) {
  const text = String(logText || "");
  const lines = text.split("\n").filter(Boolean);
  if (!lines.length) return '<div class="muted">Waiting for output…</div>';
  const RE_WOULD = /(?:\[state\.export\]\s+)?(?:\[dry-run\]|dry-run:)\s+would\s+write\s+([^\s]+)\s+\((\d+)\s+bytes\)/;
  const RE_FILE = /\bwrote\s+([^\s]+)\s+\((\d+)\s+bytes\)/;
  const RE_MERGE = /merged\s+(\S+)/;
  const RE_VERSION = /\[config\.push\]\s+version=(\d+)/;
  const RE_COMMITTED = /\[state\.export\]\s+committed\s+(\S+)\s+\((.+)\)/;
  const RE_PUSHED = /\[state\.export\]\s+pushed\s+(\S+)\s+to\s+remote\s+'([^']*)'/;
  const RE_NOCHANGES = /\[state\.export\]\s+no changes staged, skipping commit/i;
  const RE_ERROR = /^Failed:|^Exception:|FAILED at/i;
  return lines.map(line => {
    const s = line.trim();
    if (!s) return "";
    const content = s.replace(/^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\]\s+/, "");
    if (RE_WOULD.test(content)) {
      const m = RE_WOULD.exec(content);
      return `<div class="dp-log-line muted">○ Would write ${esc(m[1].split("/").pop())} (${formatBytes(m[2])})</div>`;
    }
    if (RE_FILE.test(content)) {
      const m = RE_FILE.exec(content);
      return `<div class="dp-log-line ok">✓ Wrote ${esc(m[1].split("/").pop())} (${formatBytes(m[2])})</div>`;
    }
    if (RE_MERGE.test(content)) {
      const m = RE_MERGE.exec(content);
      return `<div class="dp-log-line ok">✓ Merged ${esc(m[1])}</div>`;
    }
    if (RE_VERSION.test(content)) {
      const m = RE_VERSION.exec(content);
      return `<div class="dp-log-line muted">Config push v${m[1]}</div>`;
    }
    if (RE_COMMITTED.test(content)) {
      const m = RE_COMMITTED.exec(content);
      return `<div class="dp-log-line ok">✓ Committed ${esc(m[1].slice(0, 8))}… (${esc(m[2])})</div>`;
    }
    if (RE_PUSHED.test(content)) {
      const m = RE_PUSHED.exec(content);
      return `<div class="dp-log-line ok">✓ Pushed ${esc(m[1])} to remote '${esc(m[2])}'</div>`;
    }
    if (RE_NOCHANGES.test(content)) {
      return `<div class="dp-log-line muted">○ No changes staged, skipping commit</div>`;
    }
    if (RE_ERROR.test(content)) {
      return `<div class="dp-log-line error">✕ ${esc(content.slice(0, 140))}</div>`;
    }
    if (/^\[?(warn|dry-run|ssh|agent|pipeline)/.test(content)) {
      return `<div class="dp-log-line muted">${esc(content.slice(0, 120))}</div>`;
    }
    return "";
  }).filter(Boolean).join("") || '<div class="muted">Processing…</div>';
}

async function deployEnv(envId, params) {
  if (!envId) return;
  const msg = el("cfg-msg");
  const err = el("cfg-err");
  const btn = el("cfg-btn-deploy");
  const dryBtn = el("cfg-btn-deploy-dry");
  err.innerHTML = "";
  const env = store.envs.find((x) => x.id === envId);
  const name = env && env.name ? env.name : envId;
  const rehearsal = params.dry_run === true;
  const dryNote = rehearsal
    ? "This is a DRY-RUN rehearsal — nothing will be written or executed."
    : "The env/global dry-run setting applies — check the job log for [dry-run] markers.";
  const fromStage = el("cfg-from-stage") ? el("cfg-from-stage").value : "";
  if (fromStage) params.from_stage = fromStage;
  const pipelineNote = fromStage
    ? `genestack pipeline starting at stage "${fromStage}" (earlier stages are skipped).`
    : "full genestack pipeline (all stages, in order).";
  const ovhTalos =
    (providerState || {}).provider === "talos" && (providerState || {}).infra === "ovh";
  const ovhNote = ovhTalos
    ? "OVH + Talos: Deploy will BYOI-reinstall any node that is not answering on :50000 using talos.image_url, then run talosctl.\n\n"
    : "";
  if (
    !confirm(
      `Deploy environment "${name}"?\n\n` +
        ovhNote +
        `This pushes the rendered config to the deploy host and runs the ${pipelineNote}\n\n` +
        "Deploy stops at the first failing stage; there is no automatic rollback.\n\n" +
        dryNote
    )
  ) {
    return;
  }
  msg.textContent = "Creating deploy job…";
  if (btn) { btn.disabled = true; btn.textContent = "Deploying…"; }
  if (dryBtn) { dryBtn.disabled = true; dryBtn.textContent = "Rehearsing…"; }
  // Open the Deploy step in the workflow stepper so the progress strip appears
  try {
    const job = await api(`/api/v1/environments/${encodeURIComponent(envId)}/jobs`, {
      method: "POST",
      body: JSON.stringify({ operation: "genestack.deploy", params }),
    });
    const id = job && job.id != null ? String(job.id) : "";
    msg.innerHTML = id
      ? `deploy job created — <a href="#/activity?tab=jobs&job=${esc(id)}">view job ${esc(id.slice(0, 8))}…</a>`
      : "deploy job created";
    toast(`Deploy job ${id.slice(0, 8)}… created`, "ok");
    // Signal the workflow card to expand the deploy step so the progress strip is visible
    const evt = new CustomEvent("deploy-job-started", { detail: { envId, jobId: id } });
    window.dispatchEvent(evt);
  } catch (e) {
    msg.textContent = "";
    if (e.status === 409) {
      // The 409 body carries conflicting_job_id — link the blocking job so the
      // operator can watch it instead of guessing.
      const blockingId = e.detail && e.detail.conflicting_job_id ? String(e.detail.conflicting_job_id) : "";
      const link = blockingId
        ? ` <a href="#/activity?tab=jobs&job=${esc(blockingId)}">watch the blocking job →</a>`
        : "";
      err.innerHTML = `<div class="error">Deploy blocked: ${esc(e.message)}${link}</div>`;
      toast("Deploy blocked by a running mutating job", "bad");
    } else {
      err.innerHTML = `<div class="error">Deploy failed: ${esc(e.message)}</div>`;
      toast(`Deploy failed: ${e.message}`, "error");
    }
    if (e.status === 403) toast("Insufficient role: admin required", "bad");
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Deploy"; }
    if (dryBtn) { dryBtn.disabled = false; dryBtn.textContent = "Deploy (dry-run)"; }
  }
}

// ---------- deployment provider section ----------
// Compact GET/PUT /config/provider editor, plus the BYOI reinstall panel for
// talos environments that have OVH-sourced servers. Both degrade independently
// of the YAML config above.

const DNS_1123_RE = /^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$/;
let providerState = null; // last GET /config/provider payload
let deploymentEnvId = ""; // env the section last rendered for (guards env switches)
let byoiServers = [];
let byoiSelected = new Set();

async function loadDeploymentSection(envId) {
  const body = el("dep-body");
  if (!body) return;
  deploymentEnvId = envId;
  el("dep-err").innerHTML = "";
  el("dep-msg").textContent = "";
  body.innerHTML = '<div class="muted">Loading…</div>';
  let data;
  try {
    data = await api(`/api/v1/environments/${encodeURIComponent(envId)}/config/provider`);
  } catch (e) {
    if (deploymentEnvId !== envId) return; // env switched mid-flight
    el("dep-provider").textContent = "—";
    el("dep-provider").className = "pill";
    body.innerHTML = `<div class="muted">Unavailable — ${esc(e.message)}</div>`;
    providerState = null;
    hideByoiCard();
    return;
  }
  if (deploymentEnvId !== envId) return;
  providerState = data && typeof data === "object" ? data : {};
  renderDeploymentSection(envId);
  loadByoiSection(envId);
}

function hideDeploymentSections() {
  el("dep-provider").textContent = "—";
  el("dep-provider").className = "pill";
  el("dep-body").innerHTML = '<div class="muted">Select an environment.</div>';
  el("dep-msg").textContent = "";
  el("dep-err").innerHTML = "";
  hideByoiCard();
}

function renderDeploymentSection(envId) {
  const data = providerState || {};
  const provider = data.provider === "talos" ? "talos" : "kubespray";
  const talos = data.talos && typeof data.talos === "object" ? data.talos : {};
  const deploy = data.deploy && typeof data.deploy === "object" ? data.deploy : {};
  const ovh = data.ovh && typeof data.ovh === "object" ? data.ovh : {};
  const pill = el("dep-provider");
  const infra = data.infra === "ovh" ? "ovh" : "";
  pill.textContent = infra ? `${provider} · ${infra}` : provider;
  pill.className = provider === "talos" ? "pill warn" : "pill ok";

  const defaultImage = data.default_image_url || "";
  const imageUrl = talos.image_url || defaultImage;
  const rows =
    provider === "talos"
      ? [
          ["Cluster name", talos.cluster_name || "—"],
          ["Install disk", talos.install_disk || "—"],
          ["Image URL", imageUrl || "not set — required for OVH BYOI"],
          ...(infra
            ? [
                ["Infrastructure", "OVH dedicated (BYOI)"],
                ["NICs", "private = Talos/K8s/Genestack · public = default-deny edge"],
                ["vRack", ovh.vrack || "(set on Inventory)"],
                [
                  "VLAN",
                  ovh.vlan_id == null || ovh.vlan_id === ""
                    ? "(untagged)"
                    : Number(ovh.vlan_id) === 0
                      ? "0 (untagged)"
                      : String(ovh.vlan_id),
                ],
                ["Private CIDR", ovh.private_cidr || "(from inventory IPs)"],
              ]
            : []),
        ]
      : [
          ["SSH user", deploy.ssh_user || "—"],
          ["SSH password", deploy.ssh_password ? "***" : "—"],
          ...(infra ? [["Infrastructure", "OVH dedicated"]] : []),
        ];

  el("dep-body").innerHTML = `
  <table style="margin-top:.25rem">
    <tbody>${rows
      .map(([k, val]) => `<tr><th style="width:10rem;font-weight:600">${esc(k)}</th><td>${esc(String(val))}</td></tr>`)
      .join("")}</tbody>
  </table>
  <div style="margin-top:.6rem;padding-top:.5rem;border-top:1px solid var(--border,#333)">
    <div class="row" style="gap:.5rem;align-items:center;flex-wrap:wrap">
      <span class="muted" style="font-size:.78rem">Edit:</span>
      <select id="dep-edit-provider" style="width:auto">
        <option value="kubespray"${provider === "kubespray" ? " selected" : ""}>kubespray</option>
        <option value="talos"${provider === "talos" ? " selected" : ""}>talos</option>
      </select>
      <span id="dep-edit-fields" style="display:inline-flex;gap:.5rem;flex-wrap:wrap;align-items:center">${depEditFieldsHtml(provider)}</span>
      <button class="secondary btn-sm" id="dep-save" type="button" ${gate(canRun(), "operator")}>Save deployment</button>
    </div>
  </div>`;

  el("dep-edit-provider").addEventListener("change", () => {
    el("dep-edit-fields").innerHTML = depEditFieldsHtml(el("dep-edit-provider").value);
  });
  el("dep-save").addEventListener("click", () => saveDeploymentSection(envId));
  if (provider === "talos") {
    const hint = document.createElement("div");
    hint.className = "muted";
    hint.style.cssText = "font-size:.72rem;margin-top:.35rem;max-width:48rem";
    hint.innerHTML = imageUrl
      ? "OVH BYOI uses this factory <code>metal-amd64.qcow2</code> (iscsi-tools + util-linux-tools for Longhorn). Blank save writes the console factory default."
      : '<span class="error">No image URL — BYOI cannot reinstall until you save a factory qcow2 URL.</span>';
    el("dep-body").appendChild(hint);
    const extra = document.createElement("div");
    extra.className = "muted";
    extra.style.cssText = "font-size:.72rem;margin-top:.35rem;max-width:48rem;line-height:1.45";
    extra.innerHTML =
      "<div>kube-ovn IFACE = private NIC VLAN iface (<code>….100</code>), not the untagged parent.</div>" +
      "<div>Confirm install disk (Rise often NVMe, not <code>/dev/sda</code>).</div>" +
      "<div>After public firewall lockdown, console CIDR must be in <code>talos.public_management_cidrs</code>.</div>";
    el("dep-body").appendChild(extra);
  }
}

function depEditFieldsHtml(provider) {
  const data = providerState || {};
  const talos = data.talos && typeof data.talos === "object" ? data.talos : {};
  const deploy = data.deploy && typeof data.deploy === "object" ? data.deploy : {};
  if (provider === "talos") {
    const defaultImage = data.default_image_url || "";
    const imageUrl = talos.image_url || defaultImage;
    return `
    <input id="dep-edit-cluster" type="text" value="${esc(talos.cluster_name || "")}" placeholder="cluster name" style="width:11rem" />
    <input id="dep-edit-disk" type="text" value="${esc(talos.install_disk || "/dev/sda")}" placeholder="/dev/sda" style="width:8rem" />
    <input id="dep-edit-image" type="text" value="${esc(imageUrl)}" placeholder="${esc(defaultImage || "https://factory.talos.dev/image/…/metal-amd64.qcow2")}" style="width:min(42rem,100%)" title="Talos Image Factory metal-amd64.qcow2 with iscsi-tools + util-linux-tools" />`;
  }
  return `
  <input id="dep-edit-user" type="text" value="${esc(deploy.ssh_user || "")}" placeholder="ssh user" style="width:8rem" />
  <input id="dep-edit-pass" type="password" value="" placeholder="ssh password (blank keeps current)" style="width:11rem" />`;
}

async function saveDeploymentSection(envId) {
  if (!envId) return;
  const provider = el("dep-edit-provider").value;
  const body = { provider };
  if (provider === "kubespray") {
    const user = el("dep-edit-user") ? el("dep-edit-user").value.trim() : "";
    const pass = el("dep-edit-pass") ? el("dep-edit-pass").value : "";
    body.deploy = { ssh_user: user || null };
    if (pass) body.deploy.ssh_password = pass; // encrypted at rest; blank keeps current
  } else {
    const cluster = el("dep-edit-cluster") ? el("dep-edit-cluster").value.trim() : "";
    if (!cluster) {
      el("dep-err").innerHTML = '<div class="error">Cluster name is required for talos.</div>';
      return;
    }
    if (cluster.length > 253 || !DNS_1123_RE.test(cluster)) {
      el("dep-err").innerHTML = '<div class="error">Cluster name must be a valid k8s dns-1123 name (≤ 253 chars).</div>';
      return;
    }
    const disk = el("dep-edit-disk") ? el("dep-edit-disk").value.trim() : "";
    const image = el("dep-edit-image") ? el("dep-edit-image").value.trim() : "";
    const defaultImage = (providerState || {}).default_image_url || "";
    body.talos = {
      cluster_name: cluster,
      install_disk: disk || "/dev/sda",
      image_url: image || defaultImage || null,
    };
  }
  el("dep-err").innerHTML = "";
  const saveBtn = el("dep-save");
  saveBtn.disabled = true;
  saveBtn.textContent = "Saving…";
  try {
    const res = await api(`/api/v1/environments/${encodeURIComponent(envId)}/config/provider`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
    const warnings = res && Array.isArray(res.warnings) ? res.warnings : [];
    toast(
      res && res.version != null ? `Deployment saved → v${res.version}` : "Deployment saved",
      "ok"
    );
    if (warnings.length) {
      el("dep-err").innerHTML = `<div class="hint" style="color:var(--warn)">warnings:<ul style="margin:.25rem 0 0">${warnings
        .map((w) => `<li>${esc(w)}</li>`)
        .join("")}</ul></div>`;
    }
    await loadDeploymentSection(envId);
  } catch (e) {
    el("dep-err").innerHTML = `<div class="error">Deployment save failed: ${esc(e.message)}</div>`;
    toast(`Deployment save failed: ${e.message}`, "error");
    if (e.status === 403) toast("Insufficient role: operator required", "bad");
  } finally {
    const b = el("dep-save");
    if (b) {
      b.disabled = false;
      b.textContent = "Save deployment";
    }
  }
}

// ---------- BYOI reinstall panel (talos + OVH servers only) ----------

const BYOI_POLL_MS = 5000;
let byoiTimer = null;

function hideByoiCard() {
  const card = el("byoi-card");
  if (!card) return;
  card.style.display = "none";
  if (byoiTimer) {
    clearTimeout(byoiTimer);
    byoiTimer = null;
  }
  byoiServers = [];
  byoiSelected = new Set();
  el("byoi-msg").textContent = "";
  el("byoi-err").innerHTML = "";
}

async function loadByoiSection(envId) {
  const card = el("byoi-card");
  if (!card) return;
  if ((providerState || {}).provider !== "talos") {
    hideByoiCard();
    return;
  }
  const ovhEnv = (providerState || {}).infra === "ovh";
  let servers = [];
  let ovhBound = ovhEnv;
  try {
    const data = await api(`/api/v1/environments/${encodeURIComponent(envId)}/servers`);
    servers = Array.isArray(data) ? data : data && Array.isArray(data.servers) ? data.servers : [];
    if (data && !Array.isArray(data) && data.ovh_bound) ovhBound = true;
  } catch {
    if (!ovhEnv) {
      if (deploymentEnvId === envId) hideByoiCard();
      return;
    }
  }
  if (deploymentEnvId !== envId) return;
  if (ovhBound && canRun()) {
    try {
      const adopted = await api(`/api/v1/environments/${encodeURIComponent(envId)}/ovh/adopt`, {
        method: "POST",
        body: "{}",
      });
      if (adopted && Array.isArray(adopted.adopted) && adopted.adopted.length) {
        const data2 = await api(`/api/v1/environments/${encodeURIComponent(envId)}/servers`);
        servers = Array.isArray(data2)
          ? data2
          : data2 && Array.isArray(data2.servers)
            ? data2.servers
            : servers;
      }
    } catch {
      /* 403/503 — still show the card; BYOI job will resolve by IP */
    }
  }
  if (deploymentEnvId !== envId) return;
  const ovh = servers.filter((s) => s && s.source === "ovh");
  if (!ovhBound && !ovh.length) {
    hideByoiCard();
    return;
  }
  byoiServers = ovh;
  byoiSelected = new Set(ovh.map((s) => s.hostname || s.service_name));
  card.style.display = "";
  renderByoiSection(envId);
}

function renderByoiSection(envId) {
  const rows = byoiServers
    .map((s) => {
      const id = s.hostname || s.service_name;
      const checked = byoiSelected.has(id) ? " checked" : "";
      return `<label class="check" style="margin-top:.3rem">
      <input type="checkbox" class="byoi-sel" data-id="${esc(id)}"${checked} ${gate(canRun(), "operator")} />
      <span><strong>${esc(s.hostname || "?")}</strong> <span class="muted" style="font-size:.75rem">· ${esc(s.service_name || "—")} · os: ${esc(s.os || "—")}</span></span>
    </label>`;
    })
    .join("");
  const imageUrl = ((providerState || {}).talos || {}).image_url || "";
  el("byoi-body").innerHTML = `
  <div class="hint" style="font-size:.78rem;margin:.4rem 0">Deploy already reinstalls nodes that are not answering Talos :50000. Use this only to force a wipe of selected Rise boxes.</div>
  <div class="hint" style="font-size:.75rem;margin:.3rem 0;color:var(--warn,#ffc107)">Re-run <strong>Connect</strong> on the OVH account (Admin → OVH) if BYOI returns 403 — the key must allow POST reinstall.</div>
  <div style="font-size:.78rem;margin:.4rem 0">Image URL: ${
    imageUrl
      ? `<code>${esc(imageUrl)}</code>`
      : '<span class="error">not set — save a Talos factory URL (prefer metal-amd64.qcow2) on the Deployment card</span>'
  }</div>
  <div id="byoi-tpl-wrap" class="muted" style="margin:.5rem 0">${rows ? "Loading templates…" : "No OVH servers in inventory yet — Import from OVH on the Inventory tab."}</div>
  <div id="byoi-servers">${rows}</div>
  <div id="byoi-status" style="margin-top:.5rem"></div>`;
  el("byoi-body")
    .querySelectorAll(".byoi-sel")
    .forEach((cb) => {
      cb.addEventListener("change", () => {
        if (cb.checked) byoiSelected.add(cb.dataset.id);
        else byoiSelected.delete(cb.dataset.id);
      });
    });
  loadByoiTemplates(envId);
}

function byoiTemplateRank(t) {
  if (/byoi/i.test(t)) return 2;
  if (/talos/i.test(t)) return 1;
  return 0;
}

function byoiTemplateLabel(t) {
  if (/byoi/i.test(t)) return `${t} — Talos (custom)`;
  if (/talos/i.test(t)) return `${t} — Talos`;
  return t;
}

// The installable list comes from two OVH endpoints that vary by region:
// hardware_templates (osAvailabilities, empty on some regions like US) and
// compatible (install/compatibleTemplates, a category->list mapping). Merge
// both, de-duplicating, so the dropdown is populated either way.
function collectByoiTemplates(data) {
  const out = [];
  const seen = new Set();
  const add = (t) => {
    const s = String(t);
    if (s && !seen.has(s)) {
      seen.add(s);
      out.push(s);
    }
  };
  const hw = data && Array.isArray(data.hardware_templates) ? data.hardware_templates : [];
  hw.forEach(add);
  const comp = data && data.compatible && typeof data.compatible === "object" ? data.compatible : {};
  for (const value of Object.values(comp)) {
    if (Array.isArray(value)) value.forEach(add);
    else if (typeof value === "string") add(value);
  }
  return out;
}

async function loadByoiTemplates(envId) {
  const wrap = el("byoi-tpl-wrap");
  if (!wrap) return;
  const first = byoiServers[0] || {};
  const svc = first.service_name || first.hostname;
  if (!svc) return;
  let templates;
  try {
    const data = await api(
      `/api/v1/environments/${encodeURIComponent(envId)}/ovh/templates?server=${encodeURIComponent(svc)}`
    );
    templates = collectByoiTemplates(data);
  } catch (e) {
    if (deploymentEnvId !== envId) return;
    wrap.innerHTML = `<div class="error">Templates unavailable: ${esc(e.message)}</div>`;
    return;
  }
  if (deploymentEnvId !== envId) return;
  if (!templates.length) {
    wrap.innerHTML = '<div class="muted">No image templates available for this server.</div>';
    return;
  }
  if (!templates.some((t) => /byoi/i.test(t))) templates.push("byoi_64");
  const sorted = templates.slice().sort((a, b) => byoiTemplateRank(b) - byoiTemplateRank(a));
  wrap.innerHTML = `
  <label class="field" style="margin:0;width:auto">
    <span>Image template</span>
    <select id="byoi-template" style="width:auto;min-width:16rem">
      ${sorted.map((t) => `<option value="${esc(t)}">${esc(byoiTemplateLabel(t))}</option>`).join("")}
    </select>
  </label>`;
}

function startByoiReinstall(envId) {
  const card = el("byoi-card");
  if (!card) return;
  const btn = el("byoi-reinstall");
  const msg = el("byoi-msg");
  el("byoi-err").innerHTML = "";
  const selected = byoiServers.filter((s) => byoiSelected.has(s.hostname || s.service_name));
  if (!selected.length) {
    toast("Select at least one server", "warn");
    return;
  }
  const tplSel = el("byoi-template");
  const template = tplSel ? tplSel.value : "byoi_64";
  const imageUrl = ((providerState || {}).talos || {}).image_url || "";
  if (!imageUrl) {
    toast("Set talos.image_url on the Deployment card first", "warn");
    return;
  }
  const hosts = selected.map((s) => s.hostname).filter(Boolean);
  if (
    !confirm(
      `Reinstall ${hosts.length} server(s) with Talos image?\n\n${imageUrl}\n\nOS template: ${template}\n${hosts.join(", ")}\n\nThis wipes the selected servers and waits until Talos is up on :50000.`
    )
  ) {
    return;
  }
  btn.disabled = true;
  msg.textContent = "Creating reinstall job…";
  api(`/api/v1/environments/${encodeURIComponent(envId)}/ovh/byoi`, {
    method: "POST",
    body: JSON.stringify({
      operating_system: template,
      server_hostnames: hosts,
      image_url: imageUrl,
      wait: true,
    }),
  })
    .then((job) => {
      if (!el("byoi-card") || deploymentEnvId !== envId) {
        // Env switched while the job was being created — drop the button lock.
        const b = el("byoi-reinstall");
        if (b) b.disabled = !canRun();
        return;
      }
      const id = job && (job.id != null || job.job_id != null) ? String(job.id || job.job_id) : "";
      if (!id) {
        msg.textContent = "reinstall job created (no job ID)";
        btn.disabled = !canRun();
        return;
      }
      toast(`BYOI job ${id.slice(0, 8)}… created`, "ok");
      pollByoiJob(envId, id);
    })
    .catch((e) => {
      msg.textContent = "";
      el("byoi-err").innerHTML = `<div class="error">BYOI failed: ${esc(e.message)}</div>`;
      toast(`BYOI failed: ${e.message}`, "error");
      if (e.status === 403) toast("Insufficient role: operator required", "bad");
      btn.disabled = !canRun();
    });
}

function pollByoiJob(envId, jobId) {
  if (byoiTimer) clearTimeout(byoiTimer);
  const btn = el("byoi-reinstall");
  api(`/api/v1/jobs/${encodeURIComponent(jobId)}`)
    .then((job) => {
      if (!el("byoi-card") || deploymentEnvId !== envId) return; // navigated away / env switch
      const status = String(job.status || "").toLowerCase();
      el("byoi-status").innerHTML = `
      <span class="pill ${status === "success" ? "ok" : status === "failed" ? "bad" : "warn"}">${esc(status)}</span>
      <span class="muted" style="font-size:.78rem">job ${esc(jobId.slice(0, 8))}…</span>
      ${job.error ? `<span class="muted" style="font-size:.75rem">— ${esc(job.error)}</span>` : ""}
      <a href="#/activity?tab=jobs&job=${esc(jobId)}" style="font-size:.78rem">View job log</a>`;
      if (status === "queued" || status === "running") {
        byoiTimer = setTimeout(() => pollByoiJob(envId, jobId), BYOI_POLL_MS);
      } else {
        if (btn) btn.disabled = !canRun();
        el("byoi-msg").textContent = status === "success" ? "reinstall finished" : `reinstall ${status}`;
        toast(
          `BYOI reinstall ${status === "success" ? "succeeded" : status || "finished"}`,
          status === "success" ? "ok" : "bad"
        );
        if (status === "success") loadByoiSection(envId);
      }
    })
    .catch(() => {
      if (deploymentEnvId !== envId) return; // env switched — drop the retry
      // Transient fetch failure: keep the button disabled and retry next tick.
      byoiTimer = setTimeout(() => pollByoiJob(envId, jobId), BYOI_POLL_MS);
    });
}

// ---------- pre-deploy preflight ----------
// host.preflight runs the ansible host_preflight.yml playbook against the
// inventory hosts (local/ssh). Non-mutating, operator-gated — same jobs API
// the Deploy button uses; result surfaces as a pass/fail badge.

const PREFLIGHT_POLL_MS = 4000;
let preflightTimer = null;
let preflightJobId = "";

function setPreflightBadge(html) {
  const badge = el("cfg-preflight-badge");
  if (badge) badge.innerHTML = html;
}

async function runPreflight(envId) {
  if (!envId) return;
  const msg = el("cfg-msg");
  const err = el("cfg-err");
  const btn = el("cfg-btn-preflight");
  err.innerHTML = "";
  msg.textContent = "Creating preflight job…";
  btn.disabled = true;
  btn.textContent = "Running preflight…";
  setPreflightBadge('<span class="pill warn">preflight running…</span>');
  try {
    const job = await api(`/api/v1/environments/${encodeURIComponent(envId)}/jobs`, {
      method: "POST",
      body: JSON.stringify({ operation: "host.preflight", params: {} }),
    });
    const id = job && job.id != null ? String(job.id) : "";
    if (!id) {
      setPreflightBadge('<span class="pill">preflight job created (no job ID)</span>');
      msg.textContent = "preflight job created";
      return;
    }
    preflightJobId = id;
    msg.innerHTML = `preflight job created — <a href="#/activity?tab=jobs&job=${esc(id)}">view job ${esc(id.slice(0, 8))}…</a>`;
    toast(`Preflight job ${id.slice(0, 8)}… created`, "ok");
    pollPreflightJob(id);
  } catch (e) {
    msg.textContent = "";
    err.innerHTML = `<div class="error">Preflight failed: ${esc(e.message)}</div>`;
    setPreflightBadge("");
    toast(`Preflight failed: ${e.message}`, "error");
    if (e.status === 403) toast("Insufficient role: operator required", "bad");
  } finally {
    btn.disabled = false;
    btn.textContent = "Run preflight";
  }
}

function pollPreflightJob(jobId) {
  if (preflightTimer) clearTimeout(preflightTimer);
  api(`/api/v1/jobs/${encodeURIComponent(jobId)}`)
    .then((job) => {
      const status = String(job.status || "").toLowerCase();
      const link = `<a href="#/activity?tab=jobs&job=${esc(jobId)}">job ${esc(jobId.slice(0, 8))}…</a>`;
      if (status === "queued" || status === "running") {
        setPreflightBadge(`<span class="pill warn">preflight ${esc(status)}…</span> ${link}`);
        preflightTimer = setTimeout(() => pollPreflightJob(jobId), PREFLIGHT_POLL_MS);
      } else if (status === "success") {
        setPreflightBadge(`<span class="pill ok">preflight ok</span> ${link}`);
        toast("Preflight ok", "ok");
      } else {
        setPreflightBadge(`<span class="pill bad">preflight failed — review before deploying</span> ${link}`);
        toast("Preflight failed — review before deploying", "error");
      }
    })
    .catch(() => {
      if (preflightJobId === jobId) preflightTimer = setTimeout(() => pollPreflightJob(jobId), PREFLIGHT_POLL_MS);
    });
}

export function destroyConfigCard() {
  for (const t of pollTimers.values()) clearTimeout(t);
  pollTimers.clear();
  if (preflightTimer) {
    clearTimeout(preflightTimer);
    preflightTimer = null;
  }
  preflightJobId = "";
  if (byoiTimer) {
    clearTimeout(byoiTimer);
    byoiTimer = null;
  }
  providerState = null;
  deploymentEnvId = "";
  byoiServers = [];
  byoiSelected = new Set();
}
