// Honeycomb nest: one level of the tree at a time. Click a cell to bloom
// a hex action menu around it (see / open / logs / in). Double-click or
// Enter still drills. Breadcrumb to go up. Does not replace 2D or 3D.

const LAYERS = [
  { id: "metal", label: "Metal", kinds: ["machine", "group", "env"], color: "#fb923c" },
  { id: "overlay", label: "Overlay / VPC", kinds: ["overlay", "net", "subnet", "router", "vpc"], color: "#38bdf8" },
  { id: "net", label: "Registry", kinds: ["registry", "regcache", "tests", "testsuite"], color: "#7dd3fc" },
  { id: "k8s", label: "Kubernetes", kinds: ["k8s", "ns", "svc"], color: "#a78bfa" },
  { id: "pods", label: "Pods", kinds: ["pod"], color: "#34d399" },
  { id: "nova", label: "Nova VMs", kinds: ["vm"], color: "#fbbf24" },
  { id: "tenants", label: "Tenants", kinds: ["tenant"], color: "#fda4af" },
  { id: "edge", label: "Edge / Ingress", kinds: ["edge", "ingress", "fip", "lb", "gw", "route"], color: "#f472b6" },
];

const MENU_COL = {
  in: "#34c759",
  see: "#94a3b8",
  open: "#34d399",
  logs: "#fbbf24",
  up: "#64748b",
  close: "#475569",
};

const KIND_LAYER = new Map();
for (const layer of LAYERS) {
  for (const kind of layer.kinds) KIND_LAYER.set(kind, layer);
}

const STATE_COL = {
  ok: "#34d399",
  run: "#fbbf24",
  warn: "#fb923c",
  bad: "#f87171",
  wait: "#64748b",
};

let canvas = null;
let ctx = null;
let hud = null;
let raf = 0;
let enabled = false;
let wired = false;
let cssW = 0;
let cssH = 0;
let getGraph = () => ({ nodes: [], edges: [], byId: new Map() });
let onSelect = () => {};
let onAction = () => {};
let focusId = "infra";
let hoverId = "";
let selectedId = "";
let menuId = "";
let menuCells = [];
let menuBorn = 0;
let stageOn = false;
let stageId = "";
let moveBorn = 0;
const lastPos = new Map();
const fromPos = new Map();
let alertsOnly = false;
let hiddenLayers = new Set();
let hexR = 42;
let cells = [];
let crumbCells = [];
let t0 = performance.now();
let lastHud = "";
const listeners = [];

function on(el, type, fn, opts) {
  el.addEventListener(type, fn, opts);
  listeners.push([el, type, fn, opts]);
}

function unbind() {
  for (const [el, type, fn, opts] of listeners) {
    try {
      el.removeEventListener(type, fn, opts);
    } catch {
      /* ignore */
    }
  }
  listeners.length = 0;
  wired = false;
}

function graphSnapshot() {
  try {
    return getGraph() || { nodes: [], edges: [], byId: new Map() };
  } catch {
    return { nodes: [], edges: [], byId: new Map() };
  }
}

function nodeOf(id, g) {
  if (!id) return null;
  if (g.byId && g.byId.get) return g.byId.get(id) || null;
  return (g.nodes || []).find((n) => n && n.id === id) || null;
}

function layerOf(kind) {
  return KIND_LAYER.get(kind) || null;
}

function isHot(n) {
  if (n && n.hot) return true;
  const s = String((n && n.state) || "");
  return s === "bad" || s === "warn" || s === "run";
}

function liveLine(g) {
  const live = g && g.live;
  if (!live || !live.live) return "";
  const bits = [live.stage, live.item || live.service].filter(Boolean);
  return bits.length ? `live · ${bits.join(" · ")}` : "live";
}

function kidsOf(id, g) {
  const out = [];
  const seen = new Set();
  for (const e of g.edges || []) {
    const s = e.source || e.from;
    const t = e.target || e.to;
    if (s !== id || !t || seen.has(t)) continue;
    seen.add(t);
    const n = nodeOf(t, g);
    if (n) out.push(n);
  }
  return out;
}

