// pages/environment_servers.js — server inventory card for the environment detail page.
// Saved servers render in an Inventory table with role checkboxes, Save, and Remove.
// Add a host by address, or import OVH dedicated servers. Talos is installed from
// the network by this console. Failures degrade to an "unavailable" note.
import { api, esc, toast } from "../api.js";
import { canAdmin, canRun, gate } from "../store.js";
import { ROLES, ROLE_LABELS } from "../roles.js";
import { clearOvhPoll } from "../ovh.js";

// Topology presets — replace raw role checkboxes for the add-host form.
// Each preset maps to a set of roles.  The UI shows human-readable labels.
const TOPOLOGY_PRESETS = {
  aio: {
    label: "All-in-One (single node)",
    desc: "Everything on one host: K8s, etcd, OpenStack, compute, storage",
    roles: ["k8s_control_plane", "etcd", "control", "compute", "network", "storage"],
  },
  control: {
    label: "Control Plane",
    desc: "K8s control + etcd + OpenStack control",
    roles: ["k8s_control_plane", "etcd", "control"],
  },
  worker: {
    label: "Worker (compute + storage)",
    desc: "Runs workloads and persistent storage",
    roles: ["compute", "storage"],
  },
  storage: {
    label: "Storage only",
    desc: "Dedicated storage node (longhorn)",
    roles: ["storage"],
  },
  custom: {
    label: "Custom",
    desc: "Manually select individual roles",
    roles: [],
  },
};

// Minimum roles a valid genestack cluster needs across all its nodes.
const REQUIRED_ROLE_SETS = [
  { label: "K8s control plane", role: "k8s_control_plane", min: 1 },
  { label: "etcd", role: "etcd", min: 1 },
  { label: "OpenStack control", role: "control", min: 1 },
  { label: "Worker (compute)", role: "compute", min: 1 },
  { label: "Storage", role: "storage", min: 1 },
];

let serversLoadedEnvId = ""; // env the card last rendered — guards against env switches
let adoptingOvh = false; // re-entrancy guard for auto-adopt on load
let vrackPollTimer = null; // ovh.vrack.attach job poll

export function serversCardHtml() {
  return `
<style>
#srv-card .toolbar { display:flex; align-items:center; gap:.5rem; padding-bottom:.4rem; margin-bottom:.3rem; border-bottom:1px solid var(--border, #222); flex-wrap:wrap; }
#srv-card .toolbar h2 { margin:0; font-size:.95rem; font-weight:600; }
#srv-card .card-empty { text-align:center; padding:1.5rem 1rem; color:var(--fg-muted, #666); }
#srv-card .card-empty .empty-icon { font-size:1.5rem; opacity:.3; margin-bottom:.4rem; }
#srv-card .card-empty p { font-size:.78rem; margin:.15rem 0; }
#srv-card .card-empty .empty-hint { font-size:.7rem; color:var(--fg-muted, #555); margin-top:.3rem; }
#srv-card .hint-row { display:flex; align-items:center; gap:.5rem; padding:.3rem 0; margin-bottom:.3rem; font-size:.72rem; color:var(--fg-muted, #666); }
#srv-card .pill { display:inline-block; padding:.15rem .45rem; border-radius:.2rem; font-size:.72rem; font-weight:500; }
#srv-card .pill.ok { background:#1a3a1a; color:#4caf50; }
#srv-card .pill.bad { background:#3a1a1a; color:#ef5350; }
#srv-card .pill.warn { background:#3a3a1a; color:#ffc107; }
#srv-card h3 { font-size:.82rem; font-weight:600; margin:.5rem 0 .3rem; }
#srv-card .add-host-section { margin-top:.5rem; padding-top:.4rem; border-top:1px solid var(--border, #222); }
</style>
<div class="card span-12" id="srv-card">
  <div class="toolbar">
    <h2>OVH infrastructure</h2>
    <span class="muted">Dedicated · vRack · private fabric</span>
    <button class="secondary btn-sm" id="srv-refresh" type="button">Refresh</button>
    <span id="srv-msg" class="muted"></span>
  </div>
  <div class="hint-row">Rise dual-NIC: public management hole, private NIC on the vRack. Talos control-plane / worker roles are assigned here.</div>
  <div id="srv-ovh-banner"></div>
  <div id="srv-vrack"></div>
  <div id="srv-err"></div>
  <h3>Inventory (assigned)</h3>
  <table>
    <thead><tr>
      <th>Hostname</th><th>IP</th><th>SSH user</th><th>Source</th><th>Roles</th><th></th>
    </tr></thead>
    <tbody id="srv-tbody"><tr><td colspan="6">
      <div class="card-empty">
        <div class="empty-icon">⬡</div>
        <p>No servers configured</p>
        <p class="empty-hint">Import Rise boxes from OVH, or add a host below</p>
      </div>
    </td></tr></tbody>
  </table>
  <div class="add-host-section">
    <div id="srv-add-host"></div>
  </div>
  <div id="srv-discovered"></div>
</div>`;
}

export function wireServersCard(getEnvId) {
  const card = document.getElementById("srv-card");

  document.getElementById("srv-refresh").addEventListener("click", () => loadServersCard(getEnvId()));

  // Event delegation: the add-host hint buttons are re-injected on every
  // loadServersCard render, so direct listeners (wired once at page mount)
  // would never land on them.
  card?.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-srv-copy-key],[data-srv-view-key]");
    if (!btn) return;
    if (btn.hasAttribute("data-srv-copy-key")) {
      const envId = getEnvId();
      try {
        const data = await api(`/api/v1/environments/${encodeURIComponent(envId)}/ssh-key/public`);
        if (data?.public_key) {
          navigator.clipboard.writeText(data.public_key).then(() => {
            btn.textContent = "Copied ✓";
            setTimeout(() => { btn.textContent = "Copy public key"; }, 1500);
          }).catch(() => {
            btn.textContent = "Failed";
            setTimeout(() => { btn.textContent = "Copy public key"; }, 1500);
          });
        }
      } catch {
        btn.textContent = "Failed";
        setTimeout(() => { btn.textContent = "Copy public key"; }, 1500);
      }
    } else {
      const tabBtn = document.querySelector('.tab[data-tab="config"]');
      if (tabBtn) tabBtn.click();
    }
  });
}

function roleBoxes(scope, rowIdx, roles) {
  return ROLES.map(
    (r) => `<label class="check" style="margin:0 .6rem 0 0">
      <input type="checkbox" data-scope="${scope}" data-row="${rowIdx}" data-role="${r}"${roles.includes(r) ? " checked" : ""} ${gate(canRun(), "operator")} /> ${ROLE_LABELS[r] || r}
    </label>`
  ).join("");
}

function checkedRoles(scope, rowIdx) {
  const roles = [];
  document
    .querySelectorAll(`#srv-card input[data-scope="${scope}"][data-row="${rowIdx}"][data-role]`)
    .forEach((cb) => {
      if (cb.checked) roles.push(cb.dataset.role);
    });
  return roles;
}

