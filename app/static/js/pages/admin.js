// pages/admin.js — platform-admin console: tenants, users, reach, and the database.
//
// Gated on store.platformAdmin (from /api/v1/auth/whoami); non-admins get a
// 403-style note instead of the management UI. The endpoints themselves
// enforce platform-admin server-side; this page is just the first UI for
// what used to be curl-only.
import { api, esc, fmtTime, toast } from "../api.js";
import { store } from "../store.js";
import { clearOvhPoll, ovhConnectAccount } from "../ovh.js";

export const title = "Admin";

export function destroy() {
  clearOvhPoll();
  document.getElementById("ovh-preview-modal")?.remove();
}

const ROLES = ["viewer", "operator", "admin"];

let tenants = [];
let users = [];
let ovhAccounts = [];
const members = {}; // tenantId -> MemberRead[] once loaded
let openTenant = null; // tenant id with the members row expanded
let resetUser = null; // username with the reset-password row expanded
let editOvhAccount = null; // account id with the edit row expanded
let connectOvhAccount = null; // account id with the Connect row expanded

function roleOptions(selected) {
  return ROLES.map(
    (r) => `<option value="${r}"${r === selected ? " selected" : ""}>${r}</option>`
  ).join("");
}

function rolePill(role) {
  const cls = role === "admin" ? "bad" : role === "operator" ? "warn" : "";
  return `<span class="pill ${cls}">${esc(role)}</span>`;
}

// ---------------------------------------------------------------------------
// Tenants
// ---------------------------------------------------------------------------

function tenantRowHtml(t) {
  const open = openTenant === t.id;
  let html = `<tr>
    <td><strong>${esc(t.name)}</strong></td>
    <td class="muted">${esc(t.description || "—")}</td>
    <td class="muted">${esc(fmtTime(t.created_at))}</td>
    <td>
      <button class="secondary btn-sm" type="button" data-members="${esc(t.id)}">${open ? "Hide members" : "Members"}</button>
      <button class="secondary btn-sm" type="button" data-del-tenant="${esc(t.id)}">Delete</button>
    </td>
  </tr>`;
  if (open) html += membersRowHtml(t);
  return html;
}

function membersRowHtml(t) {
  const list = members[t.id];
  let body;
  if (!list) {
    body = '<div class="muted">Loading…</div>';
  } else {
    const rows = list
      .map(
        (m) => `<tr>
          <td>${esc(m.username)}</td>
          <td>${rolePill(m.role)}</td>
          <td><button class="secondary btn-sm" type="button" data-remove-member="${esc(
            t.id
          )}:${esc(m.user_id)}">Remove</button></td>
        </tr>`
      )
      .join("");
    body = `
      <table>
        <thead><tr><th>User</th><th>Role</th><th></th></tr></thead>
        <tbody>${rows || '<tr><td colspan="3" class="muted">No members.</td></tr>'}</tbody>
      </table>
      <form class="adm-inline-form" data-add-member="${esc(t.id)}">
        <input name="username" type="text" placeholder="username" required maxlength="64" />
        <select name="role">${roleOptions("viewer")}</select>
        <button class="secondary btn-sm" type="submit">Add member</button>
      </form>`;
  }
  return `<tr class="adm-expand"><td colspan="4">${body}</td></tr>`;
}

function renderTenants() {
  const host = document.getElementById("adm-tenants");
  document.getElementById("adm-tenant-count").textContent = `${tenants.length} tenant(s)`;
  if (!tenants.length) {
    host.innerHTML = '<div class="muted">No tenants yet.</div>';
    return;
  }
  host.innerHTML = `
    <table>
      <thead><tr><th>Name</th><th>Description</th><th>Created</th><th></th></tr></thead>
      <tbody>${tenants.map(tenantRowHtml).join("")}</tbody>
    </table>
    <p class="muted" style="font-size:.78rem;margin-top:.5rem">
      Deleting a tenant removes its memberships; environments in the tenant are not
      deleted — they lose their tenant and become platform-admin only.
    </p>`;

  host.querySelectorAll("button[data-members]").forEach((btn) =>
    btn.addEventListener("click", () => toggleMembers(btn.dataset.members))
  );
  host.querySelectorAll("button[data-del-tenant]").forEach((btn) =>
    btn.addEventListener("click", () => deleteTenant(btn.dataset.delTenant))
  );
  host.querySelectorAll("button[data-remove-member]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const [tenantId, userId] = btn.dataset.removeMember.split(":");
      removeMember(tenantId, userId);
    })
  );
  host.querySelectorAll("form[data-add-member]").forEach((form) =>
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      addMember(form.dataset.addMember, form);
    })
  );
}

