// ovh.js — shared OVH dedicated-server import helpers.
//
// The credential model:
//   * OVH accounts (name + region endpoint + app key/secret) live on the
//     OvhAccount rows, managed by a platform admin under Admin → OVH accounts.
//   * The consumer key lives on the ACCOUNT too: a platform admin
//     runs "Connect" (request → approve in OVH → poll → store, encrypted).
//     Connect requests list + BYOI reinstall + IPMI + vRack attach.
//   * An ENVIRONMENT binds to one account (operator action); listing servers
//     then uses the bound account's key.
//
// Shared pieces (each bound to a target container):
//
//   ovhAccountPicker({ envId, box, onPicked })
//       Environment-side: pick the OVH account to bind this environment to.
//       Accounts without an approved consumer key are flagged; binding one
//       still works, but server listing will ask a platform admin to
//       Connect the account first.
//
//   ovhConnectAccount({ accountId, box, onConnected })
//       Admin-side consumer-key flow: request a key (list + BYOI reinstall +
//       IPMI + vRack attach), show the OVH validation URL, poll until
//       approved, store it on the account. Re-run Connect if 403.
//
//   ovhServerTable({ envId, box, onUse })
//       Loads the bound account's dedicated servers (server-side role
//       suggestion) and renders a selectable table. "Use selected" invokes
//       onUse(selectedServers) so the caller (wizard / detail page) can feed
//       them into /servers/static.
import { api, esc, toast } from "./api.js";
import { ROLE_LABELS } from "./roles.js";

// Single global poll timer so navigating away cancels the validation loop.
let pollTimer = null;

