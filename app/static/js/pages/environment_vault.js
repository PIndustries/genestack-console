// pages/environment_vault.js — records stored for the open environment.
import { api, downloadAuth, esc, toast } from "../api.js";
import { canAdmin, canRun } from "../store.js";

export function envVaultCardHtml() {
  const add = canAdmin()
    ? `<button type="button" class="secondary btn-sm" data-env-vault-add>Add note</button>`
    : "";
  return `<div class="card" id="env-vault-card">
    <style>
      #env-vault-items ul { list-style: none; margin: 0; padding: 0; display: grid; gap: 0.35rem; }
      #env-vault-items li { display: flex; flex-wrap: wrap; align-items: center; gap: 0.35rem; }
      #env-vault-editor { margin-top: 0.85rem; display: grid; gap: 0.55rem; }
      #env-vault-editor[hidden] { display: none; }
    </style>
    <div class="toolbar"><h2>Vault</h2>${add}</div>
    <p class="muted">Names stored for this environment. An admin can view, edit, and delete a record. Values stay out of this list. Download grabs kubeconfig and talosconfig. Regenerate is on Machines.</p>
    <div id="env-vault-items"><p class="muted">Loading…</p></div>
    <form id="env-vault-editor" hidden autocomplete="off">
      <p id="env-vault-editor-title"></p>
      <p class="muted" id="env-vault-hint"></p>
      <label class="field" id="env-vault-name-field" hidden>
        <span>Name</span>
        <input id="env-vault-name" type="text" autocomplete="off" spellcheck="false" maxlength="200" />
      </label>
      <label class="field">
        <span>Value</span>
        <textarea id="env-vault-value" rows="8" spellcheck="false" autocomplete="off"></textarea>
      </label>
      <div class="toolbar">
        <button type="submit" class="btn-sm" id="env-vault-save">Save</button>
        <button type="button" class="secondary btn-sm" data-env-vault-close>Close</button>
      </div>
    </form>
  </div>`;
}

function rank(row) {
  if (row.kind === "kubeconfig") return 0;
  if (row.kind === "talosconfig") return 1;
  return 2;
}

function actionsHtml(row) {
  const name = esc(row.name);
  const kind = esc(row.kind || "");
  const bits = [];
  if (canAdmin()) {
    bits.push(`<button type="button" class="secondary btn-sm" data-env-vault-view="${name}" data-env-vault-kind="${kind}">View</button>`);
    bits.push(`<button type="button" class="secondary btn-sm" data-env-vault-edit="${name}" data-env-vault-kind="${kind}">Edit</button>`);
    bits.push(`<button type="button" class="danger btn-sm" data-env-vault-del="${name}" data-env-vault-kind="${kind}">Delete</button>`);
  }
  if (canRun() && (row.kind === "kubeconfig" || row.kind === "talosconfig")) {
    bits.push(`<button type="button" class="secondary btn-sm" data-env-vault-dl="${esc(row.kind)}">Download</button>`);
  }
  return bits.join(" ");
}

function itemsHtml(rows) {
  if (!rows.length) return `<p class="muted">Nothing is stored for this environment yet.</p>`;
  const ordered = rows.slice().sort((a, b) => {
    const byKind = rank(a) - rank(b);
    if (byKind) return byKind;
    return String(a.name || "").localeCompare(String(b.name || ""));
  });
  return `<ul>${ordered.map((row) => {
    return `<li><code>${esc(row.name)}</code> <span class="muted">${esc(row.kind || "")}</span>${actionsHtml(row)}</li>`;
  }).join("")}</ul>`;
}

let currentEnv = "";
let currentTenant = "";
let editing = null;
let editorSeq = 0;
let busy = false;

function editorEls() {
  return {
    form: document.getElementById("env-vault-editor"),
    title: document.getElementById("env-vault-editor-title"),
    hint: document.getElementById("env-vault-hint"),
    nameField: document.getElementById("env-vault-name-field"),
    nameInput: document.getElementById("env-vault-name"),
    area: document.getElementById("env-vault-value"),
    save: document.getElementById("env-vault-save"),
  };
}

function closeEditor() {
  editorSeq += 1;
  editing = null;
  const els = editorEls();
  if (els.area) els.area.value = "";
  if (els.nameInput) els.nameInput.value = "";
  if (els.title) els.title.textContent = "";
  if (els.hint) els.hint.textContent = "";
  if (els.form) els.form.hidden = true;
}

function hintFor(kind, mode) {
  const clear = " Close clears this value from the page.";
  if (mode === "add") return `A new note is stored under secret/.${clear}`;
  if (kind === "ssh") {
    return `Saving replaces this environment's SSH key and its public key. The SSH Keys card shows the public key.${clear}`;
  }
  if (kind === "bmc") return `Saving replaces the management password for this machine.${clear}`;
  if (kind === "kubeconfig" || kind === "talosconfig") {
    return `Saving replaces the vault copy. The file on this console stays. Regenerate is on Machines.${clear}`;
  }
  return `Saving replaces this note.${clear}`;
}