async function toggleMembers(tenantId) {
  openTenant = openTenant === tenantId ? null : tenantId;
  if (openTenant && !members[openTenant]) {
    try {
      members[openTenant] = await api(
        `/api/v1/tenants/${encodeURIComponent(openTenant)}/members`
      );
    } catch (e) {
      toast(`Load members failed: ${e.message}`, "bad");
      openTenant = null;
    }
  }
  renderTenants();
}

async function reloadMembers(tenantId) {
  members[tenantId] = await api(`/api/v1/tenants/${encodeURIComponent(tenantId)}/members`);
}

async function addMember(tenantId, form) {
  const username = form.elements.username.value.trim();
  const role = form.elements.role.value;
  if (!username) return;
  try {
    await api(`/api/v1/tenants/${encodeURIComponent(tenantId)}/members`, {
      method: "POST",
      body: JSON.stringify({ username, role }),
    });
    toast(`Added ${username} as ${role}`, "ok");
    await reloadMembers(tenantId);
    renderTenants();
  } catch (e) {
    toast(`Add member failed: ${e.message}`, "bad");
  }
}

async function removeMember(tenantId, userId) {
  const m = (members[tenantId] || []).find((x) => x.user_id === userId);
  if (!window.confirm(`Remove ${m ? m.username : userId} from this tenant?`)) return;
  try {
    await api(
      `/api/v1/tenants/${encodeURIComponent(tenantId)}/members/${encodeURIComponent(userId)}`,
      { method: "DELETE" }
    );
    toast("Member removed", "ok");
    await reloadMembers(tenantId);
    renderTenants();
  } catch (e) {
    toast(`Remove failed: ${e.message}`, "bad");
  }
}

async function deleteTenant(tenantId) {
  const t = tenants.find((x) => x.id === tenantId);
  const name = t ? t.name : tenantId;
  if (
    !window.confirm(
      `Delete tenant '${name}'?\n\nMemberships are removed. Environments in this tenant are NOT deleted — they lose their tenant assignment and become platform-admin only.`
    )
  )
    return;
  try {
    await api(`/api/v1/tenants/${encodeURIComponent(tenantId)}`, { method: "DELETE" });
    toast(`Tenant '${name}' deleted`, "ok");
    if (openTenant === tenantId) openTenant = null;
    delete members[tenantId];
    await loadData();
  } catch (e) {
    toast(`Delete failed: ${e.message}`, "bad");
  }
}

async function createTenant(form) {
  const name = form.elements.name.value.trim();
  const description = form.elements.description.value.trim();
  if (!name) return;
  try {
    await api("/api/v1/tenants", {
      method: "POST",
      body: JSON.stringify({ name, description: description || null }),
    });
    toast(`Tenant '${name}' created`, "ok");
    form.reset();
    form.closest("details").removeAttribute("open");
    await loadData();
  } catch (e) {
    toast(`Create failed: ${e.message}`, "bad");
  }
}

// ---------------------------------------------------------------------------
// Users
// ---------------------------------------------------------------------------

function userRoleSummaryHtml(u) {
  const chips = [];
  if (u.platform_admin) chips.push('<span class="pill bad">platform admin</span>');
  for (const t of u.tenants || []) {
    chips.push(`<span class="chip">${esc(t.name)}: ${esc(t.role)}</span>`);
  }
  return chips.length ? chips.join(" ") : '<span class="muted">—</span>';
}

function userRowHtml(u) {
  const isSelf = store.username && u.username === store.username;
  const resetOpen = resetUser === u.username;
  let html = `<tr>
    <td><strong>${esc(u.username)}</strong>${isSelf ? ' <span class="muted">(you)</span>' : ""}</td>
    <td>${userRoleSummaryHtml(u)}</td>
    <td>${
      u.active
        ? '<span class="pill ok">active</span>'
        : '<span class="pill warn">disabled</span>'
    }</td>
    <td class="muted">${esc(fmtTime(u.created_at))}</td>
    <td>
      <button class="secondary btn-sm" type="button" data-reset="${esc(u.username)}">${resetOpen ? "Cancel" : "Reset password"}</button>
      <button class="secondary btn-sm" type="button" data-del-user="${esc(u.username)}"${
        isSelf ? ' disabled title="Cannot delete your own account"' : ""
      }>Delete</button>
    </td>
  </tr>`;
  if (resetOpen) {
    html += `<tr class="adm-expand"><td colspan="5">
      <form class="adm-inline-form" data-reset-form="${esc(u.username)}">
        <input name="password" type="password" placeholder="new password" required autocomplete="new-password" />
        <button class="secondary btn-sm" type="submit">Set password</button>
      </form>
    </td></tr>`;
  }
  return html;
}

