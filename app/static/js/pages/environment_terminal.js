// pages/environment_terminal.js — Quake-style shell drawer.
// Backtick or tilde opens the drawer and a list of machines this console can
// reach. Shell is an xterm ssh. Console is that machine's management port.
// Esc or Hide closes the drawer. A backtick typed while the terminal has
// focus goes to the shell. Sessions stay up when the environment page changes.
// The server refuses any host that is not in that environment's inventory.
import { api, esc, toast } from "../api.js";
import { canAdmin, canRun, store } from "../store.js";

const VENDOR_BASE = "/static/vendor/xterm";
const FS_CLASS = "gsc-term-fullscreen";

const TERM_THEME = {
  background: "#0c0c0c",
  foreground: "#e6e6e6",
  cursor: "#4ade80",
  cursorAccent: "#0c0c0c",
  selectionBackground: "#1f3d2b",
  black: "#0c0c0c",
  red: "#e06c75",
  green: "#98c379",
  yellow: "#d19a66",
  blue: "#61afef",
  magenta: "#c678dd",
  cyan: "#56b6c2",
  white: "#e6e6e6",
  brightBlack: "#5c6370",
  brightRed: "#e06c75",
  brightGreen: "#98c379",
  brightYellow: "#d19a66",
  brightBlue: "#61afef",
  brightMagenta: "#c678dd",
  brightCyan: "#56b6c2",
  brightWhite: "#ffffff",
};

const TERM_FONT =
  '"SF Mono", "Cascadia Mono", "JetBrains Mono", ui-monospace, Menlo, Consolas, monospace';

const TERM_OPTIONS = {
  theme: TERM_THEME,
  fontFamily: TERM_FONT,
  fontSize: 13,
  fontWeight: 400,
  fontWeightBold: 700,
  letterSpacing: 0,
  lineHeight: 1.1,
  cursorBlink: true,
  cursorStyle: "block",
  scrollback: 5000,
  fastScrollModifier: "alt",
  bellStyle: "none",
  macOptionIsMeta: true,
};

let xtermLoading = null;
let mounted = false;
const sessions = new Map();
let activeKey = "";
let menuGen = 0;

function loadXterm() {
  if (window.Terminal && window.FitAddon) return Promise.resolve(true);
  if (!xtermLoading) {
    xtermLoading = (async () => {
      if (!document.querySelector("link[data-xterm-css]")) {
        const link = document.createElement("link");
        link.rel = "stylesheet";
        link.href = VENDOR_BASE + "/xterm.css";
        link.setAttribute("data-xterm-css", "");
        document.head.appendChild(link);
      }
      const loadScript = (src) =>
        new Promise((resolve, reject) => {
          const s = document.createElement("script");
          s.src = src;
          s.onload = resolve;
          s.onerror = reject;
          document.head.appendChild(s);
        });
      await loadScript(VENDOR_BASE + "/xterm.js");
      await loadScript(VENDOR_BASE + "/xterm-addon-fit.js");
      return !!(window.Terminal && window.FitAddon && window.FitAddon.FitAddon);
    })().catch(() => false);
  }
  return xtermLoading;
}

function sessionKey(envId, machine) {
  return `${encodeURIComponent(envId)}|${encodeURIComponent(machine || "")}`;
}

function currentEnvId() {
  const match = /^#\/environment_detail\/([^?]+)/.exec(location.hash || "");
  if (!match) return "";
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return match[1];
  }
}

function envTitle(id) {
  const row = (store.envs || []).find((e) => e && e.id === id);
  return (row && row.name) || id || "environment";
}

function quakeEl() {
  return document.getElementById("gsc-quake");
}

function isQuakeOpen() {
  const el = quakeEl();
  return !!(el && el.classList.contains("open"));
}

function frontModalOpen() {
  return !!document.querySelector(
    ".gsc-modal:not([hidden]), #srv-bmc-modal:not([hidden]), .os-console-modal:not([hidden])"
  );
}

