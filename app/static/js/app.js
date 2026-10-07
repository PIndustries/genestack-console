// app.js — portal shell: login, whoami, hash router, topbar.
import { api, setUnauthorizedHandler, getKey, setKey, clearKey, setRefresh, clearRefresh, esc, toast, fmtAge, setConsoleRestarting, isConsoleRestarting } from "./api.js";
import { store, loadEnvs, applyEnvLifecycle, refreshEnvSelects } from "./store.js";
import { connect, closeAll } from "./stream.js";
import { initTenantSwitcher, resetTenantSwitcher } from "./pages/tenant.js";
import * as fleet from "./pages/fleet.js?v=ls39";
import * as hosts from "./pages/hosts.js";
import * as environments from "./pages/environments.js?v=ls30";
import * as hardware from "./pages/hardware.js";
import * as activity from "./pages/activity.js";
import * as operations from "./pages/operations.js";
import * as observe from "./pages/observe.js";
import * as environmentDetail from "./pages/environment_detail.js?v=ls48";
import { mountQuake, destroyAllSessions } from "./pages/environment_terminal.js?v=ls48";
import * as envWizard from "./pages/env_wizard.js?v=ls27";
import * as admin from "./pages/admin.js";

const PAGES = { fleet, hosts, environments, hardware, activity, operations, observe, environment_detail: environmentDetail, setup: envWizard, admin };

// Pages reached via cross-page navigation that should highlight another nav item.
const NAV_ALIAS = { setup: "fleet", environments: "fleet" };

// Legacy hashes from before the nav collapse — send each to its new home.
const ROUTE_REDIRECTS = {
  dashboard: () => "#/fleet",
  machines: () => "#/hardware?tab=baremetal",
  alerts: () => "#/activity?tab=alerts",
  audit: () => "#/activity?tab=audit",
  jobs: (param) => "#/activity?tab=jobs" + (param ? "&job=" + encodeURIComponent(param) : ""),
};

let current = null;
let authed = false;
let alertsStream = null;
let envStream = null;

// ---------- firing-alerts nav badge ----------

async function refreshAlertsBadge() {
  const badge = $("alerts-badge");
  if (!badge) return;
  try {
    const s = await api("/api/v1/alerts/summary");
    const n = s && typeof s.firing === "number" ? s.firing : 0;
    badge.textContent = String(n);
    badge.classList.toggle("hidden", n <= 0);
  } catch {
    badge.classList.add("hidden");
  }
}

function startAlertsBadge() {
  if (alertsStream) alertsStream.close();
  refreshAlertsBadge();
  alertsStream = connect(["alerts"], {
    alerts: () => refreshAlertsBadge(),
  });
}

function stopAlertsBadge() {
  if (alertsStream) {
    alertsStream.close();
    alertsStream = null;
  }
  const badge = $("alerts-badge");
  if (badge) badge.classList.add("hidden");
}

// One subscription for the life of the session. Create, update, and delete
// all arrive on "environments", and every surface that lists environments
// reads that event.
function startEnvLifecycle() {
  if (envStream) envStream.close();
  envStream = connect(["environments"], {
    environments: async (payload) => {
      if (!payload || payload.action !== "deleted") {
        try { await loadEnvs(); } catch { /* keep the list we have */ }
      }
      applyEnvLifecycle(payload || { action: "updated" });
    },
  });
}

function stopEnvLifecycle() {
  if (envStream) {
    envStream.close();
    envStream = null;
  }
}

function $(id) {
  return document.getElementById(id);
}
function show(el, on) {
  el.classList.toggle("hidden", !on);
}
function setPill(el, text, kind) {
  if (!el) return;
  el.className = "pill " + (kind || "");
  el.textContent = text;
}

// Version line ("genestack-console 2026.08.06 (build abc1234)") — cosmetic,
// sourced from the unauthenticated /health so it also shows on the login card.
function versionText(h) {
  if (!h || !h.version) return "";
  const build = h.build && h.build !== "dev" ? ` (build ${h.build})` : "";
  return `genestack-console ${h.version}${build}`;
}

function setVersionLines(text) {
  const login = $("login-version");
  const side = $("sidebar-version");
  if (login) login.textContent = text;
  if (side) side.textContent = text;
}

async function loadVersionLines() {
  try {
    setVersionLines(versionText(await api("/health")));
  } catch { /* version line is cosmetic */ }
}

// Drop a slow /health result once a newer route has started its own refresh.
let topbarSeq = 0;

