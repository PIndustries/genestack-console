// pages/audit.js — audit log with filters.
import { api, esc, fmtTime } from "../api.js";
import { store, loadEnvs, envName, envOptionsHtml } from "../store.js";

export const title = "Audit";

export async function render(root) {
  if (!store.envs.length) await loadEnvs().catch(() => {});
  root.innerHTML = `
  <div class="card">
    <div class="toolbar">
      <h2>Audit entries</h2>
      <select id="f-env">${envOptionsHtml(null, { includeNone: true, noneLabel: "all environments" })}</select>
      <input id="f-action" type="text" placeholder="action (e.g. environment.create)" style="min-width:16rem" />
      <button class="secondary btn-sm" id="btn-apply" type="button">Apply</button>
      <span id="audit-msg" class="muted"></span>
    </div>
    <div id="audit-err"></div>
    <table>
      <thead><tr><th>Time</th><th>Actor</th><th>Action</th><th>Resource</th><th>Environment</th><th>Result</th><th>Details</th></tr></thead>
      <tbody id="audit-tbody"><tr><td colspan="7" class="muted">Loading…</td></tr></tbody>
    </table>
  </div>`;

  document.getElementById("btn-apply").addEventListener("click", loadAudit);
  document.getElementById("f-env").addEventListener("change", loadAudit);
  document.getElementById("f-action").addEventListener("keydown", (e) => {
    if (e.key === "Enter") loadAudit();
  });
  await loadAudit();
}

async function loadAudit() {
  const tbody = document.getElementById("audit-tbody");
  const err = document.getElementById("audit-err");
  err.innerHTML = "";

  const qs = new URLSearchParams();
  const envId = document.getElementById("f-env").value;
  const action = document.getElementById("f-action").value.trim();
  if (envId) qs.set("environment_id", envId);
  if (action) qs.set("action", action);
  qs.set("limit", "200");

  try {
    const items = (await api("/api/v1/audit?" + qs.toString())) || [];
    document.getElementById("audit-msg").textContent = `${items.length} entr${items.length === 1 ? "y" : "ies"}`;
    tbody.innerHTML =
      items
        .map((a) => {
          const resource = [a.resource_type, a.resource_id].filter(Boolean).join("/");
          const details =
            a.details && Object.keys(a.details).length
              ? `<details><summary>view</summary><pre class="log-inline">${esc(JSON.stringify(a.details, null, 2))}</pre></details>`
              : "";
          const result = a.success
            ? '<span class="pill ok">ok</span>'
            : '<span class="pill bad">failed</span>';
          return `<tr>
            <td class="muted">${esc(fmtTime(a.timestamp))}</td>
            <td>${esc(a.actor)}</td>
            <td><code>${esc(a.action)}</code></td>
            <td class="muted">${esc(resource)}</td>
            <td class="muted">${esc(envName(a.environment_id))}</td>
            <td>${result}</td>
            <td>${details}</td>
          </tr>`;
        })
        .join("") || `<tr><td colspan="7" class="muted">No entries</td></tr>`;
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="7" class="muted">Unavailable</td></tr>`;
    err.innerHTML = `<div class="error">${esc(e.message)}</div>`;
  }
}