function setStatus(sess, html) {
  if (sess) sess.statusHtml = html;
  if (!sess || sess.key === activeKey) {
    const el = document.getElementById("gsc-term-status");
    if (el) el.innerHTML = html || "";
  }
}

function renderTabs() {
  const bar = document.getElementById("gsc-quake-tabs");
  if (!bar) return;
  if (!sessions.size) {
    bar.innerHTML = `<span class="gsc-quake-hint">New lists machines</span>`;
    setStatus(null, '<span class="pill">idle</span>');
    paintEmpty();
    return;
  }
  bar.innerHTML = [...sessions.values()]
    .map((sess) => {
      const on = sess.key === activeKey ? " on" : "";
      return `<div class="gsc-quake-tab${on}" role="tab" data-quake-tab="${esc(sess.key)}" aria-selected="${on ? "true" : "false"}">
        <span>${esc(sess.label || "shell")}</span>
        <button type="button" data-quake-close="${esc(sess.key)}" aria-label="Close ${esc(sess.label || "shell")}">×</button>
      </div>`;
    })
    .join("");
  paintEmpty();
}

function paintEmpty() {
  const root = document.getElementById("gsc-quake-panes");
  if (!root) return;
  let empty = document.getElementById("gsc-quake-empty");
  if (sessions.size) {
    if (empty) empty.hidden = true;
    return;
  }
  if (!empty) {
    empty = document.createElement("div");
    empty.id = "gsc-quake-empty";
    empty.className = "gsc-quake-empty muted";
    empty.textContent = "Pick a machine. Shell is SSH. Console is the management port.";
    root.appendChild(empty);
  }
  empty.hidden = false;
}

function focusSession(key) {
  if (!sessions.has(key)) return;
  activeKey = key;
  sessions.forEach((sess) => {
    if (sess.pane) sess.pane.classList.toggle("on", sess.key === key);
  });
  renderTabs();
  const sess = sessions.get(key);
  setStatus(sess, sess.statusHtml || "");
  if (!isQuakeOpen()) return;
  window.setTimeout(() => {
    if (!isQuakeOpen() || activeKey !== key || !sess.fit || !sess.term) return;
    try {
      sess.fit.fit();
    } catch {
      return;
    }
    if (sess.sendResize) sess.sendResize();
    sess.term.focus();
  }, 180);
}

function setQuakeOpen(on) {
  const el = quakeEl();
  if (!el) return;
  el.classList.toggle("open", !!on);
  el.setAttribute("aria-hidden", on ? "false" : "true");
  if (!on) {
    el.classList.remove(FS_CLASS);
    document.body.classList.remove("gsc-term-fs-lock");
    const menu = document.getElementById("gsc-quake-menu");
    if (menu) menu.hidden = true;
    const focused = document.activeElement;
    if (focused && el.contains(focused) && focused.blur) focused.blur();
    return;
  }
  if (activeKey) focusSession(activeKey);
}

function setFullscreen(on) {
  const el = quakeEl();
  if (!el) return;
  if (on) setQuakeOpen(true);
  el.classList.toggle(FS_CLASS, !!on);
  document.body.classList.toggle("gsc-term-fs-lock", !!on);
  const btn = document.getElementById("gsc-quake-expand");
  if (btn) {
    btn.textContent = on ? "⤡" : "⤢";
    btn.title = on ? "Exit fullscreen" : "Expand shell";
  }
  if (activeKey) focusSession(activeKey);
}

async function copyText(text) {
  if (!text) return false;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    toast("copied", "info");
  }
  return true;
}

function copySelection(term) {
  if (!term.hasSelection()) return false;
  return copyText(term.getSelection());
}

async function pasteFromClipboard(sess) {
  let text = "";
  try {
    text = await navigator.clipboard.readText();
  } catch {
    toast("Paste blocked — allow clipboard access", "warn");
    return;
  }
  if (text && sess.ws && sess.ws.readyState === WebSocket.OPEN) {
    sess.ws.send(JSON.stringify({ type: "input", data: text }));
  }
  if (sess.term) sess.term.focus();
}