export function clearOvhPoll() {
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

// Status for one environment: which account is bound and whether that
// account has an approved consumer key.
export async function ovhEnvStatus(envId) {
  try {
    const s = await api(`/api/v1/ovh?environment_id=${encodeURIComponent(envId)}`);
    return { has_consumer_key: !!s?.has_consumer_key, account_id: s?.account_id || null };
  } catch {
    return { has_consumer_key: false, account_id: null };
  }
}

export async function ovhEnvKeyStored(envId) {
  return (await ovhEnvStatus(envId)).has_consumer_key;
}

export async function listOvhAccounts(envId) {
  try {
    const res = await api(
      `/api/v1/environments/${encodeURIComponent(envId)}/ovh/accounts`
    );
    return res.accounts || [];
  } catch {
    return [];
  }
}

export async function bindOvhAccount(envId, accountId) {
  return api(`/api/v1/environments/${encodeURIComponent(envId)}/ovh/bind`, {
    method: "POST",
    body: JSON.stringify({ account_id: accountId }),
  });
}

export async function unbindOvhAccount(envId) {
  return api(`/api/v1/environments/${encodeURIComponent(envId)}/ovh/bind`, {
    method: "DELETE",
  });
}

function chipList(roles) {
  const rs = Array.isArray(roles) ? roles : [];
  if (!rs.length) return '<span class="muted">—</span>';
  return rs.map((r) => `<span class="chip">${esc(ROLE_LABELS[r] || r)}</span>`).join(" ");
}

function endpointRegion(a) {
  return (a.endpoint || "").replace(/^https?:\/\//, "");
}

function accountOptions(accounts, selectedId) {
  return accounts
    .map((a) => {
      const keyBadge = a.has_consumer_key ? " · key approved" : " · NO key (admin must Connect)";
      return `<option value="${esc(a.id)}"${a.id === selectedId ? " selected" : ""}>${esc(
        a.name
      )} (${esc(endpointRegion(a))}${keyBadge})</option>`;
    })
    .join("");
}

// ---------------------------------------------------------------------------
// Environment-side: account picker (bind / unbind)
// ---------------------------------------------------------------------------

export async function ovhAccountPicker({ envId, box, onPicked }) {
  clearOvhPoll();
  const accounts = await listOvhAccounts(envId);
  if (!accounts.length) {
    box.innerHTML = `
    <div class="ovh-panel" style="border:1px solid var(--border,#ddd);border-radius:6px;padding:.6rem .7rem">
      <div class="error" style="font-size:.82rem">
        No OVH accounts are configured. A platform admin must add one first
        (Admin → OVH accounts) — it stores the region endpoint + app
        key/secret and approves a consumer key there (list + BYOI reinstall +
        IPMI + vRack attach). Re-run Connect if 403.
      </div>
    </div>`;
    return;
  }

  const envStatus = await ovhEnvStatus(envId);
  const selectedId =
    envStatus.account_id && accounts.some((a) => a.id === envStatus.account_id)
      ? envStatus.account_id
      : "";

  box.innerHTML = `
  <div class="ovh-panel" style="border:1px solid var(--border,#ddd);border-radius:6px;padding:.6rem .7rem">
    <div class="row" style="align-items:center;gap:.5rem;flex-wrap:wrap">
      <span class="muted" style="font-size:.8rem">Import from OVH account:</span>
      <select id="ovh-account-select">
        ${selectedId ? "" : '<option value="" disabled selected>select account…</option>'}
        ${accountOptions(accounts, selectedId)}
      </select>
      <button class="secondary btn-sm" id="ovh-bind-btn" type="button">Use this account</button>
      <button class="secondary btn-sm" id="ovh-unbind-btn" type="button" style="display:none">Unbind</button>
    </div>
    <div class="muted" style="font-size:.75rem;margin-top:.35rem">
      Binding an account lets this environment import its dedicated servers.
      The account must have an approved consumer key (Connect under Admin → OVH
      accounts). Re-run Connect if BYOI reinstall returns 403.
    </div>
    <div id="ovh-pick-status" style="margin-top:.5rem"></div>
  </div>`;

  const statusBox = box.querySelector("#ovh-pick-status");
  const bindBtn = box.querySelector("#ovh-bind-btn");
  const unbindBtn = box.querySelector("#ovh-unbind-btn");

  if (selectedId) {
    bindBtn.style.display = "none";
    unbindBtn.style.display = "";
    const bound = accounts.find((a) => a.id === selectedId);
    if (bound?.has_consumer_key) {
      statusBox.innerHTML =
        '<div class="ok" style="font-size:.8rem">Bound and ready — load the servers below.</div>';
    } else {
      statusBox.innerHTML =
        '<div class="error" style="font-size:.8rem">This account has no approved consumer key yet — a platform admin must Connect it first (Admin → OVH accounts).</div>';
    }
  }

  unbindBtn.addEventListener("click", async () => {
    try {
      await unbindOvhAccount(envId);
      toast("OVH account unbound", "ok");
      ovhAccountPicker({ envId, box, onPicked });
    } catch (e) {
      toast(`Unbind failed: ${e.message}`, "bad");
    }
  });

  bindBtn.addEventListener("click", async () => {
    const accountId = box.querySelector("#ovh-account-select").value;
    if (!accountId) return;
    bindBtn.disabled = true;
    bindBtn.textContent = "Binding…";
    try {
      await bindOvhAccount(envId, accountId);
      toast("OVH account bound", "ok");
      if (onPicked) onPicked(accountId);
    } catch (e) {
      statusBox.innerHTML = `<div class="error">${esc(e.message)}</div>`;
      bindBtn.disabled = false;
      bindBtn.textContent = "Use this account";
    }
  });
}

// ---------------------------------------------------------------------------
// Admin-side: consumer-key Connect flow (stores on the account)
// ---------------------------------------------------------------------------

export async function ovhConnectAccount({ accountId, box, onConnected }) {
  clearOvhPoll();
  let pendingKey = null;
  box.innerHTML = `
  <div class="ovh-panel" style="border:1px solid var(--border,#ddd);border-radius:6px;padding:.6rem .7rem">
    <div class="row" style="align-items:center;gap:.5rem;flex-wrap:wrap">
      <button class="secondary btn-sm" id="ovh-connect-btn" type="button">Connect (request consumer key)</button>
      <button class="secondary btn-sm" id="ovh-key-forget-btn" type="button" style="display:none">Forget key</button>
    </div>
    <div class="muted" style="font-size:.75rem;margin-top:.35rem">
      Mints a consumer key for this OVH account (list servers, <strong>BYOI reinstall</strong>,
      IPMI, <strong>vRack attach</strong>) and stores it encrypted. Re-run Connect after a
      console update so the key picks up reinstall and vRack permission. Changing app
      credentials or endpoint resets it.
    </div>
    <div id="ovh-connect-status" style="margin-top:.5rem"></div>
  </div>`;

  const statusBox = box.querySelector("#ovh-connect-status");
  const connectBtn = box.querySelector("#ovh-connect-btn");
  const forgetBtn = box.querySelector("#ovh-key-forget-btn");

  forgetBtn.addEventListener("click", async () => {
    try {
      await api(`/api/v1/ovh/accounts/${encodeURIComponent(accountId)}/consumer-key`, { method: "DELETE" });
      toast("Consumer key forgotten", "ok");
      if (onConnected) onConnected();
    } catch (e) {
      toast(`Forget failed: ${e.message}`, "bad");
    }
  });

  async function requestKey() {
    connectBtn.disabled = true;
    connectBtn.textContent = "Requesting…";
    statusBox.innerHTML = "";
    try {
      const res = await api(
        `/api/v1/ovh/accounts/${encodeURIComponent(accountId)}/consumer-key/request`,
        { method: "POST", body: "{}" }
      );
      pendingKey = res.consumer_key;
      if (!res.validation_url) {
        statusBox.innerHTML =
          '<div class="error">OVH returned no validation URL — check the account app key/secret.</div>';
        connectBtn.disabled = false;
        connectBtn.textContent = "Connect (request consumer key)";
        return;
      }
      statusBox.innerHTML = `
        <div style="font-size:.82rem">
          <p style="margin:.2rem 0">Open this link, log in to that OVH account, and approve access (server list + reinstall + IPMI + vRack):</p>
          <p style="word-break:break-all;background:var(--bg-2,#f4f4f4);padding:.4rem .5rem;border-radius:4px;font-size:.75rem">
            <a href="${esc(res.validation_url)}" target="_blank" rel="noopener noreferrer">${esc(res.validation_url)}</a>
          </p>
          <p class="muted" style="margin-top:.4rem">Waiting for approval… this finishes automatically once you confirm it in OVH.</p>
        </div>`;
      poll();
    } catch (e) {
      statusBox.innerHTML = `<div class="error">${esc(e.message)}</div>`;
      connectBtn.disabled = false;
      connectBtn.textContent = "Connect (request consumer key)";
    }
  }

  function poll() {
    clearOvhPoll();
    pollTimer = setTimeout(async () => {
      try {
        const st = await api(
          `/api/v1/ovh/accounts/${encodeURIComponent(accountId)}/consumer-key/validate?consumer_key=${encodeURIComponent(pendingKey)}`
        );
        if (st.valid) {
          await api(
            `/api/v1/ovh/accounts/${encodeURIComponent(accountId)}/consumer-key/store`,
            { method: "POST", body: JSON.stringify({ consumer_key: pendingKey }) }
          );
          pendingKey = null;
          clearOvhPoll();
          toast("OVH consumer key stored", "ok");
          if (onConnected) onConnected();
        } else {
          poll();
        }
      } catch (e) {
        clearOvhPoll();
        statusBox.innerHTML = `<div class="error">${esc(e.message)}</div>`;
        connectBtn.disabled = false;
        connectBtn.textContent = "Connect (request consumer key)";
      }
    }, 4000);
  }

  connectBtn.addEventListener("click", requestKey);
}

// ---------------------------------------------------------------------------
// Server table (uses the environment's bound account + its key)
// ---------------------------------------------------------------------------

export async function ovhServerTable({ envId, box, onUse }) {
  clearOvhPoll();
  box.innerHTML = `
  <div class="ovh-panel">
    <div id="ovh-load" class="muted" style="font-size:.82rem">Loading OVH servers…</div>
    <div id="ovh-servers" style="display:none"></div>
    <div id="ovh-actions" style="display:none;margin-top:.6rem;gap:.5rem;align-items:center" class="row">
      <button class="secondary btn-sm" id="ovh-reload" type="button">Refresh</button>
      <button class="btn-sm" id="ovh-use" type="button">Use selected</button>
      <span id="ovh-count" class="muted" style="font-size:.8rem"></span>
    </div>
  </div>`;

  const loadEl = box.querySelector("#ovh-load");
  const serversEl = box.querySelector("#ovh-servers");
  const actionsEl = box.querySelector("#ovh-actions");
  const countEl = box.querySelector("#ovh-count");

  let servers = [];
  let selected = new Set();

  async function load() {
    loadEl.style.display = "";
    loadEl.textContent = "Loading OVH servers…";
    serversEl.style.display = "none";
    actionsEl.style.display = "none";
    try {
      const res = await api(`/api/v1/environments/${encodeURIComponent(envId)}/ovh/servers`);
      servers = res.servers || [];
    } catch (e) {
      // 404 = env unbound, 503 = bound account lacks an approved key.
      let hint = "";
      if (e.status === 404) {
        hint = '<p class="muted" style="font-size:.78rem">Bind an OVH account first (account picker above).</p>';
      } else if (e.status === 503) {
        hint = '<p class="muted" style="font-size:.78rem">A platform admin must run <strong>Connect</strong> on this account (Admin → OVH accounts). Connect requests list + BYOI reinstall + IPMI + vRack attach. Re-run Connect if 403.</p>';
      }
      loadEl.innerHTML = `<div class="error">${esc(e.message)}</div>${hint}`;
      return;
    }
    if (!servers.length) {
      loadEl.innerHTML = '<span class="muted">No dedicated servers found on this OVH account.</span>';
      return;
    }
    selected = new Set(servers.map((s) => s.server_id));
    render();
  }

  function render() {
    loadEl.style.display = "none";
    const rows = servers
      .map((s) => {
        const checked = selected.has(s.server_id) ? " checked" : "";
        const ipCell = s.private_ip
          ? `${esc(s.private_ip)} <span style="font-size:.7rem">priv</span>` +
            (s.public_ip ? `<div style="font-size:.7rem">${esc(s.public_ip)} pub</div>` : "")
          : esc(s.ip || "—");
        return `<tr data-id="${esc(s.server_id)}">
        <td style="width:1.4rem"><input type="checkbox" class="ovh-sel" data-id="${esc(s.server_id)}"${checked} /></td>
        <td><strong>${esc(s.hostname || s.server_id)}</strong>${
          s.private_mac ? `<div class="muted" style="font-size:.7rem">${esc(s.private_mac)}</div>` : ""
        }</td>
        <td>${ipCell}</td>
        <td>${esc(s.model || "—")}</td>
        <td>${esc(s.cpu || "—")}</td>
        <td>${s.cores != null ? s.cores : "—"}</td>
        <td>${s.ram_gb != null ? s.ram_gb + " GB" : "—"}</td>
        <td>${s.disk_gb != null ? s.disk_gb + " GB" : "—"}</td>
        <td>${esc(s.datacenter || "—")}</td>
        <td class="ovh-role-cell">${chipList(s.roles)}</td>
      </tr>`;
      })
      .join("");
    serversEl.innerHTML = `
      <table class="tbl" style="width:100%">
        <thead><tr>
          <th></th><th>Server</th><th>IP</th><th>Model</th><th>CPU</th><th>Cores</th><th>RAM</th><th>Disk</th><th>DC</th><th>Suggested roles</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
      ${servers.length < 3 ? '<p class="muted" style="font-size:.75rem;margin-top:.4rem">⚠ Fewer than 3 servers — required roles (control plane + etcd + control) all land on the largest node. Add more or adjust roles after import.</p>' : ""}`;
    serversEl.style.display = "";
    actionsEl.style.display = "flex";
    updateCount();

    serversEl.querySelectorAll(".ovh-sel").forEach((el) => {
      el.addEventListener("change", () => {
        if (el.checked) selected.add(el.dataset.id);
        else selected.delete(el.dataset.id);
        updateCount();
      });
    });
  }

  function updateCount() {
    countEl.textContent = `${selected.size} of ${servers.length} selected`;
  }

  function selectedServers() {
    return servers.filter((s) => selected.has(s.server_id));
  }

  box.querySelector("#ovh-reload").addEventListener("click", load);
  box.querySelector("#ovh-use").addEventListener("click", () => {
    const chosen = selectedServers();
    if (!chosen.length) {
      toast("Select at least one server", "warn");
      return;
    }
    if (onUse) onUse(chosen);
  });

  await load();
}