const VRACK_INTERCONNECT = {
  ok: { cls: "ok", label: "interconnected" },
  partial: { cls: "warn", label: "partial" },
  unattached: { cls: "bad", label: "not on vRack" },
  unconfigured: { cls: "warn", label: "vRack not selected" },
  empty: { cls: "", label: "no OVH servers" },
  unknown: { cls: "warn", label: "not checked yet" },
};

function vrackPill(status) {
  const key = String((status && status.interconnect) || "unknown");
  const meta = VRACK_INTERCONNECT[key] || VRACK_INTERCONNECT.unknown;
  return `<span class="pill ${meta.cls}">${esc(meta.label)}</span>`;
}

function vrackOptionLabel(v) {
  const id = (v && v.id) || "";
  const name = String((v && v.name) || "").trim();
  const desc = String((v && v.description) || "").trim();
  if (name && name !== id) {
    return desc && desc !== name ? `${name} — ${desc}` : name;
  }
  if (desc && desc !== id) return desc;
  return id;
}

function fabricSteps(data) {
  const servers = Array.isArray(data.servers) ? data.servers : [];
  const hasServers = servers.length > 0;
  const hasVrack = !!(data.vrack);
  const hasCidr = !!(data.private_cidr);
  const nicsOk =
    hasServers &&
    servers.every((s) => s.private_mac && s.vrack_vni && (s.private_ip || s.public_ip));
  const attached = data.interconnect === "ok";
  const steps = [
    { id: "import", label: "1. Import OVH servers into inventory", done: hasServers },
    { id: "vrack", label: "2. Pick the vRack (name, not just pn- id)", done: hasVrack },
    { id: "vlan", label: "3. Apply defaults (VLAN + private CIDR + .11/.12 IPs)", done: hasVrack && hasCidr },
    { id: "nics", label: "4. Confirm each private NIC (MAC, VNI, private IP)", done: nicsOk },
    { id: "attach", label: "5. Attach private NICs to the vRack", done: attached },
    { id: "deploy", label: "6. Deploy the cluster (Workflow → Deploy)", done: data.interconnect === "ok" },
  ];
  let marked = false;
  return steps.map((s) => {
    const next = !s.done && !marked;
    if (next) marked = true;
    return { ...s, next };
  });
}

function formatCheckedAt(iso) {
  if (!iso) return "never";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return String(iso);
  const ago = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (ago < 15) return "just now";
  if (ago < 60) return `${ago}s ago`;
  if (ago < 3600) return `${Math.round(ago / 60)}m ago`;
  return `${Math.round(ago / 3600)}h ago`;
}

async function loadVrackPanel(envId, opts) {
  const box = document.getElementById("srv-vrack");
  if (!box) return;
  const seed = opts && opts.seed;
  if (seed && box.dataset.dirty !== "1") {
    renderVrackPanel(envId, seed, { checking: true });
  } else if (!box.dataset.ready) {
    // Instant local snapshot (no OVH). Never block the page on the API.
    let cached;
    try {
      cached = await api(`/api/v1/environments/${encodeURIComponent(envId)}/ovh/vrack`);
    } catch (e) {
      if (serversLoadedEnvId !== envId || !document.getElementById("srv-vrack")) return;
      box.innerHTML = `<div class="error" style="font-size:.78rem">vRack status unavailable: ${esc(e.message)}</div>`;
      return;
    }
    if (serversLoadedEnvId !== envId || !document.getElementById("srv-vrack")) return;
    renderVrackPanel(envId, cached || {}, { checking: true });
  }
  // Background live check. Keeps the panel usable while OVH enumerates NICs.
  refreshVrackLive(envId);
}

async function refreshVrackLive(envId) {
  const box = document.getElementById("srv-vrack");
  if (!box) return;
  setVrackChecking(true);
  let data;
  try {
    data = await api(
      `/api/v1/environments/${encodeURIComponent(envId)}/ovh/vrack?refresh=true`
    );
  } catch (e) {
    if (serversLoadedEnvId !== envId || !document.getElementById("srv-vrack")) return;
    setVrackChecking(false);
    const live = document.getElementById("srv-vrack-live");
    if (live) live.innerHTML = `<span class="error">OVH refresh failed: ${esc(e.message)}</span>`;
    return;
  }
  if (serversLoadedEnvId !== envId || !document.getElementById("srv-vrack")) return;
  if (box.dataset.dirty === "1") {
    setVrackChecking(false);
    applyLiveStrip(data || {});
    const msg = document.getElementById("srv-vrack-msg");
    if (msg) msg.textContent = "OVH updated — unsaved NIC edits kept. Refresh after Save to merge.";
    return;
  }
  renderVrackPanel(envId, data || {}, { checking: false });
}

function setVrackChecking(on) {
  const live = document.getElementById("srv-vrack-live");
  if (!live) return;
  live.dataset.checking = on ? "1" : "";
  const spin = live.querySelector("[data-checking]");
  if (spin) spin.style.display = on ? "" : "none";
}

function applyLiveStrip(data) {
  const live = document.getElementById("srv-vrack-live");
  if (!live) return;
  const src = data.source === "live" ? "OVH" : data.source === "cache" ? "cached" : "local";
  live.innerHTML = `${vrackPill(data)}
    <span class="muted" style="font-size:.75rem">Last ${esc(src)} check: ${esc(
      formatCheckedAt(data.checked_at)
    )}</span>
    <span data-checking class="muted" style="font-size:.75rem">checking OVH…</span>`;
}

async function loadVrackPanelFromServers(envId, data) {
  const seed = Object.assign({}, data && data.fabric, data && data.ovh, {
    servers: (data && data.fabric && data.fabric.servers) || [],
  });
  await loadVrackPanel(envId, { seed });
}