function renderUsers() {
  const host = document.getElementById("adm-users");
  document.getElementById("adm-user-count").textContent = `${users.length} user(s)`;
  if (!users.length) {
    host.innerHTML = '<div class="muted">No local users.</div>';
    return;
  }
  host.innerHTML = `
    <table>
      <thead><tr><th>Username</th><th>Roles</th><th>Status</th><th>Created</th><th></th></tr></thead>
      <tbody>${users.map(userRowHtml).join("")}</tbody>
    </table>`;

  host.querySelectorAll("button[data-reset]").forEach((btn) =>
    btn.addEventListener("click", () => {
      resetUser = resetUser === btn.dataset.reset ? null : btn.dataset.reset;
      renderUsers();
      const input = host.querySelector("form[data-reset-form] input[name=password]");
      if (input) input.focus();
    })
  );
  host.querySelectorAll("button[data-del-user]").forEach((btn) =>
    btn.addEventListener("click", () => deleteUser(btn.dataset.delUser))
  );
  host.querySelectorAll("form[data-reset-form]").forEach((form) =>
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      resetPassword(form.dataset.resetForm, form);
    })
  );
}

function renderTenantPicker() {
  const sel = document.getElementById("adm-user-tenants");
  sel.innerHTML = tenants
    .map((t) => `<option value="${esc(t.id)}">${esc(t.name)}</option>`)
    .join("");
}

async function resetPassword(username, form) {
  const password = form.elements.password.value;
  if (!password) return;
  try {
    await api(`/api/v1/users/${encodeURIComponent(username)}/password`, {
      method: "POST",
      body: JSON.stringify({ password }),
    });
    toast(`Password updated for ${username}`, "ok");
    resetUser = null;
    renderUsers();
  } catch (e) {
    toast(`Reset failed: ${e.message}`, "bad");
  }
}

async function deleteUser(username) {
  if (
    !window.confirm(
      `Delete user '${username}'?\n\nTheir memberships and sessions are removed; this cannot be undone.`
    )
  )
    return;
  try {
    await api(`/api/v1/users/${encodeURIComponent(username)}`, { method: "DELETE" });
    toast(`User '${username}' deleted`, "ok");
    await loadData();
  } catch (e) {
    toast(`Delete failed: ${e.message}`, "bad");
  }
}

async function createUser(form) {
  const username = form.elements.username.value.trim();
  const password = form.elements.password.value;
  if (!username || !password) return;
  const role = form.elements.mrole.value;
  const memberships = Array.from(form.elements.mtenants.selectedOptions).map((opt) => ({
    tenant_id: opt.value,
    role,
  }));
  try {
    await api("/api/v1/users", {
      method: "POST",
      body: JSON.stringify({
        username,
        password,
        platform_admin: form.elements.padmin.checked,
        memberships,
      }),
    });
    toast(`User '${username}' created`, "ok");
    form.reset();
    form.closest("details").removeAttribute("open");
    await loadData();
  } catch (e) {
    toast(`Create failed: ${e.message}`, "bad");
  }
}

// ---------------------------------------------------------------------------
// OVH accounts
// ---------------------------------------------------------------------------

// The three OVHcloud regions OVH documents (same list the backend validates
// against; the dropdown is the primary input, custom URLs stay API-only).
const OVH_ENDPOINTS = [
  { region: "EU", endpoint: "https://eu.api.ovh.com/1.0" },
  { region: "US", endpoint: "https://api.us.ovhcloud.com/1.0" },
  { region: "CA", endpoint: "https://ca.api.ovh.com/1.0" },
];

function ovhEndpointOptions(value) {
  const options = OVH_ENDPOINTS.map(
    (e) =>
      `<option value="${e.endpoint}"${e.endpoint === value ? " selected" : ""}>${e.region} — ${e.endpoint}</option>`
  );
  // Custom endpoints (other OVH-hosted regions) are accepted by the API;
  // offer the current value as a fallback option so editing doesn't blank it.
  if (value && !OVH_ENDPOINTS.some((e) => e.endpoint === value)) {
    options.push(`<option value="${esc(value)}" selected>${esc(value)} (custom)</option>`);
  }
  return options.join("");
}

function ovhKeyPill(a) {
  return a.has_consumer_key
    ? '<span class="pill ok">key approved</span>'
    : '<span class="pill">no key</span>';
}

