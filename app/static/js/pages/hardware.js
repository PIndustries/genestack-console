// pages/hardware.js — Hardware page: one environment selector + three tabs that
// reuse the existing modules untouched — "Discovery" (the environment_discovery
// card), "Bare metal" (the environment_baremetal card), and "MAAS machines"
// (the fleet-wide machines page). Tabs mount lazily on first open and stay
// mounted afterwards (their element ids don't collide); changing the env
// selector reloads every already-mounted tab.
import { api, esc, toast } from "../api.js";
import { store, loadEnvs, envOptionsHtml, canAdmin, canRun, gate } from "../store.js";
import { applyTenantFilter } from "./tenant.js";
import {
  discoveryCardHtml,
  wireDiscoveryCard,
  loadDiscoveryCard,
  destroyDiscoveryCard,
} from "./environment_discovery.js";
import {
  baremetalCardHtml,
  wireBaremetalCard,
  loadBaremetalCard,
  destroyBaremetalCard,
} from "./environment_baremetal.js";
import * as machines from "./machines.js";

export const title = "Hardware";

const TABS = [
  { id: "discovery", label: "Inventory" },
  { id: "baremetal", label: "Bare metal" },
  { id: "providers", label: "Providers" },
  { id: "maas", label: "MAAS" },
];
const TAB_ALIAS = { ovh: "providers", inventory: "discovery", redfish: "discovery" };

let envId = "";
const mounted = new Set(); // tab ids already wired/loaded

export function destroy() {
  // The card modules' poll timers guard on their root element existing, so
  // clearing the DOM (router does that) stops them; the destroy fns also reset
  // module-level row locks / done-id sets that would otherwise survive a
  // remount (e.g. a bare-metal job abandoned mid-flight).
  destroyDiscoveryCard();
  destroyBaremetalCard();
  mounted.clear();
  envId = "";
}

export async function render(root, { query } = {}) {
  destroy();
  if (!store.envs.length) await loadEnvs().catch(() => {});
  const envs = applyTenantFilter(store.envs);
  envId = envs.length ? envs[0].id : "";
  const requested = TAB_ALIAS[(query && query.get("tab")) || ""] || (query && query.get("tab"));
  const initialTab = TABS.some((t) => t.id === requested) ? requested : "discovery";

  root.innerHTML = `
  <div class="card">
    <div class="toolbar">
      <h2>Hardware</h2>
      <select id="hw-env" title="Environment for discovery &amp; bare metal">${envOptionsHtml(envId, {
        includeNone: true,
        noneLabel: "(no environment)",
      })}</select>
      <span class="muted" style="font-size:.78rem">inventory &amp; bare metal (Redfish BMC) are per-environment; providers &amp; MAAS are fleet-wide</span>
    </div>
    <div class="tab-bar" role="tablist">
      ${TABS.map(
        (t) =>
          `<button class="tab-btn" type="button" role="tab" data-hw-tab="${t.id}">${t.label}</button>`
      ).join("")}
    </div>
  </div>
  <div id="hw-pane-discovery" class="hw-pane hidden">${discoveryCardHtml()}</div>
  <div id="hw-pane-baremetal" class="hw-pane hidden">${baremetalCardHtml()}</div>
  <div id="hw-pane-providers" class="hw-pane hidden"></div>
  <div id="hw-pane-maas" class="hw-pane hidden"></div>`;

  document.getElementById("hw-env").addEventListener("change", (e) => {
    envId = e.target.value;
    reloadMounted();
  });
  root.querySelectorAll("[data-hw-tab]").forEach((btn) =>
    btn.addEventListener("click", () => activateTab(btn.dataset.hwTab))
  );
  wireDiscoveryCard(() => envId);
  wireBaremetalCard(() => envId);

  await activateTab(initialTab, { keepHash: true });
}

async function activateTab(id, { keepHash = false } = {}) {
  if (!keepHash) {
    history.replaceState(null, "", "#/hardware" + (id === "discovery" ? "" : "?tab=" + id));
  }
  document.querySelectorAll("[data-hw-tab]").forEach((b) =>
    b.classList.toggle("active", b.dataset.hwTab === id)
  );
  TABS.forEach((t) => {
    const pane = document.getElementById("hw-pane-" + t.id);
    if (pane) pane.classList.toggle("hidden", t.id !== id);
  });
  if (mounted.has(id)) return;
  mounted.add(id);
  if (id === "discovery") {
    await loadDiscoveryCard(envId);
  } else if (id === "baremetal") {
    await loadBaremetalCard(envId);
  } else if (id === "providers") {
    await loadProvidersTab();
  } else if (id === "maas") {
    const pane = document.getElementById("hw-pane-maas");
    if (!pane) return;
    await machines.render(pane);
    syncMachinesEnv();
  }
}

