// pages/alerts.js — firing/resolved alert events with ack, plus alert rule
// management (create/edit/delete). Firing list refreshes over the "alerts" SSE
// topic; the firing-count badge in the nav is owned by app.js.
import { api, esc, fmtTime, fmtAge, toast } from "../api.js";
import { store, loadEnvs, envName, envOptionsHtml, canRun, gate } from "../store.js";
import { connect } from "../stream.js";

export const title = "Alerts";

const CONDITIONS = ["node_not_ready", "pod_crashloop", "probe_failed", "service_down"];
const SEVERITIES = ["info", "warning", "critical"];

let streamHandle = null;
let editingId = null;

export function destroy() {
  if (streamHandle) {
    streamHandle.close();
    streamHandle = null;
  }
  editingId = null;
}

export async function render(root) {
  if (!store.envs.length) await loadEnvs().catch(() => {});
  root.innerHTML = `
  <div class="card">
    <div class="toolbar">
      <h2>Firing alerts</h2>
      <select id="f-env">${envOptionsHtml(null, { includeNone: true, noneLabel: "all environments" })}</select>
      <button class="secondary btn-sm" id="btn-al-refresh" type="button">Refresh</button>
      <span id="al-msg" class="muted"></span>
    </div>
    <div id="al-err"></div>
    <table>
      <thead><tr><th>Severity</th><th>Rule</th><th>Environment</th><th>Fired</th><th>Details</th><th></th></tr></thead>
      <tbody id="al-tbody"><tr><td colspan="6" class="muted">Loading…</td></tr></tbody>
    </table>
    <details style="margin-top:.75rem">
      <summary class="muted">Resolved alerts</summary>
      <table style="margin-top:.5rem">
        <thead><tr><th>Severity</th><th>Rule</th><th>Environment</th><th>Fired</th><th>Details</th></tr></thead>
        <tbody id="al-resolved-tbody"><tr><td colspan="5" class="muted">Loading…</td></tr></tbody>
      </table>
    </details>
  </div>
  <div class="card" style="margin-top:1rem">
    <h2>Alert rules</h2>
    <div id="rules-err"></div>
    <table>
      <thead><tr><th>Name</th><th>Condition</th><th>Severity</th><th>Threshold</th><th>Environment</th><th>Webhook</th><th>Enabled</th><th></th></tr></thead>
      <tbody id="rules-tbody"><tr><td colspan="8" class="muted">Loading…</td></tr></tbody>
    </table>
    <h3 id="rule-form-title" style="font-size:.9rem;margin:1rem 0 .5rem">New rule</h3>
    <form id="rule-form">
      <div class="grid">
        <label class="field span-4"><span>Name</span><input id="rf-name" type="text" required /></label>
        <label class="field span-4"><span>Condition</span><select id="rf-condition">${CONDITIONS.map(
          (c) => `<option value="${c}">${c}</option>`
        ).join("")}</select></label>
        <label class="field span-2"><span>Severity</span><select id="rf-severity">${SEVERITIES.map(
          (s) => `<option value="${s}">${s}</option>`
        ).join("")}</select></label>
        <label class="field span-2"><span>Threshold</span><input id="rf-threshold" type="number" min="1" step="1" value="1" /></label>
        <label class="field span-4"><span>Environment</span><select id="rf-env">${envOptionsHtml(null, {
          includeNone: true,
          noneLabel: "(all environments)",
        })}</select></label>
        <label class="field span-6"><span>Webhook URL</span><input id="rf-webhook" type="text" placeholder="https://…" /></label>
        <label class="span-2 row" style="align-items:end; gap:.4rem"><input id="rf-enabled" type="checkbox" checked /> <span class="muted">enabled</span></label>
      </div>
      <div class="row" style="margin-top:.75rem">
        <button id="btn-rule-save" type="submit" ${gate(canRun(), "operator")}>Create rule</button>
        <button class="secondary btn-sm hidden" id="btn-rule-cancel" type="button">Cancel edit</button>
        <span id="rf-msg" class="muted"></span>
      </div>
    </form>
  </div>`;

  document.getElementById("btn-al-refresh").addEventListener("click", loadEvents);
  document.getElementById("f-env").addEventListener("change", loadEvents);
  document.getElementById("rule-form").addEventListener("submit", saveRule);
  document.getElementById("btn-rule-cancel").addEventListener("click", resetForm);

  await Promise.all([loadEvents(), loadRules()]);

  streamHandle = connect(["alerts"], {
    alerts: () => loadEvents(),
  });
}

