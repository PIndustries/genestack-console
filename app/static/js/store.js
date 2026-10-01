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

export async function loadEnvs() {
  store.envs = (await api("/api/v1/environments")) || [];
  return store.envs;
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