function parentOf(id, g) {
  const n = nodeOf(id, g);
  if (n && n.nestParent) {
    const nest = nodeOf(n.nestParent, g);
    if (nest) return nest;
  }
  for (const e of g.edges || []) {
    const s = e.source || e.from;
    const t = e.target || e.to;
    if (t === id && s) return nodeOf(s, g);
  }
  return null;
}

function descendantsHot(id, g, depth) {
  if (depth > 6) return false;
  const n = nodeOf(id, g);
  if (n && isHot(n)) return true;
  for (const c of kidsOf(id, g)) {
    if (descendantsHot(c.id, g, depth + 1)) return true;
  }
  return false;
}

function layerHidden(n) {
  const layer = layerOf(n && n.kind);
  return !!(layer && hiddenLayers.has(layer.id));
}

function visibleKids(id, g) {
  let kids = kidsOf(id, g);
  kids = kids.filter((n) => !layerHidden(n));
  if (alertsOnly) kids = kids.filter((n) => descendantsHot(n.id, g, 0));
  kids.sort((a, b) => {
    const ha = isHot(a) ? 0 : 1;
    const hb = isHot(b) ? 0 : 1;
    if (ha !== hb) return ha - hb;
    return String(a.title || a.id).localeCompare(String(b.title || b.id));
  });
  return kids;
}

function crumb(g) {
  const path = [];
  let cur = nodeOf(focusId, g);
  const guard = new Set();
  while (cur && !guard.has(cur.id)) {
    guard.add(cur.id);
    path.unshift(cur);
    cur = parentOf(cur.id, g);
  }
  if (!path.length) {
    const env = nodeOf("env", g) || nodeOf("infra", g);
    if (env) path.push(env);
  }
  return path;
}

function hexCenters(n, cx, cy, r) {
  if (n <= 0) return [];
  if (n === 1) return [{ x: cx, y: cy }];
  const pts = [];
  const dx = r * 1.78;
  const dy = r * 1.54;
  const cols = Math.max(1, Math.ceil(Math.sqrt(n)));
  const rows = Math.ceil(n / cols);
  const gridW = (cols - 1) * dx + (rows > 1 ? dx * 0.5 : 0);
  const gridH = (rows - 1) * dy;
  let i = 0;
  for (let row = 0; row < rows && i < n; row++) {
    const colsHere = Math.min(cols, n - i);
    const rowW = (colsHere - 1) * dx;
    const ox = cx - rowW / 2 + (row % 2) * (dx * 0.5);
    const oy = cy - gridH / 2 + row * dy;
    for (let col = 0; col < colsHere && i < n; col++, i++) {
      pts.push({ x: ox + col * dx, y: oy });
    }
  }
  return pts;
}

function hexPath(x, y, r) {
  ctx.beginPath();
  for (let i = 0; i < 6; i++) {
    const a = (Math.PI / 3) * i - Math.PI / 6;
    const px = x + Math.cos(a) * r;
    const py = y + Math.sin(a) * r;
    if (i) ctx.lineTo(px, py);
    else ctx.moveTo(px, py);
  }
  ctx.closePath();
}

function layoutCrumbCells(path, w) {
  const r = 20;
  const y = 70;
  const n = path.length;
  const span = Math.max(0, n - 1) * r * 1.92;
  let x0 = Math.max(r + 12, w / 2 - span / 2);
  if (x0 + span + r > w - 12) x0 = r + 12;
  crumbCells = path.map((node, i) => {
    const layer = layerOf(node.kind);
    const here = i === n - 1;
    return {
      id: node.id,
      x: x0 + i * r * 1.92,
      y,
      r: here ? r + 5 : r,
      label: nodeLabel(node).slice(0, here ? 12 : 8),
      sub: "",
      col: STATE_COL[node.state] || (layer && layer.color) || STATE_COL.wait,
      hot: isHot(node),
      crumb: true,
      here,
    };
  });
}

