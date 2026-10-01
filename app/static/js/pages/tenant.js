// pages/tenant.js — tenant switcher (top nav) + shared tenant-selection helpers.
//
// The selection is UI-side only: the server already scopes /api/v1/environments
// to the caller's memberships, so the switcher just filters env pickers/lists
// client-side via applyTenantFilter(). Other page modules can import
// currentTenantId() / applyTenantFilter() to apply the selection.
import { esc } from "../api.js";

const TENANT_STORAGE = "gs_console_tenant";

/** Currently selected tenant id, or "" for "All tenants". */
export function currentTenantId() {
  return localStorage.getItem(TENANT_STORAGE) || "";
}

export function setCurrentTenantId(id) {
  if (id) localStorage.setItem(TENANT_STORAGE, id);
  else localStorage.removeItem(TENANT_STORAGE);
}

/** Filter an environment list to the selected tenant ("" = no filtering). */
export function applyTenantFilter(envs) {
  const tid = currentTenantId();
  if (!tid) return envs || [];
  return (envs || []).filter((e) => e && e.tenant_id === tid);
}

/**
 * Populate and wire the top-nav tenant <select>.
 * opts: { platformAdmin, tenants, fetchTenants, onChange }
 *  - tenants: whoami memberships ([{id, name, role}])
 *  - fetchTenants: () => Promise<[TenantRead]> — used for platform_admin when
 *    whoami returned no memberships (e.g. static API keys)
 *  - onChange: (tenantId) => void — called after the selection is persisted
 */
export async function initTenantSwitcher(select, { platformAdmin, tenants, fetchTenants, onChange }) {
  let list = Array.isArray(tenants) ? tenants.slice() : [];
  if (platformAdmin && !list.length && fetchTenants) {
    try {
      list = (await fetchTenants()) || [];
    } catch {
      list = [];
    }
  }

  if (!platformAdmin && !list.length) {
    select.classList.add("hidden");
    setCurrentTenantId("");
    return;
  }

  const opts = [];
  if (platformAdmin) opts.push('<option value="">All tenants</option>');
  for (const t of list) {
    opts.push(`<option value="${esc(t.id)}">${esc(t.name)}</option>`);
  }
  select.innerHTML = opts.join("");

  // Restore the persisted selection; fall back when it is no longer valid.
  const valid = new Set(list.map((t) => t.id));
  let cur = currentTenantId();
  if (cur && !valid.has(cur)) cur = "";
  if (!cur && !platformAdmin && list.length) cur = list[0].id;
  setCurrentTenantId(cur);
  select.value = cur;
  select.classList.remove("hidden");

  select.onchange = () => {
    setCurrentTenantId(select.value);
    window.dispatchEvent(new CustomEvent("tenantchange", { detail: { tenantId: select.value } }));
    if (onChange) onChange(select.value);
  };
}

/** Reset the switcher (logout). */
export function resetTenantSwitcher(select) {
  select.onchange = null;
  select.innerHTML = "";
  select.classList.add("hidden");
}
