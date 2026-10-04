// pages/env_wizard.js — guided setup: create an environment + connection + servers.
//
// Reached via #/setup (from the environments page "Guided setup" button or the
// first-run hero). Partial inputs persist in sessionStorage so Back never loses
// work; the entry is cleared once the environment is created.
//
// Flow:
//   Step 0 — Basics (name, region, tier, description)
//   Step 1 — Connection method (This console / Agent / Deploy host SSH)
//            → creates the environment
//            → This console (later): jobs run on the fleet hub, then → Step 2
//            → Agent: triggers install job, polls until done, then → Step 2
//            → Deploy host: PATCHes ssh host, then → Step 2
//   Step 2 — Deployment (provider: talos / kubespray + provider fields)
//   Step 3 — Servers / metal source (Terraform, OVH API, PXE, static SSH, BMC)
//   Step 4 — Summary + "Open environment" → Workflow tab
import { api, esc, toast } from "../api.js";
import { loadEnvs, canRun, gate, store } from "../store.js";
import { ROLES, REQUIRED_ROLES, ROLE_LABELS } from "../roles.js";
import { currentTenantId } from "./tenant.js";
import { setBreadcrumbs } from "../components/breadcrumbs.js";
import { ovhAccountPicker, ovhServerTable, ovhEnvStatus, clearOvhPoll } from "../ovh.js";

export const title = "Guided setup";

const STORAGE = "gs_env_wizard_state";
const STEPS = ["Basics", "Connect", "Deployment", "Servers", "Summary"];
const TIERS = ["dev", "lab", "staging", "prod"];
const INSTALL_POLL_MS = 5000;
const MAX_SERVER_ROWS = 12;
const DEFAULT_VLAN_ID = 100;
const DEFAULT_PRIVATE_CIDR = "10.10.0.0/24";

// Plain-English labels for the checkbox UI. Inventory ids are unchanged.
const ROLE_PLAIN = {
  k8s_control_plane: "control plane",
  etcd: "etcd",
  control: "control",
  compute: "worker",
  network: "network",
  storage: "storage",
};

function rolePlain(r) {
  return ROLE_PLAIN[r] || ROLE_LABELS[r] || r;
}

// agent.install requires the admin role server-side (catalog). Agent and
// deploy-host SSH stay behind Advanced; the community default is this console
// as the fleet hub (conn = "later") so the default path needs no remote agent.

// Metal-source picker on the Servers step. "manual" is a legacy sessionStorage
// value from before Static IPs / SSH was a first-class source.
const METAL_SOURCES = [
  {
    id: "terraform",
    title: "Terraform",
    hint: "AWS, Azure, GCP, or Rackspace. Save keys on Hardware → Providers; plan/apply there after this environment exists.",
  },
  {
    id: "ovh",
    title: "OVH via API",
    hint: "Import dedicated servers from a bound OVH account.",
  },
  {
    id: "pxe",
    title: "PXE",
    hint: "Claim PXE/DHCP sightings on Hardware → Inventory.",
  },
  {
    id: "static",
    title: "Static IPs / SSH",
    hint: "Hostname and IP. Talos from an ISO, or Ubuntu that is already installed. A BMC is not required.",
  },
  {
    id: "bmc",
    title: "BMC / Redfish",
    hint: "Optional. Powers the machine, network-boots it, and opens the console wall.",
  },
];
const TF_KIND_ORDER = ["aws", "azure", "gcp", "rackspace"];
const TF_KIND_LABELS = { aws: "AWS", azure: "Azure", gcp: "GCP", rackspace: "Rackspace" };
const HARDWARE_SOURCES = new Set(["terraform", "pxe", "bmc"]);

function metalSource() {
  const s = state.serverSource;
  if (s === "manual" || !s) return "static";
  return METAL_SOURCES.some((o) => o.id === s) ? s : "static";
}

function metalSourceLabel(src) {
  const id = src || metalSource();
  const found = METAL_SOURCES.find((o) => o.id === id);
  return found ? found.title : "—";
}

function serversNeedInventory() {
  const src = metalSource();
  return src === "ovh" || src === "static";
}

function serversActionLabel() {
  return serversNeedInventory() ? "Add servers" : "Continue";
}

function agentEnabled() {
  return !!store.platformAdmin;
}
function defaultConn() {
  return "later";
}
function effectiveConn() {
  const conn = v("conn");
  if (!conn) return defaultConn();
  if (conn === "agent" && !agentEnabled()) return defaultConn();
  return conn;
}
function createButtonLabel() {
  return effectiveConn() === "later" ? "Create environment" : "Create & connect";
}

let state = loadState();

// Poll timer guard for agent install
let installPollTimer = null;

function loadState() {
  try {
    const s = JSON.parse(sessionStorage.getItem(STORAGE) || "{}");
    return typeof s === "object" && s ? s : {};
  } catch {
    return {};
  }
}
function saveState() {
  sessionStorage.setItem(STORAGE, JSON.stringify(state));
}
function clearState() {
  clearInstallPoll();
  state = {};
  sessionStorage.removeItem(STORAGE);
}

function v(key, fallback = "") {
  const val = state[key];
  return val == null || val === "" ? fallback : String(val);
}

export async function render(root) {
  clearInstallPoll();
  clearOvhPoll();
  if (typeof state.step !== "number") state.step = 0;
  setBreadcrumbs([
    { label: "Environments", href: "#/environments" },
    { label: "New Environment" },
  ]);
  root.innerHTML = `
  <div class="wz-wrap">
    <div class="card">
      <div class="toolbar">
        <h2>Guided setup</h2>
        <a href="#/environments" class="muted" style="font-size:.8rem">← back to environments</a>
      </div>
      <div class="wz-steps" id="wz-steps"></div>
      <div id="wz-body"></div>
      <div id="wz-err"></div>
      <div class="wz-nav" id="wz-nav"></div>
    </div>
  </div>`;
  renderStep();
}

// ---------- cleanup on unload ----------

export function destroy() {
  clearInstallPoll();
}

function clearInstallPoll() {
  if (installPollTimer) {
    clearTimeout(installPollTimer);
    installPollTimer = null;
  }
}

// ---------- step indicator ----------

function stepIndicatorHtml() {
  return STEPS.map((name, i) => {
    // Legacy sessionStorage only: "later" used to skip Deployment + Servers.
    const skipped = state.skipServers && (i === 2 || i === 3);
    const cls = i === state.step
      ? "active"
      : skipped
        ? "skipped"
        : i < state.step
          ? "done"
          : "";
    const mark = skipped
      ? "—"
      : i < state.step
        ? "✓"
        : String(i + 1);
    return `<div class="wz-step ${cls}" data-step="${i}"><span class="wz-num">${mark}</span><span>${esc(name)}</span></div>`;
  }).join("");
}

// ---------- step bodies ----------