function showEditor(mode, name, kind, value) {
  const els = editorEls();
  if (!els.form || !els.area) return;
  const adding = mode === "add";
  els.form.hidden = false;
  els.title.textContent = adding ? "New note" : `${mode === "view" ? "View" : "Edit"} ${name}`;
  els.hint.textContent = hintFor(kind, mode);
  els.nameField.hidden = !adding;
  if (els.nameInput) els.nameInput.value = "";
  els.area.value = value || "";
  els.area.readOnly = mode === "view";
  els.save.hidden = mode === "view";
  editing = { name: name || "", kind: kind || "", mode, envId: currentEnv, seq: editorSeq };
  if (adding && els.nameInput) els.nameInput.focus();
  else els.area.focus();
}

function recordUrl(name) {
  const params = new URLSearchParams({
    tenant_id: currentTenant,
    environment_id: currentEnv,
    name,
  });
  return `/api/v1/vault/records?${params}`;
}

function reservedName(name) {
  if (name === "ssh" || name === "kubeconfig" || name === "talosconfig") return true;
  return name.startsWith("bmc/");
}

async function openRecord(name, mode) {
  const envId = currentEnv;
  const seq = editorSeq;
  if (!envId || !currentTenant || !name || busy) return;
  busy = true;
  try {
    const payload = await api(recordUrl(name));
    if (seq !== editorSeq || currentEnv !== envId) return;
    showEditor(mode, payload.name || name, payload.kind || "", payload.value || "");
  } catch (error) {
    toast(error && error.message ? error.message : "Could not open that record.", "bad");
  } finally {
    busy = false;
  }
}

function deletePrompt(name, kind) {
  if (kind === "ssh" || name === "ssh") {
    return "Delete the SSH key for this environment? The SSH Keys card can generate a new pair. Machines that use this key will stop accepting it.";
  }
  if (kind === "bmc") {
    return `Delete the management password for ${name}? Power actions for that machine fail until a password is saved again.`;
  }
  if (kind === "kubeconfig" || kind === "talosconfig") {
    return `Delete the ${kind} stored in this vault? The copy on this console stays. The next download can file it again.`;
  }
  return `Delete ${name} from this vault?`;
}

async function removeRecord(name, kind) {
  const envId = currentEnv;
  if (!envId || !currentTenant || !name || busy) return;
  if (!window.confirm(deletePrompt(name, kind))) return;
  busy = true;
  try {
    await api(recordUrl(name), { method: "DELETE" });
    if (currentEnv !== envId) return;
    closeEditor();
    toast("Deleted from this environment's vault.", "ok");
    await loadEnvVault(envId);
  } catch (error) {
    toast(error && error.message ? error.message : "Could not delete that record.", "bad");
  } finally {
    busy = false;
  }
}

async function saveEditor() {
  if (!editing || editing.mode === "view" || busy) return;
  const envId = editing.envId;
  if (!envId || envId !== currentEnv || !currentTenant) {
    closeEditor();
    return;
  }
  const els = editorEls();
  const name = editing.mode === "add"
    ? (els.nameInput ? els.nameInput.value.trim() : "")
    : editing.name;
  const value = els.area ? els.area.value : "";
  if (!name || !value.trim()) {
    toast("A secret needs a name and a value.", "bad");
    return;
  }
  if (editing.mode === "add" && reservedName(name)) {
    toast("Edit that record from its row.", "bad");
    return;
  }
  busy = true;
  if (els.save) els.save.disabled = true;
  try {
    await api("/api/v1/vault/records", {
      method: "PUT",
      body: JSON.stringify({
        tenant_id: currentTenant,
        environment_id: envId,
        name,
        value,
      }),
    });
    if (currentEnv !== envId) return;
    closeEditor();
    toast("Saved in this environment's vault.", "ok");
    await loadEnvVault(envId);
  } catch (error) {
    toast(error && error.message ? error.message : "Could not save that record.", "bad");
  } finally {
    busy = false;
    const save = document.getElementById("env-vault-save");
    if (save) save.disabled = false;
  }
}

export function wireEnvVault(getEnvId) {
  const card = document.getElementById("env-vault-card");
  if (!card) return;
  card.addEventListener("click", async (event) => {
    if (event.target.closest("#env-vault-editor")) return;
    const add = event.target.closest("[data-env-vault-add]");
    if (add) {
      if (!currentEnv || !currentTenant) {
        toast("Select an environment.", "bad");
        return;
      }
      showEditor("add", "", "note", "");
      return;
    }
    const del = event.target.closest("[data-env-vault-del]");
    if (del && !del.disabled) {
      await removeRecord(del.dataset.envVaultDel || "", del.dataset.envVaultKind || "");
      return;
    }
    const edit = event.target.closest("[data-env-vault-edit]");
    if (edit && !edit.disabled) {
      await openRecord(edit.dataset.envVaultEdit || "", "edit");
      return;
    }
    const view = event.target.closest("[data-env-vault-view]");
    if (view && !view.disabled) {
      await openRecord(view.dataset.envVaultView || "", "view");
      return;
    }
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
  const form = document.getElementById("env-vault-editor");
  if (!form) return;
  form.addEventListener("click", (event) => {
    if (event.target.closest("[data-env-vault-close]")) {
      event.preventDefault();
      closeEditor();
    }
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    saveEditor();
  });
}

export async function loadEnvVault(envId) {
  const list = document.getElementById("env-vault-items");
  if (!list) return;
  closeEditor();
  currentEnv = envId || "";
  currentTenant = "";
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
  currentTenant = tenantId;
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
  closeEditor();
  currentEnv = "";
  currentTenant = "";
}
