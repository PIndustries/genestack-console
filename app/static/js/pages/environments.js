// pages/environments.js — env list + identity card. Empty state is the
// Metal → Talos → Kubernetes → OpenStack → Prove stand-up board, same as fleet.
import { api, esc, fmtTime, toast } from "../api.js";
import { loadEnvs, canRun, canAdmin, gate, isDemoEnv } from "../store.js";
import { applyTenantFilter, currentTenantId } from "./tenant.js";

export const title = "Environments";

let selectedId = null;

export async function render(root, { param }) {
  root.innerHTML = `
  <div class="grid">
    <div class="card span-7">
      <div class="toolbar">
        <h2>Registered environments</h2>
        <button class="btn-sm" id="btn-new-env" type="button" ${gate(canRun(), "operator")}>+ New environment</button>
      </div>
      <div id="env-list-err"></div>
      <div id="env-hero" class="hero hidden">
        <h3>Stand up a cloud</h3>
        <p class="muted">Metal → Talos → Kubernetes → OpenStack → Prove</p>
        <div class="fl-steps" style="max-width:36rem;margin:.6rem auto 1rem">
          <div class="fl-seg current pending"><div class="fl-seg-bar"></div><div class="fl-seg-label">Metal</div></div>
          <div class="fl-seg pending"><div class="fl-seg-bar"></div><div class="fl-seg-label">Talos</div></div>
          <div class="fl-seg pending"><div class="fl-seg-bar"></div><div class="fl-seg-label">Kubernetes</div></div>
          <div class="fl-seg pending"><div class="fl-seg-bar"></div><div class="fl-seg-label">OpenStack</div></div>
          <div class="fl-seg pending"><div class="fl-seg-bar"></div><div class="fl-seg-label">Prove</div></div>
        </div>
        <p class="muted">How does metal arrive?</p>
        <div class="row" style="justify-content:center;margin:.4rem 0 0">
          <a class="btn-sm" href="#/setup?metal=pxe">Discover on L2</a>
          <a class="btn-sm" href="#/setup?metal=static">Paste IPs</a>
          <a class="btn-sm" href="#/setup?metal=ovh">OVH</a>
          <a class="btn-sm" href="#/setup?metal=terraform">Terraform</a>
          <a class="btn-sm" href="#/setup?metal=bmc">BMC</a>
        </div>
        <p class="muted" style="margin:.8rem 0 0;font-size:.8rem"><a href="#/setup">Advanced setup</a></p>
      </div>
      <table id="env-table">
        <thead><tr><th>Name</th><th>Region</th><th>Tier</th><th>ID</th><th></th></tr></thead>
        <tbody id="env-tbody"><tr><td colspan="5" class="muted">Loading…</td></tr></tbody>
      </table>
    </div>
    <div class="card span-5" id="env-detail">
      <h2>Environment detail</h2>
      <div class="muted" id="env-detail-body">Select an environment.</div>
    </div>
  </div>`;

  document.getElementById("btn-new-env").addEventListener("click", () => {
    location.hash = "#/setup";
  });

  await reloadList();
  if (param) selectEnv(param);
}