function renderVrackPanel(envId, data, opts) {
  const box = document.getElementById("srv-vrack");
  if (!box) return;
  box.dataset.ready = "1";
  box.dataset.dirty = "";
  const checking = !!(opts && opts.checking);
  const vracks = Array.isArray(data.vracks) ? data.vracks.slice() : [];
  const servers = Array.isArray(data.servers) ? data.servers : [];
  const blocks = Array.isArray(data.ip_blocks) ? data.ip_blocks : [];
  const discovered = Array.isArray(data.discovered_vlans) ? data.discovered_vlans : [];
  const suggested = data.suggested_vrack && data.suggested_vrack.id ? data.suggested_vrack : null;
  const defaults = data.defaults && typeof data.defaults === "object" ? data.defaults : {};
  const proposedIps = data.proposed_ips && typeof data.proposed_ips === "object" ? data.proposed_ips : {};
  const selected = data.vrack || (suggested && suggested.id) || "";
  const vlanId = data.vlan_id == null ? defaults.vlan_id ?? 100 : data.vlan_id;
  const cidr = data.private_cidr || defaults.private_cidr || "10.10.0.0/24";
  const attachedN = Array.isArray(data.attached) ? data.attached.length : 0;
  const missingN = Array.isArray(data.missing) ? data.missing.length : 0;
  const listed = new Set(vracks.map((v) => v.id));
  if (selected && !listed.has(selected)) {
    vracks.unshift({ id: selected, name: selected, description: "", selected: true });
  }
  const vrackList = vracks.length
    ? vracks
        .map((v) => {
          const id = v.id || "";
          const isSug = suggested && suggested.id === id;
          const checked = id === selected ? " checked" : "";
          return `<label class="check" style="display:flex;align-items:flex-start;gap:.45rem;margin:.28rem 0;cursor:pointer">
            <input type="radio" name="srv-vrack-choice" value="${esc(id)}"${checked} ${gate(canRun(), "operator")} />
            <span>
              <strong>${esc(vrackOptionLabel(v))}</strong>
              <span class="muted" style="font-size:.72rem"> · ${esc(id)}</span>
              ${isSug ? ' <span class="pill ok">suggested</span>' : ""}
            </span>
          </label>`;
        })
        .join("")
    : '<div class="muted" style="font-size:.78rem">No vRacks on this OVH account yet.</div>';
  const vrackControl = `<input id="srv-vrack-id" type="hidden" value="${esc(selected)}" />`;
  const vlanChips = discovered
    .map(
      (v) =>
        `<button type="button" class="secondary btn-sm" data-vlan="${esc(String(v))}">VLAN ${esc(
          String(v)
        )}${Number(v) === 0 ? " (untagged)" : ""}</button>`
    )
    .join(" ");
  const blockHint = blocks.length
    ? blocks
        .map((b) => {
          const vlan = b.vlan == null ? "" : ` vlan ${b.vlan}`;
          return `${b.ip || "?"}${vlan}`;
        })
        .join(" · ")
    : "";
  const vlanLabel = Number(vlanId) > 0 ? String(vlanId) : "0 untagged";
  const rows = servers.length
    ? servers
        .map((s, i) => {
          const on = s.attached_to
            ? `<span class="pill ok">${esc(s.attached_to)}</span>`
            : data.checked_at
              ? '<span class="pill bad">unattached</span>'
              : '<span class="pill warn">not checked</span>';
          const host = s.hostname || "";
          const nics = Array.isArray(s.nics) ? s.nics : [];
          const nicHint = nics
            .map((n) => `${n.role || n.link_type || "nic"} ${n.mac || ""}`)
            .filter(Boolean)
            .join(" · ");
          return `<tr data-nic-row="${i}" data-host="${esc(host)}">
            <td>
              <strong>${esc(host || "?")}</strong>
              <div class="muted" style="font-size:.7rem">${esc(s.service_name || "")}</div>
              ${nicHint ? `<div class="muted" style="font-size:.68rem">${esc(nicHint)}</div>` : ""}
            </td>
            <td><input data-nic="public_ip" value="${esc(s.public_ip || "")}" placeholder="public IP" style="width:8.5rem" ${gate(canRun(), "operator")} /></td>
            <td><input data-nic="public_mac" value="${esc(s.public_mac || "")}" placeholder="public MAC" style="width:9rem" ${gate(canRun(), "operator")} /></td>
            <td><input data-nic="private_ip" value="${esc(s.private_ip || proposedIps[host] || "")}" placeholder="${esc(
              proposedIps[host] || "10.10.0.x"
            )}" style="width:8.5rem" ${gate(canRun(), "operator")} /></td>
            <td><input data-nic="private_mac" value="${esc(s.private_mac || "")}" placeholder="private MAC" style="width:9rem" ${gate(canRun(), "operator")} /></td>
            <td><input data-nic="vrack_vni" value="${esc(s.vrack_vni || "")}" placeholder="VNI" style="width:8rem" ${gate(canRun(), "operator")} /></td>
            <td class="muted" style="font-size:.75rem">${esc(vlanLabel)}</td>
            <td>${on}</td>
            <td style="white-space:nowrap">
              <button class="secondary btn-sm" type="button" data-nic-save="${i}" ${gate(canRun(), "operator")}>Save NIC</button>
              <button class="secondary btn-sm" type="button" data-nic-attach="${i}" ${gate(canAdmin(), "admin")}>Attach</button>
            </td>
          </tr>`;
        })
        .join("")
    : `<tr><td colspan="9" class="muted">Import OVH servers first (step 1).</td></tr>`;
  const err = data.ok === false && data.error
    ? `<div class="error" style="font-size:.78rem;margin-top:.3rem">${esc(data.error)}</div>`
    : "";
  const steps = fabricSteps(data);
  const next = steps.find((s) => s.next);
  const stepHtml = steps
    .map((s) => {
      const mark = s.done ? "✓" : s.next ? "→" : "·";
      const cls = s.done ? "ok" : s.next ? "" : "muted";
      return `<div class="${cls}" style="font-size:.78rem;margin:.12rem 0">${mark} ${esc(s.label)}</div>`;
    })
    .join("");
  const src = data.source === "live" ? "OVH" : data.source === "cache" ? "cached" : "local";
  box.innerHTML = `
  <div class="ovh-panel" style="border:1px solid var(--border,#ddd);border-radius:6px;padding:.6rem .7rem;margin:.4rem 0 .7rem">
    <div class="row" style="align-items:center;gap:.5rem;flex-wrap:wrap">
      <strong style="font-size:.82rem">vRack fabric</strong>
      <span id="srv-vrack-live" class="row" style="gap:.4rem;align-items:center;flex-wrap:wrap">
        ${vrackPill(data)}
        <span class="muted" style="font-size:.75rem">Last ${esc(src)} check: ${esc(formatCheckedAt(data.checked_at))}</span>
        <span data-checking class="muted" style="font-size:.75rem;${checking ? "" : "display:none"}">checking OVH…</span>
      </span>
      <span class="muted" style="font-size:.75rem">${attachedN} attached${missingN ? ` · ${missingN} missing` : ""}</span>
    </div>
    <div style="margin:.45rem 0 .55rem;padding:.4rem .5rem;background:var(--bg-2,#111);border-radius:4px">
      <div style="font-size:.78rem;font-weight:600;margin-bottom:.2rem">${
        next
          ? `Next: ${esc(next.label.replace(/^\d+\.\s*/, ""))}`
          : "Fabric is ready — Deploy from the Guide tab. Deploy will reinstall boxes that are not yet Talos."
      }</div>
      ${stepHtml}
    </div>
    ${err}
    <h3 style="margin-top:.15rem">vRacks on this account</h3>
    <p class="muted" style="font-size:.75rem;margin:.2rem 0 .35rem">
      Pick the fabric these Rise nodes share. IDs are OVH <code>pn-…</code> service names;
      the label is the name set in OVH (or suggested from this cluster).
      ${
        suggested
          ? `Suggested: <strong>${esc(vrackOptionLabel(suggested))}</strong> (${esc(suggested.id)})${
              suggested.reason ? ` — ${esc(suggested.reason)}` : ""
            }.`
          : ""
      }
    </p>
    <div id="srv-vrack-choices" style="margin:.2rem 0 .5rem">${vrackList}</div>
    ${vrackControl}
    <div class="row" style="flex-wrap:wrap;gap:.5rem;align-items:flex-end">
      <label class="field" style="margin:0;width:auto">
        <span>VLAN tag on private NIC</span>
        <input id="srv-vrack-vlan" type="number" min="0" max="4000" step="1" value="${esc(
          String(vlanId)
        )}" style="width:6rem" ${gate(canRun(), "operator")} />
      </label>
      <label class="field" style="margin:0;width:auto">
        <span>Private CIDR</span>
        <input id="srv-vrack-cidr" type="text" value="${esc(cidr)}" placeholder="10.10.0.0/24" style="width:10rem" ${gate(
          canRun(),
          "operator"
        )} />
      </label>
      <button class="secondary btn-sm" type="button" id="srv-vrack-save" ${gate(canRun(), "operator")}>Save fabric</button>
      <button class="btn-sm" type="button" id="srv-vrack-apply-attach" ${gate(canAdmin(), "admin")}>Apply VLAN + IPs + attach</button>
      <button class="secondary btn-sm" type="button" id="srv-vrack-apply" ${gate(canRun(), "operator")}>Apply defaults + private IPs</button>
      <button class="secondary btn-sm" type="button" id="srv-vrack-attach" ${gate(canAdmin(), "admin")}>Attach all to vRack</button>
      ${
        data.interconnect === "ok"
          ? `<button class="btn-sm" type="button" id="srv-vrack-deploy" ${gate(canAdmin(), "admin")}>Deploy cluster</button>`
          : ""
      }
      <button class="secondary btn-sm" type="button" id="srv-vrack-refresh">Refresh from OVH</button>
    </div>
    <p class="muted" style="font-size:.72rem;margin:.4rem 0 .2rem">
      Greenfield defaults: VLAN ${esc(String(defaults.vlan_id ?? 100))} (0 = untagged, 100+ = 802.1q) · CIDR ${esc(
        defaults.private_cidr || "10.10.0.0/24"
      )} · private IPs start at .11.
      Apply writes those onto every OVH server (skips hosts that already have a private IP). Then Attach plugs the private NIC into the selected vRack.
    </p>
    ${
      vlanChips
        ? `<div class="row" style="margin-top:.25rem;font-size:.75rem;gap:.35rem;flex-wrap:wrap;align-items:center">
            <span class="muted">VLANs looked up on this vRack:</span>${vlanChips}
          </div>`
        : ""
    }
    ${blockHint ? `<div class="muted" style="font-size:.72rem;margin-top:.25rem">IP blocks: ${esc(blockHint)}</div>` : ""}
    <h3 style="margin-top:.65rem">NICs</h3>
    <table style="margin-top:.25rem;width:100%">
      <thead><tr>
        <th>Server</th><th>Public IP</th><th>Public MAC</th><th>Private IP</th><th>Private MAC</th><th>vRack VNI</th><th>VLAN</th><th>vRack</th><th></th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div id="srv-vrack-msg" class="muted" style="font-size:.75rem;margin-top:.35rem"></div>
  </div>`;

  const markDirty = () => {
    box.dataset.dirty = "1";
  };
  box.querySelectorAll("input, select").forEach((el) => el.addEventListener("input", markDirty));
  box.querySelectorAll("input, select").forEach((el) => el.addEventListener("change", markDirty));
  box.querySelectorAll("[data-vlan]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const input = document.getElementById("srv-vrack-vlan");
      if (input) input.value = btn.dataset.vlan;
      markDirty();
    });
  });
  const saveBtn = document.getElementById("srv-vrack-save");
  if (saveBtn) saveBtn.addEventListener("click", () => saveVrackFabric(envId));
  const applyAttachBtn = document.getElementById("srv-vrack-apply-attach");
  if (applyAttachBtn) applyAttachBtn.addEventListener("click", () => provisionVrackFabric(envId, true));
  const applyBtn = document.getElementById("srv-vrack-apply");
  if (applyBtn) applyBtn.addEventListener("click", () => provisionVrackFabric(envId, false));
  const attachBtn = document.getElementById("srv-vrack-attach");
  if (attachBtn) attachBtn.addEventListener("click", () => attachVrackFabric(envId));
  const deployBtn = document.getElementById("srv-vrack-deploy");
  if (deployBtn) deployBtn.addEventListener("click", () => startFabricDeploy(envId));
  const refreshBtn = document.getElementById("srv-vrack-refresh");
  if (refreshBtn) refreshBtn.addEventListener("click", () => refreshVrackLive(envId));
  box.querySelectorAll('input[name="srv-vrack-choice"]').forEach((el) => {
    el.addEventListener("change", () => {
      const hidden = document.getElementById("srv-vrack-id");
      if (hidden) hidden.value = el.value;
      markDirty();
    });
  });
  box.querySelectorAll("[data-nic-save]").forEach((btn) => {
    btn.addEventListener("click", () => saveNicRow(envId, btn.closest("tr")));
  });
  box.querySelectorAll("[data-nic-attach]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const host = btn.closest("tr") && btn.closest("tr").dataset.host;
      attachVrackFabric(envId, host ? [host] : null);
    });
  });
  setVrackChecking(checking);
}