function step0Html() {
  return `
  <p class="wz-lead muted">Name the cloud (e.g. <code>lab</code>, <code>prod</code>). Region is optional — the site or geography if you know it.</p>
  <div class="form-grid">
    <label class="field"><span>Name *</span>
      <input id="wz-name" type="text" value="${esc(v("name"))}" autocomplete="off" />
      <div class="hint muted">Required. A short name you'll recognize later, e.g. <code>lab</code> or <code>prod</code>.</div>
    </label>
    <label class="field"><span>Region</span>
      <input id="wz-region" type="text" value="${esc(v("region"))}" autocomplete="off" />
      <div class="hint muted">Optional. Site or geography, e.g. <code>us-east</code>.</div>
    </label>
    <label class="field"><span>Tier</span>
      <select id="wz-tier">
        <option value="">— choose —</option>
        ${TIERS.map((t) => `<option value="${t}"${v("tier") === t ? " selected" : ""}>${t}</option>`).join("")}
      </select>
      <div class="hint muted">How critical this cloud is. Pick <code>dev</code> or <code>lab</code> while you're learning.</div>
    </label>
    <label class="field field-wide"><span>Description</span>
      <input id="wz-description" type="text" value="${esc(v("description"))}" autocomplete="off" />
      <div class="hint muted">Optional one-liner — what this cloud is for.</div>
    </label>
  </div>`;
}

function step1Html() {
  const conn = effectiveConn();
  const admin = agentEnabled();
  const sel = (method) => `id="wz-cx-${method}" type="radio" name="wz-conn" value="${method}"${conn === method ? " checked" : ""}${method === "agent" && !admin ? " disabled" : ""}`;
  const showAdvanced = conn === "agent" || conn === "deployhost";
  return `
  <p class="wz-lead muted">Jobs run on this console host. You do not need a remote agent for the default path.</p>
  <div class="wz-choice">
    <label class="wz-radio${conn === "later" ? " selected" : ""}" data-radio="later">
      <input ${sel("later")} />
      <div>
        <strong>This console <span class="pill ok" style="font-size:.65rem;vertical-align:middle">recommended</span></strong>
        <div class="hint muted">This console is the fleet hub. Jobs run here — no remote agent to install.</div>
      </div>
    </label>
  </div>
  <details id="wz-advanced-conn" style="margin-top:.8rem" ${showAdvanced ? "open" : ""}>
    <summary style="font-size:.78rem;color:var(--fg-muted,#666);cursor:pointer">Advanced: Agent or deploy host SSH</summary>
    <div class="wz-choice" style="margin-top:.3rem">
      <label class="wz-radio${conn === "agent" ? " selected" : ""}${admin ? "" : " disabled"}" data-radio="agent">
        <input ${sel("agent")} />
        <div>
          <strong>Agent${admin ? "" : ' <span class="hint muted" style="font-size:.65rem">(admin required)</span>'}</strong>
          <div class="hint muted">Console SSH-pushes an agent to a host. Agent dials out over WebSocket.</div>
        </div>
      </label>
      <label class="wz-radio${conn === "deployhost" ? " selected" : ""}" data-radio="deployhost">
        <input ${sel("deployhost")} />
        <div>
          <strong>Deploy host SSH <span class="pill warn" style="font-size:.65rem;vertical-align:middle">legacy</span></strong>
          <div class="hint muted">Console SSHs into a deploy host for all operations. Being phased out in favor of agents.</div>
        </div>
      </label>
    </div>
  </details>
  <div id="wz-cx-fields">
    ${connFieldsHtml()}
  </div>`;
}

function connFieldsHtml() {
  const method = effectiveConn();
  if (method === "agent") {
    const disabled = agentEnabled() ? "" : " disabled";
    return `
    <div class="form-grid" style="margin-top:.8rem">
      <label class="field"><span>Host IP or hostname *</span>
        <input id="wz-ag-host" type="text" value="${esc(v("ag_host"))}" placeholder="10.0.0.5 or node01.example.com" autocomplete="off"${disabled} />
      </label>
      <label class="field"><span>SSH user</span>
        <input id="wz-ag-user" type="text" value="${esc(v("ag_user") || "root")}" placeholder="root" autocomplete="off"${disabled} />
      </label>
      <label class="field"><span>SSH port</span>
        <input id="wz-ag-port" type="number" value="${esc(v("ag_port") || "22")}" min="1" max="65535" placeholder="22"${disabled} />
      </label>
      <label class="field"><span>Agent name</span>
        <input id="wz-ag-name" type="text" value="${esc(v("ag_name"))}" placeholder="${esc(v("name") || "")}-agent" autocomplete="off"${disabled} />
      </label>
    </div>`;
  }
  if (method === "deployhost") {
    return `
    <div class="form-grid" style="margin-top:.8rem">
      <label class="field field-wide"><span>Deploy host *</span>
        <input id="wz-dh-host" type="text" value="${esc(v("dh_host"))}" placeholder="user@hostname or just hostname" autocomplete="off" />
      </label>
    </div>`;
  }
  return "";
}

function step2Html() {
  const provider = v("provider", "talos");
  const isTalos = provider !== "kubespray";
  const sel = (p) => `id="wz-prov-${p}" type="radio" name="wz-provider" value="${p}"${provider === p ? " checked" : ""}`;
  return `
  <p class="wz-lead muted">Talos is the operating system this console prefers. Machines that are already waiting from an ISO get a config from here. The console does not power them. Empty machines with a management port can still be network-booted later.</p>
  <div class="wz-choice">
    <label class="wz-radio${isTalos ? " selected" : ""}" data-radio="talos">
      <input ${sel("talos")} />
      <div>
        <strong>Talos <span class="pill ok" style="font-size:.65rem;vertical-align:middle">recommended</span></strong>
        <div class="hint muted">The console applies Talos with talosctl. An ISO you already booted is Talos is already installed. Deploy is OpenStack after Kubernetes is up.</div>
      </div>
    </label>
  </div>
  <div id="wz-dep-fields">${isTalos ? talosFieldsHtml() : ""}</div>
  <details id="wz-advanced-prov" style="margin-top:.8rem" ${isTalos ? "" : "open"}>
    <summary style="font-size:.78rem;color:var(--fg-muted,#666);cursor:pointer">Advanced: Kubespray (Ansible)</summary>
    <div class="wz-choice" style="margin-top:.3rem">
      <label class="wz-radio${!isTalos ? " selected" : ""}" data-radio="kubespray">
        <input ${sel("kubespray")} />
        <div>
          <strong>Kubespray (Ansible)</strong>
          <div class="hint muted">Classic ansible-driven OpenStack + k8s install. Needs SSH access to every node.</div>
        </div>
      </label>
    </div>
    <div id="wz-kubespray-fields">${isTalos ? "" : kubesprayFieldsHtml()}</div>
  </details>`;
}

function kubesprayFieldsHtml() {
  return `
  <div class="form-grid" style="margin-top:.8rem">
    <label class="field"><span>SSH user</span>
      <input id="wz-dep-ssh-user" type="text" value="${esc(v("dep_ssh_user"))}" placeholder="root" autocomplete="off" />
      <div class="hint muted">Applied to every node in this cluster.</div>
    </label>
    <label class="field"><span>SSH password</span>
      <input id="wz-dep-ssh-pass" type="password" value="${esc(v("dep_ssh_pass"))}" autocomplete="off" />
      <div class="hint muted">Stored encrypted on each server. Leave blank for key auth.</div>
    </label>
  </div>`;
}

