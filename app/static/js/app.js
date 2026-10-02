// app.js — portal shell: login, whoami, hash router, topbar.
import { api, setUnauthorizedHandler, getKey, setKey, clearKey, setRefresh, clearRefresh, esc, toast } from "./api.js";
import { store, loadEnvs } from "./store.js";
import { connect, closeAll } from "./stream.js";
import { initTenantSwitcher, resetTenantSwitcher } from "./pages/tenant.js";
import * as fleet from "./pages/fleet.js";
import * as hosts from "./pages/hosts.js";
import * as environments from "./pages/environments.js";
import * as hardware from "./pages/hardware.js";
import * as activity from "./pages/activity.js";
import * as operations from "./pages/operations.js";
import * as observe from "./pages/observe.js";
import * as environmentDetail from "./pages/environment_detail.js?v=ls17";
import * as envWizard from "./pages/env_wizard.js";
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

async function refreshTopbar() {
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
    store.health = h;
    setPill($("health-pill"), "health: " + (h.status || "ok"), "ok");
    setPill($("dryrun-pill"), h.dry_run ? "dry-run ON" : "dry-run OFF", h.dry_run ? "warn" : "ok");
    const hl = $("user-health-line");
    const dl = $("user-dryrun-line");
    if (hl) hl.textContent = h.status === "ok" ? "Console healthy" : `Console ${h.status || "unknown"}`;
    if (dl) dl.textContent = h.dry_run ? "Dry-run is on — jobs rehearse only" : "Live operations enabled";
    setVersionLines(versionText(h));
  } catch {
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
      try {
        const r = await api("/api/v1/update/apply", { method: "POST", body: "{}" });
        toast(r.message || "Update applied");
      } catch (err) {
        toast(err.message || "Update failed");
      }
    };
  } catch { /* channel optional */ }
}

async function enterApp() {
  authed = true;
  show($("login-view"), false);
  show($("app-shell"), true);
  refreshTopbar();
  startAlertsBadge();
  updateAdminNav();
  await setupTenantSwitcher();
  const envsPromise = loadEnvs();
  envsPromise.catch(() => {});
  route();
  maybeShowWelcome();
  checkUpdateBanner();
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
      <p class="muted">Three steps to a Genestack cloud:</p>
      <div class="gs-welcome-panels">
        <div class="gs-welcome-panel">
          <div class="gs-welcome-num">1</div>
          <p><strong>Sign in (done)</strong> → Guided setup</p>
          <a href="#/setup" data-gs-welcome-link>Guided setup →</a>
        </div>
        <div class="gs-welcome-panel">
          <div class="gs-welcome-num">2</div>
          <p><strong>Bring metal</strong> — Terraform (AWS, Azure, GCP, Rackspace), OVH API, PXE, SSH, or BMC</p>
        </div>
        <div class="gs-welcome-panel">
          <div class="gs-welcome-num">3</div>
          <p><strong>Deploy</strong> Talos, then Kubernetes, then OpenStack from the same hub</p>
        </div>
      </div>
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
  stopAlertsBadge();
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
    const res = await api("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    // Session tokens resolve through the same X-API-Key header as static keys,
    // so the token goes into the existing storage slot untouched.
    setKey(res.token);
    setRefresh(res.refresh_token);
    await establishSession();
    await enterApp();
  } catch (e) {
    clearKey();
    clearRefresh();
    loginError(e.status === 401 ? "Invalid username or password." : e.message || String(e));
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

// Ask the server which login methods are on and reveal the SSO button when the
// oidc: config section enables it. Unauthenticated endpoint; failures just mean
// local-only login.
async function loadAuthMethods() {
  try {
    const res = await fetch("/api/v1/auth/methods");
    if (!res.ok) return;
    const methods = await res.json();
    if (methods.oidc) {
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
  document.querySelectorAll(".nav-link[data-env-tab]").forEach((a) => {
    a.classList.toggle("active", inEnv && a.dataset.envTab === envTab);
    if (inEnv) a.href = envHash(param, a.dataset.envTab, a.dataset.envTab === "platform" ? "ovh" : "");
  });
  const titleLink = $("nav-env-title");
  if (titleLink && inEnv) {
    const env = (store.envs || []).find((e) => e.id === param);
    titleLink.textContent = (env && env.name) || "Environment";
    titleLink.href = envHash(param, "workflow");
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
window.addEventListener("gsc-nav-sync", () => {
  const { name, param, query } = parseHash();
  syncNav(name, param, query);
});

(async function boot() {
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