async function refreshTopbar() {
  const seq = ++topbarSeq;
  const badge = $("role-badge");
  const name = $("user-chip-name");
  const degraded = store.degradedAuth ? " (auth degraded)" : "";
  const who = store.username || store.keyName || "api-key";
  if (name) name.textContent = who;
  if (badge) {
    badge.textContent = (store.role || "viewer") + degraded;
    badge.className = "badge role-" + (store.role || "viewer");
  }
  try {
    const h = await api("/health");
    if (seq !== topbarSeq) return;
    store.health = h;
    setPill($("health-pill"), "health: " + (h.status || "ok"), "ok");
    const hl = $("user-health-line");
    if (hl) hl.textContent = h.status === "ok" ? "Console healthy" : `Console ${h.status || "unknown"}`;
    setVersionLines(versionText(h));

    // Own true/false wins. null inherits the console default. A missing health
    // payload counts as logging, matching paintEnvApply.
    let logging = h ? !!h.dry_run : true;
    let envScoped = false;
    const routed = parseHash();
    if (routed.name === "environment_detail" && routed.param) {
      try {
        const env = await api(`/api/v1/environments/${encodeURIComponent(routed.param)}`);
        if (seq !== topbarSeq) return;
        envScoped = true;
        logging = env.dry_run == null ? (h ? !!h.dry_run : true) : env.dry_run === true;
      } catch {
        if (seq !== topbarSeq) return;
        envScoped = false;
        logging = h ? !!h.dry_run : true;
      }
    }
    if (seq !== topbarSeq) return;
    setPill($("dryrun-pill"), logging ? "Look around" : "Apply", logging ? "warn" : "ok");
    const dl = $("user-dryrun-line");
    if (dl) {
      if (envScoped) {
        dl.textContent = logging
          ? "Look around. Jobs from here only write a log."
          : "Apply. Jobs from here change the machines.";
      } else {
        dl.textContent = h.dry_run
          ? "Console default is Look around. Each environment can Apply from its own switch."
          : "Console default is Apply. An environment can still Look around.";
      }
    }
  } catch {
    if (seq !== topbarSeq) return;
    if (isConsoleRestarting()) {
      setPill($("health-pill"), "health: restarting", "warn");
      const hl = $("user-health-line");
      if (hl) hl.textContent = "Console is restarting";
      return;
    }
    setPill($("health-pill"), "health: unreachable", "bad");
    const hl = $("user-health-line");
    if (hl) hl.textContent = "Console unreachable";
  }
}

async function checkUpdateBanner() {
  try {
    const u = await api("/api/v1/update");
    const pill = $("update-pill");
    if (!pill || !u || !u.update_available) return;
    pill.textContent = "update " + (u.latest || "");
    pill.className = "pill warn";
    pill.classList.remove("hidden");
    pill.onclick = async () => {
      if (store.role !== "admin") {
        toast("Ask an admin to apply the Console update.");
        return;
      }
      if (!confirm("Install Console " + u.latest + " and restart this hub?")) return;
      await installConsoleUpdate(u.latest);
    };
  } catch { /* channel optional */ }
}