function attachKeys(sess) {
  const term = sess.term;
  term.attachCustomKeyEventHandler((e) => {
    if (e.type !== "keydown") return true;
    const key = (e.key || "").toLowerCase();
    if (e.ctrlKey && e.shiftKey && key === "c") {
      copySelection(term);
      return false;
    }
    if (e.ctrlKey && e.shiftKey && key === "v") {
      pasteFromClipboard(sess);
      return false;
    }
    if (e.metaKey && key === "v") {
      pasteFromClipboard(sess);
      return false;
    }
    if (e.metaKey && key === "c") {
      if (!term.hasSelection()) return true;
      copySelection(term);
      return false;
    }
    return true;
  });
}

function disposeSession(key) {
  const sess = sessions.get(key);
  if (!sess) return;
  sessions.delete(key);
  if (sess.observer) sess.observer.disconnect();
  try {
    if (sess.ws && sess.ws.readyState <= WebSocket.OPEN) sess.ws.close(1000, "panel closed");
  } catch {
    /* already gone */
  }
  if (sess.term) {
    try {
      sess.term.dispose();
    } catch {
      /* half-initialized */
    }
  }
  if (sess.pane) {
    sess.pane.querySelectorAll("iframe").forEach((frame) => {
      frame.src = "about:blank";
    });
    sess.pane.remove();
  }
  if (activeKey === key) {
    activeKey = sessions.keys().next().value || "";
    if (activeKey) focusSession(activeKey);
    else renderTabs();
  } else {
    renderTabs();
  }
}

export function destroyAllSessions() {
  setFullscreen(false);
  setQuakeOpen(false);
  [...sessions.keys()].forEach((key) => disposeSession(key));
  activeKey = "";
  renderTabs();
}

function wsUrl(envId, ticket, machine) {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  let url =
    `${proto}//${location.host}/api/v1/terminal` +
    `?environment_id=${encodeURIComponent(envId)}&ticket=${encodeURIComponent(ticket)}`;
  if (machine) url += `&machine=${encodeURIComponent(machine)}`;
  return url;
}

function startFallback(body, ws) {
  body.innerHTML =
    '<pre class="gsc-term-fallback" tabindex="0"></pre>' +
    '<input class="gsc-term-hidden-input" aria-hidden="true" autocomplete="off" />';
  const pre = body.querySelector("pre");
  const input = body.querySelector("input");
  const stripAnsi = (s) => s.replace(/\x1b\[[0-9;?]*[ -/]*[@-~]/g, "").replace(/\x1b[()][0-9A-B]/g, "");
  const append = (text) => {
    pre.textContent += stripAnsi(text);
    pre.scrollTop = pre.scrollHeight;
  };
  const send = (data) => {
    if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "input", data }));
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "`" || e.key === "~" || e.code === "Backquote") return;
    if (e.key === "Escape") return;
    if (e.key === "Enter") send("\r");
    else if (e.key === "Backspace") send("\x7f");
    else if (e.key === "Tab") send("\t");
    else if (e.key === "ArrowUp") send("\x1b[A");
    else if (e.key === "ArrowDown") send("\x1b[B");
    else if (e.key === "ArrowRight") send("\x1b[C");
    else if (e.key === "ArrowLeft") send("\x1b[D");
    else if (e.ctrlKey && e.key.length === 1) {
      const code = e.key.toLowerCase().charCodeAt(0) - 96;
      if (code >= 1 && code <= 26) send(String.fromCharCode(code));
    } else if (e.key.length === 1 && !e.metaKey && !e.altKey) send(e.key);
    e.preventDefault();
  });
  pre.addEventListener("click", () => input.focus());
  if (isQuakeOpen()) input.focus();
  append("[fallback terminal — xterm.js assets unavailable]\r\n");
  return { append };
}