function hitTest(clientX, clientY) {
  if (!canvas) return { id: "", d: Infinity, crumb: false };
  const rect = canvas.getBoundingClientRect();
  const x = clientX - rect.left;
  const y = clientY - rect.top;
  let best = { id: "", d: Infinity, crumb: false, menu: false, action: "" };
  for (const c of menuCells) {
    const d = Math.hypot(c.x - x, c.y - y);
    if (d < c.r * 0.92 && d < best.d) {
      best = { id: c.id, d, crumb: false, menu: true, action: c.action };
    }
  }
  if (best.menu) return best;
  const pool = stageOn
    ? cells
    : menuId
      ? crumbCells.concat(cells.filter((c) => c.id === menuId))
      : crumbCells.concat(cells);
  for (const c of pool) {
    const p = lastPos.get(c.id) || c;
    const d = Math.hypot(p.x - x, p.y - y);
    if (d < (p.r || c.r) * 0.92 && d < best.d) best = { id: c.id, d, crumb: !!c.crumb, menu: false, action: "" };
  }
  return best;
}

function closeMenu() {
  menuId = "";
  menuCells = [];
}

function bumpMove() {
  fromPos.clear();
  for (const [id, p] of lastPos) fromPos.set(id, { x: p.x, y: p.y, r: p.r });
  moveBorn = performance.now();
}

function visCell(c) {
  const t = Math.min(1, (performance.now() - moveBorn) / 280);
  const e = 1 - Math.pow(1 - t, 3);
  const prev = fromPos.get(c.id);
  const v = prev
    ? {
        ...c,
        x: prev.x + (c.x - prev.x) * e,
        y: prev.y + (c.y - prev.y) * e,
        r: (prev.r || c.r) + (c.r - (prev.r || c.r)) * e,
      }
    : c;
  lastPos.set(c.id, { x: v.x, y: v.y, r: v.r });
  return v;
}

function relatedForStage(id, g) {
  const out = [];
  const seen = new Set([id]);
  const push = (n) => {
    if (!n || !n.id || seen.has(n.id)) return;
    seen.add(n.id);
    out.push(n);
  };
  push(parentOf(id, g));
  for (const k of kidsOf(id, g)) push(k);
  const p = parentOf(id, g);
  if (p) {
    for (const s of visibleKids(p.id, g)) push(s);
  }
  for (const e of g.edges || []) {
    const s = e.source || e.from;
    const t = e.target || e.to;
    if (s === id) push(nodeOf(t, g));
    if (t === id) push(nodeOf(s, g));
  }
  return out.slice(0, 11);
}

function actionsFor(node, g) {
  const acts = [];
  if (!node) return acts;
  if (kidsOf(node.id, g).length) acts.push({ id: "in", label: "in", sub: "nest" });
  acts.push({ id: "see", label: "see", sub: "details" });
  if (node.kind === "vm") acts.push({ id: "open", label: "open", sub: "vnc" });
  else if (node.kind === "pod") {
    acts.push({ id: "open", label: "open", sub: "shell" });
    acts.push({ id: "logs", label: "logs", sub: "follow" });
  } else if (node.kind === "machine" || node.kind === "k8s" || node.kind === "osrole") {
    acts.push({ id: "open", label: "open", sub: "iLO" });
    acts.push({ id: "logs", label: "logs", sub: "talos" });
  } else if (node.kind === "ingress" || node.kind === "edge" || node.kind === "gw" || node.kind === "route") {
    acts.push({ id: "open", label: "open", sub: "url" });
  }
  if (parentOf(node.id, g)) acts.push({ id: "up", label: "up", sub: "nest" });
  acts.push({ id: "close", label: "close", sub: "menu" });
  return acts.slice(0, 6);
}

function openMenu(id) {
  if (!id) {
    closeMenu();
    return;
  }
  if (menuId !== id) menuBorn = performance.now();
  menuId = id;
}

