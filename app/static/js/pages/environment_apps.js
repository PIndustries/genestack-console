// pages/environment_apps.js — Railway-style GitHub repo → deploy.
import { api, esc, toast } from "../api.js";
import { roleAtLeast } from "../store.js";

export function appsCardHtml() {
  return `<div class="card" id="apps-card">
    <style>
      #apps-card .apps-toolbar { display:flex; align-items:center; justify-content:space-between; gap:.5rem; flex-wrap:wrap; }
      #apps-card .apps-grid { display:grid; gap:.75rem; margin-top:.75rem; }
      #apps-card .app-row { border:1px solid var(--border,#333); border-radius:.4rem; padding:.7rem .8rem; background:rgba(0,0,0,.15); }
      #apps-card .app-head { display:flex; justify-content:space-between; gap:.5rem; align-items:baseline; }
      #apps-card .app-name { font-weight:600; }
      #apps-card .app-badge { font-size:.7rem; text-transform:uppercase; letter-spacing:.04em; padding:.1rem .4rem; border-radius:.25rem; background:rgba(74,158,255,.15); color:var(--accent,#4a9eff); }
      #apps-card .app-meta { font-size:.78rem; color:var(--fg-muted,#888); margin-top:.25rem; word-break:break-all; }
      #apps-card .app-actions { display:flex; gap:.4rem; margin-top:.5rem; flex-wrap:wrap; }
      #apps-card .apps-form { display:grid; gap:.4rem; margin-top:.75rem; max-width:40rem; }
      #apps-card .apps-form label { font-size:.78rem; color:var(--fg-muted,#888); }
      #apps-card .apps-form input, #apps-card .apps-form select { font:inherit; padding:.3rem .5rem; background:rgba(0,0,0,.3); color:var(--fg,#ddd); border:1px solid var(--border,#333); border-radius:.25rem; }
      #apps-card .hook-box { font-family:ui-monospace,monospace; font-size:.75rem; background:rgba(0,0,0,.3); padding:.45rem .6rem; border-radius:.3rem; word-break:break-all; margin:.35rem 0; }
    </style>
    <div class="apps-toolbar">
      <h2>Apps</h2>
      <span class="muted" id="apps-msg"></span>
    </div>
    <p class="muted" style="font-size:.8rem;margin:.35rem 0 .5rem">
      Connect a GitHub repository. Kubernetes applies Helm/Kustomize/manifests;
      OpenStack runs Heat, Terraform, or Ansible from the repo. Push to the
      webhook (when GitHub can reach this hub) or click Deploy now.
    </p>
    <div id="apps-body"><div class="card-empty">Loading…</div></div>
  </div>`;
}

let _getEnvId = () => "";
let _secretOnce = null;

export function wireAppsCard(getEnvId) {
  _getEnvId = getEnvId;
  const card = document.getElementById("apps-card");
  if (!card) return;
  card.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-apps-action]");
    if (!btn) return;
    const envId = _getEnvId();
    if (!envId) return;
    const action = btn.dataset.appsAction;
    if (action === "toggle-form") {
      const form = document.getElementById("apps-form");
      if (form) form.hidden = !form.hidden;
    } else if (action === "create") {
      createApp(envId);
    } else if (action === "deploy") {
      deployApp(envId, btn.dataset.appId);
    } else if (action === "delete") {
      if (confirm("Remove this app link? Workloads already applied are not deleted.")) {
        deleteApp(envId, btn.dataset.appId);
      }
    } else if (action === "copy") {
      const text = btn.dataset.copy || "";
      navigator.clipboard.writeText(text).then(
        () => toast("Copied", "ok"),
        () => toast("Copy failed", "warn"),
      );
    }
  });
}

export async function loadAppsCard(envId) {
  const body = document.getElementById("apps-body");
  const msg = document.getElementById("apps-msg");
  if (!body) return;
  if (!envId) {
    body.innerHTML = `<div class="card-empty">Select an environment.</div>`;
    return;
  }
  if (msg) msg.textContent = "Loading…";
  try {
    const data = await api(`/api/v1/environments/${encodeURIComponent(envId)}/apps`);
    if (msg) msg.textContent = "";
    render(data.apps || []);
  } catch (e) {
    if (msg) msg.textContent = "";
    body.innerHTML = `<div class="card-empty">Apps unavailable: ${esc(String(e.message || e))}</div>`;
  }
}

export function destroyAppsCard() {
  _secretOnce = null;
}

