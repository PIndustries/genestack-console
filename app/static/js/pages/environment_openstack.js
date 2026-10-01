// pages/environment_openstack.js — "OpenStack" card on the environment detail
// page. Shows live OpenStack control-plane state from
// GET /api/v1/environments/{id}/openstack: compute services, network agents,
// images, and users. Defensive: the endpoint may 404 (backend not deployed
// yet), return partial payloads, or report available:false — every path
// degrades to a muted note, never a page break.
import { api, esc } from "../api.js";

const USER_CHIP_LIMIT = 20;

let activeEnvId = ""; // guards against stale DOM after navigation/env switch

function unavail(note) {
  return `<div class="muted">Unavailable${note ? " — " + esc(note) : ""}.</div>`;
}

function availPillHtml(available) {
  if (available === true) return '<span class="pill ok">available</span>';
  if (available === false) return '<span class="pill bad">unavailable</span>';
  return '<span class="pill">unknown</span>';
}

function computeServicesHtml(services) {
  if (!services.length) return "";
  const rows = services
    .map((s) => {
      const info = s && typeof s === "object" ? s : {};
      const enabled = String(info.status || "").toLowerCase() === "enabled";
      const up = String(info.state || "").toLowerCase() === "up";
      return `<tr>
        <td><strong>${esc(info.name || "?")}</strong></td>
        <td><code>${esc(info.host || "—")}</code></td>
        <td class="muted">${esc(info.zone || "—")}</td>
        <td>${enabled ? '<span class="pill ok">enabled</span>' : '<span class="pill">disabled</span>'}</td>
        <td>${up ? '<span class="pill ok">up</span>' : '<span class="pill bad">down</span>'}</td>
      </tr>`;
    })
    .join("");
  return `<h3 class="lc-title">Compute services</h3>
    <table>
      <thead><tr><th>Service</th><th>Host</th><th>Zone</th><th>Status</th><th>State</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function networkAgentsHtml(agents) {
  if (!agents.length) return "";
  const rows = agents
    .map((a) => {
      const info = a && typeof a === "object" ? a : {};
      const alive = info.alive === true;
      const state = info.state != null && info.state !== "" ? ` <span class="muted">(${esc(info.state)})</span>` : "";
      return `<tr>
        <td>${esc(info.type || "?")}</td>
        <td><code>${esc(info.host || "—")}</code></td>
        <td>${alive ? '<span class="pill ok">✓ alive</span>' : '<span class="pill bad">✕ dead</span>'}${state}</td>
      </tr>`;
    })
    .join("");
  return `<h3 class="lc-title">Network agents</h3>
    <table>
      <thead><tr><th>Type</th><th>Host</th><th>Alive</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function imagesHtml(images) {
  if (!images.length) return "";
  const rows = images
    .map((img) => {
      const info = img && typeof img === "object" ? img : {};
      const active = String(info.status || "").toLowerCase() === "active";
      return `<tr>
        <td><strong>${esc(info.name || "?")}</strong></td>
        <td>${
          active
            ? '<span class="pill ok">active</span>'
            : `<span class="pill warn">${esc(info.status || "unknown")}</span>`
        }</td>
      </tr>`;
    })
    .join("");
  return `<h3 class="lc-title">Images</h3>
    <table>
      <thead><tr><th>Name</th><th>Status</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// Users as a count plus a capped chip list.
function usersHtml(users) {
  if (!users.length) return "";
  const names = users.map((u) => (u && typeof u === "object" ? u.name : u)).filter(Boolean);
  const shown = names.slice(0, USER_CHIP_LIMIT);
  const chips = shown.map((n) => `<span class="chip">${esc(n)}</span>`).join("");
  const more =
    names.length > shown.length ? `<span class="muted lc-more">+ ${names.length - shown.length} more</span>` : "";
  return `<h3 class="lc-title">Users <span class="muted" style="font-weight:normal">(${users.length})</span></h3>
    <div class="lc-users">${chips}${more}</div>`;
}

function openstackBodyHtml(d) {
  const data = d && typeof d === "object" ? d : {};
  const services = Array.isArray(data.compute_services) ? data.compute_services : [];
  const agents = Array.isArray(data.network_agents) ? data.network_agents : [];
  const images = Array.isArray(data.images) ? data.images : [];
  const users = Array.isArray(data.users) ? data.users : [];
  const parts = [computeServicesHtml(services), networkAgentsHtml(agents), imagesHtml(images), usersHtml(users)];
  const errNote = data.error ? `<div class="hint muted">reported error: ${esc(data.error)}</div>` : "";
  return parts.join("") + errNote;
}

// ---------- public API ----------

export function openstackCardHtml() {
  return `
  <div class="card span-12" id="os-card">
    <div class="toolbar">
      <h2>OpenStack</h2>
      <span id="os-pill"></span>
      <span id="os-msg" class="muted"></span>
    </div>
    <div id="os-body" class="muted">Select an environment.</div>
  </div>`;
}

export function wireOpenstackCard() {
  // No interactive controls yet; hook kept for symmetry with the other cards.
}

export async function loadOpenstackCard(envId) {
  activeEnvId = envId || "";
  const body = document.getElementById("os-body");
  if (!body) return;
  const msg = document.getElementById("os-msg");
  const pill = document.getElementById("os-pill");
  if (!activeEnvId) {
    if (msg) msg.textContent = "";
    if (pill) pill.innerHTML = "";
    body.classList.add("muted");
    body.innerHTML = "Select an environment.";
    return;
  }
  if (msg) msg.textContent = "Loading…";

  let d;
  try {
    d = await api(`/api/v1/environments/${encodeURIComponent(activeEnvId)}/openstack`);
  } catch (e) {
    if (activeEnvId !== envId || !document.getElementById("os-body")) return;
    if (msg) msg.textContent = "";
    if (pill) pill.innerHTML = "";
    body.classList.remove("muted");
    body.innerHTML = unavail(e.message);
    return;
  }
  if (activeEnvId !== envId || !document.getElementById("os-body")) return;
  if (msg) msg.textContent = "";

  const data = d && typeof d === "object" ? d : {};
  if (pill) pill.innerHTML = availPillHtml(data.available);

  // API not reachable for this env: show the reason, plus any partial data.
  if (data.available === false) {
    body.classList.remove("muted");
    body.innerHTML = unavail(data.error || "OpenStack API not available") + openstackBodyHtml(data);
    return;
  }
  body.classList.remove("muted");
  const html = openstackBodyHtml(data);
  body.innerHTML = html || unavail("empty OpenStack response");
}

export function destroyOpenstackCard() {}