function layoutMenu() {
  menuCells = [];
  if (!menuId) return;
  const cell = cells.find((c) => c.id === menuId);
  if (!cell) {
    closeMenu();
    return;
  }
  const g = graphSnapshot();
  const node = nodeOf(menuId, g);
  const acts = actionsFor(node, g);
  const n = acts.length;
  if (!n) return;
  const r = stageOn ? Math.max(18, cell.r * 0.78) : cell.r;
  const dist = r * 1.78;
  const w = cssW;
  const h = cssH;
  const cx = w / 2;
  const cy = h / 2 + 6;
  const base = stageOn ? Math.atan2(cell.y - cy, cell.x - cx) : -Math.PI / 2;
  for (let i = 0; i < n; i++) {
    const a = acts[i];
    const ang = stageOn ? base + (i - (n - 1) / 2) * 0.46 : -Math.PI / 2 + (i * Math.PI * 2) / 6;
    let x = cell.x + Math.cos(ang) * dist;
    let y = cell.y + Math.sin(ang) * dist;
    x = Math.max(r + 8, Math.min(w - r - 8, x));
    y = Math.max(r + 52, Math.min(h - r - 32, y));
    menuCells.push({
      action: a.id,
      id: `__menu:${a.id}`,
      x,
      y,
      r,
      label: a.label,
      sub: a.sub,
      col: MENU_COL[a.id] || "#94a3b8",
      hot: a.id === "open" || a.id === "in",
      menu: true,
    });
  }
}

function runMenu(action, id) {
  if (action === "close") {
    closeMenu();
    return;
  }
  if (action === "in") {
    closeMenu();
    drill(id);
    return;
  }
  if (action === "up") {
    closeMenu();
    goUp();
    return;
  }
  if (action === "see") onSelect(id);
  onAction(action, id);
}

function nodeLabel(n) {
  return String((n && (n.title || n.label || n.name || n.id)) || "").replace(/^m:/, "");
}

function drawHex(c, now) {
  const pulse = c.hot ? 1 + 0.06 * Math.sin(now * 4.2) : 1;
  const r = c.r * pulse;
  hexPath(c.x, c.y, r);
  if (c.menu) {
    ctx.fillStyle = "rgba(16, 5, 5, 0.94)";
    ctx.fill();
    ctx.lineWidth = c.id === hoverId ? 3.2 : 2.2;
    ctx.strokeStyle = c.col;
    ctx.stroke();
    hexPath(c.x, c.y, r * 0.86);
    ctx.strokeStyle = c.col + "66";
    ctx.lineWidth = 1;
    ctx.stroke();
  } else {
    ctx.fillStyle = c.col + "cc";
    ctx.fill();
    ctx.lineWidth = c.here || c.id === selectedId ? 3 : c.id === hoverId ? 2 : 1;
    ctx.strokeStyle = c.here || c.id === selectedId ? "#fff" : "rgba(255,255,255,0.35)";
    ctx.stroke();
    if (c.hot) {
      hexPath(c.x, c.y, r * 1.12);
      ctx.strokeStyle = c.col;
      ctx.lineWidth = 1.4;
      ctx.stroke();
    }
  }
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  const label = c.label.slice(0, 18);
  ctx.font = `600 ${Math.max(9, Math.min(13, r * 0.32))}px ui-sans-serif, system-ui`;
  ctx.fillStyle = c.menu ? c.col : "#f5f5f5";
  ctx.fillText(label, c.x, c.y - (c.sub ? 6 : 0));
  if (c.sub) {
    ctx.font = `500 ${Math.max(8, Math.min(11, r * 0.24))}px ui-sans-serif, system-ui`;
    ctx.fillStyle = c.menu ? "rgba(245,245,245,0.78)" : "rgba(245,245,245,0.72)";
    ctx.fillText(c.sub.slice(0, 22), c.x, c.y + 10);
  }
}