function readVrackForm() {
  const sel = document.getElementById("srv-vrack-id");
  const vlanEl = document.getElementById("srv-vrack-vlan");
  const cidrEl = document.getElementById("srv-vrack-cidr");
  const vlanRaw = vlanEl ? vlanEl.value.trim() : "";
  const vlan = vlanRaw === "" ? NaN : Number(vlanRaw);
  return {
    vrack: sel ? sel.value.trim() : "",
    vlan_id: Number.isFinite(vlan) ? vlan : 100,
    private_cidr: cidrEl ? cidrEl.value.trim() : "",
  };
}

async function startFabricDeploy(envId) {
  if (!envId) return;
  if (
    !confirm(
      "Deploy the cluster?\n\n" +
        "OVH + Talos will BYOI-reinstall any node that is not answering on :50000, then run talosctl bootstrap, then the genestack pipeline.\n\n" +
        "This wipes boxes that are not yet Talos. Deploy stops at the first failing stage; there is no automatic rollback."
    )
  ) {
    return;
  }
  const msg = document.getElementById("srv-vrack-msg");
  const btn = document.getElementById("srv-vrack-deploy");
  if (msg) msg.textContent = "Creating deploy job…";
  if (btn) btn.disabled = true;
  try {
    const job = await api(`/api/v1/environments/${encodeURIComponent(envId)}/jobs`, {
      method: "POST",
      body: JSON.stringify({ operation: "genestack.deploy", params: {} }),
    });
    const id = job && job.id != null ? String(job.id) : "";
    toast(id ? `Deploy job ${id.slice(0, 8)}… created` : "Deploy job created", "ok");
    if (msg) {
      msg.innerHTML = id
        ? `deploy job created — <a href="#/activity?tab=jobs&job=${esc(id)}">view job ${esc(id.slice(0, 8))}…</a>`
        : "deploy job created";
    }
    window.dispatchEvent(new CustomEvent("deploy-job-started", { detail: { envId, jobId: id } }));
  } catch (e) {
    if (msg) msg.textContent = "";
    if (e.status === 409) toast("Deploy blocked by a running mutating job", "bad");
    else toast(`Deploy failed: ${e.message}`, "error");
    if (e.status === 403) toast("Insufficient role: admin required", "bad");
  } finally {
    if (btn) btn.disabled = !canAdmin();
  }
}