async function connect(opts) {
  const envId = opts && opts.envId;
  const machine = (opts && opts.machine) || "";
  const label = (opts && opts.label) || (machine ? machine : `${envTitle(envId)} · deploy host`);
  if (!envId) return;
  const key = sessionKey(envId, machine);
  const existing = sessions.get(key);
  if (existing && existing.ws && existing.ws.readyState <= WebSocket.OPEN) {
    focusSession(key);
    return;
  }
  if (existing) disposeSession(key);
  if (!canAdmin()) {
    toast("An admin opens a shell", "warn");
    return;
  }
  const pane = document.createElement("div");
  pane.className = "gsc-quake-pane";
  const root = document.getElementById("gsc-quake-panes");
  if (!root) return;
  root.appendChild(pane);
  const sess = {
    key,
    envId,
    machine,
    label,
    ws: null,
    term: null,
    fit: null,
    observer: null,
    fallback: null,
    sendResize: null,
    pane,
    statusHtml: '<span class="pill warn">connecting…</span>',
  };
  sessions.set(key, sess);
  focusSession(key);
  setStatus(sess, sess.statusHtml);

  const xtermOk = await loadXterm();
  if (!sessions.has(key)) return;

  let ticket;
  try {
    ({ ticket } = await api("/api/v1/auth/ticket", { method: "POST" }));
  } catch (e) {
    setStatus(sess, `<span class="pill bad">auth failed — ${esc(e.message || "ticket error")}</span>`);
    return;
  }
  if (!sessions.has(key)) return;

  const ws = new WebSocket(wsUrl(envId, ticket, machine));
  sess.ws = ws;

  const sendResize = () => {
    if (!sess.fit || ws.readyState !== WebSocket.OPEN) return;
    const dims = sess.fit.proposeDimensions();
    if (dims) ws.send(JSON.stringify({ type: "resize", cols: dims.cols, rows: dims.rows }));
  };
  sess.sendResize = sendResize;

  ws.onopen = () => {
    if (!sessions.has(key)) return;
    setStatus(sess, `<span class="pill ok">connected — ${esc(label)}</span>`);
    if (xtermOk) {
      pane.innerHTML = "";
      const term = new window.Terminal(TERM_OPTIONS);
      const fit = new window.FitAddon.FitAddon();
      sess.term = term;
      sess.fit = fit;
      term.loadAddon(fit);
      term.open(pane);
      try {
        fit.fit();
      } catch {
        /* drawer still opening */
      }
      attachKeys(sess);
      term.onData((data) => {
        if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "input", data }));
      });
      term.onSelectionChange(() => {
        if (term.hasSelection()) copySelection(term);
      });
      pane.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        pasteFromClipboard(sess);
      });
      if (window.ResizeObserver) {
        sess.observer = new ResizeObserver(() => {
          try {
            fit.fit();
          } catch {
            return;
          }
          sendResize();
        });
        sess.observer.observe(pane);
      }
      sendResize();
      if (activeKey === key && isQuakeOpen()) term.focus();
    } else {
      sess.fallback = startFallback(pane, ws);
    }
  };

  ws.onmessage = (ev) => {
    if (!sessions.has(key)) return;
    let frame;
    try {
      frame = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (!frame || typeof frame !== "object") return;
    if (frame.type === "output" && typeof frame.data === "string") {
      if (sess.term) sess.term.write(frame.data);
      else if (sess.fallback) sess.fallback.append(frame.data);
    } else if (frame.type === "exit") {
      if (sess.term) sess.term.write("\r\n\x1b[2m[process exited]\x1b[0m\r\n");
      else if (sess.fallback) sess.fallback.append("\r\n[process exited]\r\n");
    }
  };

  ws.onclose = (ev) => {
    if (!sessions.has(key)) return;
    const reason = ev.reason || (ev.code && ev.code !== 1000 ? `code ${ev.code}` : "");
    if (sess.term) {
      sess.term.write(`\r\n\x1b[2m[session ended${reason ? " — " + reason : ""}]\x1b[0m\r\n`);
    } else if (sess.fallback) {
      sess.fallback.append(`\r\n[session ended${reason ? " — " + reason : ""}]\r\n`);
    } else {
      pane.innerHTML = `<div class="muted" style="padding:.6rem">Session ended${reason ? " — " + esc(reason) : ""}.</div>`;
    }
    setStatus(sess, `<span class="pill">session ended${reason ? " — " + esc(reason) : ""}</span>`);
  };

  ws.onerror = () => {
    if (!sessions.has(key)) return;
    setStatus(sess, '<span class="pill bad">connection error</span>');
  };
}