function layoutCells() {
  const g = graphSnapshot();
  const w = cssW;
  const h = cssH;
  const parent = nodeOf(focusId, g) || nodeOf("infra", g) || nodeOf("env", g);
  const fid = parent ? parent.id : "infra";
  if (parent && focusId !== fid) focusId = fid;
  const kids = parent ? visibleKids(fid, g) : (g.nodes || []).filter((n) => n && n.kind === "machine");
  const n = kids.length;
  const pad = 88;
  const availW = Math.max(160, w - pad * 2);
  const availH = Math.max(140, h - 150);
  const cols = Math.max(1, Math.ceil(Math.sqrt(n || 1)));
  const rows = Math.max(1, Math.ceil((n || 1) / cols));
  hexR = Math.max(28, Math.min(56, availW / (cols * 1.9), availH / (rows * 1.7)));
  const path = crumb(g);
  layoutCrumbCells(path, w);
  if (stageOn) {
    layoutStage(g, w, h);
    layoutMenu();
    return;
  }
  const pts = hexCenters(n, w / 2, h / 2 + 28, hexR);
  cells = kids.map((node, i) => {
    const p = pts[i] || { x: w / 2, y: h / 2 };
    const layer = layerOf(node.kind);
    return {
      id: node.id,
      x: p.x,
      y: p.y,
      r: hexR,
      label: nodeLabel(node),
      sub: node.kind === "machine" ? String(node.subtitle || node.state || "") : String(node.kind || ""),
      col: STATE_COL[node.state] || (layer && layer.color) || STATE_COL.wait,
      hot: isHot(node) || descendantsHot(node.id, g, 0),
      hasKids: kidsOf(node.id, g).length > 0,
    };
  });
  layoutMenu();
}

function cellFromNode(node, x, y, r, extra) {
  const layer = layerOf(node.kind);
  return {
    id: node.id,
    x,
    y,
    r,
    label: nodeLabel(node),
    sub: node.kind === "machine" ? String(node.subtitle || node.state || "") : String(node.kind || ""),
    col: STATE_COL[node.state] || (layer && layer.color) || STATE_COL.wait,
    hot: isHot(node) || descendantsHot(node.id, graphSnapshot(), 0),
    hasKids: kidsOf(node.id, graphSnapshot()).length > 0,
    ...(extra || {}),
  };
}

function layoutStage(g, w, h) {
  const focus = nodeOf(stageId, g) || nodeOf(selectedId, g);
  const cx = w / 2;
  const cy = h / 2 + 6;
  const well = Math.min(w, h) * 0.29;
  const r = Math.max(22, Math.min(34, well * 0.18));
  const ring = well + r * 1.62;
  const related = focus ? relatedForStage(focus.id, g) : [];
  const ringNodes = [];
  const seen = new Set();
  if (focus) {
    ringNodes.push(focus);
    seen.add(focus.id);
  }
  for (const n of related) {
    if (!n || seen.has(n.id)) continue;
    seen.add(n.id);
    ringNodes.push(n);
    if (ringNodes.length >= 12) break;
  }
  const n = Math.max(ringNodes.length, 6);
  cells = ringNodes.map((node, i) => {
    const ang = -Math.PI / 2 + (i * Math.PI * 2) / n;
    return cellFromNode(node, cx + Math.cos(ang) * ring, cy + Math.sin(ang) * ring, r, {
      staged: true,
      well,
    });
  });
  const leftovers = (visibleKids(focusId, g) || []).filter((n) => n && !seen.has(n.id)).slice(0, 10);
  leftovers.forEach((node, i) => {
    const side = i % 4;
    const slot = Math.floor(i / 4);
    const pr = r * 0.7;
    let x = pr + 12;
    let y = 90 + slot * (pr * 2.1);
    if (side === 1) {
      x = w - pr - 12;
      y = 90 + slot * (pr * 2.1);
    } else if (side === 2) {
      x = 28 + slot * (pr * 2.2);
      y = h - pr - 18;
    } else if (side === 3) {
      x = w - 28 - slot * (pr * 2.2);
      y = h - pr - 18;
    }
    cells.push(cellFromNode(node, x, y, pr, { parked: true }));
  });
}