// ---------- events ----------

function severityPill(s) {
  const k = String(s || "").toLowerCase();
  const cls = k === "critical" ? "bad" : k === "warning" ? "warn" : "";
  return `<span class="pill ${cls}">${esc(s || "info")}</span>`;
}

function eventRuleName(ev) {
  return ev.rule_name || (ev.rule && ev.rule.name) || ev.name || "—";
}

function eventDetailsHtml(ev) {
  const details = ev.details || ev.message || ev.labels || null;
  if (!details) return "";
  const text = typeof details === "string" ? details : JSON.stringify(details, null, 2);
  return `<details><summary>view</summary><pre class="log-inline">${esc(text)}</pre></details>`;
}

function eventRowHtml(ev, { firing }) {
  const ack = firing
    ? `<button class="secondary btn-sm" data-ack="${esc(ev.id)}" type="button" ${gate(canRun(), "operator")}>Ack</button>`
    : "";
  return `<tr>
    <td>${severityPill(ev.severity)}</td>
    <td>${esc(eventRuleName(ev))}</td>
    <td class="muted">${esc(envName(ev.environment_id))}</td>
    <td class="muted" title="${esc(fmtTime(ev.fired_at))}">${esc(fmtAge(ev.fired_at) || fmtTime(ev.fired_at))}</td>
    <td>${eventDetailsHtml(ev)}</td>
    ${firing ? `<td>${ack}</td>` : ""}
  </tr>`;
}

async function fetchEvents(status) {
  const qs = new URLSearchParams();
  qs.set("status", status);
  const envId = document.getElementById("f-env").value;
  if (envId) qs.set("environment_id", envId);
  qs.set("limit", "100");
  return (await api("/api/v1/alerts/events?" + qs.toString())) || [];
}

async function loadEvents() {
  const tbody = document.getElementById("al-tbody");
  const rtbody = document.getElementById("al-resolved-tbody");
  const err = document.getElementById("al-err");
  if (!tbody || !rtbody) return; // page was unloaded mid-flight
  err.innerHTML = "";

  let firing, resolved;
  try {
    [firing, resolved] = await Promise.all([fetchEvents("firing"), fetchEvents("resolved")]);
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6" class="muted">Unavailable</td></tr>`;
    rtbody.innerHTML = `<tr><td colspan="5" class="muted">Unavailable</td></tr>`;
    err.innerHTML = `<div class="error">${esc(e.message)}</div>`;
    return;
  }

  document.getElementById("al-msg").textContent = `${firing.length} firing`;
  tbody.innerHTML =
    firing.map((ev) => eventRowHtml(ev, { firing: true })).join("") ||
    `<tr><td colspan="6" class="muted">No firing alerts</td></tr>`;
  rtbody.innerHTML =
    resolved.map((ev) => eventRowHtml(ev, { firing: false })).join("") ||
    `<tr><td colspan="5" class="muted">No resolved alerts</td></tr>`;
  tbody.querySelectorAll("button[data-ack]").forEach((btn) =>
    btn.addEventListener("click", () => ackEvent(btn.dataset.ack))
  );
}

async function ackEvent(id) {
  try {
    await api(`/api/v1/alerts/events/${encodeURIComponent(id)}/ack`, { method: "POST" });
    toast("Alert acknowledged", "ok");
    await loadEvents();
  } catch (e) {
    toast(e.message, "bad");
    if (e.status === 403) toast("Insufficient role: operator required", "bad");
  }
}

// ---------- rules ----------