function replaceStarted(result) {
  if (!result || typeof result !== "object") return false;
  if (result.applied) return true;
  return String(result.message || "").startsWith("binary replaced");
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// The binary replace restarts this process. The socket drops for a few
// seconds. That gap is the restart, not a failed replace.
async function waitForConsole(previous) {
  const deadline = Date.now() + 45000;
  let sawGap = false;
  while (Date.now() < deadline) {
    await sleep(1000);
    try {
      const h = await api("/health", { timeout: 2500 });
      const version = h && h.version ? String(h.version) : "";
      if (!version) continue;
      if (sawGap || (previous && version !== previous)) {
        location.reload();
        return "back";
      }
    } catch (err) {
      if (err && (err.isNetwork || err.isTimeout || err.isRestarting)) sawGap = true;
    }
  }
  return sawGap ? "down" : "same";
}

async function installConsoleUpdate(latest) {
  let previous = bootedVersion;
  try {
    const h = await api("/health");
    if (h && h.version) previous = String(h.version);
  } catch {
    /* the apply call still reports whether the file was replaced */
  }
  setConsoleRestarting(true);
  toast("Installing " + (latest || "the update") + ". This console will restart.", "ok");
  let result = null;
  try {
    result = await api("/api/v1/update/apply", { method: "POST", body: "{}", timeout: 180000 });
  } catch (err) {
    if (!(err && (err.isNetwork || err.isTimeout || err.isRestarting))) {
      setConsoleRestarting(false);
      toast(err && err.message ? err.message : "Update failed", "bad");
      return;
    }
    result = { applied: true, message: "binary replaced; restarting" };
  }
  if (!replaceStarted(result)) {
    setConsoleRestarting(false);
    const message = (result && result.message) || "Already current";
    toast(message, result && result.ok === false ? "bad" : "ok");
    return;
  }
  toast("Restarting. This page will come back on the new build.", "ok");
  const outcome = await waitForConsole(previous);
  if (outcome === "back") return;
  setConsoleRestarting(false);
  if (outcome === "down") {
    toast("The console did not come back yet. Refresh in a moment.", "bad");
    return;
  }
  toast((result && result.message) || "The new build did not come up.", "bad");
}

// The page remembers the build it loaded. A newer process reloads the page
// so the new script runs, then the release notes stay up until acknowledged.
const ACK_KEY = "gsc-ack-version";
let bootedVersion = "";
let versionTimer = null;

function stopVersionWatch() {
  if (versionTimer) {
    clearInterval(versionTimer);
    versionTimer = null;
  }
}

function startVersionWatch() {
  stopVersionWatch();
  versionTimer = setInterval(() => {
    watchRunningVersion();
  }, 20000);
}

async function rememberBootVersion() {
  try {
    const h = await api("/health");
    bootedVersion = h && h.version ? String(h.version) : "";
  } catch {
    bootedVersion = "";
  }
}

async function watchRunningVersion() {
  let version = "";
  try {
    const h = await api("/health");
    version = h && h.version ? String(h.version) : "";
  } catch {
    return;
  }
  if (!version) return;
  if (!bootedVersion) {
    bootedVersion = version;
    if (!document.getElementById("gsc-notes")) maybeAnnounceUpdate();
    return;
  }
  if (version !== bootedVersion) location.reload();
}

function githubHref(url) {
  const text = String(url || "");
  const prefix = "https://github.com/PIndustries/genestack-console/";
  return text.startsWith(prefix) ? text : "";
}

function pipeLabel(row) {
  const conclusion = row && row.conclusion ? String(row.conclusion) : "";
  const status = row && row.status ? String(row.status) : "";
  const key = conclusion || status;
  if (key === "success") return "Succeeded";
  if (key === "failure") return "Failed";
  if (key === "cancelled") return "Cancelled";
  if (key === "skipped") return "Skipped";
  if (status === "in_progress" || key === "in_progress") return "Running";
  if (status === "queued" || key === "queued") return "Queued";
  return key || "Unknown";
}

function pipeKind(row) {
  const conclusion = row && row.conclusion ? String(row.conclusion) : "";
  const status = row && row.status ? String(row.status) : "";
  if (conclusion === "success") return "ok";
  if (conclusion === "failure" || conclusion === "cancelled") return "bad";
  if (status === "in_progress" || status === "queued") return "warn";
  return "";
}

function notesHtml(release) {
  if (!release) {
    return '<p class="muted">Release notes for this build are not listed yet.</p>';
  }
  const parts = [];
  if (release.summary) parts.push(`<p>${esc(release.summary)}</p>`);
  const items = Array.isArray(release.items) ? release.items : [];
  if (items.length) {
    const lines = items.map((item) => `<li>${esc(item)}</li>`).join("");
    parts.push(`<ul class="gsc-notes-list">${lines}</ul>`);
  }
  if (!parts.length) {
    return '<p class="muted">Release notes for this build are not listed yet.</p>';
  }
  return parts.join("");
}

function closeNotes(overlay) {
  if (overlay && overlay.parentNode) overlay.remove();
}

function showUpdated(version, release) {
  const open = document.getElementById("gsc-notes");
  if (open) open.remove();
  const overlay = document.createElement("div");
  overlay.id = "gsc-notes";
  overlay.className = "gsc-notes";
  overlay.innerHTML = `
    <div class="gsc-notes-card" role="dialog" aria-modal="true" aria-label="Console updated">
      <h2>Updated to ${esc(version)}</h2>
      <p class="muted">This console is on that build.</p>
      ${notesHtml(release)}
      <div class="gsc-notes-actions">
        <button type="button" class="btn-sm" data-gsc-ack>Acknowledge</button>
      </div>
    </div>`;
  overlay.addEventListener("click", (e) => {
    if (!e.target.closest("[data-gsc-ack]")) return;
    try {
      localStorage.setItem(ACK_KEY, version);
    } catch { /* the note returns next time */ }
    closeNotes(overlay);
  });
  document.body.appendChild(overlay);
  const ack = overlay.querySelector("[data-gsc-ack]");
  if (ack) ack.focus();
}

async function maybeAnnounceUpdate() {
  const current = bootedVersion;
  if (!current || document.getElementById("gsc-notes")) return false;
  let known = "";
  try {
    known = localStorage.getItem(ACK_KEY) || "";
  } catch {
    return false;
  }
  if (!known) {
    try {
      localStorage.setItem(ACK_KEY, current);
    } catch { /* never nag when storage is blocked */ }
    return false;
  }
  if (known === current) return false;
  let release = null;
  try {
    const feed = await api("/api/v1/update/feed");
    const rows = Array.isArray(feed.releases) ? feed.releases : [];
    release = rows.find((row) => row && row.version === current) || null;
  } catch { /* the note still says the version */ }
  if (document.getElementById("gsc-notes")) return true;
  showUpdated(current, release);
  return true;
}

function renderPipeline(runs) {
  const rows = Array.isArray(runs) ? runs.slice(0, 12) : [];
  if (!rows.length) {
    return '<p class="muted">No pipeline runs are visible from this console.</p>';
  }
  return `<div class="gsc-pipe">${rows.map((run) => {
    const href = githubHref(run.html_url);
    const link = href ? `<a href="${esc(href)}" target="_blank" rel="noopener">View</a>` : "";
    const jobs = Array.isArray(run.jobs) ? run.jobs : [];
    const jobHtml = jobs.length
      ? `<ul class="gsc-notes-list">${jobs.map((job) => `<li>${esc(job.name)} — ${esc(pipeLabel(job))}</li>`).join("")}</ul>`
      : "";
    const when = fmtAge(run.started_at);
    const meta = [run.title, run.sha, when].filter(Boolean).map((bit) => esc(bit)).join(" · ");
    return `<div class="gsc-pipe-row">
      <div class="gsc-pipe-top">
        <strong>${esc(run.name || "workflow")}</strong>
        <span class="pill ${pipeKind(run)}">${esc(pipeLabel(run))}</span>
      </div>
      <div class="muted">${meta}</div>
      ${jobHtml}
      ${link}
    </div>`;
  }).join("")}</div>`;
}

function renderReleases(releases) {
  const rows = Array.isArray(releases) ? releases.slice(0, 12) : [];
  if (!rows.length) {
    return '<p class="muted">No release notes are visible from this console.</p>';
  }
  return rows.map((rel) => {
    const href = githubHref(rel.html_url);
    const link = href ? `<a href="${esc(href)}" target="_blank" rel="noopener">Release</a>` : "";
    const when = fmtAge(rel.published_at);
    const flag = rel.prerelease ? '<span class="muted">prerelease</span>' : "";
    return `<section class="gsc-rel">
      <div class="gsc-pipe-top">
        <h3>${esc(rel.version || "")}</h3>
        <span class="muted">${esc(when)} ${flag}</span>
      </div>
      ${notesHtml(rel)}
      ${link}
    </section>`;
  }).join("");
}

async function openChangelog() {
  const open = document.getElementById("gsc-notes");
  if (open && open.querySelector("[data-gsc-ack]")) return;
  if (open) open.remove();
  const overlay = document.createElement("div");
  overlay.id = "gsc-notes";
  overlay.className = "gsc-notes";
  overlay.innerHTML = `
    <div class="gsc-notes-card" role="dialog" aria-modal="true" aria-label="Changelog">
      <h2>Changelog</h2>
      <p class="muted">Loading the published builds.</p>
    </div>`;
  function onKey(e) {
    if (e.key !== "Escape") return;
    document.removeEventListener("keydown", onKey);
    closeNotes(overlay);
  }
  document.addEventListener("keydown", onKey);
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay || e.target.closest("[data-gsc-notes-close]")) {
      document.removeEventListener("keydown", onKey);
      closeNotes(overlay);
    }
  });
  document.body.appendChild(overlay);
  let feed = null;
  try {
    feed = await api("/api/v1/update/feed");
  } catch (err) {
    const card = overlay.querySelector(".gsc-notes-card");
    if (!card || !overlay.isConnected) return;
    card.innerHTML = `
      <h2>Changelog</h2>
      <p class="muted">${esc(err.message || "The changelog is not reachable.")}</p>
      <div class="gsc-notes-actions">
        <button type="button" class="secondary btn-sm" data-gsc-notes-close>Close</button>
      </div>`;
    return;
  }
  if (!overlay.isConnected) return;
  const card = overlay.querySelector(".gsc-notes-card");
  if (!card) return;
  const watchLine = feed && feed.watch
    ? "A newer build installs on its own after queued and running jobs finish."
    : "Automatic install is off. The update button installs a newer build.";
  const missing = feed && feed.github_reachable === false
    ? '<p class="muted">GitHub is not reachable from this console.</p>'
    : "";
  card.innerHTML = `
    <h2>Changelog</h2>
    <p class="muted">${esc(watchLine)}</p>
    ${missing}
    <h3>Pipeline</h3>
    ${renderPipeline(feed && feed.pipeline)}
    <h3>Releases</h3>
    ${renderReleases(feed && feed.releases)}
    <div class="gsc-notes-actions">
      <button type="button" class="secondary btn-sm" data-gsc-notes-close>Close</button>
    </div>`;
}