function talosFieldsHtml() {
  return `
  <div class="form-grid" style="margin-top:.8rem">
    <label class="field"><span>Cluster name *</span>
      <input id="wz-talos-cluster" type="text" value="${esc(state.talos_cluster ?? v("name"))}" placeholder="${esc(v("name")) || "cluster name"}" autocomplete="off" />
      <div class="hint muted">Kubernetes DNS-1123 name (lowercase letters, digits, "-", "."). Prefills from the environment name.</div>
    </label>
    <label class="field"><span>Install disk</span>
      <input id="wz-talos-disk" type="text" value="${esc(v("talos_disk", "/dev/sda"))}" placeholder="/dev/sda" autocomplete="off" />
      <div class="hint muted">A SCSI disk is often /dev/sda. Confirm the name on the machine.</div>
    </label>
    <label class="field"><span>Image URL</span>
      <input id="wz-talos-image" type="text" value="${esc(v("talos_image"))}" placeholder="(leave blank)" autocomplete="off" />
      <div class="hint muted" id="wz-talos-iso">Leave this blank. Boot each guest from the Talos ISO, then return to this console.</div>
    </label>
  </div>`;
}

function step3Html() {
  const src = metalSource();
  const radios = METAL_SOURCES.map((opt) => {
    const checked = src === opt.id;
    return `<label class="wz-radio${checked ? " selected" : ""}" data-metal="${opt.id}">
      <input id="wz-metal-${opt.id}" type="radio" name="wz-metal" value="${opt.id}"${checked ? " checked" : ""} />
      <div>
        <strong>${esc(opt.title)}</strong>
        <div class="hint muted">${esc(opt.hint)}</div>
      </div>
    </label>`;
  }).join("");
  return `
  <p class="wz-lead muted">Start with a hostname and an IP. That covers a virtual machine and any machine with no management port. Boot a Talos ISO on it yourself, or use Ubuntu that is already installed. Terraform, OVH, PXE, and a management port are optional.</p>
  <div class="wz-choice wz-source-grid">${radios}</div>
  <div id="wz-src-panel" class="wz-src-panel">${metalPanelHtml()}</div>`;
}

function metalPanelHtml() {
  const src = metalSource();
  if (src === "terraform") return terraformPanelHtml();
  if (src === "ovh") return ovhPanelHtml();
  if (src === "pxe") return pxePanelHtml();
  if (src === "static") return staticPanelHtml();
  if (src === "bmc") return bmcPanelHtml();
  return `<p class="muted" style="font-size:.85rem;margin:0">Choose a source to continue.</p>`;
}

function terraformPanelHtml() {
  const chips = TF_KIND_ORDER.map((k) => `<span class="chip">${esc(TF_KIND_LABELS[k])}</span>`).join("");
  return `
  <p class="wz-lead muted">Save Terraform keys on <a href="#/hardware?tab=providers">Hardware → Providers</a>, then run plan/apply from there after this environment exists. The wizard does not provision machines.</p>
  <div class="wz-tf-kinds">${chips}</div>
  <div id="wz-tf-accounts" class="muted" style="font-size:.82rem">Loading provider accounts…</div>`;
}

function ovhPanelHtml() {
  const imported = (state.servers || []).some((s) => s && s.service_name);
  const review = imported && (state.servers || []).length
    ? `<p class="hint muted" style="margin:.8rem 0 .4rem">Review roles, then add.</p>
       <div id="wz-servers-list">${state.servers.map((srv, i) => serverRowHtml(srv, i)).join("")}</div>`
    : `<div id="wz-servers-list"></div>`;
  return `<div id="wz-srv-ovh"></div>${review}`;
}

function pxePanelHtml() {
  return `
  <p class="wz-lead muted">PXE-boot and claim nodes from Hardware → Inventory after this environment exists.</p>
  <p style="margin:0"><a href="#/hardware">Hardware → Inventory</a>
    · <a href="#/hardware?tab=discovery">Discovery</a></p>`;
}

function bmcPanelHtml() {
  return `
  <p class="wz-lead muted">A management port is optional. Register one on Hardware → Bare metal when you want the console to power the machine, or claim one from Inventory, after this environment exists.</p>
  <p style="margin:0"><a href="#/hardware?tab=baremetal">Hardware → Bare metal</a>
    · <a href="#/hardware">Inventory</a>
    · <a href="#/hardware?tab=discovery">Discovery</a></p>`;
}

function staticPanelHtml() {
  const servers = Array.isArray(state.servers) && state.servers.length
    ? state.servers
    : [{ hostname: "node01", ip: "", roles: [...REQUIRED_ROLES] }];
  const serversHtml = servers.map((srv, i) => serverRowHtml(srv, i)).join("");
  const showAddBtn = servers.length < MAX_SERVER_ROWS;
  const atCap = servers.length >= MAX_SERVER_ROWS;
  const canRemove = servers.length > 1;
  return `
  <p class="hint muted" style="margin:.2rem 0 .6rem">These machines already have an address. Talos from an ISO is waiting for a config. Ubuntu is already installed. The console does not power them. Mark at least one <strong>control plane</strong> and one <strong>worker</strong>. Required inventory roles: control plane, etcd, and control.</p>
  <div id="wz-servers-list">${serversHtml}</div>
  <div style="display:flex;gap:.4rem;margin-top:.5rem;flex-wrap:wrap;align-items:center">
    ${canRemove ? `<button class="secondary btn-sm" id="wz-srv-remove" type="button">− Remove last</button>` : ""}
    ${showAddBtn ? `<button class="secondary btn-sm" id="wz-srv-add" type="button">+ Add server</button>` : ""}
    ${atCap ? '<span class="hint muted">Add more servers later from the environment detail page.</span>' : ""}
  </div>`;
}

function serverRowHtml(srv, idx) {
  const roles = Array.isArray(srv.roles) ? srv.roles : [];
  const roleChecks = ROLES.map((r) => {
    const checked = roles.includes(r) ? " checked" : "";
    const required = REQUIRED_ROLES.includes(r);
    return `<label style="display:inline-flex;align-items:center;gap:.25rem;font-size:.75rem;margin-right:.6rem">
      <input type="checkbox" class="wz-srv-role" data-idx="${idx}" data-role="${esc(r)}"${checked} />
      ${esc(rolePlain(r))}${required ? ' <span style="color:var(--accent)">*</span>' : ""}
    </label>`;
  }).join("");

  return `
  <div class="wz-srv-row" data-idx="${idx}">
    <div class="form-grid" style="margin-bottom:0">
      <label class="field"><span>Hostname *</span>
        <input class="wz-srv-hostname" data-idx="${idx}" type="text" value="${esc(srv.hostname || "")}" placeholder="node${idx + 1}" autocomplete="off" />
      </label>
      <label class="field"><span>IP</span>
        <input class="wz-srv-ip" data-idx="${idx}" type="text" value="${esc(srv.ip || "")}" placeholder="10.0.0.${10 + idx}" autocomplete="off" />
      </label>
    </div>
    <div class="wz-srv-roles">
      <span style="font-size:.75rem;color:var(--muted)">Roles (control plane / worker):</span>
      ${roleChecks}
    </div>
  </div>`;
}