// Push the page-level env selection into the machines module's own env select
// (machines.js owns its filter; dispatching change makes it reload).
function syncMachinesEnv() {
  const sel = document.getElementById("mach-env");
  if (!sel) return;
  sel.value = envId;
  sel.dispatchEvent(new Event("change"));
}

function reloadMounted() {
  if (mounted.has("discovery")) loadDiscoveryCard(envId);
  if (mounted.has("baremetal")) loadBaremetalCard(envId);
  if (mounted.has("providers")) loadProvidersTab();
  if (mounted.has("maas")) syncMachinesEnv();
}

const TF_KINDS = [
  { id: "rackspace", label: "Rackspace", fields: ["username", "api_key"], region: "region" },
  { id: "aws", label: "AWS", fields: ["access_key", "secret_key"], region: "region" },
  { id: "azure", label: "Azure", fields: ["tenant_id", "client_id", "client_secret", "subscription_id"], region: "location" },
  { id: "gcp", label: "GCP", fields: ["project_id", "service_account_json"], region: "region" },
];

async function loadProvidersTab() {
  const pane = document.getElementById("hw-pane-providers");
  if (!pane) return;
  pane.innerHTML = '<div class="card"><div class="muted" style="font-size:.82rem">Loading providers…</div></div>';
  let tf = { accounts: [] };
  let ovh = { accounts: [] };
  try {
    tf = await api("/api/v1/hardware/accounts");
  } catch (e) {
    tf = { accounts: [], error: e.message };
  }
  try {
    ovh = await api("/api/v1/ovh/accounts/overview");
  } catch (e) {
    ovh = { accounts: [], error: e.message };
  }
  const byKind = {};
  (tf.accounts || []).forEach((a) => {
    (byKind[a.kind] || (byKind[a.kind] = [])).push(a);
  });
  const envName = (id) => store.envs.find((x) => x.id === id)?.name || id;
  const tfCards = TF_KINDS.map((k) => {
    const rows = byKind[k.id] || [];
    const body = rows.length
      ? `<table class="tbl" style="width:100%"><thead><tr><th>Account</th><th>Region</th><th></th><th></th></tr></thead><tbody>${rows
          .map((a) => {
            const runTitle = !envId
              ? "Select an environment"
              : !canRun()
                ? "Requires operator role"
                : "";
            const runGate = !envId || !a.has_credentials ? `disabled title="${esc(runTitle || "No credentials")}"` : gate(canRun(), "operator");
            const actions = a.has_credentials
              ? `<label class="field" style="display:inline-flex;align-items:center;gap:.35rem;margin:0">
                  <span class="muted" style="font-size:.72rem">count</span>
                  <input data-hw-tf-count="${esc(a.id)}" type="number" min="1" max="32" value="1" style="width:3.6rem" ${runGate} />
                </label>
                <button class="secondary btn-sm" type="button" data-hw-tf-plan="${esc(a.id)}" data-hw-kind="${esc(k.id)}" ${runGate}>Plan</button>
                <button class="btn-sm" type="button" data-hw-tf-apply="${esc(a.id)}" data-hw-kind="${esc(k.id)}" ${runGate}>Apply</button>`
              : "";
            return `<tr>
              <td><strong>${esc(a.name)}</strong></td>
              <td class="muted">${esc(a.region || "—")}</td>
              <td>${a.has_credentials ? '<span class="pill ok">keys stored</span>' : ""}</td>
              <td style="white-space:nowrap">${actions}</td>
            </tr>`;
          })
          .join("")}</tbody></table>`
      : `<p class="muted" style="font-size:.78rem;margin:.35rem 0">No account yet. Store Terraform credentials — metal is yours, the Console just holds the keys.</p>`;
    const fields = k.fields
      .map(
        (f) =>
          `<label class="field"><span>${esc(f.split("_").join(" "))}</span><input data-hw-tf-field="${esc(f)}" type="${
            f.indexOf("secret") >= 0 || f.indexOf("json") >= 0 || f.indexOf("key") >= 0 ? "password" : "text"
          }" autocomplete="off" /></label>`
      )
      .join("");
    const form = canAdmin()
      ? `<form data-hw-tf-form="${esc(k.id)}" class="form-grid" style="margin-top:.65rem">
        <label class="field"><span>Name</span><input name="name" required placeholder="${esc(k.id)}-prod" autocomplete="off" /></label>
        <label class="field"><span>${esc(k.region)}</span><input name="region" placeholder="optional" autocomplete="off" /></label>
        ${fields}
        <div><button type="submit" class="btn-sm">Save account</button></div>
      </form>`
      : `<p class="muted" style="font-size:.75rem">Platform admin stores Terraform keys here.</p>`;
    return `<div class="card" data-hw-kind="${esc(k.id)}" style="margin-top:.75rem">
      <div class="toolbar"><h3>${esc(k.label)} · Terraform</h3></div>
      ${body}
      ${form}
    </div>`;
  }).join("");

  const ovhRows = (ovh.accounts || []).length
    ? ovh.accounts
        .map((a) => {
          const bound = (a.bound_environment_ids || [])
            .map((eid) => `<a href="#/environments/${esc(eid)}">${esc(envName(eid))}</a>`)
            .join(" · ");
          return `<tr>
            <td><strong>${esc(a.name)}</strong></td>
            <td class="muted">${esc(a.endpoint)}</td>
            <td>${a.has_consumer_key ? '<span class="pill ok">approved</span>' : '<span class="pill">not connected</span>'}</td>
            <td>${bound || '<span class="muted">—</span>'}</td>
          </tr>`;
        })
        .join("")
    : `<tr><td colspan="4" class="muted">No OVH API accounts. Add one in <a href="#/admin">Admin → OVH</a> if you use that provider.</td></tr>`;

  pane.innerHTML = `
  <div class="card">
    <h3>How metal arrives</h3>
    <p class="muted" style="font-size:.78rem">Terraform bare metal for Rackspace, AWS, Azure, and GCP — add an account below. OVH via API. PXE, SSH, and BMC/Redfish on the Inventory and Bare metal tabs.</p>
  </div>
  ${tf.error ? `<div class="error">${esc(tf.error)}</div>` : ""}
  ${tfCards}
  <div class="card" style="margin-top:.75rem">
    <h3>OVH · provider API</h3>
    ${ovh.error ? `<div class="error">${esc(ovh.error)}</div>` : ""}
    <table class="tbl" style="width:100%">
      <thead><tr><th>Account</th><th>Endpoint</th><th>Consumer key</th><th>Bound environments</th></tr></thead>
      <tbody>${ovhRows}</tbody>
    </table>
  </div>`;

  pane.querySelectorAll("[data-hw-tf-form]").forEach((form) => {
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const kind = form.dataset.hwTfForm;
      const credentials = {};
      form.querySelectorAll("[data-hw-tf-field]").forEach((inp) => {
        if (inp.value.trim()) credentials[inp.dataset.hwTfField] = inp.value.trim();
      });
      try {
        await api("/api/v1/hardware/accounts", {
          method: "POST",
          body: JSON.stringify({
            kind,
            name: form.querySelector("[name=name]").value.trim(),
            region: form.querySelector("[name=region]").value.trim(),
            credentials,
          }),
        });
        await loadProvidersTab();
      } catch (e) {
        pane.insertAdjacentHTML("afterbegin", `<div class="error">${esc(e.message)}</div>`);
      }
    });
  });

  pane.querySelectorAll("[data-hw-tf-plan], [data-hw-tf-apply]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const apply = btn.hasAttribute("data-hw-tf-apply");
      const accountId = apply ? btn.dataset.hwTfApply : btn.dataset.hwTfPlan;
      startTfJob(accountId, btn.dataset.hwKind || "", apply);
    });
  });
}

async function startTfJob(accountId, kind, apply) {
  if (!envId) {
    toast("Select an environment to plan or apply Terraform", "bad");
    return;
  }
  const operation = apply ? "hardware.terraform.apply" : "hardware.terraform.plan";
  const label = apply ? "apply" : "plan";
  if (apply && !confirm(`Apply Terraform (${kind || "account"}) into the selected environment inventory?`)) {
    return;
  }
  const countEl = document.querySelector(`[data-hw-tf-count="${accountId}"]`);
  const count = countEl ? parseInt(countEl.value, 10) : 1;
  const params = { account_id: accountId };
  if (Number.isFinite(count) && count > 0) params.count = count;
  try {
    const job = await api(`/api/v1/environments/${encodeURIComponent(envId)}/jobs`, {
      method: "POST",
      body: JSON.stringify({ operation, params }),
    });
    const id = job && job.id != null ? String(job.id) : "";
    toast(`${kind || "terraform"} ${label} job ${id.slice(0, 8)}… created`, "ok");
  } catch (e) {
    toast(`${label} failed to start: ${e.message}`, "bad");
    if (e.status === 403) toast("Insufficient role: operator required", "bad");
  }
}