function drawMenu(nowMs) {
  if (!menuCells.length) return;
  const cell = cells.find((c) => c.id === menuId);
  const t = Math.min(1, (nowMs - menuBorn) / 200);
  const ease = 1 - Math.pow(1 - t, 3);
  for (const c of menuCells) {
    const x = cell ? cell.x + (c.x - cell.x) * ease : c.x;
    const y = cell ? cell.y + (c.y - cell.y) * ease : c.y;
    if (cell) {
      ctx.strokeStyle = `rgba(245,245,245,${0.12 + 0.16 * ease})`;
      ctx.lineWidth = 1.6;
      ctx.beginPath();
      ctx.moveTo(cell.x, cell.y);
      ctx.lineTo(x, y);
      ctx.stroke();
    }
    drawHex({ ...c, x, y, r: c.r * (0.35 + 0.65 * ease) }, nowMs / 1000);
  }
}

function draw() {
  raf = 0;
  if (!enabled || !canvas || !ctx) return;
  const w = cssW || canvas.width;
  const h = cssH || canvas.height;
  if (w < 8 || h < 8) {
    raf = requestAnimationFrame(draw);
    return;
  }
  const now = (performance.now() - t0) / 1000;
  layoutCells();
  ctx.clearRect(0, 0, w, h);
  const g = ctx.createRadialGradient(w * 0.5, h * 0.2, 20, w * 0.5, h * 0.45, Math.max(w, h) * 0.8);
  g.addColorStop(0, "rgba(12, 48, 22, 0.4)");
  g.addColorStop(1, "rgba(1, 8, 4, 0.7)");
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, w, h);
  if (stageOn) {
    const well = Math.min(w, h) * 0.29;
    hexPath(w / 2, h / 2 + 6, well);
    ctx.fillStyle = "rgba(1, 8, 4, 0.22)";
    ctx.fill();
    ctx.strokeStyle = "rgba(52, 199, 89, 0.4)";
    ctx.lineWidth = 2.2;
    ctx.stroke();
  }
  if (crumbCells.length > 1 && !stageOn) {
    ctx.strokeStyle = "rgba(52,199,89,0.35)";
    ctx.lineWidth = 2;
    ctx.beginPath();
    crumbCells.forEach((c, i) => {
      if (i) ctx.lineTo(c.x, c.y);
      else ctx.moveTo(c.x, c.y);
    });
    ctx.stroke();
  }
  if (!stageOn) for (const c of crumbCells) drawHex(c, now);
  for (const c of cells) {
    const v = visCell(c);
    if (v.parked) {
      ctx.globalAlpha = 0.4;
      drawHex(v, now);
      ctx.globalAlpha = 1;
      continue;
    }
    if (!stageOn && menuId && c.id !== menuId) {
      ctx.globalAlpha = 0.22;
      drawHex(v, now);
      ctx.globalAlpha = 1;
      continue;
    }
    drawHex(v, now);
  }
  drawMenu(performance.now());
  if (!cells.length && !menuCells.length) {
    ctx.fillStyle = "rgba(245,245,245,0.7)";
    ctx.font = "600 14px ui-sans-serif, system-ui";
    ctx.textAlign = "center";
    ctx.fillText(alertsOnly ? "No alerts in this cell" : "Empty nest — go up", w / 2, h / 2);
  }
  paintHud();
  if (enabled) raf = requestAnimationFrame(draw);
}

function sizeCanvas() {
  if (!canvas || !canvas.parentElement || !ctx) return;
  const rect = canvas.parentElement.getBoundingClientRect();
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = Math.max(320, Math.floor(rect.width));
  const h = Math.max(240, Math.floor(rect.height));
  const pw = Math.max(320, Math.floor(w * dpr));
  const ph = Math.max(240, Math.floor(h * dpr));
  if (canvas.width !== pw) canvas.width = pw;
  if (canvas.height !== ph) canvas.height = ph;
  canvas.style.width = w + "px";
  canvas.style.height = h + "px";
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  cssW = w;
  cssH = h;
}