function step4Html() {
  const envName = v("name");
  const connMethod = v("conn");
  const connDetail = connDetailLabel(connMethod);
  const servers = state.servers || [];
  const allRoles = collectServerRoles(servers);
  const requiredMet = REQUIRED_ROLES.every((r) => (allRoles[r] || 0) > 0);
  const roleChips = ROLES
    .filter((r) => (allRoles[r] || 0) > 0)
    .map((r) => `<span class="chip">${esc(rolePlain(r))}: ${allRoles[r]}</span>`)
    .join("");

  const provider = v("provider", "talos");
  const depDetail =
    provider === "talos"
      ? `Talos — cluster "${state.talos_cluster || v("name") || "—"}"`
      : `Kubespray (Ansible) — ssh user ${v("dep_ssh_user") || "—"}`;
  const src = metalSource();
  const nextHint = metalNextHint(src, provider);

  const tid = currentTenantId();
  const serverCount = servers.length
    ? String(servers.length)
    : HARDWARE_SOURCES.has(src)
      ? "add from Hardware"
      : "—";
  const rows = [
    ["Environment", envName],
    ["Region", v("region") || "—"],
    ["Tier", v("tier") || "—"],
    ["Description", v("description") || "—"],
    ["Connection", connDetail],
    ["Deployment", depDetail],
    ["Metal source", metalSourceLabel(src)],
    ["Servers", serverCount],
    ["Roles covered", roleChips || "—"],
  ];
  if (nextHint) rows.push(["Next", nextHint]);
  if (tid) rows.push(["Tenant", tid]);

  const tableRows = rows
    .map(([k, val]) => {
      const isHtml = typeof val === "string" && val.startsWith("<");
      return `<tr><th style="width:11rem">${esc(k)}</th><td>${isHtml ? val : esc(String(val || "—"))}</td></tr>`;
    })
    .join("");

  const detailUrl = state.envId
    ? "#/environment_detail/" + encodeURIComponent(state.envId) + "?tab=workflow"
    : "#/environments";
  const hostsUrl = state.envId
    ? "#/environment_detail/" + encodeURIComponent(state.envId) + "?tab=platform&ptab=ovh"
    : "#/environments";
  const openHosts = metalSource() === "static";
  const openUrl = openHosts ? hostsUrl : detailUrl;
  const openLabel = openHosts ? "Open Hosts" : "Open guided deploy →";

  return `
  <div class="wz-success" style="text-align:left">
    <h3>✓ Environment <strong>'${esc(envName)}'</strong> is ready</h3>
    <div class="hint-row" id="wz-apply">This environment only logs until you apply it. The switch is on the environment. No file edit and no restart.</div>
    ${HARDWARE_SOURCES.has(src) && !servers.length
      ? '<p class="muted" style="font-size:.85rem;margin:.3rem 0">Metal is added from Hardware after this step.</p>'
      : requiredMet
        ? '<p style="color:var(--ok);font-size:.85rem;margin:.3rem 0">All required roles covered.</p>'
        : '<p style="color:var(--warn);font-size:.85rem;margin:.3rem 0">⚠ Some required roles not yet assigned — add servers from the detail page.</p>'}
    <table class="wz-review"><tbody>${tableRows}</tbody></table>
    <div class="row" style="margin-top:1.2rem">
      <button class="btn-sm" type="button" id="wz-apply-btn" ${gate(canRun(), "operator")}>Apply on this environment</button>
      <a class="hero-cta" href="${esc(openUrl)}">${esc(openLabel)}</a>
      <button class="hero-cta alt" type="button" id="wz-create-another">Create another</button>
    </div>
  </div>`;
}

function wireSummaryApply() {
  const btn = document.getElementById("wz-apply-btn");
  const slot = document.getElementById("wz-apply");
  if (!btn || !state.envId) return;
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      await api(`/api/v1/environments/${encodeURIComponent(state.envId)}`, {
        method: "PATCH",
        body: JSON.stringify({ dry_run: false }),
      });
      if (slot) {
        slot.textContent = "This environment applies. Jobs from here change the machines. Open Hosts and continue in this console.";
      }
      btn.textContent = "This environment applies";
      toast("This environment applies", "ok");
    } catch (e) {
      btn.disabled = false;
      toast(e.message || "Could not apply this environment", "bad");
    }
  });
}

function metalNextHint(src, provider) {
  if (src === "terraform") return "Save Terraform keys on Hardware → Providers, then plan/apply from there.";
  if (src === "pxe") return "Claim PXE nodes on Hardware → Inventory, then continue Workflow.";
  if (src === "bmc") return "Register BMCs on Hardware → Bare metal, then continue Workflow.";
  if (src === "static" && provider === "talos") {
    return "Boot the Talos ISO on each guest. Then Hosts, Talos is already installed. The console does not power them. After Kubernetes is up, Settings, Config, Deploy, start stage infrastructure.";
  }
  if (src === "static") {
    return "Hosts, Already have an OS, records them. Deploy from this console installs Kubernetes and OpenStack over SSH.";
  }
  if (provider === "talos") return "Next: open Workflow, then Deploy. Deploy wipes boxes that are not yet Talos.";
  return null;
}

function connDetailLabel(method) {
  switch (method) {
    case "agent": return `Agent on ${esc(v("ag_host") || "host")}`;
    case "deployhost": return `SSH: ${esc(v("dh_host") || "host")}`;
    case "later": return "This console (fleet hub)";
    default: return "—";
  }
}

function collectServerRoles(servers) {
  const counts = {};
  for (const srv of servers) {
    for (const r of (srv.roles || [])) {
      counts[r] = (counts[r] || 0) + 1;
    }
  }
  return counts;
}

// ---------- capture + navigation ----------

function readInput(id) {
  const el = document.getElementById(id);
  return el ? el.value.trim() : "";
}

