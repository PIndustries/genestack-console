// pages/machines.js — machine table + per-row power query. Not linked from the hardware tabs.
import { api, esc, toast } from "../api.js";
import { store, loadEnvs, envOptionsHtml } from "../store.js";

export const title = "Machines";

let envId = "";
let mounted = false;
let refreshTimer = null;
const REFRESH_MS = 15000;

export function destroy() {
  mounted = false;
  if (refreshTimer) {
    clearInterval(refreshTimer);
    refreshTimer = null;
  }
}

export async function render(root) {
  mounted = true;
  if (!store.envs.length) await loadEnvs().catch(() => {});
  root.innerHTML = `
  <div class="card">
    <div class="toolbar">
      <h2>Machines</h2>
      <select id="mach-env">${envOptionsHtml(null, { includeNone: true, noneLabel: "This console" })}</select>
      <button class="secondary btn-sm" id="mach-refresh" type="button">Refresh</button>
      <span id="mach-msg" class="muted"></span>
      <span id="mach-updated" class="muted" style="font-size:.75rem"></span>
    </div>
    <div id="mach-err"></div>
    <table>
      <thead><tr><th>Hostname</th><th>Status</th><th>Power</th><th>IPs</th><th>Tags</th><th></th></tr></thead>
      <tbody id="mach-tbody"><tr><td colspan="6" class="muted">Loading…</td></tr></tbody>
    </table>
  </div>`;

  document.getElementById("mach-env").addEventListener("change", (e) => {
    envId = e.target.value;
    loadMachines();
  });
  document.getElementById("mach-refresh").addEventListener("click", () => loadMachines());
  // Background poll: power states change outside this page.
  // Skip ticks while the tab is hidden (same pattern as hosts.js).
  refreshTimer = setInterval(() => {
    if (!document.hidden) loadMachines({ background: true });
  }, REFRESH_MS);
  await loadMachines();
}

async function loadMachines({ background = false } = {}) {
  const tbody = document.getElementById("mach-tbody");
  const err = document.getElementById("mach-err");
  const msg = document.getElementById("mach-msg");
  if (!mounted || !tbody || !err) return; // page was unloaded mid-flight
  if (!background) err.innerHTML = "";
  msg.textContent = "Loading…";
  try {
    const qs = envId ? `?environment_id=${encodeURIComponent(envId)}` : "";
    const data = await api("/api/v1/maas/machines" + qs);
    if (!mounted || !document.getElementById("mach-tbody")) return;
    const machines = Array.isArray(data.machines) ? data.machines : [];
    if (data.maas_configured === false && !machines.length) {
      // Unconfigured globally and for this env: direction, not fake machines.
      msg.textContent = "";
      tbody.innerHTML = `<tr><td colspan="6" class="muted">
        No machines here. Register the server on the bare-metal page.
        The console answers DHCP and serves the boot file.</td></tr>`;
      return;
    }
    msg.textContent = data.mock ? "mock inventory" : `${machines.length} machine(s)`;
    tbody.innerHTML =
      machines
        .map((m, i) => {
          const tags = (m.tag_names || m.tags || []).join(", ");
          const ips = (m.ip_addresses || []).join(", ");
          return `<tr>
            <td><strong>${esc(m.hostname || m.fqdn || m.system_id)}</strong><div class="muted" style="font-size:.75rem">${esc(m.system_id || "")}</div></td>
            <td>${esc(m.status_name || m.status || "")}</td>
            <td id="power-cell-${i}">${esc(m.power_state || "")}</td>
            <td class="muted">${esc(ips)}</td>
            <td class="muted">${esc(tags)}</td>
            <td><button class="secondary btn-sm" type="button" data-power="${esc(m.system_id || "")}" data-row="${i}" ${m.system_id ? "" : "disabled"}>Power</button></td>
          </tr>`;
        })
        .join("") || `<tr><td colspan="6" class="muted">No machines</td></tr>`;
    tbody.querySelectorAll("button[data-power]").forEach((btn) =>
      btn.addEventListener("click", () => queryPower(btn))
    );
    const updated = document.getElementById("mach-updated");
    if (updated) updated.textContent = "updated " + new Date().toLocaleTimeString();
  } catch (e) {
    if (!mounted || !document.getElementById("mach-tbody")) return;
    msg.textContent = "";
    tbody.innerHTML = `<tr><td colspan="6" class="muted">Unavailable</td></tr>`;
    err.innerHTML = `<div class="error">${esc(e.message)}</div>`;
  }
}

async function queryPower(btn) {
  const systemId = btn.dataset.power;
  const cell = document.getElementById("power-cell-" + btn.dataset.row);
  btn.disabled = true;
  btn.textContent = "…";
  try {
    const qs = envId ? `?environment_id=${encodeURIComponent(envId)}` : "";
    const res = await api(`/api/v1/maas/machines/${encodeURIComponent(systemId)}/power` + qs);
    cell.textContent = res.power_state || "unknown";
    toast(`${systemId}: power ${res.power_state || "unknown"}`, "ok");
  } catch (e) {
    cell.innerHTML = `${esc(cell.textContent)} <span class="pill bad">query failed</span>`;
    toast(`Power query failed: ${e.message}`, "bad");
  } finally {
    btn.disabled = false;
    btn.textContent = "Power";
  }
}