function render(apps) {
  const body = document.getElementById("apps-body");
  if (!body) return;
  const can = roleAtLeast("operator");
  const form = can
    ? `<button type="button" class="secondary btn-sm" data-apps-action="toggle-form">Connect repository</button>
      <form id="apps-form" class="apps-form" hidden>
        <label>Name <input name="name" required placeholder="my-app" pattern="[a-z0-9]([-a-z0-9]*[a-z0-9])?"></label>
        <label>GitHub repository <input name="repo_url" required placeholder="https://github.com/org/app"></label>
        <label>Branch <input name="branch" value="main"></label>
        <label>Target
          <select name="target">
            <option value="kubernetes">Kubernetes</option>
            <option value="openstack">OpenStack</option>
          </select>
        </label>
        <label>Root path (optional) <input name="root_path" placeholder="deploy/"></label>
        <label>GitHub token (private repos) <input name="deploy_token" type="password" autocomplete="off"></label>
        <button type="button" class="btn-sm" data-apps-action="create">Save</button>
      </form>`
    : `<div class="muted">Operator role required to connect repositories.</div>`;
  const secret = _secretOnce
    ? `<div class="app-row">
        <div class="app-head"><span class="app-name">Webhook for ${_secretOnce.name}</span></div>
        <div class="muted" style="font-size:.78rem">Paste this into GitHub → Settings → Webhooks. The secret is shown once.</div>
        <div class="hook-box">${esc(_secretOnce.webhook_url)}</div>
        <button type="button" class="secondary btn-sm" data-apps-action="copy" data-copy="${esc(_secretOnce.webhook_url)}">Copy URL</button>
        <div class="hook-box">${esc(_secretOnce.webhook_secret)}</div>
        <button type="button" class="secondary btn-sm" data-apps-action="copy" data-copy="${esc(_secretOnce.webhook_secret)}">Copy secret</button>
      </div>`
    : "";
  const rows = (apps || [])
    .map(
      (a) => `<div class="app-row">
        <div class="app-head">
          <span class="app-name">${esc(a.name)}</span>
          <span class="app-badge">${esc(a.target)}</span>
        </div>
        <div class="app-meta">${esc(a.repo_url)} @ ${esc(a.branch)}${a.root_path ? " / " + esc(a.root_path) : ""}</div>
        <div class="app-meta">last: ${esc(a.last_status || "never")} ${a.last_sha ? esc(a.last_sha.slice(0, 12)) : ""}</div>
        <div class="app-actions">
          ${can ? `<button type="button" class="btn-sm" data-apps-action="deploy" data-app-id="${esc(a.id)}">Deploy now</button>` : ""}
          <button type="button" class="secondary btn-sm" data-apps-action="copy" data-copy="${esc(a.webhook_url)}">Copy webhook URL</button>
          ${can ? `<button type="button" class="secondary btn-sm" data-apps-action="delete" data-app-id="${esc(a.id)}">Remove</button>` : ""}
        </div>
      </div>`,
    )
    .join("");
  body.innerHTML = form + secret + (rows ? `<div class="apps-grid">${rows}</div>` : `<div class="card-empty" style="margin-top:.75rem">No repositories connected yet.</div>`);
}

async function createApp(envId) {
  const form = document.getElementById("apps-form");
  if (!form) return;
  const fd = new FormData(form);
  const body = {
    name: String(fd.get("name") || "").trim(),
    repo_url: String(fd.get("repo_url") || "").trim(),
    branch: String(fd.get("branch") || "main").trim() || "main",
    target: String(fd.get("target") || "kubernetes"),
    root_path: String(fd.get("root_path") || "").trim() || null,
    deploy_token: String(fd.get("deploy_token") || "").trim() || null,
  };
  try {
    const result = await api(`/api/v1/environments/${encodeURIComponent(envId)}/apps`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    _secretOnce = {
      name: result.app.name,
      webhook_url: result.webhook_url,
      webhook_secret: result.webhook_secret,
    };
    toast("Repository connected", "ok");
    await loadAppsCard(envId);
  } catch (e) {
    toast(String(e.message || e), "err");
  }
}

async function deployApp(envId, appId) {
  try {
    const result = await api(
      `/api/v1/environments/${encodeURIComponent(envId)}/apps/${encodeURIComponent(appId)}/deploy`,
      { method: "POST", body: JSON.stringify({ force: true }) },
    );
    toast(`Deploy queued (${result.job_id})`, "ok");
    await loadAppsCard(envId);
  } catch (e) {
    toast(String(e.message || e), "err");
  }
}

async function deleteApp(envId, appId) {
  try {
    await api(
      `/api/v1/environments/${encodeURIComponent(envId)}/apps/${encodeURIComponent(appId)}`,
      { method: "DELETE" },
    );
    toast("App removed", "ok");
    await loadAppsCard(envId);
  } catch (e) {
    toast(String(e.message || e), "err");
  }
}