function paintHud() {
  if (!hud) return;
  const g = graphSnapshot();
  const path = crumb(g);
  const here = nodeOf(focusId, g);
  const crumbs = path
    .map((n, i) => {
      const here = i === path.length - 1;
      return `<button type="button" class="sf-nest-crumb hex${here ? " here" : ""}" data-nest-id="${escapeAttr(
        n.id
      )}" title="${escapeAttr(nodeLabel(n))}"><span>${escapeHtml(nodeLabel(n))}</span></button>`;
    })
    .join("<span class=\"sf-nest-sep\" aria-hidden=\"true\">▸</span>");
  const layers = LAYERS.map((l) => {
    const on = !hiddenLayers.has(l.id);
    return `<button type="button" class="sf-nest-layer${on ? " on" : ""}" data-nest-layer="${l.id}" style="--c:${l.color}">${l.label}</button>`;
  }).join("");
  const kidN = cells.length;
  const html = `
    <div class="sf-nest-bar">
      <div class="sf-nest-crumbs">${crumbs || "<span class=\"muted\">nest</span>"}</div>
      <div class="sf-nest-tools">
        <button type="button" class="sf-nest-up" data-nest-up ${path.length < 2 ? "disabled" : ""}>up</button>
        <button type="button" class="sf-nest-alerts${alertsOnly ? " on" : ""}" data-nest-alerts>alerts</button>
      </div>
    </div>
    <div class="sf-nest-layers">${layers}</div>
    <div class="sf-nest-hint">${
      liveLine(g)
        ? escapeHtml(liveLine(g))
        : stageOn
          ? "console sits in the well · hexes wrap it · click a hex to switch · close to return to the nest"
          : "overlay · tenants · edge · click a hex for a hex menu · in / see / open / logs · double-click to go down"
    } · ${kidN} here${
      here && here.subtitle ? " · " + escapeHtml(String(here.subtitle)) : ""
    }</div>`;
  if (html === lastHud) return;
  lastHud = html;
  hud.innerHTML = html;
}