async function reloadList() {
  const tbody = document.getElementById("env-tbody");
  const err = document.getElementById("env-list-err");
  err.innerHTML = "";
  try {
    const envs = applyTenantFilter(await loadEnvs());
    const firstRun = !envs.length && !currentTenantId();
    document.getElementById("env-hero").classList.toggle("hidden", !firstRun);
    document.getElementById("env-table").classList.toggle("hidden", firstRun);
    const detail = document.getElementById("env-detail");
    if (detail) detail.classList.toggle("hidden", firstRun);
    const emptyMsg = currentTenantId()
      ? "No environments in the selected tenant"
      : "No environments yet";
    tbody.innerHTML =
      envs
        .map(
          (e) => `<tr class="clickable${e.id === selectedId ? " selected" : ""}" data-env="${esc(e.id)}">
            <td><strong>${esc(e.name)}</strong>${isDemoEnv(e) ? ' <span class="pill warn">walkthrough</span>' : ""}<div class="muted">${esc(e.description || "")}</div></td>
            <td>${esc(e.region || "")}</td>
            <td>${isDemoEnv(e) ? '<span class="pill">sample</span>' : esc(e.tier || "")}</td>
            <td class="muted" style="font-size:.75rem">${esc(e.id)}</td>
            <td><button class="secondary btn-sm" type="button" data-detail="${esc(e.id)}">Open cloud</button></td>
          </tr>`
        )
        .join("") || `<tr><td colspan="5" class="muted">${emptyMsg}</td></tr>`;
    tbody.querySelectorAll("tr[data-env]").forEach((tr) =>
      tr.addEventListener("click", () => selectEnv(tr.dataset.env))
    );
    tbody.querySelectorAll("button[data-detail]").forEach((btn) =>
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        location.hash = "#/environment_detail/" + btn.dataset.detail + "?tab=workflow";
      })
    );
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" class="muted">Unavailable</td></tr>`;
    err.innerHTML = `<div class="error">${esc(e.message)}</div>`;
  }
}

async function selectEnv(id) {
  selectedId = id;
  history.replaceState(null, "", "#/environments/" + id);
  document.querySelectorAll("#env-tbody tr[data-env]").forEach((tr) =>
    tr.classList.toggle("selected", tr.dataset.env === id)
  );
  const body = document.getElementById("env-detail-body");
  body.innerHTML = "Loading…";
  let env;
  try {
    env = await api(`/api/v1/environments/${encodeURIComponent(id)}`);
  } catch (e) {
    body.innerHTML = `<div class="error">${esc(e.message)}</div>`;
    return;
  }

  body.innerHTML = `
    <div style="margin-bottom:.5rem">
      <h2 style="margin:0">${esc(env.name)}${isDemoEnv(env) ? ' <span class="pill warn">walkthrough</span> <span class="pill">sample</span>' : ""}</h2>
      <div class="muted">${esc(env.description || "")}</div>
    </div>
    ${isDemoEnv(env) ? '<div class="demo-banner" style="margin:.6rem 0">This is a sample full deployment so you can click around. It is not your metal. Create a new environment to provision.</div>' : ""}
    <div style="margin-top:.5rem">
      <div><strong>ID:</strong> <span class="muted">${esc(env.id)}</span></div>
      <div><strong>Region:</strong> <span class="muted">${esc(env.region || "")}</span></div>
      <div><strong>Tier:</strong> <span class="pill">${esc(env.tier || "")}</span></div>
      <div><strong>Created:</strong> <span class="muted">${esc(fmtTime(env.created_at))}</span></div>
      <div><strong>Updated:</strong> <span class="muted">${esc(fmtTime(env.updated_at))}</span></div>
    </div>
    <div class="row" style="margin-top:1rem; gap:.5rem">
      <button id="btn-env-workflow" type="button" ${gate(canRun(), "operator")}>Open cloud</button>
      <button class="danger btn-sm" id="btn-env-delete" type="button" ${gate(canAdmin(), "admin")}>Delete</button>
    </div>`;

  document.getElementById("btn-env-workflow").addEventListener("click", () => {
    location.hash = "#/environment_detail/" + env.id + "?tab=workflow";
  });
  document.getElementById("btn-env-delete").addEventListener("click", () => deleteEnv(env));
}

async function deleteEnv(env) {
  if (!window.confirm(`Delete environment '${env.name}'?\n\nThis cannot be undone.`)) return;
  try {
    await api(`/api/v1/environments/${encodeURIComponent(env.id)}`, { method: "DELETE" });
    toast(`Environment '${env.name}' deleted`, "ok");
    selectedId = null;
    history.replaceState(null, "", "#/environments");
    document.getElementById("env-detail-body").innerHTML =
      '<div class="muted">Select an environment.</div>';
    await reloadList();
  } catch (e) {
    toast(`Delete failed: ${e.message}`, "bad");
  }
}
