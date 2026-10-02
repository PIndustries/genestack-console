// api.js — fetch wrapper (X-API-Key auth) + shared UI helpers.

// The API key (or session token) and the refresh token live in
// sessionStorage, NOT localStorage: localStorage outlives the tab and is
// shared across tabs, so a key leaked via XSS there is a persistent
// platform-admin credential. A fresh tab requires a fresh login.
const KEY_STORAGE = "gs_console_api_key";
const REFRESH_STORAGE = "gs_console_refresh_token";

export class ApiError extends Error {
  constructor(status, message, opts = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.isNetwork = opts.isNetwork ?? false;
    this.isTimeout = opts.isTimeout ?? false;
    this.retryable = opts.retryable ?? (opts.isNetwork ?? false);
  }
}

let unauthorizedHandler = null;
export function setUnauthorizedHandler(fn) {
  unauthorizedHandler = fn;
}

export function getKey() {
  return sessionStorage.getItem(KEY_STORAGE) || "";
}
export function setKey(key) {
  sessionStorage.setItem(KEY_STORAGE, key);
}
export function clearKey() {
  sessionStorage.removeItem(KEY_STORAGE);
}
export function getRefresh() {
  return sessionStorage.getItem(REFRESH_STORAGE) || "";
}
export function setRefresh(token) {
  if (token) sessionStorage.setItem(REFRESH_STORAGE, token);
  else sessionStorage.removeItem(REFRESH_STORAGE);
}
export function clearRefresh() {
  sessionStorage.removeItem(REFRESH_STORAGE);
}

let refreshInFlight = null;

async function refreshSession() {
  const refresh = getRefresh();
  if (!refresh) return false;
  if (!refreshInFlight) {
    refreshInFlight = (async () => {
      let res;
      try {
        res = await fetch("/api/v1/auth/refresh", {
          method: "POST",
          headers: { Accept: "application/json", "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: refresh }),
        });
      } catch {
        return false;
      }
      if (!res.ok) {
        clearKey();
        clearRefresh();
        return false;
      }
      let data = null;
      try {
        data = await res.json();
      } catch {
        return false;
      }
      if (!data || !data.token || !data.refresh_token) {
        clearKey();
        clearRefresh();
        return false;
      }
      setKey(data.token);
      setRefresh(data.refresh_token);
      return true;
    })().finally(() => {
      refreshInFlight = null;
    });
  }
  return refreshInFlight;
}

const DEFAULT_TIMEOUT = 30000; // 30 s

function httpError(res, data, text) {
  if (res.status === 401 && unauthorizedHandler) unauthorizedHandler();
  // FastAPI returns { detail: "message" }; some backends use { message: "…" }.
  // A structured detail object (e.g. 409 conflict payloads with a
  // conflicting_job_id) is kept on the error so callers can link to it.
  let detail = "";
  let structured = null;
  if (data && typeof data === "object") {
    if (data.detail !== undefined && data.detail !== null) structured = data.detail;
    else if (data.message !== undefined && data.message !== null) structured = data.message;
    detail = data.detail ?? data.message ?? JSON.stringify(data);
    if (Array.isArray(detail)) {
      // FastAPI 422 validation errors ship detail as [{loc, msg, type}].
      detail = detail
        .map((d) => {
          if (d && typeof d === "object") {
            const loc = Array.isArray(d.loc) ? d.loc.slice(1).join(".") : "";
            return loc ? `${loc}: ${d.msg}` : String(d.msg);
          }
          return String(d);
        })
        .join("; ");
    }
  } else if (text) {
    detail = text;
  } else {
    detail = res.statusText || `HTTP ${res.status}`;
  }
  if (typeof detail !== "string") detail = String(detail);
  const error = new ApiError(res.status, detail);
  if (structured && typeof structured === "object") error.detail = structured;
  return error;
}

async function authedFetch(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  const key = getKey();
  if (key) headers["X-API-Key"] = key;

  const timeout = opts.timeout ?? DEFAULT_TIMEOUT;
  const rest = Object.assign({}, opts);
  delete rest.timeout;
  delete rest.headers;
  let res;
  try {
    if (timeout > 0) {
      const ctrl = new AbortController();
      const tid = setTimeout(() => ctrl.abort(), timeout);
      try {
        res = await fetch(path, { ...rest, headers, signal: ctrl.signal });
      } finally {
        clearTimeout(tid);
      }
    } else {
      res = await fetch(path, { ...rest, headers });
    }
  } catch (e) {
    if (e.name === "AbortError" || String(e.message).includes("abort")) {
      throw new ApiError(0, "Request timed out. Check your connection and try again.", { isTimeout: true });
    }
    if (e.name === "TypeError" || e.name === "NetworkError") {
      throw new ApiError(0, "Console server unreachable. Check that the backend is running and your network connection is active.", { isNetwork: true });
    }
    throw new ApiError(0, "Network error: " + (e.message || String(e)), { isNetwork: true });
  }
  return res;
}

export async function api(path, opts = {}) {
  const retried = !!opts._retried;
  const headers = Object.assign({ Accept: "application/json" }, opts.headers || {});
  if (opts.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  const clean = Object.assign({}, opts);
  delete clean._retried;

  const res = await authedFetch(path, { ...clean, headers });
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }

  if (!res.ok) {
    const skipRefresh =
      path === "/api/v1/auth/login" ||
      path === "/api/v1/auth/refresh" ||
      path === "/api/v1/auth/logout";
    if (res.status === 401 && !retried && !skipRefresh && getRefresh()) {
      const renewed = await refreshSession();
      if (renewed) return api(path, { ...opts, _retried: true });
    }
    throw httpError(res, data, text);
  }
  return data;
}

// Authenticated file download (kubeconfig / talosconfig / similar blobs).
export async function downloadAuth(path, filename) {
  let res = await authedFetch(path, { headers: { Accept: "*/*" } });
  if (res.status === 401 && getRefresh()) {
    const renewed = await refreshSession();
    if (renewed) res = await authedFetch(path, { headers: { Accept: "*/*" } });
  }
  if (!res.ok) {
    const text = await res.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = text;
    }
    throw httpError(res, data, text);
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename || "download";
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// ---------- shared UI helpers ----------

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[c]);
}