export function openShell(opts) {
  mountQuake();
  setQuakeOpen(true);
  const envId = (opts && opts.envId) || currentEnvId();
  if (!envId) {
    openMenu();
    return;
  }
  connect({
    envId,
    machine: (opts && opts.machine) || "",
    label: opts && opts.label,
  });
}

function toggleQuake() {
  mountQuake();
  if (!isQuakeOpen()) {
    setQuakeOpen(true);
    openMenu();
    return;
  }
  const menu = document.getElementById("gsc-quake-menu");
  if (menu && !menu.hidden) {
    menu.hidden = true;
    menuGen += 1;
    return;
  }
  openMenu();
}

async function loadEnvTargets(envId) {
  const [serversRes, bmRes] = await Promise.all([
    api(`/api/v1/environments/${encodeURIComponent(envId)}/servers`).catch(() => null),
    api(`/api/v1/environments/${encodeURIComponent(envId)}/baremetal`).catch(() => null),
  ]);
  const servers = ((serversRes && serversRes.servers) || []).filter(
    (s) => s && s.hostname && (s.private_ip || s.ip)
  );
  const nodes = ((bmRes && bmRes.nodes) || []).filter((n) => n && n.id && n.bmc_host);
  return { servers, nodes };
}

function menuEnvIds() {
  const here = currentEnvId();
  const ordered = [];
  if (here) ordered.push(here);
  (store.envs || []).forEach((env) => {
    if (env && env.id && env.id !== here) ordered.push(env.id);
  });
  return [...new Set(ordered)].slice(0, 12);
}