function ovhAccountRowHtml(a) {
  const editOpen = editOvhAccount === a.id;
  const connectOpen = connectOvhAccount === a.id;
  let html = `<tr>
    <td><strong>${esc(a.name)}</strong></td>
    <td class="muted">${esc(a.endpoint)}</td>
    <td>${ovhKeyPill(a)}</td>
    <td class="muted">${esc(fmtTime(a.created_at))}</td>
    <td>
      <button class="secondary btn-sm" type="button" data-ovh-connect="${esc(a.id)}">${connectOpen ? "Close" : a.has_consumer_key ? "Reconnect" : "Connect"}</button>
      <button class="secondary btn-sm" type="button" data-ovh-preview="${esc(a.id)}">Preview</button>
      <button class="secondary btn-sm" type="button" data-ovh-edit="${esc(a.id)}">${editOpen ? "Cancel" : "Edit"}</button>
      <button class="secondary btn-sm" type="button" data-ovh-test="${esc(a.id)}">Test</button>
      <button class="secondary btn-sm" type="button" data-ovh-del="${esc(a.id)}">Delete</button>
    </td>
  </tr>`;
  if (connectOpen) {
    html += `<tr class="adm-expand"><td colspan="5">
      <div class="muted" style="font-size:.8rem;margin-bottom:.4rem">
        Consumer key for <strong>${esc(a.name)}</strong> — approving it in OVH lets any bound
        environment import this account's dedicated servers.
      </div>
      <div id="ovh-connect-box"></div>
    </td></tr>`;
  }
  if (editOpen) {
    html += `<tr class="adm-expand"><td colspan="5">
      <form class="adm-inline-form" data-ovh-edit-form="${esc(a.id)}">
        <input name="name" type="text" placeholder="name" value="${esc(a.name)}" required maxlength="128" />
        <select name="endpoint" required>${ovhEndpointOptions(a.endpoint)}</select>
        <input name="app_key" type="text" placeholder="app key" value="${esc(a.app_key || "")}" required maxlength="256" autocomplete="off" />
        <input name="app_secret" type="password" placeholder="app secret (blank = unchanged)" autocomplete="new-password" />
        <input name="consumer_key" type="password" placeholder="consumer key (blank = unchanged)" autocomplete="off" title="Paste an existing approved OVH consumer key; stored encrypted on this account." />
        <button class="secondary btn-sm" type="submit">Save</button>
      </form>
      <p class="muted" style="font-size:.75rem">Paste a consumer key you already have, or use <strong>Connect</strong> to mint one. Changing the region, app key or app secret resets the stored key.</p>
    </td></tr>`;
  }
  return html;
}

function renderOvhAccounts() {
  const host = document.getElementById("adm-ovh");
  document.getElementById("adm-ovh-count").textContent = `${ovhAccounts.length} account(s)`;
  if (!ovhAccounts.length) {
    host.innerHTML = '<div class="muted">No OVH accounts yet. Add one to import dedicated servers in the wizard.</div>';
    return;
  }
  host.innerHTML = `
    <table>
      <thead><tr><th>Name</th><th>Region endpoint</th><th>Consumer key</th><th>Created</th><th></th></tr></thead>
      <tbody>${ovhAccounts.map(ovhAccountRowHtml).join("")}</tbody>
    </table>`;

  host.querySelectorAll("button[data-ovh-edit]").forEach((btn) =>
    btn.addEventListener("click", () => {
      editOvhAccount = editOvhAccount === btn.dataset.ovhEdit ? null : btn.dataset.ovhEdit;
      renderOvhAccounts();
    })
  );
  host.querySelectorAll("button[data-ovh-connect]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const id = btn.dataset.ovhConnect;
      connectOvhAccount = connectOvhAccount === id ? null : id;
      renderOvhAccounts();
      if (connectOvhAccount) {
        const box = host.querySelector("#ovh-connect-box");
        if (box) ovhConnectAccount({ accountId: id, box, onConnected: () => loadOvhAccounts() });
      }
    })
  );
  host.querySelectorAll("button[data-ovh-preview]").forEach((btn) =>
    btn.addEventListener("click", () => previewOvhAccount(btn.dataset.ovhPreview))
  );
  host.querySelectorAll("button[data-ovh-test]").forEach((btn) =>
    btn.addEventListener("click", () => testOvhAccount(btn.dataset.ovhTest))
  );
  host.querySelectorAll("button[data-ovh-del]").forEach((btn) =>
    btn.addEventListener("click", () => deleteOvhAccount(btn.dataset.ovhDel))
  );
  host.querySelectorAll("form[data-ovh-edit-form]").forEach((form) =>
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      updateOvhAccount(form.dataset.ovhEditForm, form);
    })
  );
}

async function testOvhAccount(accountId) {
  try {
    const res = await api(`/api/v1/ovh/accounts/${encodeURIComponent(accountId)}/test`, { method: "POST", body: "{}" });
    toast(`Endpoint reachable (${res.latency_ms} ms)`, "ok");
  } catch (e) {
    toast(`Test failed: ${e.message}`, "bad");
  }
}

// ----- OVH account preview modal -----------------------------------------

function ovhPreviewModalHtml(a) {
  return `
  <div class="wf-agent-modal" id="ovh-preview-modal">
    <div class="wf-agent-modal-card" style="max-width:56rem">
      <h2>Preview: ${esc(a.name)}</h2>
      <div class="muted" style="font-size:.8rem;margin-bottom:.9rem">${esc(a.endpoint)}</div>
      <div id="ovh-preview-body"><div class="muted" style="font-size:.82rem">Loading servers…</div></div>
      <div class="wf-agent-modal-actions" style="margin-top:1rem">
        <button class="secondary btn-sm" id="ovh-preview-close" type="button">Close</button>
      </div>
    </div>
  </div>`;
}

