// pages/environment_terminal.js — in-browser ssh terminal on the env's deploy host.
// The card lives in the workflow tab panel of the environment detail page.
// "Open terminal ▸" loads the vendored xterm.js assets (no CDN —
// /static/vendor/xterm/), fetches a single-use ticket (POST /api/v1/auth/ticket),
// opens WS /api/v1/terminal?environment_id=..&ticket=.., and
// bridges frames: xterm onData → {type:"input"}, {type:"output"} → term.write, fit
// addon + ResizeObserver → {type:"resize"}. On close the panel shows a "session
// ended" state with a Reconnect button. Role-gated: the connect button requires
// admin+ (canAdmin), matching the backend. If the vendored assets fail to load, a
// minimal fallback (plain <pre> + hidden input) still gives a read/write console.
//
// Native-terminal feel: dark full-width panel with a neutral #0c0c0c/#e6e6e6 theme
// and green cursor/accents, blinking block cursor, ~60vh height plus a fullscreen
// overlay toggle (Esc exits), copy-on-select, right-click / Ctrl+Shift+V / Cmd+V
// paste, Ctrl+Shift+C copy (never sends ^C for that chord). All other keys pass
// through xterm's own key handling, so Ctrl+C, arrows, Tab, Ctrl+L (form feed —
// the remote shell clears) and Ctrl+D reach the pty as raw codes via onData.
import { api, esc, toast } from "../api.js";
import { canAdmin, gate } from "../store.js";

const VENDOR_BASE = "/static/vendor/xterm";
const FS_CLASS = "gsc-term-fullscreen";

// One consistent palette: near-black background, neutral foreground, green
// cursor + selection accents (ANSI colors tuned to sit well on #0c0c0c).
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
  bellStyle: "none", // no audible bell
  macOptionIsMeta: true,
};

let xtermLoading = null; // shared one-shot loader promise (true = usable)
let current = null; // live session { envId, ws, term, fit, observer, fallback, sendResize }
let loadedEnvId = ""; // env the card last rendered for
let target = ""; // "user@host" label of the last fetched env

// Load xterm.css + the UMD bundles once (they set window.Terminal /
// window.FitAddon). Resolves false when the vendored files are missing —
// callers then use the fallback console instead of a dead panel.
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

function setStatus(html) {
  const el = document.getElementById("gsc-term-status");
  if (el) el.innerHTML = html;
}

function setButtons() {
  const open = document.getElementById("gsc-term-open");
  const close = document.getElementById("gsc-term-close");
  const live = !!(current && current.ws && current.ws.readyState === WebSocket.OPEN);
  if (open) {
    open.textContent = current ? "Reconnect ▸" : "Open terminal ▸";
    open.disabled = !canAdmin() || live;
  }
  if (close) close.disabled = !live;
}

// An idle card is the header only. The overview map keeps the rest of the window.
function setTermOpen(open) {
  const card = document.getElementById("gsc-term-card");
  if (!card) return;
  card.classList.toggle("gsc-term-idle", !open);
}

// ---------- clipboard (native-terminal chords) ----------

// Copy text via the clipboard API. Where the API is blocked (non-secure
// context, denied permission) degrade to a toast so the user gets feedback
// instead of silence.
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

// Read the clipboard and push it into the pty as terminal input.
async function pasteFromClipboard(sess) {
  let text = "";
  try {
    text = await navigator.clipboard.readText();
  } catch {
    toast("Paste blocked — allow clipboard access", "warn");
    return;
  }
  if (text && sess.ws.readyState === WebSocket.OPEN) {
    sess.ws.send(JSON.stringify({ type: "input", data: text }));
  }
  if (sess.term) sess.term.focus();
}

// Clipboard chords + fullscreen Esc live here; every other key returns true so
// xterm's own key handling turns it into raw bytes (Ctrl+C → \x03, arrows →
// escape sequences, Tab → \t, Ctrl+L → \x0c form feed, Ctrl+D → \x04) which
// flow to the pty through onData.
function attachKeys(sess) {
  const term = sess.term;
  term.attachCustomKeyEventHandler((e) => {
    if (e.type !== "keydown") return true;
    const key = (e.key || "").toLowerCase();
    if (e.key === "Escape" && isFullscreen()) {
      setFullscreen(false);
      return false;
    }
    if (e.ctrlKey && e.shiftKey && key === "c") {
      copySelection(term); // copy — never send ^C for this chord
      return false;
    }
    if (e.ctrlKey && e.shiftKey && key === "v") {
      pasteFromClipboard(sess);
      return false;
    }
    if (e.metaKey && key === "v") {
      pasteFromClipboard(sess); // macOS paste
      return false;
    }
    if (e.metaKey && key === "c") {
      if (!term.hasSelection()) return true;
      copySelection(term); // macOS copy only with a selection
      return false;
    }
    return true;
  });
}

// ---------- fullscreen overlay ----------

function isFullscreen() {
  const card = document.getElementById("gsc-term-card");
  return !!(card && card.classList.contains(FS_CLASS));
}