async function openMenu() {
  const menu = document.getElementById("gsc-quake-menu");
  const btn = document.getElementById("gsc-quake-new");
  if (!menu || !btn) return;
  const gen = ++menuGen;
  menu.hidden = false;
  menu.innerHTML = `<div class="muted">Loading machines…</div>`;
  const rect = btn.getBoundingClientRect();
  menu.style.top = `${rect.bottom + 4}px`;
  menu.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - 280))}px`;
  const ids = menuEnvIds();
  const loaded = await Promise.all(
    ids.map(async (id) => ({ id, ...(await loadEnvTargets(id)) }))
  );
  if (gen !== menuGen || menu.hidden) return;
  const bits = [];
  const admin = canAdmin();
  const operate = canRun();
  const shellDis = admin ? "" : " disabled";
  const consoleDis = operate ? "" : " disabled";
  loaded.forEach(({ id, servers, nodes }) => {
    const envRow = (store.envs || []).find((e) => e && e.id === id);
    const deployHost = envRow && String(envRow.deployer_ssh_host || "").trim();
    const seen = new Set();
    const rows = [];
    if (deployHost) {
      rows.push(
        `<div class="gsc-quake-row">
          <span class="gsc-quake-row-name">Deploy host <span class="muted">${esc(deployHost)}</span></span>
          <button type="button" data-quake-env="${esc(id)}" data-quake-machine=""${shellDis}>Shell</button>
        </div>`
      );
    }
    (servers || []).forEach((s) => {
      const name = String(s.hostname || "");
      seen.add(name);
      const addr = s.private_ip || s.ip || "";
      const login = s.ssh_user ? `${s.ssh_user}@` : "";
      const node = (nodes || []).find((n) => String(n.name || "") === name);
      const consoleBtn = node
        ? `<button type="button" data-quake-console="${esc(node.id)}" data-quake-env="${esc(id)}" data-quake-label="${esc(envTitle(id))} · ${esc(name)} · console"${consoleDis}>Console</button>`
        : "";
      rows.push(
        `<div class="gsc-quake-row">
          <span class="gsc-quake-row-name">${esc(name)} <span class="muted">${esc(login + addr)}</span></span>
          <button type="button" data-quake-env="${esc(id)}" data-quake-machine="${esc(name)}"${shellDis}>Shell</button>
          ${consoleBtn}
        </div>`
      );
    });
    (nodes || []).forEach((n) => {
      const name = String(n.name || "");
      if (!name || seen.has(name)) return;
      rows.push(
        `<div class="gsc-quake-row">
          <span class="gsc-quake-row-name">${esc(name)} <span class="muted">${esc(n.bmc_host || "")}</span></span>
          <button type="button" data-quake-console="${esc(n.id)}" data-quake-env="${esc(id)}" data-quake-label="${esc(envTitle(id))} · ${esc(name)} · console"${consoleDis}>Console</button>
        </div>`
      );
    });
    if (!rows.length) return;
    bits.push(`<div class="gsc-quake-menu-label">${esc(envTitle(id))}</div>`);
    bits.push(rows.join(""));
  });
  if (!bits.length) {
    bits.push(`<div class="muted">No machines with an address or a management port.</div>`);
  }
  if (!admin) bits.push(`<div class="muted">An admin opens a shell.</div>`);
  menu.innerHTML = bits.join("");
}

async function openConsole(opts) {
  const envId = opts && opts.envId;
  const nodeId = opts && opts.nodeId;
  const label = (opts && opts.label) || "console";
  if (!envId || !nodeId) return;
  if (!canRun()) {
    toast("An operator opens a console", "warn");
    return;
  }
  mountQuake();
  setQuakeOpen(true);
  const key = sessionKey(envId, `console:${nodeId}`);
  if (sessions.has(key)) {
    focusSession(key);
    return;
  }
  const pane = document.createElement("div");
  pane.className = "gsc-quake-pane";
  const root = document.getElementById("gsc-quake-panes");
  if (!root) return;
  root.appendChild(pane);
  const sess = {
    key,
    envId,
    machine: `console:${nodeId}`,
    label,
    ws: null,
    term: null,
    fit: null,
    observer: null,
    fallback: null,
    sendResize: null,
    pane,
    statusHtml: '<span class="pill warn">opening console…</span>',
  };
  sessions.set(key, sess);
  focusSession(key);
  setStatus(sess, sess.statusHtml);
  try {
    const data = await api(
      `/api/v1/environments/${encodeURIComponent(envId)}/baremetal/nodes/${encodeURIComponent(nodeId)}/console/session`,
      { method: "POST", timeout: 25000 }
    );
    if (!sessions.has(key)) return;
    const embed = data && data.embed_url ? String(data.embed_url) : "";
    if (!data || !data.ok || !embed.startsWith("/") || embed.startsWith("//")) {
      pane.textContent = (data && data.error) || "Console unavailable";
      setStatus(sess, '<span class="pill bad">console unavailable</span>');
      return;
    }
    const frame = document.createElement("iframe");
    frame.title = label;
    frame.referrerPolicy = "no-referrer";
    frame.allow = "fullscreen";
    frame.src = embed;
    pane.replaceChildren(frame);
    setStatus(sess, '<span class="pill ok">console</span>');
  } catch (err) {
    if (!sessions.has(key)) return;
    pane.textContent = err && err.message ? err.message : "Console unavailable";
    setStatus(sess, '<span class="pill bad">console unavailable</span>');
  }
}

function onQuakeClick(e) {
  const close = e.target.closest("[data-quake-close]");
  if (close) {
    e.stopPropagation();
    disposeSession(close.dataset.quakeClose || "");
    return;
  }
  const tab = e.target.closest("[data-quake-tab]");
  if (tab) {
    focusSession(tab.dataset.quakeTab || "");
    return;
  }
  if (e.target.closest("#gsc-quake-hide")) {
    setQuakeOpen(false);
    return;
  }
  if (e.target.closest("#gsc-quake-expand")) {
    const el = quakeEl();
    setFullscreen(!(el && el.classList.contains(FS_CLASS)));
    return;
  }
  if (e.target.closest("#gsc-quake-new")) {
    const menu = document.getElementById("gsc-quake-menu");
    if (menu && !menu.hidden) {
      menu.hidden = true;
      menuGen += 1;
      return;
    }
    openMenu();
    return;
  }
  const consoleBtn = e.target.closest("[data-quake-console]");
  if (consoleBtn) {
    const menu = document.getElementById("gsc-quake-menu");
    if (menu) menu.hidden = true;
    openConsole({
      envId: consoleBtn.dataset.quakeEnv || "",
      nodeId: consoleBtn.dataset.quakeConsole || "",
      label: consoleBtn.dataset.quakeLabel || "console",
    });
    return;
  }
  const pick = e.target.closest("[data-quake-env]");
  if (pick) {
    const menu = document.getElementById("gsc-quake-menu");
    if (menu) menu.hidden = true;
    const envId = pick.dataset.quakeEnv || "";
    const machine = pick.dataset.quakeMachine || "";
    const label = machine
      ? `${envTitle(envId)} · ${machine}`
      : `${envTitle(envId)} · deploy host`;
    connect({ envId, machine, label });
    return;
  }
  const sess = sessions.get(activeKey);
  if (sess && sess.term && e.target.closest("#gsc-quake-panes")) sess.term.focus();
}

function onQuakeKey(e) {
  const shell = document.getElementById("app-shell");
  if (!shell || shell.classList.contains("hidden")) return;
  const tick = e.key === "`" || e.key === "~" || e.code === "Backquote";
  if (tick) {
    const el = quakeEl();
    if (isQuakeOpen() && el && el.contains(document.activeElement)) return;
    const target = e.target;
    if (target && target.closest && target.closest("input, textarea, select, [contenteditable='true']")) return;
    e.preventDefault();
    toggleQuake();
    return;
  }
  if (e.key === "Escape" && isQuakeOpen() && !frontModalOpen()) {
    e.preventDefault();
    e.stopPropagation();
    setQuakeOpen(false);
  }
}