async function provisionVrackFabric(envId, attach) {
  const msg = document.getElementById("srv-vrack-msg");
  const body = readVrackForm();
  if (!body.vrack) {
    toast("Pick a vRack first", "warn");
    return;
  }
  if (body.vlan_id < 0 || body.vlan_id > 4000) {
    toast("VLAN must be 0 (untagged) or 1–4000", "warn");
    return;
  }
  const who = attach ? "and attach every private NIC" : "and assign private IPs (.11, .12, …)";
  if (
    !confirm(
      `Apply fabric on ${body.vrack}?\n\nVLAN ${body.vlan_id} · ${body.private_cidr || "10.10.0.0/24"}\nThis ${who}. Existing private IPs are left alone.`
    )
  ) {
    return;
  }
  if (msg) msg.textContent = attach ? "Provisioning and attaching…" : "Applying fabric defaults…";
  try {
    const res = await api(`/api/v1/environments/${encodeURIComponent(envId)}/ovh/vrack/provision`, {
      method: "POST",
      body: JSON.stringify({
        vrack: body.vrack,
        vlan_id: body.vlan_id,
        private_cidr: body.private_cidr || "10.10.0.0/24",
        assign_ips: true,
        attach: !!attach,
      }),
    });
    toast(res.message || "Fabric defaults applied", "ok");
    const box = document.getElementById("srv-vrack");
    if (box) box.dataset.dirty = "";
    const jobId = res && (res.job_id != null || res.id != null) ? String(res.job_id || res.id) : "";
    if (jobId) {
      pollVrackJob(envId, jobId);
      return;
    }
    await loadServersCard(envId);
  } catch (e) {
    if (msg) msg.textContent = "";
    toast(`Provision failed: ${e.message}`, "bad");
  }
}

async function saveVrackFabric(envId) {
  const msg = document.getElementById("srv-vrack-msg");
  const body = readVrackForm();
  if (body.vlan_id < 0 || body.vlan_id > 4000) {
    toast("VLAN must be 0 (untagged) or 1–4000", "warn");
    return;
  }
  if (msg) msg.textContent = "Saving…";
  try {
    await api(`/api/v1/environments/${encodeURIComponent(envId)}/ovh/vrack`, {
      method: "PUT",
      body: JSON.stringify({
        vrack: body.vrack || null,
        vlan_id: body.vlan_id,
        private_cidr: body.private_cidr || null,
      }),
    });
    toast("vRack fabric saved", "ok");
    const box = document.getElementById("srv-vrack");
    if (box) box.dataset.dirty = "";
    await refreshVrackLive(envId);
  } catch (e) {
    if (msg) msg.textContent = "";
    toast(`Save fabric failed: ${e.message}`, "bad");
  }
}