function captureStep() {
  if (state.step === 0) {
    state.name = readInput("wz-name");
    state.region = readInput("wz-region");
    state.tier = readInput("wz-tier");
    state.description = readInput("wz-description");
  } else if (state.step === 1) {
    const checked = document.querySelector('input[name="wz-conn"]:checked');
    state.conn = checked ? checked.value : (v("conn") || defaultConn());
    if (state.conn === "agent") {
      state.ag_host = readInput("wz-ag-host");
      state.ag_user = readInput("wz-ag-user") || "root";
      state.ag_port = readInput("wz-ag-port") || "22";
      state.ag_name = readInput("wz-ag-name") || `${v("name")}-agent`;
    } else if (state.conn === "deployhost") {
      state.dh_host = readInput("wz-dh-host");
    }
  } else if (state.step === 2) {
    const checkedProv = document.querySelector('input[name="wz-provider"]:checked');
    state.provider = checkedProv ? checkedProv.value : v("provider", "talos");
    if (state.provider === "kubespray") {
      state.dep_ssh_user = readInput("wz-dep-ssh-user");
      state.dep_ssh_pass = readInput("wz-dep-ssh-pass");
    } else {
      state.talos_cluster = readInput("wz-talos-cluster");
      state.talos_disk = readInput("wz-talos-disk") || "/dev/sda";
      state.talos_image = readInput("wz-talos-image");
    }
  } else if (state.step === 3) {
    const checkedMetal = document.querySelector('input[name="wz-metal"]:checked');
    if (checkedMetal) state.serverSource = checkedMetal.value;
    captureMetalServerRows();
  }
  saveState();
}

function captureServers() {
  const rows = document.querySelectorAll(".wz-srv-row");
  const servers = [];
  rows.forEach((row) => {
    const idx = parseInt(row.dataset.idx, 10);
    const hostnameEl = row.querySelector(`.wz-srv-hostname[data-idx="${idx}"]`);
    const ipEl = row.querySelector(`.wz-srv-ip[data-idx="${idx}"]`);
    const roleEls = row.querySelectorAll(`.wz-srv-role[data-idx="${idx}"]`);
    const roles = [];
    roleEls.forEach((el) => { if (el.checked) roles.push(el.dataset.role); });
    servers.push({
      hostname: hostnameEl ? hostnameEl.value.trim() : "",
      ip: ipEl ? ipEl.value.trim() : "",
      roles,
    });
  });
  state.servers = servers;
}

function captureMetalServerRows() {
  const src = metalSource();
  const rows = document.querySelectorAll(".wz-srv-row");
  if (!rows.length) return;
  if (src !== "static" && src !== "ovh") return;
  const imported = (state.servers || []).some((s) => s && s.service_name);
  if (src === "ovh" && !imported) return;
  const prev = Array.isArray(state.servers) ? state.servers : [];
  captureServers();
  state.servers = (state.servers || []).map((srv, i) => {
    const old = prev[i] || prev.find((p) => p && p.hostname === srv.hostname) || {};
    return Object.assign({}, old, srv);
  });
}

function validateStep() {
  if (state.step === 0) {
    if (!state.name) return "Name is required.";
    if (/^[a-zA-Z0-9][a-zA-Z0-9_-]*$/.test(state.name) === false) return "Name must start with a letter or digit and contain only alphanumerics, hyphens, and underscores.";
    if (state.name.length > 128) return "Name must be at most 128 characters.";
  }
  if (state.step === 1) {
    const checked = document.querySelector('input[name="wz-conn"]:checked');
    if (!checked && (v("ag_host") || v("dh_host"))) return "Choose a connection method";
    if (state.conn === "agent" && !state.ag_host) return "Host IP or hostname is required for agent install.";
    if (state.conn === "deployhost" && !state.dh_host) return "Deploy host is required.";
  }
  if (state.step === 2) {
    if (state.provider === "talos") {
      const cluster = String(state.talos_cluster || "").trim();
      if (!cluster) return "Cluster name is required for Talos.";
      if (cluster.length > 253) return "Cluster name must be at most 253 characters.";
      if (!/^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$/.test(cluster)) return "Cluster name must be a valid k8s dns-1123 name (lowercase letters, digits, '-', '.', must start/end with a letter or digit).";
    }
  }
  if (state.step === 3) {
    const src = metalSource();
    if (!src) return "Choose how metal arrives.";
    if (HARDWARE_SOURCES.has(src)) return null;
    const servers = state.servers || [];
    if (!servers.length) {
      return src === "ovh"
        ? "Pick the dedicated servers to import, or switch to Static IPs / SSH."
        : "Enter at least one hostname.";
    }
    for (const srv of servers) {
      if (!srv.hostname) return `Hostname is required for all servers.`;
    }
    const roles = collectServerRoles(servers);
    const missing = REQUIRED_ROLES.filter((r) => !roles[r]);
    if (missing.length) return `Missing required role(s): ${missing.map(rolePlain).join(", ")}. Every environment needs at least 1 node with each of: ${REQUIRED_ROLES.map(rolePlain).join(", ")}.`;
  }
  return null;
}

function renderStep() {
  const bodyFns = [step0Html, step1Html, step2Html, step3Html, step4Html];
  document.getElementById("wz-steps").innerHTML = stepIndicatorHtml();
  document.getElementById("wz-err").innerHTML = "";
  const body = document.getElementById("wz-body");
  const fn = bodyFns[state.step];
  if (fn) body.innerHTML = fn();

  // Post-render wiring for step-specific UI
  if (state.step === 1) wireConnectStep();
  if (state.step === 2) wireDeploymentStep();
  if (state.step === 3) wireServersStep();
  if (state.step === 4) wireSummaryApply();

  const nav = document.getElementById("wz-nav");
  const isSummary = state.step === 4;

  if (isSummary) {
    nav.classList.add("hidden");
    return;
  }

  nav.classList.remove("hidden");
  const actionBtn = state.step === 1
    ? `<button id="wz-create" type="button" ${gate(canRun(), "operator")}>${esc(createButtonLabel())}</button>`
    : state.step === 3
      ? `<button id="wz-add-servers" type="button" ${gate(canRun(), "operator")}>${esc(serversActionLabel())}</button>`
      : `<button id="wz-next" type="button">Next →</button>`;

  nav.innerHTML = `
    <button class="secondary" id="wz-back" type="button" ${state.step === 0 ? "disabled" : ""}>← Back</button>
    ${actionBtn}`;

  document.getElementById("wz-back").addEventListener("click", () => {
    captureStep();
    state.step = Math.max(0, state.step - 1);
    saveState();
    renderStep();
  });

  const next = document.getElementById("wz-next");
  if (next) {
    next.addEventListener("click", async () => {
      captureStep();
      const err = validateStep();
      if (err) {
        showError(err);
        return;
      }
      if (state.step === 2 && state.envId) {
        try {
          await saveDeploymentConfig(state.envId);
        } catch {
          toast("Deployment config not saved — set it on the environment page", "warn");
        }
      }
      state.step = Math.min(STEPS.length - 1, state.step + 1);
      saveState();
      renderStep();
    });
  }

  const create = document.getElementById("wz-create");
  if (create) create.addEventListener("click", createAndConnect);

  const addBtn = document.getElementById("wz-add-servers");
  if (addBtn) addBtn.addEventListener("click", addServers);

  const again = document.getElementById("wz-create-another");
  if (again) {
    again.addEventListener("click", () => {
      clearState();
      state.step = 0;
      saveState();
      renderStep();
    });
  }
}

function showError(msg) {
  document.getElementById("wz-err").innerHTML = `<div class="error">${esc(msg)}</div>`;
}

