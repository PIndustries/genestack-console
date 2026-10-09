// pages/environment_vault.js — names stored for the open environment.
import { api, downloadAuth, esc, toast } from "../api.js";
import { canRun } from "../store.js";

export function envVaultCardHtml() {
  return `<div class="card" id="env-vault-card">
    <div class="toolbar"><h2>Vault</h2></div>
    <p class="muted">Names stored for this environment. Values stay in the console. Download grabs kubeconfig and talosconfig. Regenerate is on Machines.</p>
    <div id="env-vault-items"><p class="muted">Loading…</p></div>
  </div>`;
}

function rank(row) {
  if (row.kind === "kubeconfig") return 0;
  if (row.kind === "talosconfig") return 1;
  return 2;
}

function itemsHtml(rows) {
  if (!rows.length) return `<p class="muted">Nothing is stored for this environment yet.</p>`;
  const ordered = rows.slice().sort((a, b) => {
    const byKind = rank(a) - rank(b);
    if (byKind) return byKind;
    return String(a.name || "").localeCompare(String(b.name || ""));
  });
  return `<ul>${ordered.map((row) => {
    const grab = canRun() && (row.kind === "kubeconfig" || row.kind === "talosconfig")
      ? ` <button type="button" class="secondary btn-sm" data-env-vault-dl="${esc(row.kind)}">Download</button>`
      : "";
    return `<li><code>${esc(row.name)}</code> <span class="muted">${esc(row.kind || "")}</span>${grab}</li>`;
  }).join("")}</ul>`;
}

let currentEnv = "";

export function wireEnvVault(getEnvId) {
  const card = document.getElementById("env-vault-card");
  if (!card) return;
  card.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-env-vault-dl]");
    if (!button || button.disabled) return;
    const envId = getEnvId();
    const kind = button.dataset.envVaultDl;
    if (!envId || (kind !== "kubeconfig" && kind !== "talosconfig")) return;
    button.disabled = true;
    try {
      const source = await downloadAuth(
        `/api/v1/environments/${encodeURIComponent(envId)}/access/${kind}`,
        kind
      );
      toast(
        source === "filed"
          ? "Saved in this environment's vault."
          : "Downloaded from this environment's vault.",
        "ok"
      );
      await loadEnvVault(envId);
    } catch (error) {
      toast(error && error.message ? error.message : "Download failed.", "bad");
    } finally {
      button.disabled = false;
    }
  });
}

export async function loadEnvVault(envId) {
  const list = document.getElementById("env-vault-items");
  if (!list) return;
  currentEnv = envId || "";
  if (!envId) {
    list.innerHTML = `<p class="muted">Select an environment.</p>`;
    return;
  }
  let env;
  try {
    env = await api(`/api/v1/environments/${encodeURIComponent(envId)}`);
  } catch (error) {
    if (currentEnv !== envId) return;
    list.innerHTML = `<div class="error">${esc(error && error.message ? error.message : "Could not load this environment.")}</div>`;
    return;
  }
  if (currentEnv !== envId) return;
  const tenantId = env && env.tenant_id;
  if (!tenantId) {
    list.innerHTML = `<p class="muted">This environment is not in a tenant, so it has no vault.</p>`;
    return;
  }
  try {
    const payload = await api(
      `/api/v1/vault/items?tenant_id=${encodeURIComponent(tenantId)}&environment_id=${encodeURIComponent(envId)}`
    );
    if (currentEnv !== envId) return;
    const rows = payload && Array.isArray(payload.items) ? payload.items : [];
    list.innerHTML = itemsHtml(rows);
  } catch (error) {
    if (currentEnv !== envId) return;
    list.innerHTML = `<div class="error">${esc(error && error.message ? error.message : "Could not load the vault.")}</div>`;
  }
}

export function destroyEnvVault() {
  currentEnv = "";
}