async function enterApp() {
  authed = true;
  show($("login-view"), false);
  show($("app-shell"), true);
  refreshTopbar();
  startAlertsBadge();
  updateAdminNav();
  await setupTenantSwitcher();
  startEnvLifecycle();
  const envsPromise = loadEnvs();
  envsPromise.catch(() => {});
  route();
  checkUpdateBanner();
  await rememberBootVersion();
  startVersionWatch();
  const announced = await maybeAnnounceUpdate();
  if (!announced) maybeShowWelcome();
  // First-run: zero environments and no explicit destination — take the
  // operator straight to the guided setup instead of an empty board.
  // Empty hash and #/fleet both count (OIDC lands on #/fleet). Leave
  // #/setup, #/admin, and every other page alone.
  const destName = (h) => (h || "").replace(/^#\/?/, "").split("?")[0];
  const hashName = destName(location.hash);
  if (!hashName || hashName === "fleet") {
    envsPromise
      .then((envs) => {
        const now = destName(location.hash);
        if (Array.isArray(envs) && envs.length === 0 && (!now || now === "fleet")) {
          location.hash = "#/setup";
        }
      })
      .catch(() => {});
  }
}

// ---------- first-login welcome overlay ----------
// One-time overlay shown after login until the user dismisses it. The localStorage
// flag is the only record — no backend state. Both "Dismiss" and "Don't show again"
// set the same flag (the overlay is one-time either way).

const WELCOME_FLAG = "gs_console_seen_welcome";

function welcomeSeen() {
  try {
    return localStorage.getItem(WELCOME_FLAG) === "1";
  } catch {
    return true; // storage unavailable (private mode) — never nag
  }
}

function markWelcomeSeen() {
  try {
    localStorage.setItem(WELCOME_FLAG, "1");
  } catch { /* ignore */ }
}

function maybeShowWelcome() {
  if (welcomeSeen() || document.getElementById("gs-welcome")) return;
  const overlay = document.createElement("div");
  overlay.id = "gs-welcome";
  overlay.className = "gs-welcome";
  overlay.innerHTML = `
    <div class="gs-welcome-card" role="dialog" aria-modal="true" aria-label="Welcome to the Genestack console">
      <h2>Welcome to the Genestack console</h2>
      <p class="muted">This machine stays outside the cluster. Inventory is one list.</p>
      <div class="gs-welcome-panels">
        <div class="gs-welcome-panel">
          <div class="gs-welcome-num">1</div>
          <p><strong>Add each machine by hostname and IP.</strong> A virtual machine and a physical server are the same row.</p>
        </div>
        <div class="gs-welcome-panel">
          <div class="gs-welcome-num">2</div>
          <p><strong>On Machines, record Talos or Ubuntu.</strong> Then Deploy.</p>
        </div>
        <div class="gs-welcome-panel">
          <div class="gs-welcome-num">3</div>
          <p><strong>A cluster that is already running</strong> is Settings, Access, Adopt Kubespray. Paste the kubeconfig.</p>
        </div>
      </div>
      <p><a href="#/setup" data-gs-welcome-link>Guided setup →</a></p>
      <div class="gs-welcome-actions">
        <button class="secondary" type="button" data-gs-welcome-dismiss>Dismiss</button>
        <button type="button" data-gs-welcome-dismiss>Don't show again</button>
      </div>
    </div>`;
  overlay.addEventListener("click", (e) => {
    if (e.target.closest("[data-gs-welcome-dismiss]")) {
      markWelcomeSeen();
      overlay.remove();
      return;
    }
    // Following the Guided setup link closes the overlay but keeps the flag unset,
    // so the welcome returns on the next login until explicitly dismissed.
    if (e.target.closest("[data-gs-welcome-link]")) overlay.remove();
  });
  document.body.appendChild(overlay);
}

/** The Admin nav link is only meaningful for platform admins. */
function updateAdminNav() {
  const link = $("nav-admin-link");
  if (link) link.classList.toggle("hidden", !store.platformAdmin);
}

async function setupTenantSwitcher() {
  await initTenantSwitcher($("tenant-switch"), {
    platformAdmin: !!store.platformAdmin,
    tenants: store.tenants,
    fetchTenants: () => api("/api/v1/tenants"),
    onChange: () => {
      loadEnvs().catch(() => {});
      route();
    },
  });
}

/** Best-effort server-side session invalidation (no-op for static keys). */
async function serverLogout() {
  const token = getKey();
  if (!token) return;
  try {
    await api("/api/v1/auth/logout", {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
    });
  } catch { /* ignore */ }
}

function logout(msg) {
  authed = false;
  if (current && current.destroy) {
    try { current.destroy(); } catch { /* ignore */ }
    current = null;
  }
  destroyAllSessions();
  stopAlertsBadge();
  stopEnvLifecycle();
  stopVersionWatch();
  bootedVersion = "";
  closeAll(); // no stream may outlive the session that authorized it
  serverLogout();
  clearKey();
  clearRefresh();
  store.role = null;
  store.keyName = null;
  store.username = null;
  store.authMethod = null;
  store.platformAdmin = false;
  store.tenants = [];
  store.degradedAuth = false;
  store.envs = [];
  updateAdminNav();
  resetTenantSwitcher($("tenant-switch"));
  show($("app-shell"), false);
  show($("login-view"), true);
  const err = $("login-error");
  if (msg) {
    err.textContent = msg;
    err.classList.remove("hidden");
  }
}

setUnauthorizedHandler(() => {
  if (authed) logout("Session expired — sign in again.");
});

/**
 * Establish session from the stored key. Primary path: GET /api/v1/auth/whoami.
 * Fallback (whoami unreachable with a non-401 error): validate the key against
 * the role-filtered operations catalog and assume the conservative role —
 * operator, never admin — so a transient whoami outage can't over-privilege
 * the UI. Server-side enforcement still applies either way.
 */
async function establishSession() {
  try {
    const me = await api("/api/v1/auth/whoami");
    store.role = me.role;
    store.keyName = me.key_name;
    store.username = me.key_name;
    store.authMethod = me.auth_method || "api_key";
    store.platformAdmin = !!me.platform_admin;
    store.tenants = me.tenants || [];
    store.degradedAuth = false;
    return true;
  } catch (e) {
    if (e.status === 401 || e.status === 0) throw e;
    try {
      await api("/api/v1/operations");
      store.role = "operator";
      store.keyName = "api-key";
      store.username = "api-key";
      store.authMethod = "api_key";
      store.platformAdmin = false;
      store.tenants = [];
      store.degradedAuth = true;
      return true;
    } catch (e2) {
      throw e2;
    }
  }
}

function loginError(message) {
  const err = $("login-error");
  err.textContent = message;
  err.classList.remove("hidden");
}

async function doLogin() {
  const username = $("login-username").value.trim();
  const password = $("login-password").value;
  $("login-error").classList.add("hidden");
  if (!username || !password) {
    loginError("Enter a username and password.");
    return;
  }
  try {
    // OAuth 2 resource-owner password grant. The access token is what the
    // rest of the page sends as X-API-Key, same as a static key.
    const res = await fetch("/api/v1/oauth/token", {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
      },
      body: new URLSearchParams({
        grant_type: "password",
        username,
        password,
        client_id: "genestack-console",
        scope: "console",
      }),
    });
    let data = null;
    try {
      data = await res.json();
    } catch {
      data = null;
    }
    if (!res.ok || !data || !data.access_token) {
      clearKey();
      clearRefresh();
      const invalid = res.status === 400 || res.status === 401;
      loginError(
        invalid
          ? "Invalid username or password."
          : (data && (data.error_description || data.detail)) || "Sign-in failed."
      );
      return;
    }
    setKey(data.access_token);
    setRefresh(data.refresh_token);
    await establishSession();
    await enterApp();
  } catch (e) {
    clearKey();
    clearRefresh();
    loginError(e.message || String(e));
  }
}