function syncCreateButtonLabel() {
  const btn = document.getElementById("wz-create");
  if (btn && !btn.disabled) btn.textContent = createButtonLabel();
}

// ---------- connect step wiring ----------

function wireConnectStep() {
  const radios = document.querySelectorAll('input[name="wz-conn"]');
  const labels = document.querySelectorAll(".wz-radio[data-radio]");

  radios.forEach((radio) => {
    radio.addEventListener("change", () => {
      state.conn = radio.value;
      labels.forEach((l) => {
        l.classList.toggle("selected", l.dataset.radio === radio.value);
      });
      if (radio.value === "agent" || radio.value === "deployhost") {
        const adv = document.getElementById("wz-advanced-conn");
        if (adv) adv.open = true;
      }
      document.getElementById("wz-cx-fields").innerHTML = connFieldsHtml();
      saveState();
      defaultAgentName();
      syncCreateButtonLabel();
    });
  });

  defaultAgentName();
}

function defaultAgentName() {
  const nameInput = document.getElementById("wz-ag-name");
  if (nameInput && !nameInput.value) {
    const base = v("name");
    if (base) nameInput.value = `${base}-agent`;
  }
}

// ---------- deployment step wiring ----------

function wireDeploymentStep() {
  const radios = document.querySelectorAll('input[name="wz-provider"]');
  const labels = document.querySelectorAll(".wz-radio[data-radio]");
  radios.forEach((radio) => {
    radio.addEventListener("change", () => {
      state.provider = radio.value;
      labels.forEach((l) => {
        l.classList.toggle("selected", l.dataset.radio === radio.value);
      });
      const isTalos = radio.value === "talos";
      const fields = document.getElementById("wz-dep-fields");
      const kubeFields = document.getElementById("wz-kubespray-fields");
      if (fields) fields.innerHTML = isTalos ? talosFieldsHtml() : "";
      if (kubeFields) kubeFields.innerHTML = isTalos ? "" : kubesprayFieldsHtml();
      const adv = document.getElementById("wz-advanced-prov");
      if (adv && !isTalos) adv.open = true;
      saveState();
      if (isTalos) fillTalosIso();
    });
  });
  fillTalosIso();
}

async function fillTalosIso() {
  const slot = document.getElementById("wz-talos-iso");
  if (!slot || !state.envId) return;
  try {
    const prov = await api(
      `/api/v1/environments/${encodeURIComponent(state.envId)}/config/provider`
    );
    const iso = prov && prov.default_iso_url;
    if (!iso) return;
    slot.innerHTML = `Leave this blank. Boot each machine from <a href="${esc(iso)}" target="_blank" rel="noopener">${esc(iso)}</a>. Then return here. A SCSI disk is often /dev/sda.`;
  } catch {
    /* The blank hint stays. The first-cluster page has the same ISO. */
  }
}

// ---------- servers step wiring ----------

function wireServersStep() {
  wireMetalSourcePicker();
  wireMetalPanel();
}

function wireMetalSourcePicker() {
  const radios = document.querySelectorAll('input[name="wz-metal"]');
  radios.forEach((radio) => {
    radio.addEventListener("change", () => {
      captureMetalServerRows();
      state.serverSource = radio.value;
      saveState();
      document.querySelectorAll("[data-metal]").forEach((l) => {
        l.classList.toggle("selected", l.dataset.metal === radio.value);
      });
      const panel = document.getElementById("wz-src-panel");
      if (panel) panel.innerHTML = metalPanelHtml();
      wireMetalPanel();
      syncServersActionButton();
    });
  });
}

function wireMetalPanel() {
  const src = metalSource();
  if (src === "terraform") loadTerraformAccounts();
  if (src === "ovh") {
    const ovhBox = document.getElementById("wz-srv-ovh");
    if (ovhBox) mountOvhPanel(ovhBox);
  }
  if (src === "static" || src === "ovh") wireStaticRows();
}

function syncServersActionButton() {
  const btn = document.getElementById("wz-add-servers");
  if (btn && !btn.disabled) btn.textContent = serversActionLabel();
}

function wireStaticRows() {
  const addBtn = document.getElementById("wz-srv-add");
  if (addBtn) {
    addBtn.addEventListener("click", () => {
      captureServers();
      if (!Array.isArray(state.servers)) state.servers = [];
      if (state.servers.length >= MAX_SERVER_ROWS) return;
      state.servers.push({ hostname: `node${state.servers.length + 1}`, ip: "", roles: [...REQUIRED_ROLES] });
      state.serverSource = "static";
      saveState();
      const panel = document.getElementById("wz-src-panel");
      if (panel) panel.innerHTML = staticPanelHtml();
      wireStaticRows();
    });
  }

  const removeBtn = document.getElementById("wz-srv-remove");
  if (removeBtn) {
    removeBtn.addEventListener("click", () => {
      captureServers();
      if ((state.servers || []).length <= 1) return;
      state.servers.pop();
      saveState();
      const panel = document.getElementById("wz-src-panel");
      if (panel) panel.innerHTML = staticPanelHtml();
      wireStaticRows();
    });
  }
}