function onDocClick(e) {
  const menu = document.getElementById("gsc-quake-menu");
  if (!menu || menu.hidden) return;
  if (e.target.closest("#gsc-quake-menu") || e.target.closest("#gsc-quake-new")) return;
  menu.hidden = true;
  menuGen += 1;
}

export function mountQuake() {
  if (mounted && quakeEl()) return;
  mounted = true;
  if (!quakeEl()) {
    const el = document.createElement("div");
    el.id = "gsc-quake";
    el.className = "gsc-quake";
    el.setAttribute("aria-hidden", "true");
    el.innerHTML = `
      <div class="gsc-quake-bar">
        <span class="gsc-quake-title">Shell</span>
        <div id="gsc-quake-tabs" class="gsc-quake-tabs" role="tablist"></div>
        <button type="button" class="secondary btn-sm" id="gsc-quake-new">New</button>
        <span id="gsc-term-status"></span>
        <span class="gsc-quake-hint">\` lists machines · Esc hides</span>
        <button type="button" class="secondary btn-sm" id="gsc-quake-expand" title="Expand shell">⤢</button>
        <button type="button" class="secondary btn-sm" id="gsc-quake-hide">Hide</button>
      </div>
      <div id="gsc-quake-panes" class="gsc-quake-body"></div>`;
    document.body.appendChild(el);
    const menu = document.createElement("div");
    menu.id = "gsc-quake-menu";
    menu.className = "gsc-quake-menu";
    menu.hidden = true;
    document.body.appendChild(menu);
  }
  const el = quakeEl();
  if (el && !el.dataset.wired) {
    el.dataset.wired = "1";
    el.addEventListener("click", onQuakeClick);
    const menu = document.getElementById("gsc-quake-menu");
    if (menu) menu.addEventListener("click", onQuakeClick);
    window.addEventListener("keydown", onQuakeKey, true);
    document.addEventListener("click", onDocClick);
  }
  renderTabs();
}

export function terminalCardHtml() {
  return "";
}

export function wireTerminalCard() {}

export function loadTerminalCard() {}

export function destroyTerminalCard() {}