async function loadRules() {
  const tbody = document.getElementById("rules-tbody");
  const err = document.getElementById("rules-err");
  if (!tbody) return;
  err.innerHTML = "";

  let rules;
  try {
    rules = (await api("/api/v1/alerts/rules")) || [];
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="8" class="muted">Unavailable</td></tr>`;
    err.innerHTML = `<div class="error">${esc(e.message)}</div>`;
    return;
  }

  tbody.innerHTML =
    rules
      .map(
        (r) => `<tr>
        <td><strong>${esc(r.name)}</strong></td>
        <td><code>${esc(r.condition)}</code></td>
        <td>${severityPill(r.severity)}</td>
        <td class="muted">${esc(r.threshold != null ? r.threshold : "—")}</td>
        <td class="muted">${esc(r.environment_id ? envName(r.environment_id) : "all")}</td>
        <td class="muted">${esc(r.webhook_url || "—")}</td>
        <td><input type="checkbox" data-toggle-rule="${esc(r.id)}"${r.enabled ? " checked" : ""} ${
          canRun() ? "" : 'disabled title="Requires operator role"'
        } /></td>
        <td>
          <button class="secondary btn-sm" data-edit-rule="${esc(r.id)}" type="button" ${gate(canRun(), "operator")}>Edit</button>
          <button class="secondary btn-sm" data-del-rule="${esc(r.id)}" type="button" ${gate(canRun(), "operator")}>Delete</button>
        </td>
      </tr>`
      )
      .join("") || `<tr><td colspan="8" class="muted">No rules yet</td></tr>`;

  tbody.querySelectorAll("input[data-toggle-rule]").forEach((box) =>
    box.addEventListener("change", () => toggleRule(box.dataset.toggleRule, box.checked))
  );
  tbody.querySelectorAll("button[data-edit-rule]").forEach((btn) =>
    btn.addEventListener("click", () => startEdit(rules.find((r) => String(r.id) === btn.dataset.editRule)))
  );
  tbody.querySelectorAll("button[data-del-rule]").forEach((btn) =>
    btn.addEventListener("click", () => deleteRule(btn.dataset.delRule))
  );
}

function startEdit(rule) {
  if (!rule) return;
  editingId = rule.id;
  document.getElementById("rf-name").value = rule.name || "";
  document.getElementById("rf-condition").value = CONDITIONS.includes(rule.condition) ? rule.condition : CONDITIONS[0];
  document.getElementById("rf-severity").value = SEVERITIES.includes(rule.severity) ? rule.severity : "info";
  document.getElementById("rf-threshold").value = rule.threshold != null ? rule.threshold : 1;
  document.getElementById("rf-env").value = rule.environment_id || "";
  document.getElementById("rf-webhook").value = rule.webhook_url || "";
  document.getElementById("rf-enabled").checked = rule.enabled !== false;
  document.getElementById("rule-form-title").textContent = `Edit rule: ${rule.name || rule.id}`;
  document.getElementById("btn-rule-save").textContent = "Save rule";
  document.getElementById("btn-rule-cancel").classList.remove("hidden");
}

function resetForm() {
  editingId = null;
  document.getElementById("rule-form").reset();
  document.getElementById("rf-threshold").value = 1;
  document.getElementById("rf-enabled").checked = true;
  document.getElementById("rule-form-title").textContent = "New rule";
  document.getElementById("btn-rule-save").textContent = "Create rule";
  document.getElementById("btn-rule-cancel").classList.add("hidden");
  document.getElementById("rf-msg").textContent = "";
}

async function saveRule(e) {
  e.preventDefault();
  const msg = document.getElementById("rf-msg");
  const body = {
    name: document.getElementById("rf-name").value.trim(),
    condition: document.getElementById("rf-condition").value,
    severity: document.getElementById("rf-severity").value,
    threshold: Number(document.getElementById("rf-threshold").value) || 1,
    environment_id: document.getElementById("rf-env").value || null,
    webhook_url: document.getElementById("rf-webhook").value.trim(),
    enabled: document.getElementById("rf-enabled").checked,
  };
  msg.textContent = "Saving…";
  try {
    if (editingId) {
      await api(`/api/v1/alerts/rules/${encodeURIComponent(editingId)}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      });
    } else {
      await api("/api/v1/alerts/rules", { method: "POST", body: JSON.stringify(body) });
    }
    toast(editingId ? "Rule updated" : "Rule created", "ok");
    resetForm();
    await loadRules();
  } catch (e2) {
    msg.textContent = e2.message;
    if (e2.status === 403) toast("Insufficient role: operator required", "bad");
  }
}

async function toggleRule(id, enabled) {
  try {
    await api(`/api/v1/alerts/rules/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({ enabled }),
    });
  } catch (e) {
    toast(e.message, "bad");
    await loadRules(); // snap the checkbox back to the server state
  }
}

async function deleteRule(id) {
  if (!confirm("Delete this alert rule?")) return;
  try {
    await api(`/api/v1/alerts/rules/${encodeURIComponent(id)}`, { method: "DELETE" });
    toast("Rule deleted", "ok");
    if (editingId === id) resetForm();
    await loadRules();
  } catch (e) {
    toast(e.message, "bad");
    if (e.status === 403) toast("Insufficient role: operator required", "bad");
  }
}