async function attachVrackFabric(envId, hostnames) {
  const msg = document.getElementById("srv-vrack-msg");
  const body = readVrackForm();
  const vrack = body.vrack;
  if (!vrack) {
    toast("Select a vRack and Save fabric first", "warn");
    return;
  }
  const who = hostnames && hostnames.length ? hostnames.join(", ") : "every OVH server in this environment";
  if (
    !confirm(
      `Attach ${who} to ${vrack}?\n\nRise boxes plug the private NIC (VNI) into the vRack. VLAN ${body.vlan_id} is applied later at Talos apply-config.`
    )
  ) {
    return;
  }
  const btn = document.getElementById("srv-vrack-attach");
  if (btn) btn.disabled = true;
  if (msg) msg.textContent = "Creating attach job…";
  const payload = { vrack };
  if (hostnames && hostnames.length) payload.server_hostnames = hostnames;
  try {
    const job = await api(`/api/v1/environments/${encodeURIComponent(envId)}/ovh/vrack/attach`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const id = job && (job.job_id != null || job.id != null) ? String(job.job_id || job.id) : "";
    if (!id) {
      if (msg) msg.textContent = "attach job created (no job ID)";
      if (btn) btn.disabled = !canAdmin();
      return;
    }
    toast(`vRack attach job ${id.slice(0, 8)}… created`, "ok");
    pollVrackJob(envId, id);
  } catch (e) {
    if (msg) msg.textContent = "";
    toast(`vRack attach failed: ${e.message}`, "bad");
    if (e.status === 403) toast("Insufficient role: admin required", "bad");
    if (btn) btn.disabled = !canAdmin();
  }
}

async function pollVrackJob(envId, jobId) {
  const msg = document.getElementById("srv-vrack-msg");
  const btn = document.getElementById("srv-vrack-attach");
  if (!document.getElementById("srv-vrack") || serversLoadedEnvId !== envId) return;
  let job;
  try {
    job = await api(`/api/v1/jobs/${encodeURIComponent(jobId)}`);
  } catch {
    vrackPollTimer = setTimeout(() => pollVrackJob(envId, jobId), 5000);
    return;
  }
  if (!document.getElementById("srv-vrack") || serversLoadedEnvId !== envId) return;
  const status = String((job && job.status) || "").toLowerCase();
  if (status === "queued" || status === "running") {
    if (msg) {
      msg.innerHTML = `${esc(status)}… <a href="#/activity?tab=jobs&job=${esc(jobId)}">job ${esc(
        jobId.slice(0, 8)
      )}…</a>`;
    }
    vrackPollTimer = setTimeout(() => pollVrackJob(envId, jobId), 5000);
    return;
  }
  if (btn) btn.disabled = !canAdmin();
  toast(
    `vRack attach ${status === "success" ? "succeeded" : status || "finished"}`,
    status === "success" ? "ok" : "bad"
  );
  await refreshVrackLive(envId);
}

async function saveNicRow(envId, tr) {
  if (!tr) return;
  const hostname = tr.dataset.host;
  if (!hostname) return;
  const val = (name) => {
    const el = tr.querySelector(`[data-nic="${name}"]`);
    return el ? el.value.trim() : "";
  };
  const msg = document.getElementById("srv-vrack-msg");
  if (msg) msg.textContent = `Saving NICs for ${hostname}…`;
  try {
    await api(`/api/v1/environments/${encodeURIComponent(envId)}/ovh/vrack/nics`, {
      method: "PUT",
      body: JSON.stringify({
        hostname,
        public_ip: val("public_ip") || null,
        public_mac: val("public_mac") || null,
        private_ip: val("private_ip") || null,
        private_mac: val("private_mac") || null,
        vrack_vni: val("vrack_vni") || null,
      }),
    });
    toast(`${hostname}: NIC saved`, "ok");
    const box = document.getElementById("srv-vrack");
    if (box) box.dataset.dirty = "";
    if (msg) msg.textContent = "";
  } catch (e) {
    if (msg) msg.textContent = "";
    toast(`${hostname}: ${e.message}`, "bad");
  }
}

export async function loadServersCard(envId) {
  const tbody = document.getElementById("srv-tbody");
  if (!tbody) return;
  if (envId !== serversLoadedEnvId) {
    if (vrackPollTimer) {
      clearTimeout(vrackPollTimer);
      vrackPollTimer = null;
    }
  }
  serversLoadedEnvId = envId || "";
  const err = document.getElementById("srv-err");
  const msg = document.getElementById("srv-msg");
  const discovered = document.getElementById("srv-discovered");
  err.innerHTML = "";
  discovered.innerHTML = "";
  if (!envId) {
    msg.textContent = "";
    tbody.innerHTML = `<tr><td colspan="6" class="muted">Select an environment.</td></tr>`;
    return;
  }
  msg.textContent = "Loading…";

  let data;
  try {
    data = await api(`/api/v1/environments/${encodeURIComponent(envId)}/servers`);
  } catch (e) {
    msg.textContent = "";
    tbody.innerHTML = `<tr><td colspan="6" class="muted">Unavailable</td></tr>`;
    err.innerHTML = `<div class="error">${esc(e.message)}</div>`;
    return;
  }

  // Contract is an object envelope ({servers, ovh_bound, ...}); tolerate a bare array.
  const servers = Array.isArray(data) ? data : data && Array.isArray(data.servers) ? data.servers : [];
  const ovhBound = !!(data && !Array.isArray(data) && data.ovh_bound);
  msg.textContent = servers.length ? `${servers.length} server(s)` : "No servers in inventory yet.";
  const ovhBanner = document.getElementById("srv-ovh-banner");
  if (ovhBanner) {
    ovhBanner.innerHTML = ovhBound
      ? `<div class="hint-row" style="color:var(--ok,#4caf50)">OVH environment — dedicated servers from the bound account. Talos/Kubernetes/Genestack use the <strong>private NIC</strong> (vRack). The public NIC stays up but the host firewall default-denies the internet edge.</div>`
      : "";
  }
  const vrackBox = document.getElementById("srv-vrack");
  if (vrackBox && !ovhBound) vrackBox.innerHTML = "";

  // OVH-bound env: persist source:ovh on static hosts that match live inventory
  // so Talos BYOI and the YAML agree with the bound account.
  if (ovhBound && canRun() && !adoptingOvh) {
    const needsAdopt = servers.some(
      (s) =>
        s &&
        s.assigned &&
        s.source !== "ovh" &&
        s.source !== "maas" &&
        s.source !== "baremetal" &&
        s.source !== "terraform"
    );
    if (needsAdopt) {
      adoptingOvh = true;
      try {
        const adopted = await api(`/api/v1/environments/${encodeURIComponent(envId)}/ovh/adopt`, {
          method: "POST",
          body: "{}",
        });
        const n = adopted && Array.isArray(adopted.adopted) ? adopted.adopted.length : 0;
        if (n) {
          toast(`${n} server(s) tagged as OVH`, "ok");
          adoptingOvh = false;
          return loadServersCard(envId);
        }
      } catch {
        // 403/503/unbound — inventory still renders; operator can Import from OVH.
      }
      adoptingOvh = false;
    }
  }

  const invRows = servers.filter((s) => s && typeof s === "object" && s.assigned);
  if (ovhBound) {
    const fabric = Object.assign({}, data.fabric || {}, data.ovh || {});
    if (!Array.isArray(fabric.servers) || !fabric.servers.length) {
      fabric.servers = invRows
        .filter((s) => s.source === "ovh" || s.service_name)
        .map((s) => ({
          hostname: s.hostname,
          service_name: s.service_name,
          private_ip: s.private_ip,
          public_ip: s.public_ip,
          private_mac: s.private_mac,
          public_mac: s.public_mac,
          vrack_vni: s.vrack_vni,
          nics: s.nics || [],
          attached_to: s.attached_to,
        }));
    }
    loadVrackPanel(envId, { seed: fabric });
  }
  tbody.innerHTML = invRows.length
    ? invRows
        .map((s, i) => {
          const roles = Array.isArray(s.roles) ? s.roles : [];
          const source = s.source || "static";
          const sub = s.system_id
            ? `<div class="muted" style="font-size:.75rem">${esc(s.system_id)}</div>`
            : source === "ovh" && s.service_name
              ? `<div class="muted" style="font-size:.75rem">${esc(s.service_name)}</div>`
              : "";
          const removeBtn = `<button class="secondary btn-sm" type="button" data-remove="${i}" ${gate(canRun(), "operator")}>Remove</button>`;
          const sourceCls = source === "ovh" || source === "static" || source === "terraform" ? "ok" : "";
          const ipCell = s.private_ip
            ? `${esc(s.private_ip)} <span style="font-size:.7rem">priv</span>` +
              (s.public_ip ? `<div style="font-size:.7rem">${esc(s.public_ip)} pub</div>` : "") +
              (s.private_mac ? `<div style="font-size:.7rem">${esc(s.private_mac)}</div>` : "")
            : esc(s.ip || "—");
          return `<tr data-row="${i}">
            <td><strong>${esc(s.hostname || "?")}</strong>${sub}</td>
            <td class="muted">${ipCell}</td>
            <td class="muted">${esc(s.ssh_user || "—")}</td>
            <td><span class="pill ${sourceCls}">${esc(source)}</span></td>
            <td><div class="row" style="gap:0">${roleBoxes("inv", i, roles)}</div></td>
            <td style="white-space:nowrap">
              <button class="secondary btn-sm" type="button" data-save="${i}" ${gate(canRun(), "operator")}>Save</button>
              ${removeBtn}
              <span class="muted" style="font-size:.75rem" data-save-msg="${i}"></span>
            </td>
          </tr>`;
        })
        .join("")
    : `<tr><td colspan="6" class="muted">No servers in inventory yet.</td></tr>`;

  tbody.querySelectorAll("button[data-save]").forEach((btn) =>
    btn.addEventListener("click", () => saveInventoryRow(envId, invRows[Number(btn.dataset.save)], btn.dataset.save))
  );
  tbody.querySelectorAll("button[data-remove]").forEach((btn) =>
    btn.addEventListener("click", () => removeRow(envId, invRows[Number(btn.dataset.remove)]))
  );

  // Always render the Add host form at top of discovered section.
  // Compute current cluster status across all assigned hosts.
  const clusterStatus = REQUIRED_ROLE_SETS.map((req) => {
    const count = invRows.filter((s) => {
      const roles = Array.isArray(s.roles) ? s.roles : [];
      return roles.includes(req.role);
    }).length;
    return { ...req, count };
  });

  const addHostHtml = `
    <div id="srv-add-host">
      <h3>Add host</h3>
      <div id="cluster-status-bar" style="margin:.3rem 0 .5rem"></div>
      <div class="sshkey-auth-section" style="margin-bottom:.4rem">
        <label style="font-size:.8rem;display:flex;align-items:center;gap:.4rem">
          <input type="radio" name="srv-auth-method" value="key" checked /> SSH Key (environment default)
        </label>
        <label style="font-size:.8rem;display:flex;align-items:center;gap:.4rem;margin-top:.2rem">
          <input type="radio" name="srv-auth-method" value="password" /> Username &amp; Password
        </label>
      </div>
      <div id="srv-add-fields">
        <div class="row" style="flex-wrap:wrap;gap:.5rem;align-items:center">
          <input id="srv-add-hostname" type="text" placeholder="hostname" ${gate(canRun(), "operator")} />
          <input id="srv-add-ip" type="text" placeholder="IP address" ${gate(canRun(), "operator")} />
          <input id="srv-add-ssh-user" type="text" placeholder="SSH user (default: root)" ${gate(canRun(), "operator")} />
          <input id="srv-add-ssh-pass" type="password" placeholder="SSH password" style="display:none" ${gate(canRun(), "operator")} />
        </div>
        <div class="srv-key-hint" style="font-size:.72rem;color:var(--fg-muted,#666);margin:.25rem 0">
          <span>Auth: uses the environment's SSH key.</span>
          <button type="button" data-srv-copy-key style="background:none;border:none;color:var(--accent,#4a9eff);cursor:pointer;font-size:.72rem;padding:0">Copy public key</button>
          <span style="margin-left:.3rem">View on</span>
          <button type="button" data-srv-view-key style="background:none;border:none;color:var(--accent,#4a9eff);cursor:pointer;font-size:.72rem;padding:0">Config tab → SSH Keys</button>
        </div>
        <div style="margin-top:.35rem;font-size:.78rem;color:var(--fg-muted,#888)">Topology</div>
        <div id="srv-topo-select" style="margin-top:.3rem"></div>
        <div id="srv-add-roles" style="margin-top:.5rem;display:none">
          <div class="row" style="flex-wrap:wrap;gap:.5rem;align-items:center">
            ${roleBoxes("add", 0, [])}
            <button class="secondary btn-sm" id="srv-add-btn-custom" type="button" ${gate(canRun(), "operator")}>Add</button>
          </div>
        </div>
        <div id="srv-add-quick" style="margin-top:.5rem">
          <button class="secondary btn-sm" id="srv-add-btn-quick" type="button" ${gate(canRun(), "operator")}>Add</button>
        </div>
      </div>
    </div>`;
  document.getElementById("srv-add-host").innerHTML = addHostHtml;
  // Render cluster status bar
  renderClusterStatus(clusterStatus);
  // Render topology preset selector
  renderTopologySelector();
  // Toggle password field visibility based on auth method
  const authRadios = document.querySelectorAll('input[name="srv-auth-method"]');
  authRadios.forEach(r => r.addEventListener('change', () => {
    const passField = document.getElementById('srv-add-ssh-pass');
    if (passField) passField.style.display = r.value === 'password' && r.checked ? '' : 'none';
  }));
  for (const addBtnId of ["srv-add-btn-custom", "srv-add-btn-quick"]) {
    const b = document.getElementById(addBtnId);
    if (b) b.addEventListener("click", () => addHost(envId, b));
  }

  // Optional OVH dedicated-server import.
  const ovhSection = document.createElement("details");
  ovhSection.id = "srv-ovh-import";
  ovhSection.style.marginTop = ".9rem";
  ovhSection.innerHTML = `
    <summary>Import from OVH</summary>
    <p class="muted" style="font-size:.78rem;margin:.4rem 0">
      List this OVH account's dedicated servers and add selected ones to the
      inventory as <strong>source: ovh</strong>. Roles are suggested from each
      server's specs. Talos BYOI uses this identity.
    </p>
    <div id="srv-ovh-box"></div>`;
  if (ovhBound && !invRows.length) ovhSection.open = true;
  document.getElementById("srv-add-host").appendChild(ovhSection);
  ovhSection.addEventListener("toggle", async () => {
    if (!ovhSection.open) return;
    const box = document.getElementById("srv-ovh-box");
    if (box.dataset.loaded) return;
    box.dataset.loaded = "1";
    await import("../ovh.js").then(async ({ ovhAccountPicker, ovhServerTable, ovhEnvStatus }) => {
      const status = await ovhEnvStatus(envId);
      if (!status.account_id) {
        await new Promise((resolve) =>
          ovhAccountPicker({ envId, box, onPicked: () => { box.innerHTML = ""; ovhServerTable({ envId, box, onUse: importOvh }); resolve(); } })
        );
        return;
      }
      if (!status.has_consumer_key) {
        // Bound, but the account's key is missing: picker lets the operator
        // switch/unbind; a platform admin must Connect the account in Admin.
        box.innerHTML = "";
        await ovhAccountPicker({ envId, box, onPicked: () => mountOvhServers(box) });
        return;
      }
      async function mountOvhServers(b) {
        b.innerHTML = "";
        await ovhServerTable({ envId, box: b, onUse: importOvh });
      }
      await mountOvhServers(box);
    });
  });
  function importOvh(chosen) {
    // Feed selected servers through the same /servers/static path the Add host
    // form uses, then refresh the inventory table.
    (async () => {
      let ok = 0;
      for (const s of chosen) {
        try {
          await api(`/api/v1/environments/${encodeURIComponent(envId)}/servers/static`, {
            method: "POST",
            body: JSON.stringify({
              hostname: s.hostname || s.server_id,
              ip: s.ip || s.private_ip || s.public_ip || null,
              roles: s.roles || [],
              source: "ovh",
              service_name: s.server_id || s.hostname || null,
              public_ip: s.public_ip || null,
              private_ip: s.private_ip || null,
              private_mac: s.private_mac || null,
              vrack_vni: s.vrack_vni || null,
            }),
          });
          ok += 1;
        } catch (e) {
          toast(`${s.hostname || s.server_id}: ${e.message}`, "bad");
        }
      }
      if (ok) {
        toast(`${ok} OVH server(s) imported (source: ovh)`, "ok");
        loadServersCard(envId);
      }
    })();
  }

  discovered.innerHTML = `
      <p class="muted" style="margin:.3rem 0">Import Rise boxes from OVH above, or add a host by hostname and IP. Talos is installed from the network by this console.</p>`;
}

async function saveInventoryRow(envId, server, rowIdx) {
  const info = server && typeof server === "object" ? server : {};
  const err = document.getElementById("srv-err");
  const msgSpan = document.querySelector(`[data-save-msg="${rowIdx}"]`);
  err.innerHTML = "";
  const roles = checkedRoles("inv", rowIdx);
  if (msgSpan) msgSpan.textContent = "saving…";
  const url = `/api/v1/environments/${encodeURIComponent(envId)}/servers/static`;
  const body = {
    hostname: info.hostname || "",
    ip: info.ip || null,
    ssh_user: info.ssh_user || null,
    roles,
    source: info.source || "static",
    service_name: info.service_name || null,
  };
  try {
    await api(url, { method: "POST", body: JSON.stringify(body) });
    toast(`${info.hostname || "server"}: assignment saved`, "ok");
    await loadServersCard(envId);
  } catch (e) {
    if (msgSpan) msgSpan.textContent = "";
    err.innerHTML = `<div class="error">${esc(info.hostname || "server")}: ${esc(e.message)}</div>`;
    if (e.status === 403) toast("Insufficient role: operator required", "bad");
  }
}

async function saveDiscoveredRow(envId, server, rowIdx) {
  const info = server && typeof server === "object" ? server : {};
  const err = document.getElementById("srv-err");
  const msgSpan = document.querySelector(`[data-disc-save-msg="${rowIdx}"]`);
  err.innerHTML = "";
  const roles = checkedRoles("disc", rowIdx);
  if (msgSpan) msgSpan.textContent = "saving…";
  try {
    await api(`/api/v1/environments/${encodeURIComponent(envId)}/servers/assign`, {
      method: "POST",
      body: JSON.stringify({
        system_id: info.system_id || "",
        hostname: info.hostname || "",
        roles,
        ip: info.ip || "",
      }),
    });
    toast(`${info.hostname || info.system_id || "server"}: assignment saved`, "ok");
    await loadServersCard(envId);
  } catch (e) {
    if (msgSpan) msgSpan.textContent = "";
    err.innerHTML = `<div class="error">${esc(info.hostname || info.system_id || "server")}: ${esc(e.message)}</div>`;
    if (e.status === 403) toast("Insufficient role: operator required", "bad");
  }
}

async function removeRow(envId, server) {
  const info = server && typeof server === "object" ? server : {};
  const err = document.getElementById("srv-err");
  err.innerHTML = "";
  try {
    await api(`/api/v1/environments/${encodeURIComponent(envId)}/servers/remove`, {
      method: "POST",
      body: JSON.stringify({ hostname: info.hostname || "" }),
    });
    toast(`${info.hostname || "server"}: removed from inventory`, "ok");
    await loadServersCard(envId);
  } catch (e) {
    err.innerHTML = `<div class="error">${esc(info.hostname || "server")}: ${esc(e.message)}</div>`;
    if (e.status === 403) toast("Insufficient role: operator required", "bad");
  }
}

function renderClusterStatus(statuses) {
  const bar = document.getElementById("cluster-status-bar");
  if (!bar) return;
  bar.innerHTML = statuses
    .map((s) => {
      const ok = s.count >= s.min;
      const icon = ok ? "\u2705" : "\u26A0";
      const cls = ok ? "ok" : "warn";
      return `<span class="pill ${cls}" style="margin:0 .2rem">${icon} ${esc(s.label)}: ${s.count}/${s.min}</span>`;
    })
    .join("");
}

function renderTopologySelector() {
  const container = document.getElementById("srv-topo-select");
  if (!container) return;
  const gateAttr = gate(canRun(), "operator");
  container.innerHTML = Object.entries(TOPOLOGY_PRESETS)
    .map(([key, preset]) =>
      `<label style="display:flex;align-items:flex-start;gap:.4rem;margin-bottom:.2rem;cursor:pointer;font-size:.78rem">
        <input type="radio" name="srv-topology" value="${key}"${key === "control" ? " checked" : ""} style="margin-top:.15rem" ${gateAttr} />
        <div>
          <strong>${esc(preset.label)}</strong>
          <div class="muted" style="font-size:.72rem">${esc(preset.desc)}</div>
        </div>
      </label>`
    )
    .join("");
  // Wire change: show/hide custom role checkboxes, or set roles on the quick-add button.
  container.querySelectorAll('input[name="srv-topology"]').forEach((radio) => {
    radio.addEventListener("change", () => {
      const preset = TOPOLOGY_PRESETS[radio.value];
      const rolesArea = document.getElementById("srv-add-roles");
      const quickArea = document.getElementById("srv-add-quick");
      if (radio.value === "custom") {
        rolesArea.style.display = "";
        quickArea.style.display = "none";
      } else {
        rolesArea.style.display = "none";
        quickArea.style.display = "";
      }
    });
  });
}

export function destroyServersCard() {
  clearOvhPoll();
  adoptingOvh = false;
}

async function addHost(envId, addBtn = null) {
  const card = document.getElementById("srv-card");
  const authMethod = card?.querySelector('input[name="srv-auth-method"]:checked')?.value || "key";
  const err = document.getElementById("srv-err");
  err.innerHTML = "";
  const hostname = (document.getElementById("srv-add-hostname") || {}).value || "";
  const ip = (document.getElementById("srv-add-ip") || {}).value || "";
  const sshUser = (document.getElementById("srv-add-ssh-user") || {}).value || "";
  const sshPass = (document.getElementById("srv-add-ssh-pass") || {}).value || "";
  // Resolve roles from topology preset or manual checkboxes.
  const topoVal = card?.querySelector('input[name="srv-topology"]:checked')?.value || "control";
  let roles;
  if (topoVal === "custom") {
    roles = checkedRoles("add", 0);
  } else {
    const preset = TOPOLOGY_PRESETS[topoVal];
    roles = preset ? preset.roles : ["k8s_control_plane", "etcd", "control"];
  }
  if (!hostname.trim()) {
    err.innerHTML = `<div class="error">Hostname is required. Make sure it's unique and valid.</div>`;
    return;
  }
  if (addBtn) { addBtn.disabled = true; addBtn.textContent = "Adding…"; }
  const body = {
    hostname: hostname.trim(),
    ip: ip.trim() || null,
    ssh_user: sshUser.trim() || null,
    roles,
    ssh_auth_method: authMethod,
  };
  if (authMethod === "password" && sshPass) {
    body.ssh_password = sshPass;
  }
  try {
    await api(`/api/v1/environments/${encodeURIComponent(envId)}/servers/static`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    toast(`${hostname.trim()}: added to inventory`, "ok");
    const passField = document.getElementById("srv-add-ssh-pass");
    if (passField) passField.value = "";
    await loadServersCard(envId);
  } catch (e) {
    let msg = e.message;
    if (e.status === 409) msg = `Hostname "${esc(hostname.trim())}" already exists. Choose a unique name.`;
    else if (e.isNetwork || e.isTimeout) msg = `Failed to add server. Check your connection and try again.`;
    else msg = `Failed to add server: ${msg}`;
    err.innerHTML = `<div class="error">${esc(msg)}</div>`;
    toast(`Failed to add server: ${e.message}`, "error");
    if (e.status === 403) toast("Insufficient role: operator required", "bad");
  } finally {
    if (addBtn) { addBtn.disabled = false; addBtn.textContent = "Add"; }
  }
}