function onFsEsc(e) {
  if (e.key === "Escape" && isFullscreen()) {
    e.preventDefault();
    e.stopPropagation();
    setFullscreen(false);
  }
}

// Toggle the viewport-filling overlay. The card goes fixed inset:0 with the
// header on top and the terminal filling the rest; refit + resize frame after.
function setFullscreen(on) {
  const card = document.getElementById("gsc-term-card");
  if (!card) return;
  if (on) setTermOpen(true);
  else if (!current) setTermOpen(false);
  card.classList.toggle(FS_CLASS, on);
  document.body.classList.toggle("gsc-term-fs-lock", on);
  const btn = document.getElementById("gsc-term-expand");
  if (btn) {
    btn.textContent = on ? "⤡" : "⤢";
    btn.title = on ? "Exit fullscreen (Esc)" : "Expand terminal to fullscreen";
  }
  if (on) document.addEventListener("keydown", onFsEsc, true);
  else document.removeEventListener("keydown", onFsEsc, true);
  const sess = current;
  if (sess && sess.term && sess.fit) {
    try {
      sess.fit.fit();
    } catch {
      return; // not visible right now
    }
    if (sess.sendResize) sess.sendResize();
    sess.term.focus();
  }
}

// Tear down the live session (socket, terminal, observers). Safe to call
// repeatedly; used on env switch, page destroy, and before a reconnect.
export function destroyTerminalCard() {
  setFullscreen(false);
  setTermOpen(false);
  if (!current) return;
  const sess = current;
  current = null;
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
      /* half-initialized terminal */
    }
  }
  const body = document.getElementById("gsc-term-body");
  if (body) body.innerHTML = "";
  setButtons();
}

function wsUrl(envId, ticket) {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return (
    `${proto}//${location.host}/api/v1/terminal` +
    `?environment_id=${encodeURIComponent(envId)}&ticket=${encodeURIComponent(ticket)}`
  );
}

// ---------- fallback console (vendored xterm unavailable) ----------

// Minimal read/write terminal: a <pre> for output and a visually hidden input
// that keeps focus so keystrokes can be forwarded. ANSI escape sequences are
// stripped — this is a degraded path, not a full emulator.
function startFallback(body, ws, onEnded) {
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
    if (e.key === "Enter") send("\r");
    else if (e.key === "Backspace") send("\x7f");
    else if (e.key === "Tab") send("\t");
    else if (e.key === "Escape") send("\x1b");
    else if (e.key === "ArrowUp") send("\x1b[A");
    else if (e.key === "ArrowDown") send("\x1b[B");
    else if (e.key === "ArrowRight") send("\x1b[C");
    else if (e.key === "ArrowLeft") send("\x1b[D");
    else if (e.ctrlKey && e.key.length === 1) {
      const code = e.key.toLowerCase().charCodeAt(0) - 96; // Ctrl+A..Z → \x01..\x1a
      if (code >= 1 && code <= 26) send(String.fromCharCode(code));
    } else if (e.key.length === 1 && !e.metaKey && !e.altKey) send(e.key);
    e.preventDefault();
  });
  pre.addEventListener("click", () => input.focus());
  input.focus();
  append("[fallback terminal — xterm.js assets unavailable]\r\n");
  return { append, onClose: onEnded };
}

// ---------- connect ----------

async function connect(envId) {
  if (!envId || (current && current.ws && current.ws.readyState === WebSocket.OPEN)) return;
  destroyTerminalCard();
  setTermOpen(true);
  const body = document.getElementById("gsc-term-body");
  if (!body) return;
  setStatus('<span class="pill warn">connecting…</span>');
  setButtons();

  const xtermOk = await loadXterm();
  if (!document.getElementById("gsc-term-body")) return; // navigated away mid-load

  // Browsers cannot set headers on a WebSocket handshake, and a raw token in
  // the query string would land in access logs — exchange the stored
  // credential for a single-use ticket first.
  let ticket;
  try {
    ({ ticket } = await api("/api/v1/auth/ticket", { method: "POST" }));
  } catch (e) {
    setStatus(`<span class="pill bad">auth failed — ${esc(e.message || "ticket error")}</span>`);
    setButtons();
    setTermOpen(false);
    return;
  }
  if (!document.getElementById("gsc-term-body")) return; // navigated away mid-load

  const ws = new WebSocket(wsUrl(envId, ticket));
  const sess = { envId, ws, term: null, fit: null, observer: null, fallback: null, sendResize: null };
  current = sess;

  const onEnded = (note) => {
    if (current !== sess) return;
    setStatus(`<span class="pill">session ended${note ? " — " + esc(note) : ""}</span>`);
    toast("Terminal session ended", "info");
    setButtons();
  };

  const sendResize = () => {
    if (!sess.fit || ws.readyState !== WebSocket.OPEN) return;
    const dims = sess.fit.proposeDimensions();
    if (dims) ws.send(JSON.stringify({ type: "resize", cols: dims.cols, rows: dims.rows }));
  };
  sess.sendResize = sendResize;

  ws.onopen = async () => {
    if (current !== sess) return;
    setStatus(`<span class="pill ok">connected — ${esc(target || "deploy host")}</span>`);
    setButtons();
    if (xtermOk) {
      body.innerHTML = "";
      const term = new window.Terminal(TERM_OPTIONS);
      const fit = new window.FitAddon.FitAddon();
      sess.term = term;
      sess.fit = fit;
      term.loadAddon(fit);
      term.open(body);
      fit.fit();
      attachKeys(sess);
      term.onData((data) => {
        if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "input", data }));
      });
      // Copy-on-select, like a native terminal.
      term.onSelectionChange(() => {
        if (term.hasSelection()) copySelection(term);
      });
      // Right-click pastes into the pty instead of a context menu.
      body.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        pasteFromClipboard(sess);
      });
      if (window.ResizeObserver) {
        sess.observer = new ResizeObserver(() => {
          try {
            fit.fit();
          } catch {
            return; // not visible right now
          }
          sendResize();
        });
        sess.observer.observe(body);
      }
      sendResize();
      term.focus();
    } else {
      sess.fallback = startFallback(body, ws, onEnded);
    }
  };

  ws.onmessage = (ev) => {
    if (current !== sess) return;
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
    if (current !== sess) return;
    const reason = ev.reason || (ev.code && ev.code !== 1000 ? `code ${ev.code}` : "");
    if (sess.term) {
      sess.term.write(`\r\n\x1b[2m[session ended${reason ? " — " + reason : ""}]\x1b[0m\r\n`);
    } else if (sess.fallback) {
      sess.fallback.append(`\r\n[session ended${reason ? " — " + reason : ""}]\r\n`);
    }
    onEnded(reason);
  };

  ws.onerror = () => {
    if (current !== sess) return;
    setStatus('<span class="pill bad">connection error</span>');
  };
}