async function previewOvhAccount(accountId) {
  const a = ovhAccounts.find((x) => x.id === accountId);
  if (!a) return;
  document.getElementById("ovh-preview-modal")?.remove();
  const host = document.createElement("div");
  host.innerHTML = ovhPreviewModalHtml(a);
  document.body.appendChild(host.firstElementChild);
  const modal = document.getElementById("ovh-preview-modal");
  const body = modal.querySelector("#ovh-preview-body");
  modal.addEventListener("click", (e) => {
    if (e.target === modal) modal.remove();
  });
  modal.querySelector("#ovh-preview-close").addEventListener("click", () => modal.remove());

  let res;
  try {
    res = await api(`/api/v1/ovh/accounts/${encodeURIComponent(accountId)}/servers`);
  } catch (e) {
    body.innerHTML = `<div class="error">Preview failed: ${esc(e.message)}</div>`;
    return;
  }
  const envNames = (res.bound_environment_names || [])
    .map((n) => esc(n))
    .join(", ");
  if (res.consumer_key_required) {
    let boundHtml = '<span class="muted">None bound yet.</span>';
    if (envNames) {
      boundHtml = (res.bound_environment_ids || [])
        .map((eid) => `<a href="#/environments/${encodeURIComponent(eid)}" data-preview-env>${esc(store.envs.find((x) => x.id === eid)?.name || eid)}</a>`)
        .join(" · ");
    }
    body.innerHTML = `
      <div class="wf-agent-warn">A consumer key is required to list servers. OVH app key/secret alone only validates the endpoint.</div>
      <p class="muted" style="font-size:.82rem;margin:0 0 .6rem">
        ${envNames ? `Bound environment(s): ${boundHtml}.` : ""}
        Use the <strong>Connect</strong> button on this account's row to mint and approve a
        consumer key (list + BYOI reinstall + IPMI + vRack attach), then re-run this preview.
        Re-run Connect if OVH returns 403.
      </p>`;
    return;
  }
  const servers = res.servers || [];
  if (!servers.length) {
    body.innerHTML = '<div class="muted">No dedicated servers found on this OVH account.</div>';
    return;
  }
  body.innerHTML = `
    <div class="muted" style="font-size:.78rem;margin-bottom:.5rem">
      ${servers.length} dedicated server(s) — read via this account's consumer key
    </div>
    <div style="max-height:55vh;overflow:auto">
      <table class="tbl" style="width:100%">
        <thead><tr><th>Server</th><th>IP</th><th>Model</th><th>CPU</th><th>Cores</th><th>RAM</th><th>Disk</th><th>DC</th><th>Suggested roles</th></tr></thead>
        <tbody>${servers
          .map((s) => `<tr>
            <td><strong>${esc(s.hostname || s.server_id)}</strong></td>
            <td>${esc(s.ip || "—")}</td>
            <td>${esc(s.model || "—")}</td>
            <td>${esc(s.cpu || "—")}</td>
            <td>${s.cores != null ? s.cores : "—"}</td>
            <td>${s.ram_gb != null ? s.ram_gb + " GB" : "—"}</td>
            <td>${s.disk_gb != null ? s.disk_gb + " GB" : "—"}</td>
            <td>${esc(s.datacenter || "—")}</td>
            <td>${roleChips(s.roles)}</td>
          </tr>`)
          .join("")}</tbody>
      </table>
    </div>`;
}

function roleChips(roles) {
  const rs = Array.isArray(roles) ? roles : [];
  if (!rs.length) return '<span class="muted">—</span>';
  return rs.map((r) => `<span class="chip">${esc(r)}</span>`).join(" ");
}

async function deleteOvhAccount(accountId) {
  const a = ovhAccounts.find((x) => x.id === accountId);
  const name = a ? a.name : accountId;
  if (!window.confirm(`Delete OVH account '${name}'?\n\nEnvironments bound to it can no longer list servers until reconnected.`)) return;
  try {
    await api(`/api/v1/ovh/accounts/${encodeURIComponent(accountId)}`, { method: "DELETE" });
    toast(`OVH account '${name}' deleted`, "ok");
    await loadOvhAccounts();
  } catch (e) {
    toast(`Delete failed: ${e.message}`, "bad");
  }
}

