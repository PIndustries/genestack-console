// store.js — shared session state + role gating helpers.
import { api, esc } from "./api.js";
import { applyTenantFilter } from "./pages/tenant.js";

export const store = {
  role: null,      // "viewer" | "operator" | "admin" (from /api/v1/auth/whoami)
  keyName: null,   // API key name from whoami
  envs: [],        // cached environment list
  health: null,    // last /health payload
  degradedAuth: false, // true when whoami failed and role was assumed (operator)
};

const ROLE_ORDER = { viewer: 0, operator: 1, admin: 2 };

export function roleAtLeast(r) {
  const have = ROLE_ORDER[store.role];
  const need = ROLE_ORDER[r];
  return have !== undefined && need !== undefined && have >= need;
}
export function canRun() {
  return roleAtLeast("operator");
}
export function canAdmin() {
  return roleAtLeast("admin");
}

/** Returns 'disabled title=...' when the current role is insufficient. */
export function gate(allowed, requiredRole) {
  return allowed ? "" : `disabled title="Requires ${requiredRole} role"`;
}

// Bumped when a lifecycle event changes the list, so a GET that started
// earlier cannot write a deleted environment back into the store.
let envLoadSeq = 0;

export async function loadEnvs() {
  const seq = ++envLoadSeq;
  const rows = (await api("/api/v1/environments")) || [];
  if (seq !== envLoadSeq) return store.envs;
  store.envs = rows;
  return store.envs;
}

/** Drop or reload `store.envs` from one environment lifecycle event, then tell the shell. */
export function applyEnvLifecycle(payload) {
  const action = payload && payload.action;
  const id = payload && payload.environment_id ? String(payload.environment_id) : "";
  if (action === "deleted" && id) {
    envLoadSeq += 1;
    store.envs = (store.envs || []).filter((e) => e && String(e.id) !== id);
  }
  window.dispatchEvent(new CustomEvent("gsc-envs", { detail: payload || {} }));
}

/**
 * Make `store.envs` match the fleet board's environment rows.
 * Dispatches `gsc-envs` with action `reconcile` only when the list changed,
 * so a board refresh cannot reload itself.
 */
export function reconcileEnvs(rows) {
  const list = Array.isArray(rows) ? rows : [];
  const prev = store.envs || [];
  const prevById = new Map(prev.filter((e) => e && e.id).map((e) => [String(e.id), e]));
  const next = [];
  let changed = prev.length !== list.length;
  const seen = new Set();
  for (const row of list) {
    if (!row || !row.id || seen.has(String(row.id))) continue;
    seen.add(String(row.id));
    const id = String(row.id);
    const old = prevById.get(id);
    const name = row.name || (old && old.name) || id;
    const tenant_id = row.tenant_id != null ? row.tenant_id : (old ? old.tenant_id : null);
    if (!old || old.name !== name || old.tenant_id !== tenant_id) changed = true;
    next.push(old ? { ...old, id, name, tenant_id } : { id, name, tenant_id });
  }
  if (next.length !== prev.length) changed = true;
  if (!changed) return;
  envLoadSeq += 1;
  store.envs = next;
  window.dispatchEvent(new CustomEvent("gsc-envs", { detail: { action: "reconcile" } }));
}

/** Rewrite every environment `<select>` from the current store. Keeps the current value when it still exists. */
export function refreshEnvSelects(root) {
  const scope = root && root.querySelectorAll ? root : document;
  scope.querySelectorAll("select[data-gsc-env-select]").forEach((select) => {
    const previous = select.value;
    const noneLabel = select.getAttribute("data-gsc-env-none");
    const includeNone = noneLabel !== null;
    select.innerHTML = envOptionsHtml(previous, {
      includeNone,
      noneLabel: noneLabel || "(no environment)",
    });
    const still = Array.from(select.options).some((opt) => opt.value === previous);
    select.value = still ? previous : "";
  });
}

export function isDemoEnv(env) {
  if (!env) return false;
  const meta = env.metadata || env.metadata_json || {};
  if (meta && meta.demo === true) return true;
  return String(env.name || "") === "walkthrough" && String(env.tier || "") === "demo";
}

export function envName(id) {
  if (!id) return "—";
  const e = store.envs.find((x) => x.id === id);
  return e ? e.name : id;
}

export function envOptionsHtml(selectedId, { includeNone = false, noneLabel = "(no environment)" } = {}) {
  const opts = applyTenantFilter(store.envs).map(
    (e) =>
      `<option value="${esc(e.id)}"${e.id === selectedId ? " selected" : ""}>${esc(e.name)}${isDemoEnv(e) ? " (sample)" : ""}</option>`
  );
  if (includeNone) opts.unshift(`<option value="">${esc(noneLabel)}</option>`);
  return opts.join("");
}