function escapeHtml(s) {
  return String(s || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/"/g, "&quot;");
}

function escapeAttr(s) {
  return escapeHtml(s).replace(/'/g, "&#39;");
}

function drill(id) {
  const g = graphSnapshot();
  if (!id || !nodeOf(id, g)) return;
  closeMenu();
  bumpMove();
  const kids = visibleKids(id, g);
  if (!kids.length) {
    selectedId = id;
    openMenu(id);
    onSelect(id);
    return;
  }
  focusId = id;
  selectedId = id;
  onSelect(id);
}

function jumpTo(id) {
  const g = graphSnapshot();
  if (!id || !nodeOf(id, g)) return;
  closeMenu();
  bumpMove();
  focusId = id;
  selectedId = id;
  onSelect(id);
}

function goUp() {
  const g = graphSnapshot();
  const p = parentOf(focusId, g);
  if (p) jumpTo(p.id);
}

function onPointerMove(e) {
  const hit = hitTest(e.clientX, e.clientY);
  hoverId = hit.id || "";
  if (canvas) canvas.style.cursor = hoverId ? "pointer" : "default";
}

function onClick(e) {
  const up = e.target.closest("[data-nest-up]");
  if (up) {
    e.preventDefault();
    goUp();
    return;
  }
  const alerts = e.target.closest("[data-nest-alerts]");
  if (alerts) {
    e.preventDefault();
    alertsOnly = !alertsOnly;
    return;
  }
  const layerBtn = e.target.closest("[data-nest-layer]");
  if (layerBtn) {
    e.preventDefault();
    const id = layerBtn.getAttribute("data-nest-layer");
    if (hiddenLayers.has(id)) hiddenLayers.delete(id);
    else hiddenLayers.add(id);
    return;
  }
  const crumbBtn = e.target.closest("[data-nest-id]");
  if (crumbBtn) {
    e.preventDefault();
    jumpTo(crumbBtn.getAttribute("data-nest-id") || "");
    return;
  }
  const hit = hitTest(e.clientX, e.clientY);
  if (hit.menu) {
    e.preventDefault();
    runMenu(hit.action, menuId);
    return;
  }
  if (!hit.id) {
    closeMenu();
    return;
  }
  if (hit.crumb) {
    jumpTo(hit.id);
    return;
  }
  if (hit.id === selectedId && menuId === hit.id) {
    closeMenu();
    return;
  }
  selectedId = hit.id;
  openMenu(hit.id);
  onSelect(hit.id);
}

function onDblClick(e) {
  const hit = hitTest(e.clientX, e.clientY);
  if (!hit.id || hit.menu) return;
  if (hit.crumb) jumpTo(hit.id);
  else drill(hit.id);
}

function onKey(e) {
  if (!enabled) return;
  if (e.key === "Escape" || e.key === "Backspace") {
    if (stageOn) return;
    e.preventDefault();
    if (menuId) closeMenu();
    else goUp();
  } else if (e.key === "Enter" && selectedId) {
    e.preventDefault();
    drill(selectedId);
  }
}

function buildHud(flowEl) {
  hud = flowEl.querySelector(".sf-nest-hud");
  if (!hud) {
    hud = document.createElement("div");
    hud.className = "sf-nest-hud";
    flowEl.appendChild(hud);
  }
  hud.hidden = !enabled;
}

export function mountHoneycomb(flowEl, opts) {
  getGraph = (opts && opts.getGraph) || getGraph;
  onSelect = (opts && opts.onSelect) || onSelect;
  onAction = (opts && opts.onAction) || onAction;
  if (!flowEl) return;
  canvas = document.getElementById("dm-honeycomb");
  if (!canvas) {
    canvas = document.createElement("canvas");
    canvas.id = "dm-honeycomb";
    canvas.className = "sf-honeycomb";
    canvas.hidden = true;
    flowEl.appendChild(canvas);
  }
  ctx = canvas.getContext("2d");
  buildHud(flowEl);
  if (wired) unbind();
  on(canvas, "pointermove", onPointerMove);
  on(canvas, "click", onClick);
  on(canvas, "dblclick", onDblClick);
  on(hud, "click", onClick);
  on(window, "keydown", onKey);
  wired = true;
}

export function setHoneycombEnabled(on) {
  enabled = !!on;
  if (canvas) canvas.hidden = !enabled;
  if (hud) hud.hidden = !enabled;
  if (enabled) {
    const g = graphSnapshot();
    if (!nodeOf(focusId, g)) focusId = nodeOf("infra", g) ? "infra" : "env";
    sizeCanvas();
    if (!raf) raf = requestAnimationFrame(draw);
  } else {
    closeMenu();
    clearHoneycombStage();
    if (raf) {
      cancelAnimationFrame(raf);
      raf = 0;
    }
  }
}

export function isHoneycombEnabled() {
  return enabled;
}

export function resizeHoneycomb() {
  if (enabled) sizeCanvas();
}

export function honeycombReset() {
  focusId = "infra";
  alertsOnly = false;
  hiddenLayers = new Set();
  closeMenu();
  clearHoneycombStage();
}

export function stageHoneycomb(id) {
  const next = id || selectedId;
  if (!next) return;
  stageOn = true;
  stageId = next;
  selectedId = next;
  closeMenu();
  bumpMove();
}

export function clearHoneycombStage() {
  if (!stageOn && !stageId) return;
  stageOn = false;
  stageId = "";
  bumpMove();
}

export function isHoneycombStaged() {
  return stageOn;
}

export function destroyHoneycomb() {
  enabled = false;
  closeMenu();
  clearHoneycombStage();
  if (raf) cancelAnimationFrame(raf);
  raf = 0;
  cells = [];
  unbind();
  if (hud && hud.parentElement) hud.parentElement.removeChild(hud);
  hud = null;
  canvas = null;
  ctx = null;
}