async function doKeyLogin() {
  const key = $("api-key").value.trim();
  $("login-error").classList.add("hidden");
  if (!key) {
    loginError("Enter an API key.");
    return;
  }
  // An API key has no refresh token. Drop any stored one before the check,
  // so a rejected key cannot sign the previous person back in.
  clearRefresh();
  setKey(key);
  try {
    await establishSession();
    await enterApp();
  } catch (e) {
    clearKey();
    loginError(e.status === 401 ? "Invalid API key." : e.message || String(e));
  }
}

// ---------- OIDC/SSO ----------
// The callback redirects to /ui#token=<session>&refresh=<refresh>. A fragment
// is never sent to a server, so neither value can land in access/proxy logs.
// Read both once and strip them from the URL (and browser history) immediately.
function consumeOidcToken() {
  const hash = location.hash || "";
  const tokenMatch = hash.match(/^#token=([^&]+)/);
  if (!tokenMatch) return;
  setKey(decodeURIComponent(tokenMatch[1]));
  const refreshMatch = hash.match(/[?&]refresh=([^&]+)/);
  if (refreshMatch) setRefresh(decodeURIComponent(refreshMatch[1]));
  else clearRefresh();
  history.replaceState(null, "", location.pathname + location.search + "#/fleet");
}

// The portal button is for my.genestack.dev. A deploy console signs in with
// its local account. A site's own identity provider has no portal_host, so
// that button still shows there.
function portalButtonHere(methods) {
  if (!methods || !methods.oidc) return false;
  const portalHost = String(methods.portal_host || "").toLowerCase();
  if (!portalHost) return true;
  return location.hostname.toLowerCase() === portalHost;
}

// Ask the server which login methods are on and reveal the SSO button when the
// oidc: config section enables it. Unauthenticated endpoint; failures just mean
// local-only login.
async function loadAuthMethods() {
  try {
    const res = await fetch("/api/v1/auth/methods");
    if (!res.ok) return;
    const methods = await res.json();
    if (portalButtonHere(methods)) {
      $("btn-login-sso").textContent = `Sign in with ${methods.oidc_label || "SSO"}`;
      show($("login-sso"), true);
    }
  } catch { /* local login only */ }
}

// ---------- hash router ----------

function parseHash() {
  const h = (location.hash || "").replace(/^#\/?/, "");
  const [pathPart, queryPart] = h.split("?");
  const parts = pathPart.split("/").filter(Boolean);
  return {
    name: parts[0] || "fleet",
    param: parts.slice(1).join("/") || null,
    query: new URLSearchParams(queryPart || ""),
  };
}

function envHash(id, tab, ptab) {
  const q = new URLSearchParams();
  if (tab) q.set("tab", tab);
  if (tab === "platform" && ptab) q.set("ptab", ptab);
  const qs = q.toString();
  return `#/environment_detail/${encodeURIComponent(id)}${qs ? "?" + qs : ""}`;
}

function syncNav(name, param, query) {
  const inEnv = name === "environment_detail" && param;
  const envNav = $("nav-env");
  if (envNav) envNav.classList.toggle("hidden", !inEnv);
  const envTab = (query && query.get("tab")) || (inEnv ? "workflow" : "");
  document.querySelectorAll(".nav-link[data-page]").forEach((a) => {
    const page = a.dataset.page;
    let on = !inEnv && page === (NAV_ALIAS[name] || name);
    if (inEnv && page === "fleet") on = false;
    a.classList.toggle("active", on);
  });
  const rawPtab = (query && query.get("ptab")) || "machines";
  const ptab = rawPtab === "ovh" || rawPtab === "hosts" ? "machines" : rawPtab;
  document.querySelectorAll(".nav-link[data-env-tab]").forEach((a) => {
    const wantPtab = a.dataset.envPtab || "";
    a.classList.toggle("active", inEnv && a.dataset.envTab === envTab && (!wantPtab || wantPtab === ptab));
    if (inEnv) a.href = envHash(param, a.dataset.envTab, wantPtab);
  });
  const titleLink = $("nav-env-title");
  if (titleLink && inEnv) {
    let id = param;
    try { id = decodeURIComponent(param); } catch { /* keep the raw segment */ }
    const env = (store.envs || []).find((e) => e.id === id);
    titleLink.textContent = (env && env.name) || "Environment";
    titleLink.href = envHash(id, "workflow");
  } else if (titleLink) {
    titleLink.textContent = "Environment";
    titleLink.href = "#/fleet";
  }
}

async function route() {
  if (!authed) return;
  const { name, param, query } = parseHash();
  if (ROUTE_REDIRECTS[name]) {
    location.replace(ROUTE_REDIRECTS[name](param));
    return;
  }
  const page = PAGES[name] || PAGES.fleet;
  if (
    name === "environment_detail" &&
    current === page &&
    typeof page.applyQuery === "function" &&
    page.applyQuery({ param, query })
  ) {
    syncNav(name, param, query);
    $("page-title").textContent = page.title || "Environment";
    refreshTopbar();
    return;
  }
  if (current && current.destroy) {
    try { current.destroy(); } catch { /* ignore */ }
  }
  current = page;
  const activeName = PAGES[name] ? NAV_ALIAS[name] || name : "fleet";
  syncNav(name, param, query);
  $("page-title").textContent = name === "environment_detail" ? "Environment" : (page.title || activeName);
  const root = $("page-root");
  root.innerHTML = "";
  try {
    await page.render(root, { param, query });
    if (name === "environment_detail") syncNav(name, param, query);
  } catch (e) {
    root.innerHTML = `<div class="error">${esc(e.message || String(e))}</div>`;
    if (e.status === 403) toast("Insufficient role for this view", "bad");
  }
  refreshTopbar();
}

// ---------- wiring & boot ----------

$("btn-login").addEventListener("click", doLogin);
$("btn-login-key").addEventListener("click", doKeyLogin);
$("btn-login-sso").addEventListener("click", () => {
  $("login-error").classList.add("hidden");
  // Hosted/portal path must pass native=true so callback Set-Cookie gsc_console.
  // Local/dev (loopback) keeps bare login → #token= fragment handoff.
  const hosted = location.hostname !== "localhost" && location.hostname !== "127.0.0.1";
  window.location.assign("/api/v1/auth/oidc/login" + (hosted ? "?native=true" : ""));
});
$("btn-show-key").addEventListener("click", () => {
  $("login-error").classList.add("hidden");
  show($("login-user-form"), false);
  show($("login-key-form"), true);
  $("api-key").focus();
});
$("btn-show-user").addEventListener("click", () => {
  $("login-error").classList.add("hidden");
  show($("login-key-form"), false);
  show($("login-user-form"), true);
  $("login-username").focus();
});
$("login-password").addEventListener("keydown", (e) => {
  if (e.key === "Enter") doLogin();
});
$("login-username").addEventListener("keydown", (e) => {
  if (e.key === "Enter") doLogin();
});
$("api-key").addEventListener("keydown", (e) => {
  if (e.key === "Enter") doKeyLogin();
});
$("btn-logout").addEventListener("click", () => logout());
const changelogBtn = $("btn-changelog");
if (changelogBtn) changelogBtn.addEventListener("click", () => { openChangelog(); });

(function wireUserMenu() {
  const menu = $("user-menu");
  const chip = $("user-chip");
  const panel = $("user-menu-panel");
  if (!menu || !chip || !panel) return;
  chip.addEventListener("click", (e) => {
    e.stopPropagation();
    const open = panel.classList.contains("hidden");
    panel.classList.toggle("hidden", !open);
    chip.setAttribute("aria-expanded", open ? "true" : "false");
  });
  document.addEventListener("click", (e) => {
    if (!menu.contains(e.target)) {
      panel.classList.add("hidden");
      chip.setAttribute("aria-expanded", "false");
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      panel.classList.add("hidden");
      chip.setAttribute("aria-expanded", "false");
    }
  });
})();

window.addEventListener("hashchange", route);
window.addEventListener("gsc-dry-run", () => {
  refreshTopbar();
});
window.addEventListener("gsc-nav-sync", () => {
  const { name, param, query } = parseHash();
  syncNav(name, param, query);
});
window.addEventListener("gsc-envs", (ev) => {
  const detail = (ev && ev.detail) || {};
  const { name, param, query } = parseHash();
  let openId = param;
  try { if (param) openId = decodeURIComponent(param); } catch { /* keep the raw segment */ }
  if (
    detail.action === "deleted" &&
    name === "environment_detail" &&
    openId &&
    String(detail.environment_id || "") === openId
  ) {
    location.hash = "#/fleet";
    return;
  }
  syncNav(name, param, query);
  refreshEnvSelects();
});

(async function boot() {
  mountQuake();
  consumeOidcToken(); // SSO handoff must run before the getKey() check below
  loadAuthMethods();
  loadVersionLines(); // /health is unauthenticated — fills the login card too
  if (!getKey()) {
    // Dev auto-login (auth.dev_auto_login on the server): whoami succeeds
    // with no credentials at all — skip the login screen entirely.
    try {
      await establishSession();
      await enterApp();
      return;
    } catch {
      show($("login-view"), true);
      return;
    }
  }
  try {
    await establishSession();
    await enterApp();
  } catch {
    clearKey();
    show($("login-view"), true);
  }
})();