async function updateOvhAccount(accountId, form) {
  const name = form.elements.name.value.trim();
  const endpoint = form.elements.endpoint.value.trim();
  const appKey = form.elements.app_key.value.trim();
  const appSecret = form.elements.app_secret.value;
  const consumerKey = form.elements.consumer_key.value.trim();
  const payload = { name, endpoint, app_key: appKey, app_secret: appSecret || null };
  if (consumerKey) payload.consumer_key = consumerKey;
  try {
    await api(`/api/v1/ovh/accounts/${encodeURIComponent(accountId)}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    toast(`OVH account '${name}' updated`, "ok");
    editOvhAccount = null;
    await loadOvhAccounts();
  } catch (e) {
    toast(`Update failed: ${e.message}`, "bad");
  }
}

async function createOvhAccount(form) {
  const name = form.elements.name.value.trim();
  const endpoint = form.elements.endpoint.value.trim();
  const appKey = form.elements.app_key.value.trim();
  const appSecret = form.elements.app_secret.value;
  if (!name || !endpoint || !appKey || !appSecret) return;
  try {
    await api("/api/v1/ovh/accounts", {
      method: "POST",
      body: JSON.stringify({ name, endpoint, app_key: appKey, app_secret: appSecret }),
    });
    toast(`OVH account '${name}' created`, "ok");
    form.reset();
    form.closest("details").removeAttribute("open");
    await loadOvhAccounts();
  } catch (e) {
    toast(`Create failed: ${e.message}`, "bad");
  }
}

async function loadOvhAccounts() {
  try {
    const res = await api("/api/v1/ovh/accounts");
    ovhAccounts = res.accounts || [];
  } catch (e) {
    ovhAccounts = [];
  }
  renderOvhAccounts();
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

async function loadData() {
  const err = document.getElementById("adm-err");
  err.innerHTML = "";
  try {
    [tenants, users] = await Promise.all([
      api("/api/v1/tenants"),
      api("/api/v1/users"),
    ]);
    tenants = tenants || [];
    users = users || [];
  } catch (e) {
    err.innerHTML = `<div class="error">${esc(e.message)}</div>`;
    tenants = [];
    users = [];
  }
  renderTenants();
  renderUsers();
  renderTenantPicker();
  loadOvhAccounts();
  loadReach();
  loadDatabase();
}

async function loadReach() {
  const host = document.getElementById("adm-reach-status");
  if (!host) return;
  try {
    const hubs = await api("/api/v1/reach");
    host.innerHTML = (hubs || [])
      .map((row) => {
        const secret = row.secret_configured ? "secret saved" : "no secret";
        const bits = [row.status, row.address, row.endpoint, row.hostname, secret]
          .filter(Boolean)
          .map((part) => esc(String(part)))
          .join(" · ");
        const detail = row.detail ? `<div class="muted">${esc(row.detail)}</div>` : "";
        return `<div style="margin-top:.35rem"><strong>${esc(row.kind)}</strong> <span class="muted">${bits}</span>${detail}</div>`;
      })
      .join("");
  } catch (e) {
    host.innerHTML = `<div class="error">${esc(e.message)}</div>`;
  }
}

async function saveReach(kind, body) {
  try {
    await api(`/api/v1/reach/${kind}`, { method: "PUT", body: JSON.stringify(body) });
    toast("Saved", "ok");
    await loadReach();
  } catch (e) {
    toast(e.message, "bad");
  }
}

async function applyReach(kind) {
  try {
    const row = await api(`/api/v1/reach/${kind}/apply`, { method: "POST" });
    toast(row.detail || row.status, "ok");
    await loadReach();
  } catch (e) {
    toast(e.message, "bad");
  }
}

async function stopReach(kind) {
  try {
    const row = await api(`/api/v1/reach/${kind}/stop`, { method: "POST" });
    toast(row.detail || row.status, "ok");
    await loadReach();
  } catch (e) {
    toast(e.message, "bad");
  }
}

let databaseNotice = "";

async function loadDatabase() {
  const host = document.getElementById("adm-db-status");
  if (!host) return;
  try {
    const row = await api("/api/v1/database");
    const note = databaseNotice ? `<div class="muted">${esc(databaseNotice)}</div>` : "";
    host.innerHTML =
      `<div><strong>${esc(row.kind)}</strong> <span class="muted">${esc(row.url)}</span></div>${note}`;
  } catch (e) {
    host.innerHTML = `<div class="error">${esc(e.message)}</div>`;
  }
}

async function moveDatabase(form) {
  const input = form.elements.target_url;
  const targetUrl = input.value.trim();
  input.value = "";
  if (!targetUrl) {
    toast("Target URL is required", "bad");
    return;
  }
  try {
    const res = await api("/api/v1/database/move", {
      method: "POST",
      body: JSON.stringify({ target_url: targetUrl }),
    });
    databaseNotice = `Restart the console to use ${res.target}.`;
    toast("Move finished. Restart the console.", "ok");
    await loadDatabase();
  } catch (e) {
    toast(e.message, "bad");
  }
}

export async function render(root) {
  openTenant = null;
  resetUser = null;
  editOvhAccount = null;
  connectOvhAccount = null;

  if (!store.platformAdmin) {
    root.innerHTML = `
    <div class="card">
      <h2>Admin</h2>
      <div class="error">Platform admin required — tenant and user management is restricted to platform administrators.</div>
    </div>`;
    return;
  }

  root.innerHTML = `
  <div class="card">
    <div class="toolbar">
      <h2>Tenants</h2>
      <button class="secondary btn-sm" id="adm-refresh" type="button">Refresh</button>
      <span id="adm-tenant-count" class="muted"></span>
    </div>
    <div id="adm-err"></div>
    <div id="adm-tenants"><div class="muted">Loading…</div></div>
    <details style="margin-top:.75rem">
      <summary>Create tenant</summary>
      <form id="adm-tenant-form" class="adm-inline-form" style="margin-top:.5rem">
        <input name="name" type="text" placeholder="name" required maxlength="128" />
        <input name="description" type="text" placeholder="description (optional)" />
        <button class="secondary btn-sm" type="submit">Create</button>
      </form>
    </details>
  </div>
  <div class="card" style="margin-top:1rem">
    <div class="toolbar">
      <h2>Users</h2>
      <span id="adm-user-count" class="muted"></span>
    </div>
    <div id="adm-users"><div class="muted">Loading…</div></div>
    <p class="muted" style="font-size:.78rem;margin-top:.5rem">
      Deactivating a user is not exposed by the API yet — delete removes the account entirely.
    </p>
    <details style="margin-top:.75rem">
      <summary>Create user</summary>
      <form id="adm-user-form" style="margin-top:.5rem">
        <div class="grid">
          <label class="field span-4"><span>Username</span><input name="username" type="text" required maxlength="64" autocomplete="off" /></label>
          <label class="field span-4"><span>Password</span><input name="password" type="password" required autocomplete="new-password" /></label>
          <label class="field span-4"><span>Tenant memberships (optional)</span><select name="mtenants" id="adm-user-tenants" multiple size="3"></select></label>
          <label class="field span-4"><span>Role in selected tenants</span><select name="mrole">${roleOptions("viewer")}</select></label>
        </div>
        <label class="check"><input name="padmin" type="checkbox" /> Platform admin (bypasses tenancy)</label>
        <div style="margin-top:.75rem"><button type="submit">Create user</button></div>
      </form>
    </details>
  </div>
  <div class="card" style="margin-top:1rem">
    <div class="toolbar">
      <h2>OVH accounts</h2>
      <span id="adm-ovh-count" class="muted"></span>
    </div>
    <p class="muted" style="font-size:.78rem">
      One entry per OVHcloud account: region endpoint + app key/secret. A
      platform admin runs <strong>Connect</strong> per account to approve a
      consumer key (list + BYOI reinstall + IPMI + vRack attach, stored encrypted). Re-run
      Connect after this update so existing keys gain reinstall and vRack permission.
      Environments bind to an account to import its servers. The app secret is
      never returned by the API.
    </p>
    <div id="adm-ovh"><div class="muted">Loading…</div></div>
    <details style="margin-top:.75rem">
      <summary>Add OVH account</summary>
      <form id="adm-ovh-form" class="adm-inline-form" style="margin-top:.5rem">
        <input name="name" type="text" placeholder="name (e.g. homelab, work)" required maxlength="128" />
        <select name="endpoint" required title="OVHcloud region (per OVH's official documentation)">
          <option value="" disabled selected>region…</option>
          ${ovhEndpointOptions("")}
        </select>
        <input name="app_key" type="text" placeholder="application key" required maxlength="256" autocomplete="off" />
        <input name="app_secret" type="password" placeholder="application secret" required autocomplete="new-password" />
        <button class="secondary btn-sm" type="submit">Add account</button>
      </form>
    </details>
  </div>
  <div class="card" style="margin-top:1rem">
    <div class="toolbar">
      <h2>Reach</h2>
      <button class="secondary btn-sm" id="adm-reach-refresh" type="button">Refresh</button>
    </div>
    <p class="muted" style="font-size:.78rem">
      How this deploy host reaches environments. WireGuard is a VPN this console serves.
      Tailscale joins your tailnet. Cloudflare Tunnel runs cloudflared here. A secret is
      stored and is not shown again. Apply does not run when the console starts.
    </p>
    <div id="adm-reach-status"><div class="muted">Loading…</div></div>
    <form id="adm-reach-wg" class="adm-inline-form" style="margin-top:.75rem">
      <label class="check"><input name="enabled" type="checkbox" /> WireGuard</label>
      <input name="endpoint" type="text" placeholder="endpoint host:port" maxlength="253" />
      <input name="network" type="text" placeholder="10.67.67.0/24" />
      <input name="listen_port" type="number" min="1" max="65535" placeholder="51820" />
      <input name="interface" type="text" placeholder="wg-gsc" maxlength="15" />
      <button class="secondary btn-sm" type="submit">Save</button>
      <button class="secondary btn-sm" type="button" id="adm-reach-wg-apply">Apply</button>
    </form>
    <form id="adm-reach-ts" class="adm-inline-form">
      <label class="check"><input name="enabled" type="checkbox" /> Tailscale</label>
      <input name="hostname" type="text" placeholder="genestack-console" maxlength="63" />
      <input name="secret" type="password" placeholder="auth key (write-only)" autocomplete="new-password" />
      <button class="secondary btn-sm" type="submit">Save</button>
      <button class="secondary btn-sm" type="button" id="adm-reach-ts-apply">Apply</button>
    </form>
    <form id="adm-reach-cf" class="adm-inline-form">
      <label class="check"><input name="enabled" type="checkbox" /> Cloudflare Tunnel</label>
      <input name="hostname" type="text" placeholder="hostname (optional)" maxlength="253" />
      <input name="secret" type="password" placeholder="tunnel token (write-only)" autocomplete="new-password" />
      <button class="secondary btn-sm" type="submit">Save</button>
      <button class="secondary btn-sm" type="button" id="adm-reach-cf-apply">Apply</button>
      <button class="secondary btn-sm" type="button" id="adm-reach-cf-stop">Stop</button>
    </form>
  </div>
  <div class="card" style="margin-top:1rem">
    <div class="toolbar">
      <h2>Database</h2>
      <button class="secondary btn-sm" id="adm-db-refresh" type="button">Refresh</button>
    </div>
    <p class="muted" style="font-size:.78rem">
      Where this console stores its own data. SQLite is the default. Postgres is the other
      supported database. Move copies every table, writes database_url, and does not switch
      the running process. Restart the console after it finishes. The password is not shown.
    </p>
    <div id="adm-db-status"><div class="muted">Loading…</div></div>
    <form id="adm-db-move" class="adm-inline-form" style="margin-top:.75rem">
      <input name="target_url" type="password" placeholder="target database URL" autocomplete="off" />
      <button class="secondary btn-sm" type="submit">Move</button>
    </form>
  </div>`;

  document.getElementById("adm-refresh").addEventListener("click", () => loadData());
  document.getElementById("adm-tenant-form").addEventListener("submit", (e) => {
    e.preventDefault();
    createTenant(e.target);
  });
  document.getElementById("adm-user-form").addEventListener("submit", (e) => {
    e.preventDefault();
    createUser(e.target);
  });
  document.getElementById("adm-ovh-form").addEventListener("submit", (e) => {
    e.preventDefault();
    createOvhAccount(e.target);
  });
  document.getElementById("adm-reach-refresh").addEventListener("click", () => loadReach());
  document.getElementById("adm-reach-wg").addEventListener("submit", (e) => {
    e.preventDefault();
    const form = e.target;
    const body = { enabled: form.elements.enabled.checked };
    const endpoint = form.elements.endpoint.value.trim();
    const network = form.elements.network.value.trim();
    const listen = form.elements.listen_port.value.trim();
    const iface = form.elements.interface.value.trim();
    if (endpoint) body.endpoint = endpoint;
    if (network) body.network = network;
    if (listen) body.listen_port = Number(listen);
    if (iface) body.interface = iface;
    saveReach("wireguard", body);
  });
  document.getElementById("adm-reach-ts").addEventListener("submit", (e) => {
    e.preventDefault();
    const form = e.target;
    const body = { enabled: form.elements.enabled.checked };
    const hostname = form.elements.hostname.value.trim();
    const secret = form.elements.secret.value;
    if (hostname) body.hostname = hostname;
    if (secret) body.secret = secret;
    form.elements.secret.value = "";
    saveReach("tailscale", body);
  });
  document.getElementById("adm-reach-cf").addEventListener("submit", (e) => {
    e.preventDefault();
    const form = e.target;
    const body = { enabled: form.elements.enabled.checked };
    const hostname = form.elements.hostname.value.trim();
    const secret = form.elements.secret.value;
    if (hostname) body.hostname = hostname;
    if (secret) body.secret = secret;
    form.elements.secret.value = "";
    saveReach("cloudflare", body);
  });
  document.getElementById("adm-reach-wg-apply").addEventListener("click", () => applyReach("wireguard"));
  document.getElementById("adm-reach-ts-apply").addEventListener("click", () => applyReach("tailscale"));
  document.getElementById("adm-reach-cf-apply").addEventListener("click", () => applyReach("cloudflare"));
  document.getElementById("adm-reach-cf-stop").addEventListener("click", () => stopReach("cloudflare"));
  document.getElementById("adm-db-refresh").addEventListener("click", () => loadDatabase());
  document.getElementById("adm-db-move").addEventListener("submit", (e) => {
    e.preventDefault();
    moveDatabase(e.target);
  });

  await loadData();
}