async function loadTerraformAccounts() {
  const box = document.getElementById("wz-tf-accounts");
  if (!box) return;
  try {
    const res = await api("/api/v1/hardware/accounts");
    if (document.getElementById("wz-tf-accounts") !== box) return;
    const accounts = Array.isArray(res) ? res : (res && res.accounts) || [];
    if (!accounts.length) {
      box.innerHTML = `<p class="muted" style="font-size:.82rem;margin:0">No Terraform accounts stored yet. A platform admin saves keys on <a href="#/hardware?tab=providers">Hardware → Providers</a>. After this environment exists, plan/apply from there.</p>`;
      return;
    }
    const byKind = {};
    accounts.forEach((a) => {
      const k = a.kind || "other";
      (byKind[k] || (byKind[k] = [])).push(a);
    });
    const kindOrder = TF_KIND_ORDER.concat(Object.keys(byKind).filter((k) => !TF_KIND_ORDER.includes(k)));
    const sections = kindOrder
      .filter((k) => byKind[k] && byKind[k].length)
      .map((k) => {
        const rows = byKind[k]
          .map(
            (a) => `<tr>
              <td><strong>${esc(a.name)}</strong></td>
              <td class="muted">${esc(a.region || "—")}</td>
              <td>${a.has_credentials ? '<span class="pill ok">keys stored</span>' : '<span class="muted">no keys</span>'}</td>
            </tr>`
          )
          .join("");
        return `<h4 style="margin:.7rem 0 .3rem;font-size:.85rem">${esc(TF_KIND_LABELS[k] || k)}</h4>
          <table class="tbl" style="width:100%"><thead><tr><th>Account</th><th>Region</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
      })
      .join("");
    box.innerHTML = `${sections}<p class="muted" style="font-size:.78rem;margin:.7rem 0 0">Plan and apply from <a href="#/hardware?tab=providers">Hardware → Providers</a> after this environment exists.</p>`;
  } catch (e) {
    if (document.getElementById("wz-tf-accounts") !== box) return;
    const st = e && e.status;
    let msg = "Could not list Terraform accounts.";
    if (st === 403) msg = "Listing Terraform accounts is not allowed on this console.";
    if (st === 404) msg = "This console does not list Terraform accounts yet.";
    box.innerHTML = `<p class="muted" style="font-size:.82rem;margin:0">${esc(msg)} Save keys on <a href="#/hardware?tab=providers">Hardware → Providers</a>, then plan/apply from there after this environment exists.</p>`;
  }
}

// Mount the OVH import panel into the wizard's OVH box: first the account
// picker (bind if not yet bound), then the server table (the bound account
// must have an approved consumer key — platform admins Connect it in Admin).
// "Use selected" populates state.servers (manual rows are replaced) so the
// existing validation + /servers/static submit path handles the rest.
async function mountOvhPanel(ovhBox) {
  const envId = state.envId;
  ovhBox.innerHTML = '<div class="muted" style="font-size:.82rem">Checking OVH…</div>';
  if (!envId) {
    ovhBox.innerHTML = '<div class="error">Connect the environment first (previous step).</div>';
    return;
  }
  try {
    const status = await ovhEnvStatus(envId);
    if (!status.account_id) {
      ovhBox.innerHTML = "";
      ovhAccountPicker({ envId, box: ovhBox, onPicked: () => mountOvhPanel(ovhBox) });
      return;
    }
    if (!status.has_consumer_key) {
      ovhBox.innerHTML = "";
      ovhAccountPicker({ envId, box: ovhBox, onPicked: () => mountOvhPanel(ovhBox) });
      return;
    }
    ovhBox.innerHTML = "";
    await ovhServerTable({
      envId,
      box: ovhBox,
      onUse: (chosen) => {
        state.servers = chosen.map((s) => ({
          hostname: s.hostname || s.server_id,
          ip: s.private_ip || s.ip || "",
          roles: [...(s.roles || [])],
          service_name: s.server_id,
          public_ip: s.public_ip || "",
          private_ip: s.private_ip || "",
          private_mac: s.private_mac || "",
          vrack_vni: s.vrack_vni || "",
        }));
        state.serverSource = "ovh";
        saveState();
        const panel = document.getElementById("wz-src-panel");
        if (panel) panel.innerHTML = ovhPanelHtml();
        wireMetalPanel();
        toast(`${chosen.length} OVH server(s) loaded — review roles, then add`, "ok");
      },
    });
  } catch (e) {
    ovhBox.innerHTML = `<div class="error">${esc(e.message)}</div>`;
  }
}

// ---------- create + connect (Step 1 → Step 2) ----------

function buildEnvBody() {
  const orNull = (s) => (s && String(s).trim() ? String(s).trim() : null);
  return {
    name: state.name,
    region: orNull(state.region),
    tier: orNull(state.tier),
    description: orNull(state.description),
    tenant_id: orNull(currentTenantId()),
  };
}

function ensureTalosDefaults() {
  if (!state.provider) state.provider = "talos";
  if (!state.talos_cluster) state.talos_cluster = state.name;
  if (!state.talos_disk) state.talos_disk = "/dev/sda";
}

async function persistProviderAndFabric(envId) {
  try {
    await saveDeploymentConfig(envId);
  } catch {
    toast("Deployment config not saved — set it on the environment page", "warn");
  }
}

async function createAndConnect() {
  captureStep();
  const err = validateStep();
  if (err) { showError(err); return; }

  const btn = document.getElementById("wz-create");
  btn.disabled = true;
  btn.textContent = "Creating…";
  document.getElementById("wz-err").innerHTML = "";

  try {
    // Resume instead of re-POST: a previous partial run already created the
    // environment (state.envId persisted), so GET it to verify it exists and
    // skip creation. Only POST when no environment was created yet.
    let env;
    if (state.envId) {
      env = await api(`/api/v1/environments/${encodeURIComponent(state.envId)}`);
    } else {
      env = await api("/api/v1/environments", {
        method: "POST",
        body: JSON.stringify(buildEnvBody()),
      });
      state.envId = env.id;
      toast(`Environment "${env.name}" created`, "ok");
    }
    ensureTalosDefaults();
    state.skipServers = false;
    saveState();

    // Persist Talos (+ VLAN 100) even if they abandon servers later.
    await persistProviderAndFabric(env.id);

    if (state.conn === "agent") {
      btn.textContent = "Installing agent…";
      await installAgent(env.id, btn);
    } else if (state.conn === "deployhost") {
      btn.textContent = "Configuring deploy host…";
      await patchDeployHost(env.id, btn);
    }

    state.step = 2;
    saveState();
    renderStep();
  } catch (e) {
    showError(e.message);
    btn.disabled = false;
    btn.textContent = createButtonLabel();
    if (e.status === 403) {
      // Name the ACTUAL role the backend requires (detail looks like
      // "Operation 'agent.install' requires role 'admin'").
      const m = String(e.message || "").match(/requires role '([^']+)'/);
      toast(m ? `Insufficient role: ${m[1]} required` : "Insufficient role for this operation", "bad");
    } else if (e.status === 409) {
      toast("An environment with this name already exists from a previous attempt — open it from the environments page and resume there.", "bad");
    }
  }
}

async function installAgent(envId, btn) {
  const host = state.ag_host;
  const sshUser = state.ag_user || "root";
  const sshPort = parseInt(state.ag_port, 10) || 22;
  const name = state.ag_name || `${state.name}-agent`;

  try {
    const job = await api(`/api/v1/environments/${encodeURIComponent(envId)}/jobs`, {
      method: "POST",
      body: JSON.stringify({
        operation: "agent.install",
        params: { host, ssh_user: sshUser, ssh_port: sshPort, name },
      }),
    });

    const jobId = job?.id;
    if (!jobId) {
      toast("Agent install job created (no job ID — skipping poll)", "warn");
      return;
    }

    btn.textContent = `Installing agent on ${esc(host)}…`;
    await pollAgentJob(envId, jobId, btn);
    toast("Agent installed successfully", "ok");
  } catch (e) {
    if (e.status === 400 && /unknown operation/i.test(String(e.message || ""))) {
      toast("Agent push-install not available — deploy host configured instead", "warn");
      await patchDeployHostFallback(envId, btn);
    } else {
      throw e;
    }
  }
}

async function pollAgentJob(envId, jobId, btn) {
  return new Promise((resolve, reject) => {
    function poll() {
      api(`/api/v1/jobs/${encodeURIComponent(jobId)}`)
        .then((job) => {
          const st = String(job?.status || "").toLowerCase();
          if (st === "queued" || st === "running") {
            btn.textContent = `Installing… (${st})`;
            installPollTimer = setTimeout(poll, INSTALL_POLL_MS);
          } else if (st === "success") {
            clearInstallPoll();
            resolve();
          } else {
            clearInstallPoll();
            const errMsg = job?.error || job?.log_text?.slice(-100) || st;
            reject(new Error(`Agent install failed: ${errMsg}`));
          }
        })
        .catch((e) => {
          clearInstallPoll();
          reject(e);
        });
    }
    poll();
  });
}

async function patchDeployHost(envId, btn) {
  const val = state.dh_host;
  const [userPart, ...hostParts] = val.split("@");
  const host = hostParts.join("@") || userPart;
  const user = hostParts.length ? userPart : null;

  await api(`/api/v1/environments/${encodeURIComponent(envId)}`, {
    method: "PATCH",
    body: JSON.stringify({
      deployer_ssh_host: host,
      deployer_ssh_user: user,
    }),
  });
}

async function patchDeployHostFallback(envId, btn) {
  try {
    const val = state.ag_host;
    const user = state.ag_user || "root";
    await api(`/api/v1/environments/${encodeURIComponent(envId)}`, {
      method: "PATCH",
      body: JSON.stringify({
        deployer_ssh_host: val,
        deployer_ssh_user: user,
      }),
    });
    toast("Deploy host configured as fallback", "ok");
  } catch {
    toast("Connection setup skipped — configure from detail page", "warn");
  }
}

// ---------- add servers (Step 3 → Step 4) ----------

async function addServers() {
  captureStep();
  const err = validateStep();
  if (err) { showError(err); return; }

  const btn = document.getElementById("wz-add-servers");
  btn.disabled = true;
  const src = metalSource();
  const skipInventory = HARDWARE_SOURCES.has(src);
  btn.textContent = skipInventory ? "Continuing…" : "Adding servers…";
  document.getElementById("wz-err").innerHTML = "";

  const envId = state.envId;
  if (!envId) {
    showError("No environment ID. Please try again.");
    btn.disabled = false;
    btn.textContent = serversActionLabel();
    return;
  }

  if (skipInventory) {
    try {
      await saveDeploymentConfig(envId);
    } catch {
      toast("Deployment config not saved — set it on the environment page", "warn");
    }
    toast("Environment ready — add metal from Hardware when you are ready", "ok");
    state.servers = [];
    finishWizardToSummary();
    return;
  }

  const progressEl = document.getElementById("wz-body");
  let progressHtml = '<div id="wz-srv-progress"></div>';
  progressEl.innerHTML = progressHtml;
  const progress = document.getElementById("wz-srv-progress");

  let allOk = true;
  for (const srv of state.servers || []) {
    const line = document.createElement("div");
    line.style.cssText = "font-size:.85rem;padding:.25rem 0;display:flex;align-items:center;gap:.4rem";
    line.innerHTML = `<span>${esc(srv.hostname)}…</span><span class="srv-status">adding…</span>`;
    progress.appendChild(line);
    const statusSpan = line.querySelector(".srv-status");

    const body = {
      hostname: srv.hostname,
      ip: srv.ip || null,
      roles: srv.roles,
    };
    // kubespray: the Deployment step's SSH credentials apply to every node.
    // ssh_password is encrypted at rest by the console (never stored plaintext).
    if (v("provider", "talos") === "kubespray") {
      body.ssh_user = state.dep_ssh_user || null;
      body.ssh_password = state.dep_ssh_pass || null;
    }
    if (state.serverSource === "ovh") body.source = "ovh";
    if (srv.service_name) body.service_name = srv.service_name;
    if (srv.public_ip) body.public_ip = srv.public_ip;
    if (srv.private_ip) body.private_ip = srv.private_ip;
    if (srv.private_ip) body.ip = srv.private_ip;
    if (srv.private_mac) body.private_mac = srv.private_mac;
    if (srv.vrack_vni) body.vrack_vni = srv.vrack_vni;
    try {
      await api(`/api/v1/environments/${encodeURIComponent(envId)}/servers/static`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      statusSpan.innerHTML = '<span style="color:var(--ok)">✓ added</span>';
      statusSpan.className = "srv-status ok";
    } catch (e) {
      statusSpan.innerHTML = `<span style="color:var(--err)">✗ ${esc(e.message)}</span>`;
      statusSpan.className = "srv-status error";
      allOk = false;
    }
  }

  if (allOk) {
    toast(`${state.servers.length} server(s) added`, "ok");
    const depLine = document.createElement("div");
    depLine.style.cssText = "font-size:.85rem;padding:.25rem 0;color:var(--muted)";
    depLine.textContent = "Saving deployment config…";
    progress.appendChild(depLine);
    try {
      await saveDeploymentConfig(envId);
    } catch (e) {
      depLine.textContent = "";
      toast("Deployment config not saved — set it on the environment page", "warn");
    }
  } else {
    toast("Some servers failed — check detail page", "warn");
  }

  finishWizardToSummary({ delayMs: 1200 });
}

function finishWizardToSummary({ delayMs = 0 } = {}) {
  // Keep just enough state to render the Summary (name, servers, env link);
  // wipe the per-step form data so "Create another" starts clean.
  clearInstallPoll();
  state = {
    step: 4,
    envId: state.envId,
    name: state.name,
    region: state.region,
    tier: state.tier,
    description: state.description,
    servers: state.servers,
    serverSource: metalSource() || state.serverSource,
    conn: state.conn,
    ag_host: state.ag_host,
    ag_user: state.ag_user,
    ag_port: state.ag_port,
    ag_name: state.ag_name,
    dh_host: state.dh_host,
    provider: v("provider", "talos"),
    talos_cluster: state.talos_cluster,
    dep_ssh_user: state.dep_ssh_user,
    skipServers: false,
  };
  saveState();
  loadEnvs().catch(() => {});
  if (delayMs > 0) setTimeout(() => renderStep(), delayMs);
  else renderStep();
}

// Persist the wizard's Deployment step via PUT /config/provider. The summary
// row is rendered from state, so a failure must not block the finish flow.
// NOTE: kubespray ssh_password is stored per-server (encrypted) by addServers;
// the deploy: section is pushed to the host as plaintext YAML — never secrets.
// VLAN 100 / 10.10.0.0/24 is written best-effort afterwards so Talos does not
// treat a missing vlan_id as 0 (untagged).
async function saveDeploymentConfig(envId) {
  const provider = v("provider", "talos");
  const body = { provider };
  if (provider === "kubespray") {
    body.deploy = { ssh_user: state.dep_ssh_user || null };
  } else {
    body.talos = {
      cluster_name: state.talos_cluster || state.name,
      install_disk: state.talos_disk || "/dev/sda",
      image_url: state.talos_image || null,
    };
  }
  await api(`/api/v1/environments/${encodeURIComponent(envId)}/config/provider`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
  try {
    await api(`/api/v1/environments/${encodeURIComponent(envId)}/ovh/vrack`, {
      method: "PUT",
      body: JSON.stringify({ vlan_id: DEFAULT_VLAN_ID, private_cidr: DEFAULT_PRIVATE_CIDR }),
    });
  } catch {
    // Swallow 4xx (and anything else) — do not block the wizard.
  }
}

// ---------- legacy compatibility: buildBody kept for external use ----------

function buildBody() {
  return buildEnvBody();
}