// ---------- card ----------

export function terminalCardHtml() {
  return `
  <div class="card span-12 gsc-term-idle" id="gsc-term-card">
    <div class="toolbar gsc-term-header">
      <h2>Deploy host terminal</h2>
      <span id="gsc-term-target" class="muted"></span>
      <span id="gsc-term-status" class="pill">idle</span>
      <button class="secondary btn-sm" id="gsc-term-open" type="button"
        title="Open an ssh shell on the deploy host (admin+)">Open terminal ▸</button>
      <button class="secondary btn-sm gsc-term-iconbtn" id="gsc-term-expand" type="button"
        title="Expand terminal to fullscreen">⤢</button>
      <button class="secondary btn-sm" id="gsc-term-close" type="button" disabled>Close</button>
    </div>
    <div id="gsc-term-body" class="gsc-term-body">
      <div class="muted" style="padding:.5rem">No session — open a terminal to get a shell on the deploy host.</div>
    </div>
  </div>`;
}

export function wireTerminalCard(getEnvId) {
  const card = document.getElementById("gsc-term-card");
  if (!card) return;
  card.addEventListener("click", (e) => {
    if (e.target.closest("#gsc-term-open")) {
      const btn = document.getElementById("gsc-term-open");
      if (btn && !btn.disabled) connect(getEnvId());
      return;
    }
    if (e.target.closest("#gsc-term-close")) {
      destroyTerminalCard();
      return;
    }
    if (e.target.closest("#gsc-term-expand")) {
      setFullscreen(!isFullscreen());
      return;
    }
    // Clicking anywhere else in the panel hands focus back to the terminal.
    if (current && current.term) current.term.focus();
  });
}

// (Re)render the card's target line for the env. The terminal needs the env's
// deploy host; without one the backend rejects the socket, so say so up front.
export async function loadTerminalCard(envId) {
  const card = document.getElementById("gsc-term-card");
  if (!card) return;
  if ((envId || "") !== loadedEnvId) {
    destroyTerminalCard();
    loadedEnvId = envId || "";
  }
  const targetEl = document.getElementById("gsc-term-target");
  const openBtn = document.getElementById("gsc-term-open");
  if (!envId) {
    target = "";
    if (targetEl) targetEl.textContent = "";
    if (openBtn) openBtn.disabled = true;
    return;
  }
  let env = null;
  try {
    env = await api(`/api/v1/environments/${encodeURIComponent(envId)}`);
  } catch {
    env = null;
  }
  if (loadedEnvId !== envId) return; // env switched while loading
  const host = env && env.deployer_ssh_host ? String(env.deployer_ssh_host) : "";
  const user = env && env.deployer_ssh_user ? String(env.deployer_ssh_user) : "root";
  target = host ? `${user}@${host}` : "";
  if (targetEl) {
    targetEl.innerHTML = host
      ? `target <code>${esc(target)}</code>`
      : '<span class="muted">no deploy host set — edit the environment to add one</span>';
  }
  if (openBtn) {
    openBtn.disabled = !host || !canAdmin();
    openBtn.title = !host
      ? "Set deployer_ssh_host on the environment first"
      : gate(canAdmin(), "admin")
        ? "Requires admin role"
        : "Open an ssh shell on the deploy host";
  }
  setButtons();
}