export function fmtTime(v) {
  if (!v) return "";
  try {
    return new Date(v).toLocaleString();
  } catch {
    return String(v);
  }
}

// Relative age ("45s ago"). Naive timestamps (no designator) are UTC, matching
// how the API serializes server-side times.
export function fmtAge(v) {
  if (!v) return "";
  const t = String(v);
  const ts = Date.parse(/Z$|[+-]\d{2}:?\d{2}$/.test(t) ? t : t + "Z");
  if (Number.isNaN(ts)) return "";
  const s = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

export function statusPill(s) {
  const k = String(s || "").toLowerCase();
  let cls = "warn";
  if (k === "success" || k === "ok") cls = "ok";
  else if (k === "failed" || k === "error") cls = "bad";
  return `<span class="pill ${cls}">${esc(s)}</span>`;
}

// Rehearsal marker shown next to a job's status when it only dry-ran.
// Deliberately NOT the green success pill: dashed outline, uppercase.
export function dryRunPill() {
  return '<span class="pill dryrun" title="Dry-run rehearsal — nothing was executed">DRY-RUN</span>';
}

export function toast(message, kind = "info", { retryLabel, onRetry, timeout = 4200 } = {}) {
  const root = document.getElementById("toast-root");
  if (!root) return;
  const el = document.createElement("div");
  // CSS only styles ok/warn/bad; normalize the "error" alias to the bad style
  // so every call site renders consistently.
  const cls = kind === "error" ? "bad" : kind;
  el.className = "toast " + cls;
  el.textContent = message;
  if (onRetry && retryLabel) {
    const btn = document.createElement("button");
    btn.className = "toast-retry";
    btn.textContent = retryLabel;
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      onRetry();
    });
    el.appendChild(btn);
  }
  root.appendChild(el);
  requestAnimationFrame(() => el.classList.add("show"));
  setTimeout(() => {
    el.classList.remove("show");
    setTimeout(() => el.remove(), 300);
  }, timeout);
}
