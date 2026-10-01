// 3D environment space: orbit camera, stacked layers (metal → overlay/VPC →
// k8s → pods → Nova VMs → tenants → edge/ingress), live link packets. Canvas only — no Three.js.
const LAYERS = [
  { id: "metal", label: "Metal", kinds: ["machine", "group", "env"], y: 0, color: "#6ee7b7", spread: 108 },
  { id: "overlay", label: "Overlay / VPC", kinds: ["overlay", "net", "subnet", "router", "vpc"], y: 140, color: "#7dd3fc", spread: 88 },
  { id: "k8s", label: "Kubernetes", kinds: ["k8s", "ns", "svc", "registry", "regcache"], y: 290, color: "#a5b4fc", spread: 64 },
  { id: "pods", label: "Pods", kinds: ["pod"], y: 440, color: "#f9a8d4", spread: 48 },
  { id: "nova", label: "Nova VMs", kinds: ["vm"], y: 590, color: "#fcd34d", spread: 44 },
  { id: "tenants", label: "Tenants", kinds: ["tenant"], y: 750, color: "#fda4af", spread: 96 },
  { id: "edge", label: "Edge / Ingress", kinds: ["edge", "ingress", "fip", "lb", "gw", "route"], y: 910, color: "#f0abfc", spread: 80 },
];

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

const KIND_R = {
  env: 16,
  group: 13,
  machine: 11,
  overlay: 12,
  net: 9,
  subnet: 7,
  router: 9,
  vpc: 11,
  registry: 12,
  regcache: 7,
  k8s: 9,
  ns: 8,
  svc: 6,
  pod: 6,
  vm: 7,
  tenant: 12,
  edge: 12,
  ingress: 8,
  fip: 6,
  lb: 8,
  gw: 10,
  route: 8,
};

const SHAPES = [
  { id: "stack", label: "stack" },
  { id: "towers", label: "towers" },
  { id: "rings", label: "rings" },
  { id: "sphere", label: "sphere" },
];
const SPACE_MIN = 1;
const SPACE_MAX = 2.8;
let shape = "stack";
let space = 1.8;
let hidden3d = new Set();

let canvas = null;
let ctx = null;
let hud = null;
let raf = 0;
let enabled = false;
let wired = false;
let cssW = 0;
let cssH = 0;
// Negative pitch looks down the +Y axis (bird's eye). Positive is a side / worm's-eye tilt.
const PITCH_MIN = -1.55;
const PITCH_MAX = 1.55;
const PITCH_TOP = -1.52;
const DIST_MIN = 120;
const DIST_MAX = 9800;
const IDLE_SPIN_MS = 16000;

let cam = { yaw: 0.72, pitch: 0.62, dist: 1680, tx: 0, ty: 320, tz: 0 };
let camFitted = false;
let dragging = false;
let panDrag = false;
let ptrStart = { x: 0, y: 0 };
let lastPtr = { x: 0, y: 0 };
let moved = 0;
let getGraph = () => ({ nodes: [], edges: [] });
let onSelect = () => {};
let packets = [];
let lastAmbient = 0;
let t0 = performance.now();
let hoverId = "";
let idleAt = 0;
let layoutSig = "";
let worldCache = [];
let extraTenants = [];
const listeners = [];

function on(el, type, fn, opts) {
  el.addEventListener(type, fn, opts);
  listeners.push([el, type, fn, opts]);
}

function unbind() {
  for (const [el, type, fn, opts] of listeners) {
    try {
      el.removeEventListener(type, fn, opts);
    } catch (_) {
      /* ignore */
    }
  }
  listeners.length = 0;
  wired = false;
}

function layerOf(kind) {
  return KIND_LAYER.get(kind) || null;
}

function fallbackLayer() {
  return KIND_LAYER.get("k8s") || LAYERS[2] || LAYERS[0];
}

function tenantKey(n) {
  if (!n) return "";
  const v = n.tenant || n.project_id || n.project_name || "";
  return v == null ? "" : String(v).trim();
}

function hasPos(n) {
  return n && Number.isFinite(n.wx) && Number.isFinite(n.wy) && Number.isFinite(n.wz);
}

function hash32(s) {
  let h = 2166136261;
  const str = String(s);
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

function jitter(id, mag) {
  const h = hash32(id);
  return {
    x: ((h & 0xffff) / 0xffff - 0.5) * mag,
    z: (((h >>> 16) & 0xffff) / 0xffff - 0.5) * mag,
  };
}

function nodeLabel(n) {
  return String((n && (n.title || n.label || n.name || n.id)) || "");
}

function graphSnapshot() {
  try {
    return getGraph() || { nodes: [], edges: [] };
  } catch (_) {
    return { nodes: [], edges: [] };
  }
}

function liveState(n) {
  if (!n) return "wait";
  if (n.hot && n.state !== "bad" && n.state !== "warn") return "run";
  return n.state || "wait";
}

function edgeEnds(e) {
  return { s: e.source || e.from, t: e.target || e.to };
}

function synthesizeTenants(nodes) {
  if ((nodes || []).some((n) => n && n.kind === "tenant")) return [];
  const tenants = new Map();
  for (const n of nodes || []) {
    if (!n || n.kind !== "vm") continue;
    const key = tenantKey(n);
    if (!key) continue;
    const id = "tenant:" + key;
    if (tenants.has(id)) continue;
    tenants.set(id, {
      id,
      kind: "tenant",
      label: key,
      name: key,
      title: key,
      state: "ok",
    });
  }
  return [...tenants.values()];
}

function fanAround(parent, i, n, spacing) {
  const j = jitter(parent.id + ":" + i, spacing * 0.12);
  if (n <= 1) return { x: parent.wx + j.x, z: parent.wz + j.z };
  const cols = Math.ceil(Math.sqrt(n));
  const col = i % cols;
  const row = Math.floor(i / cols);
  return {
    x: parent.wx + (col - (cols - 1) / 2) * spacing + j.x,
    z: parent.wz + (row - (Math.ceil(n / cols) - 1) / 2) * spacing + j.z,
  };
}

function scaledLayers() {
  return LAYERS.map((l, i) => {
    let y = l.y * space;
    if (shape === "rings") y = l.y * space * 0.42;
    if (shape === "sphere") y = (i - (LAYERS.length - 1) / 2) * 88 * space;
    if (shape === "towers" && l.id !== "metal") y = l.y * space * 1.15;
    return { ...l, y, spread: (l.spread || 90) * space };
  });
}

function gridPlace(list, layer, placed) {
  const n = list.length;
  const cols = Math.max(1, Math.ceil(Math.sqrt(n)));
  const rowCount = Math.max(1, Math.ceil(n / cols));
  const spacing = layer.spread || 90;
  list.forEach((node, i) => {
    const col = i % cols;
    const row = Math.floor(i / cols);
    const j = jitter(node.id, spacing * 0.1);
    placed.set(node.id, {
      ...node,
      wx: (col - (cols - 1) / 2) * spacing + j.x,
      wy: layer.y,
      wz: (row - (rowCount - 1) / 2) * spacing + j.z,
      layer,
    });
  });
}

function placeGroup(kids, layer, placed, parentOf) {
  const groups = new Map();
  for (const n of kids) {
    const pid = parentOf.get(n.id) || "";
    if (!groups.has(pid)) groups.set(pid, []);
    groups.get(pid).push(n);
  }
  for (const [pid, list] of groups) {
    list.sort((a, b) => String(a.id).localeCompare(String(b.id)));
    const parent = pid ? placed.get(pid) : null;
    if (parent && hasPos(parent)) {
      const ring = layer.id === "k8s" && list[0] && list[0].kind === "ns";
      list.forEach((node, i) => {
        let pos;
        if (ring && list.length > 1) {
          const rad = Math.max(46, 28 + list.length * 6);
          const ang = (i / list.length) * Math.PI * 2 - Math.PI / 2;
          const j = jitter(node.id, 8);
          pos = {
            x: parent.wx + Math.cos(ang) * rad + j.x,
            z: parent.wz + Math.sin(ang) * rad + j.z,
          };
        } else {
          pos = fanAround(parent, i, list.length, layer.spread || 56);
        }
        placed.set(node.id, { ...node, wx: pos.x, wy: layer.y, wz: pos.z, layer });
      });
    } else {
      gridPlace(list, layer, placed);
    }
  }
}

function layoutStack(all, graph, parentOf, layers, byLayer) {
  const placed = new Map();
  const metal = byLayer.get("metal") || [];
  const machines = metal.filter((n) => n.kind === "machine").sort((a, b) => String(a.id).localeCompare(String(b.id)));
  const groups = metal.filter((n) => n.kind === "group");
  const envs = metal.filter((n) => n.kind === "env");
  gridPlace(machines, layers[0], placed);
  let cx = 0;
  let cz = 0;
  if (machines.length) {
    for (const m of machines) {
      const p = placed.get(m.id);
      if (!p) continue;
      cx += p.wx;
      cz += p.wz;
    }
    cx /= machines.length;
    cz /= machines.length;
  }
  const park = (list, zOff) => {
    list.forEach((n, i) => {
      const j = jitter(n.id, 10 * space);
      placed.set(n.id, {
        ...n,
        wx: cx + j.x,
        wy: layers[0].y,
        wz: cz - zOff - i * 36 * space + j.z,
        layer: layers[0],
      });
    });
  };
  if (machines.length || groups.length || envs.length) {
    park(groups, machines.length ? 110 * space : 0);
    park(envs, machines.length ? 210 * space : groups.length ? 110 * space : 0);
  } else {
    placeGroup(metal, layers[0], placed, parentOf);
  }
  const placeKinds = (layerId, kinds) => {
    const layer = layers.find((l) => l.id === layerId);
    if (!layer) return;
    const list = byLayer.get(layerId) || [];
    if (kinds) {
      for (const kind of kinds) {
        const kids = list.filter((n) => n.kind === kind && !placed.has(n.id));
        if (kids.length) placeGroup(kids, layer, placed, parentOf);
      }
    }
    const rest = list.filter((n) => !placed.has(n.id));
    if (rest.length) placeGroup(rest, layer, placed, parentOf);
  };
  placeKinds("overlay", ["overlay", "vpc", "net", "subnet", "router"]);
  placeKinds("k8s", ["k8s", "ns", "svc", "registry", "regcache"]);
  placeKinds("pods");
  placeKinds("nova");
  const tenantLayer = layers.find((l) => l.id === "tenants") || layers[5];
  placeTenants(all, byLayer.get("tenants") || [], tenantLayer, placed, parentOf);
  placeKinds("edge", ["edge", "gw", "route", "ingress", "lb", "fip"]);
  const leftover = all.filter((n) => !placed.has(n.id));
  if (leftover.length) {
    const fb = layers.find((l) => l.id === "k8s") || layers[2] || layers[0];
    placeGroup(leftover, fb, placed, parentOf);
  }
  return placed;
}

function layoutTowers(all, parentOf, layers, byLayer) {
  const placed = new Map();
  const machines = (byLayer.get("metal") || [])
    .filter((n) => n.kind === "machine")
    .sort((a, b) => String(a.id).localeCompare(String(b.id)));
  const gap = 150 * space;
  const cols = Math.max(1, Math.ceil(Math.sqrt(Math.max(1, machines.length))));
  machines.forEach((m, i) => {
    const col = i % cols;
    const row = Math.floor(i / cols);
    placed.set(m.id, {
      ...m,
      wx: (col - (cols - 1) / 2) * gap,
      wy: layers[0].y,
      wz: (row - (Math.ceil(machines.length / cols) - 1) / 2) * gap,
      layer: layers[0],
    });
  });
  const rest = all.filter((n) => n.kind !== "machine");
  rest.forEach((n) => {
    let root = n.id;
    for (let k = 0; k < 10; k++) {
      const node = all.find((x) => x.id === root);
      if (node && node.kind === "machine") break;
      const p = parentOf.get(root);
      if (!p) break;
      root = p;
    }
    const host = placed.get(root) || placed.get(machines[0] && machines[0].id);
    const mapped = layerOf(n.kind) || fallbackLayer();
    const layer = layers[LAYERS.findIndex((l) => l.id === mapped.id)] || layers[0];
    const j = jitter(n.id, 28 * space);
    const ang = (hash32(n.id) / 0xffffffff) * Math.PI * 2;
    const rad = 22 * space + (hash32(n.id + "r") % 18);
    placed.set(n.id, {
      ...n,
      wx: (host ? host.wx : 0) + Math.cos(ang) * rad + j.x * 0.2,
      wy: layer.y,
      wz: (host ? host.wz : 0) + Math.sin(ang) * rad + j.z * 0.2,
      layer,
    });
  });
  return placed;
}

function layoutRings(all, layers, byLayer) {
  const placed = new Map();
  layers.forEach((layer, li) => {
    const list = (byLayer.get(layer.id) || []).slice().sort((a, b) => String(a.id).localeCompare(String(b.id)));
    const rad = (li + 1) * 95 * space;
    list.forEach((n, i) => {
      const ang = list.length ? (i / list.length) * Math.PI * 2 - Math.PI / 2 : 0;
      placed.set(n.id, {
        ...n,
        wx: Math.cos(ang) * rad,
        wy: layer.y,
        wz: Math.sin(ang) * rad,
        layer,
      });
    });
  });
  return placed;
}

function layoutSphere(all, layers, byLayer) {
  const placed = new Map();
  const R = 220 * space;
  layers.forEach((layer, li) => {
    const list = (byLayer.get(layer.id) || []).slice().sort((a, b) => String(a.id).localeCompare(String(b.id)));
    const phi = ((li + 0.5) / layers.length) * Math.PI;
    const ringR = Math.max(24, Math.sin(phi) * R);
    list.forEach((n, i) => {
      const theta = list.length ? (i / list.length) * Math.PI * 2 : 0;
      placed.set(n.id, {
        ...n,
        wx: Math.cos(theta) * ringR,
        wy: Math.cos(phi) * R,
        wz: Math.sin(theta) * ringR,
        layer,
      });
    });
  });
  return placed;
}

function placeTenants(all, tenantNodes, layer, placed, parentOf) {
  const vmsByTenant = new Map();
  for (const n of all) {
    if (n.kind !== "vm") continue;
    const key = tenantKey(n);
    if (!key) continue;
    if (!vmsByTenant.has(key)) vmsByTenant.set(key, []);
    vmsByTenant.get(key).push(n.id);
  }
  const unmatched = [];
  for (const t of tenantNodes) {
    const key = tenantKey(t) || String(t.name || t.label || t.title || "").replace(/^tenant:/, "");
    const members = vmsByTenant.get(key) || vmsByTenant.get(String(t.id || "").replace(/^tenant:/, "")) || [];
    let sx = 0;
    let sz = 0;
    let c = 0;
    for (const id of members) {
      const vm = placed.get(id);
      if (!vm || !hasPos(vm)) continue;
      sx += vm.wx;
      sz += vm.wz;
      c += 1;
    }
    if (c) {
      const j = jitter(t.id, 18 * space);
      placed.set(t.id, { ...t, wx: sx / c + j.x, wy: layer.y, wz: sz / c + j.z, layer });
    } else unmatched.push(t);
  }
  if (unmatched.length) placeGroup(unmatched, layer, placed, parentOf);
}

function layoutWorld(graph) {
  const nodes = graph.nodes || [];
  extraTenants = synthesizeTenants(nodes);
  const layers = scaledLayers();
  const all = [];
  for (const n of nodes.concat(extraTenants)) {
    if (!n || n.hidden || n.id == null) continue;
    const layer = layerOf(n.kind);
    if (layer && hidden3d.has(layer.id)) continue;
    all.push(n);
  }
  const sig =
    shape +
    "|" +
    space.toFixed(2) +
    "|" +
    [...hidden3d].sort().join(",") +
    "|" +
    all
      .map((n) => n.id)
      .filter(Boolean)
      .sort()
      .join("\0") +
    "#" +
    (graph.edges || [])
      .map((e) => {
        const a = e.source || e.from || "";
        const b = e.target || e.to || "";
        return a < b ? `${a}>${b}` : `${b}>${a}`;
      })
      .sort()
      .join("\0");
  if (sig === layoutSig && worldCache.length) {
    const live = new Map(all.map((n) => [n.id, n]));
    const next = [];
    const seen = new Set();
    for (const w of worldCache) {
      const src = live.get(w.id);
      if (!src) continue;
      w.state = liveState(src);
      w.hot = src.hot;
      w.title = src.title;
      w.subtitle = src.subtitle;
      w.label = src.label;
      w.name = src.name;
      w.tenant = src.tenant;
      w.project_id = src.project_id;
      seen.add(w.id);
      next.push(w);
    }
    worldCache = next;
    return worldCache;
  }

  const parentOf = new Map();
  for (const e of graph.edges || []) {
    const { s, t } = edgeEnds(e);
    if (s && t && !parentOf.has(t)) parentOf.set(t, s);
  }

  const byLayer = new Map();
  for (const layer of layers) byLayer.set(layer.id, []);
  for (const n of all) {
    const layer = layerOf(n.kind) || fallbackLayer();
    const scaled = layers.find((l) => l.id === layer.id) || layers[0];
    byLayer.get(scaled.id).push(n);
    n._layer = scaled;
  }

  let placed;
  if (shape === "towers") placed = layoutTowers(all, parentOf, layers, byLayer);
  else if (shape === "rings") placed = layoutRings(all, layers, byLayer);
  else if (shape === "sphere") placed = layoutSphere(all, layers, byLayer);
  else placed = layoutStack(all, graph, parentOf, layers, byLayer);

  worldCache = [];
  for (const n of all) {
    const w = placed.get(n.id);
    if (w && hasPos(w)) {
      w.state = liveState(n);
      w.hot = n.hot;
      worldCache.push(w);
    }
  }
  layoutSig = sig;
  return worldCache;
}

function project(x, y, z, w, h) {
  if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(z)) return null;
  const px = x - cam.tx;
  const py = y - cam.ty;
  const pz = z - cam.tz;
  const cy = Math.cos(cam.yaw);
  const sy = Math.sin(cam.yaw);
  const cp = Math.cos(cam.pitch);
  const sp = Math.sin(cam.pitch);
  const rx = px * cy - pz * sy;
  const rz = px * sy + pz * cy;
  const ry = py * cp - rz * sp;
  const raw = rz * cp + py * sp + cam.dist;
  const depth = Math.max(12, raw);
  const f = 720 / depth;
  return {
    x: w / 2 + rx * f,
    y: h / 2 - ry * f,
    s: f,
    d: raw,
    behind: raw < 8,
  };
}

function fogA(d) {
  const near = cam.dist * 0.28;
  const far = cam.dist * 2.35;
  return Math.max(0.06, Math.min(1, 1 - (d - near) / Math.max(80, far - near)));
}

function hexA(hex, a) {
  const n = String(hex || "#64748b").replace("#", "");
  const r = parseInt(n.slice(0, 2), 16) || 0;
  const g = parseInt(n.slice(2, 4), 16) || 0;
  const b = parseInt(n.slice(4, 6), 16) || 0;
  return `rgba(${r},${g},${b},${a})`;
}

function stackBounds(nodes) {
  let minX = Infinity;
  let maxX = -Infinity;
  let minZ = Infinity;
  let maxZ = -Infinity;
  for (const n of nodes) {
    if (!hasPos(n)) continue;
    minX = Math.min(minX, n.wx);
    maxX = Math.max(maxX, n.wx);
    minZ = Math.min(minZ, n.wz);
    maxZ = Math.max(maxZ, n.wz);
  }
  if (!Number.isFinite(minX)) {
    minX = -380;
    maxX = 380;
    minZ = -220;
    maxZ = 220;
  }
  const pad = 90;
  return {
    minX: minX - pad,
    maxX: maxX + pad,
    minZ: minZ - pad,
    maxZ: maxZ + pad,
  };
}

function drawPoly(points, fill, stroke, width) {
  if (!points.length || points.every((p) => !p)) return false;
  ctx.beginPath();
  let started = false;
  for (const p of points) {
    if (!p) continue;
    if (!started) {
      ctx.moveTo(p.x, p.y);
      started = true;
    } else ctx.lineTo(p.x, p.y);
  }
  if (!started) return false;
  ctx.closePath();
  if (fill) {
    ctx.fillStyle = fill;
    ctx.fill();
  }
  if (stroke) {
    ctx.strokeStyle = stroke;
    ctx.lineWidth = width || 1;
    ctx.stroke();
  }
  return true;
}

function drawGround(bounds, w, h) {
  const y = -36;
  const pad = 140;
  const minX = bounds.minX - pad;
  const maxX = bounds.maxX + pad;
  const minZ = bounds.minZ - pad;
  const maxZ = bounds.maxZ + pad;
  const corners = [
    [minX, y, minZ],
    [maxX, y, minZ],
    [maxX, y, maxZ],
    [minX, y, maxZ],
  ].map((p) => project(p[0], p[1], p[2], w, h));
  drawPoly(corners, "rgba(18, 6, 6, 0.72)", "rgba(235, 0, 0, 0.22)", 1.2);

  const step = 56;
  ctx.lineWidth = 1;
  for (let x = Math.ceil(minX / step) * step; x <= maxX; x += step) {
    const a = project(x, y, minZ, w, h);
    const b = project(x, y, maxZ, w, h);
    if (!a || !b || a.behind || b.behind) continue;
    ctx.strokeStyle = `rgba(235,0,0,${0.05 + 0.07 * fogA((a.d + b.d) / 2)})`;
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.stroke();
  }
  for (let z = Math.ceil(minZ / step) * step; z <= maxZ; z += step) {
    const a = project(minX, y, z, w, h);
    const b = project(maxX, y, z, w, h);
    if (!a || !b || a.behind || b.behind) continue;
    ctx.strokeStyle = `rgba(235,0,0,${0.05 + 0.07 * fogA((a.d + b.d) / 2)})`;
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.stroke();
  }
}

function drawPlane(layer, bounds, w, h) {
  const corners = [
    [bounds.minX, layer.y, bounds.minZ],
    [bounds.maxX, layer.y, bounds.minZ],
    [bounds.maxX, layer.y, bounds.maxZ],
    [bounds.minX, layer.y, bounds.maxZ],
  ].map((p) => project(p[0], p[1], p[2], w, h));
  const mid = project((bounds.minX + bounds.maxX) / 2, layer.y, (bounds.minZ + bounds.maxZ) / 2, w, h);
  const a = mid ? fogA(mid.d) : 0.4;
  drawPoly(corners, hexA(layer.color, 0.055 + 0.04 * a), hexA(layer.color, 0.28 + 0.2 * a), 1.15);
  const lab = project(bounds.minX + 12, layer.y + 22, bounds.minZ + 10, w, h);
  if (!lab || lab.behind) return { layer, d: mid ? mid.d : 1e9, lab: null };
  return { layer, d: mid ? mid.d : 1e9, lab };
}

function drawRisers(bounds, w, h) {
  const sl = scaledLayers();
  const y0 = sl[0].y;
  const y1 = sl[sl.length - 1].y;
  const corners = [
    [bounds.minX, bounds.minZ],
    [bounds.maxX, bounds.minZ],
    [bounds.maxX, bounds.maxZ],
    [bounds.minX, bounds.maxZ],
  ];
  ctx.setLineDash([6, 7]);
  ctx.lineWidth = 1;
  for (const [x, z] of corners) {
    const a = project(x, y0, z, w, h);
    const b = project(x, y1, z, w, h);
    if (!a || !b || a.behind || b.behind) continue;
    ctx.strokeStyle = `rgba(235,0,0,${0.12 + 0.18 * fogA((a.d + b.d) / 2)})`;
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.stroke();
  }
  ctx.setLineDash([]);
}

function markPath(x, y, r, kind) {
  ctx.beginPath();
  if (kind === "machine" || kind === "group" || kind === "env") {
    const s = r * 0.92;
    if (ctx.roundRect) ctx.roundRect(x - s, y - s, s * 2, s * 2, 3);
    else ctx.rect(x - s, y - s, s * 2, s * 2);
  } else if (kind === "tenant" || kind === "overlay" || kind === "vpc" || kind === "edge" || kind === "gw") {
    for (let i = 0; i < 6; i++) {
      const a = (Math.PI / 3) * i - Math.PI / 6;
      const px = x + Math.cos(a) * r;
      const py = y + Math.sin(a) * r;
      if (i) ctx.lineTo(px, py);
      else ctx.moveTo(px, py);
    }
    ctx.closePath();
  } else if (kind === "vm") {
    if (ctx.roundRect) ctx.roundRect(x - r, y - r * 0.72, r * 2, r * 1.44, 2);
    else ctx.rect(x - r, y - r * 0.72, r * 2, r * 1.44);
  } else if (kind === "net" || kind === "router" || kind === "lb") {
    const s = r * 0.92;
    if (ctx.roundRect) ctx.roundRect(x - s * 1.15, y - s * 0.72, s * 2.3, s * 1.44, 3);
    else ctx.rect(x - s * 1.15, y - s * 0.72, s * 2.3, s * 1.44);
  } else if (kind === "registry" || kind === "regcache" || kind === "ingress" || kind === "fip" || kind === "route") {
    ctx.moveTo(x, y - r);
    ctx.lineTo(x + r, y);
    ctx.lineTo(x, y + r);
    ctx.lineTo(x - r, y);
    ctx.closePath();
  } else {
    ctx.arc(x, y, r, 0, Math.PI * 2);
  }
}

function tenantIdFor(vm, byId, byName) {
  const key = tenantKey(vm);
  if (!key) return "";
  if (byId.has(key) && byId.get(key).kind === "tenant") return key;
  const prefixed = "tenant:" + key;
  if (byId.has(prefixed)) return prefixed;
  return byName.get(key) || "";
}

function collectEdges(graph, nodes, byId) {
  const out = [];
  const seen = new Set();
  const add = (a, b, state) => {
    if (!a || !b || a.id === b.id || !hasPos(a) || !hasPos(b)) return;
    const key = a.id < b.id ? a.id + ">" + b.id : b.id + ">" + a.id;
    if (seen.has(key)) return;
    seen.add(key);
    out.push({ a, b, state: state || liveState(b) || liveState(a) || "wait" });
  };
  for (const e of graph.edges || []) {
    const { s, t } = edgeEnds(e);
    add(byId.get(s), byId.get(t), e.state);
  }
  const machines = [];
  const k8sByHost = new Map();
  let registry = null;
  let env = null;
  let infra = null;
  for (const n of nodes) {
    if (n.kind === "machine") machines.push(n);
    else if (n.kind === "k8s") k8sByHost.set(n.machineName || "", n);
    else if (n.kind === "registry") registry = n;
    else if (n.kind === "env") env = n;
    else if (n.kind === "group" && n.id === "infra") infra = n;
  }
  if (env && registry) add(env, registry, registry.state);
  if (env && infra) add(env, infra, infra.state);
  if (infra) {
    for (const m of machines) add(infra, m, m.state);
  }
  if (registry) {
    for (const m of machines) add(registry, m, m.state === "run" ? "run" : "ok");
  }
  for (const m of machines) {
    const k = k8sByHost.get(m.machineName) || byId.get(m.id + ":k8s");
    if (k) add(m, k, k.state || m.state);
  }
  const byName = new Map();
  for (const n of nodes) {
    if (n.kind !== "tenant") continue;
    const key = tenantKey(n) || String(n.name || n.label || n.title || n.id).replace(/^tenant:/, "");
    if (key) byName.set(key, n.id);
  }
  const vms = [];
  const fips = [];
  for (const n of nodes) {
    if (n.kind === "vm") {
      vms.push(n);
      const tid = tenantIdFor(n, byId, byName);
      if (tid) add(n, byId.get(tid), "ok");
    } else if (n.kind === "fip") fips.push(n);
  }
  const overlay = byId.get("overlay");
  if (overlay && overlay.id === "overlay") {
    for (const n of nodes) {
      if (n.kind === "net" || n.kind === "vpc") add(overlay, n, n.state);
    }
  }
  const edge = byId.get("edge");
  if (edge && edge.id === "edge") {
    for (const n of nodes) {
      if (n.kind === "ingress" || n.kind === "fip" || n.kind === "lb" || n.kind === "gw" || n.kind === "route") add(edge, n, n.state);
    }
  }
  if (fips.length && vms.length) {
    const vmByKey = new Map();
    for (const vm of vms) {
      vmByKey.set(String(vm.id), vm);
      if (vm.vmId != null) vmByKey.set(String(vm.vmId), vm);
    }
    for (const fip of fips) {
      const vid = fip.vmId != null ? fip.vmId : fip.vm_id;
      let vm = vid != null ? vmByKey.get(String(vid)) || byId.get(String(vid)) || byId.get("vm:" + vid) : null;
      if (!vm && fip.fixed_ip) {
        const ip = String(fip.fixed_ip);
        for (const v of vms) {
          if (v.fixed_ip === ip || v.addr === ip) {
            vm = v;
            break;
          }
          const addrs = v.addresses;
          if (typeof addrs === "string" && addrs.includes(ip)) {
            vm = v;
            break;
          }
        }
      }
      if (vm) add(vm, fip, fip.state || vm.state);
    }
  }
  return out;
}

function edgeColor(e) {
  const layer = (e.b && e.b.layer) || layerOf(e.b && e.b.kind) || layerOf(e.a && e.a.kind);
  if (e.state === "bad") return STATE_COL.bad;
  if (e.state === "run") return STATE_COL.run;
  if (layer && layer.color) return layer.color;
  return STATE_COL[e.state] || STATE_COL.ok;
}

function drawEdge(e, w, h) {
  const steps = 16;
  const lift = 22 + Math.min(80, Math.hypot(e.b.wx - e.a.wx, e.b.wz - e.a.wz) * 0.1);
  const pts = [];
  let depth = 0;
  for (let i = 0; i <= steps; i++) {
    const u = i / steps;
    const omu = 1 - u;
    const x = omu * e.a.wx + u * e.b.wx;
    const z = omu * e.a.wz + u * e.b.wz;
    const y = omu * e.a.wy + u * e.b.wy + lift * 4 * u * omu;
    const p = project(x, y, z, w, h);
    if (!p || p.behind) {
      if (pts.length) {
        strokeEdge(pts, depth / pts.length, e);
        pts.length = 0;
        depth = 0;
      }
      continue;
    }
    depth += p.d;
    pts.push(p);
  }
  if (pts.length > 1) strokeEdge(pts, depth / pts.length, e);
}

function strokeEdge(pts, depth, e) {
  const a = Math.max(0.45, Math.min(0.95, fogA(depth) * 1.25));
  const col = edgeColor(e);
  ctx.save();
  ctx.beginPath();
  ctx.moveTo(pts[0].x, pts[0].y);
  for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i].x, pts[i].y);
  ctx.strokeStyle = hexA(col, a);
  ctx.lineWidth = e.state === "run" ? 2.6 : 2.1;
  ctx.shadowColor = hexA(col, 0.45);
  ctx.shadowBlur = e.state === "run" ? 12 : 7;
  ctx.stroke();
  ctx.restore();
}

function seedAmbient(edges, now) {
  if (!edges.length || packets.length >= 40) return;
  if (now - lastAmbient < 0.22) return;
  lastAmbient = now;
  const e = edges[Math.floor(now * 7) % edges.length];
  if (!e || !e.a || !e.b) return;
  packets.push({
    from: e.a.id,
    to: e.b.id,
    kind: e.state === "run" ? "pxe" : "net",
    born: now,
    life: 1.5 + (hash32(String(e.a.id) + String(e.b.id)) % 8) / 10,
  });
}

function packetColor(kind) {
  if (kind === "iso") return "#fbbf24";
  if (kind === "pxe") return "#7dd3fc";
  if (kind === "boot") return "#34d399";
  return "#86efac";
}

function resolveId(id, byId, hint) {
  if (!id) return "";
  if (byId.has(id)) return id;
  if (byId.has("m:" + id)) return "m:" + id;
  if (id === "k8s" || id === "kubernetes") {
    if (hint && byId.has(hint + ":k8s")) return hint + ":k8s";
    if (hint && byId.has("m:" + hint + ":k8s")) return "m:" + hint + ":k8s";
    for (const n of byId.values()) {
      if (n.kind === "k8s" && (!hint || n.id.startsWith(hint) || n.machineName === hint)) return n.id;
    }
  }
  if (id === "registry" || id === "pxe") {
    if (byId.has("registry")) return "registry";
    for (const n of byId.values()) if (n.kind === "registry") return n.id;
  }
  return id;
}

function drawPackets(byId, now, w, h) {
  packets = packets.filter((p) => now - p.born < p.life);
  for (const p of packets) {
    const a = byId.get(resolveId(p.from, byId, p.to));
    const b = byId.get(resolveId(p.to, byId, p.from));
    if (!a || !b || !hasPos(a) || !hasPos(b)) continue;
    const raw = Math.min(1, Math.max(0, (now - p.born) / p.life));
    const u = raw * raw * (3 - 2 * raw);
    const lift = 18 + Math.min(60, Math.hypot(b.wx - a.wx, b.wz - a.wz) * 0.08);
    const x = a.wx + (b.wx - a.wx) * u;
    const y = a.wy + (b.wy - a.wy) * u + lift * 4 * u * (1 - u);
    const z = a.wz + (b.wz - a.wz) * u;
    const q = project(x, y, z, w, h);
    if (!q || q.behind) continue;
    const col = packetColor(p.kind);
    const r = Math.max(2.2, 3.4 * q.s);
    ctx.save();
    ctx.globalAlpha = fogA(q.d);
    ctx.fillStyle = col;
    ctx.shadowColor = col;
    ctx.shadowBlur = 10;
    ctx.beginPath();
    ctx.arc(q.x, q.y, r, 0, Math.PI * 2);
    ctx.fill();
    ctx.shadowBlur = 0;
    ctx.globalAlpha = fogA(q.d) * 0.35;
    for (let i = 1; i <= 3; i++) {
      const tu = Math.max(0, u - i * 0.045);
      const tx = a.wx + (b.wx - a.wx) * tu;
      const ty = a.wy + (b.wy - a.wy) * tu + lift * 4 * tu * (1 - tu);
      const tz = a.wz + (b.wz - a.wz) * tu;
      const tq = project(tx, ty, tz, w, h);
      if (!tq || tq.behind) continue;
      ctx.beginPath();
      ctx.arc(tq.x, tq.y, r * (1 - i * 0.18), 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.restore();
  }
}

function drawNodes(nodes, now, w, h) {
  const drawn = [];
  for (const n of nodes) {
    if (!hasPos(n)) continue;
    const p = project(n.wx, n.wy, n.wz, w, h);
    if (!p || p.behind) continue;
    drawn.push({ n, p });
  }
  drawn.sort((a, b) => b.p.d - a.p.d);
  let hover = null;
  for (const { n, p } of drawn) {
    const st = liveState(n);
    const pulse = st === "run" ? 1 + 0.2 * Math.sin(now * 4.2) : 1;
    const base = KIND_R[n.kind] || 7;
    const r = Math.max(3.5, base * p.s * 0.95 * pulse);
    const col = STATE_COL[st] || STATE_COL.wait;
    const a = (n.id === hoverId ? 1 : 0.92) * fogA(p.d);
    ctx.save();
    ctx.globalAlpha = a;
    if (st === "run") {
      ctx.shadowColor = col;
      ctx.shadowBlur = 14 * pulse;
    } else if (n.id === hoverId) {
      ctx.shadowColor = "#fff";
      ctx.shadowBlur = 10;
    }
    markPath(p.x, p.y, r, n.kind);
    ctx.fillStyle = col;
    ctx.fill();
    ctx.shadowBlur = 0;
    ctx.strokeStyle = n.id === hoverId ? "#fff" : "rgba(255,255,255,0.32)";
    ctx.lineWidth = n.id === hoverId ? 2 : 1;
    ctx.stroke();
    if (st === "run") {
      ctx.globalAlpha = a * 0.35;
      markPath(p.x, p.y, r * (1.35 + 0.12 * Math.sin(now * 4.2)), n.kind);
      ctx.strokeStyle = col;
      ctx.lineWidth = 1;
      ctx.stroke();
    }
    ctx.restore();
    if (n.id === hoverId) hover = { n, p, r };
    else {
      const important = n.kind === "machine" || n.kind === "env" || n.kind === "group" || n.kind === "k8s" || n.kind === "ns";
      const show = important ? p.s > 0.9 && a > 0.4 : p.s > 1.35 && a > 0.5;
      if (show) drawLabelPill(nodeLabel(n).slice(0, 20), p.x + r + 6, p.y, a);
    }
  }
  if (hover) {
    const text = nodeLabel(hover.n);
    const kind = hover.n.kind || "";
    const layer = hover.n.layer ? hover.n.layer.label : "";
    const line = [text, kind, layer].filter(Boolean).join(" · ");
    ctx.font = "600 12px ui-sans-serif, system-ui";
    const tw = ctx.measureText(line).width;
    const x = hover.p.x + hover.r + 8;
    const y = hover.p.y - 10;
    ctx.fillStyle = "rgba(16, 6, 6, 0.88)";
    ctx.strokeStyle = "rgba(235, 0, 0, 0.45)";
    ctx.lineWidth = 1;
    if (ctx.roundRect) {
      ctx.beginPath();
      ctx.roundRect(x - 6, y - 12, tw + 12, 20, 4);
      ctx.fill();
      ctx.stroke();
    } else {
      ctx.fillRect(x - 6, y - 12, tw + 12, 20);
    }
    ctx.fillStyle = "#f5f5f5";
    ctx.fillText(line, x, y + 3);
  }
  return drawn;
}

function drawLabelPill(text, x, y, a) {
  if (!text) return;
  ctx.save();
  ctx.font = "600 12px ui-sans-serif, system-ui";
  const tw = ctx.measureText(text).width;
  ctx.globalAlpha = Math.max(0.55, a);
  ctx.fillStyle = "rgba(10, 3, 3, 0.82)";
  if (ctx.roundRect) {
    ctx.beginPath();
    ctx.roundRect(x - 5, y - 9, tw + 10, 18, 4);
    ctx.fill();
  } else ctx.fillRect(x - 5, y - 9, tw + 10, 18);
  ctx.fillStyle = "#f5f5f5";
  ctx.fillText(text, x, y + 4);
  ctx.restore();
}

function drawHudLabels(labels) {
  labels.sort((a, b) => b.d - a.d);
  ctx.font = "600 11px ui-sans-serif, system-ui";
  for (const item of labels) {
    if (!item.lab) continue;
    const text = item.layer.label.toUpperCase();
    const tw = ctx.measureText(text).width;
    const x = item.lab.x;
    const y = item.lab.y;
    ctx.globalAlpha = Math.max(0.45, fogA(item.d));
    ctx.fillStyle = "rgba(12, 4, 4, 0.72)";
    if (ctx.roundRect) {
      ctx.beginPath();
      ctx.roundRect(x - 5, y - 11, tw + 10, 16, 3);
      ctx.fill();
    } else ctx.fillRect(x - 5, y - 11, tw + 10, 16);
    ctx.fillStyle = item.layer.color;
    ctx.fillText(text, x, y + 1);
    ctx.globalAlpha = 1;
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
  // Do not auto-spin: it looks like the camera "snaps back" after a good view.

  ctx.clearRect(0, 0, w, h);
  const sky = ctx.createRadialGradient(w * 0.5, h * 0.12, 20, w * 0.5, h * 0.35, Math.max(w, h) * 0.85);
  sky.addColorStop(0, "rgba(48, 10, 10, 0.35)");
  sky.addColorStop(0.45, "rgba(14, 4, 4, 0.18)");
  sky.addColorStop(1, "rgba(4, 1, 1, 0.55)");
  ctx.fillStyle = sky;
  ctx.fillRect(0, 0, w, h);

  const g = graphSnapshot();
  const nodes = layoutWorld(g);
  const byId = new Map();
  for (const n of nodes) {
    if (n && n.id != null) byId.set(n.id, n);
  }
  const bounds = stackBounds(nodes);

  drawGround(bounds, w, h);

  const planeLabels = [];
  const planes = scaledLayers()
    .filter((layer) => !hidden3d.has(layer.id) && (shape === "stack" || shape === "towers" || shape === "rings"))
    .map((layer) => {
      const mid = project((bounds.minX + bounds.maxX) / 2, layer.y, (bounds.minZ + bounds.maxZ) / 2, w, h);
      return { layer, d: mid && !mid.behind ? mid.d : 1e9 };
    })
    .sort((a, b) => b.d - a.d);
  for (const item of planes) planeLabels.push(drawPlane(item.layer, bounds, w, h));
  drawRisers(bounds, w, h);
  drawHudLabels(planeLabels);

  const edges = collectEdges(g, nodes, byId);
  edges.sort((p, q) => {
    const da = project(p.a.wx, p.a.wy, p.a.wz, w, h);
    const db = project(q.a.wx, q.a.wy, q.a.wz, w, h);
    return (db ? db.d : 0) - (da ? da.d : 0);
  });
  for (const e of edges) drawEdge(e, w, h);
  seedAmbient(edges, now);

  drawPackets(byId, now, w, h);
  drawNodes(nodes, now, w, h);
  paintLiveHud(g);

  const fog = ctx.createLinearGradient(0, 0, 0, h);
  fog.addColorStop(0, "rgba(8, 2, 2, 0.08)");
  fog.addColorStop(0.55, "rgba(8, 2, 2, 0)");
  fog.addColorStop(1, "rgba(6, 1, 1, 0.42)");
  ctx.fillStyle = fog;
  ctx.fillRect(0, 0, w, h);

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

function hitTest(clientX, clientY) {
  if (!canvas) return "";
  const rect = canvas.getBoundingClientRect();
  const x = clientX - rect.left;
  const y = clientY - rect.top;
  const nodes = worldCache.length ? worldCache : layoutWorld(graphSnapshot());
  const w = rect.width;
  const h = rect.height;
  let best = { id: "", score: Infinity };
  for (const n of nodes) {
    if (!hasPos(n)) continue;
    const p = project(n.wx, n.wy, n.wz, w, h);
    if (!p || p.behind) continue;
    const r = Math.max(8, (KIND_R[n.kind] || 7) * p.s * 1.35);
    const dist = Math.hypot(p.x - x, p.y - y);
    if (dist > r) continue;
    const score = dist + p.d * 0.0005;
    if (score < best.score) best = { id: n.id, score };
  }
  return best.id;
}

function paintLiveHud(g) {
  if (!hud) return;
  const el = hud.querySelector(".sf-space3d-live");
  if (!el) return;
  const live = g && g.live;
  const text =
    live && live.live ? `live · ${[live.stage, live.item || live.service].filter(Boolean).join(" · ")}` : "";
  if (el.textContent !== text) el.textContent = text;
}

function rebuildHud() {
  if (!hud) return;
  const shapes = SHAPES.map(
    (s) =>
      `<button type="button" class="sf-space3d-shape${shape === s.id ? " on" : ""}" data-3d-shape="${s.id}">${s.label}</button>`
  ).join("");
  const layers = LAYERS.map((l) => {
    const on = !hidden3d.has(l.id);
    return `<button type="button" class="sf-space3d-layerbtn${on ? " on" : ""}" data-3d-layer="${l.id}" style="--c:${l.color}">${l.label}</button>`;
  }).join("");
  hud.innerHTML = `
    <div class="sf-space3d-legend">
      ${layers}
    </div>
    <div class="sf-space3d-shapes">${shapes}</div>
    <div class="sf-space3d-space">
      <button type="button" data-3d-space="-">space −</button>
      <span>${space.toFixed(1)}×</span>
      <button type="button" data-3d-space="+">space +</button>
    </div>
    <div class="sf-space3d-hint">same data, different shape · drag orbit · pull down for top · click a node</div>
    <div class="sf-space3d-live"></div>
    <button type="button" class="sf-space3d-top" id="dm-space3d-top" title="Bird's-eye / top-down">top</button>`;
  const topBtn = hud.querySelector("#dm-space3d-top");
  if (topBtn) topBtn.onclick = (e) => {
    e.preventDefault();
    e.stopPropagation();
    lookTopSpace3d();
  };
  hud.querySelectorAll("[data-3d-shape]").forEach((btn) => {
    btn.onclick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      shape = btn.getAttribute("data-3d-shape") || "stack";
      layoutSig = "";
      rebuildHud();
      lookTopSpace3d();
    };
  });
  hud.querySelectorAll("[data-3d-space]").forEach((btn) => {
    btn.onclick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      const dir = btn.getAttribute("data-3d-space");
      space = Math.max(SPACE_MIN, Math.min(SPACE_MAX, space + (dir === "+" ? 0.2 : -0.2)));
      layoutSig = "";
      rebuildHud();
    };
  });
  hud.querySelectorAll("[data-3d-layer]").forEach((btn) => {
    btn.onclick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      const id = btn.getAttribute("data-3d-layer");
      if (hidden3d.has(id)) hidden3d.delete(id);
      else hidden3d.add(id);
      layoutSig = "";
      rebuildHud();
    };
  });
}

function buildHud(flowEl) {
  hud = flowEl.querySelector(".sf-space3d-hud");
  if (!hud) {
    hud = document.createElement("div");
    hud.className = "sf-space3d-hud";
    flowEl.appendChild(hud);
  }
  hud.hidden = !enabled;
  rebuildHud();
}

function onPointerDown(e) {
  if (e.button === 2 || e.shiftKey || e.button === 1) panDrag = true;
  else if (e.button !== 0) return;
  e.stopPropagation();
  dragging = true;
  moved = 0;
  idleAt = performance.now();
  ptrStart = { x: e.clientX, y: e.clientY };
  lastPtr = { x: e.clientX, y: e.clientY };
  try {
    canvas.setPointerCapture(e.pointerId);
  } catch (_) {
    /* ignore */
  }
  canvas.classList.add("dragging");
}

function onPointerMove(e) {
  e.stopPropagation();
  hoverId = hitTest(e.clientX, e.clientY);
  canvas.style.cursor = hoverId ? "pointer" : dragging ? "grabbing" : "grab";
  if (!dragging) return;
  idleAt = performance.now();
  const dx = e.clientX - lastPtr.x;
  const dy = e.clientY - lastPtr.y;
  lastPtr = { x: e.clientX, y: e.clientY };
  moved += Math.hypot(dx, dy);
  if (panDrag) {
    const k = cam.dist * 0.0016;
    const cy = Math.cos(cam.yaw);
    const sy = Math.sin(cam.yaw);
    cam.tx -= dx * k * cy;
    cam.tz += dx * k * sy;
    cam.ty -= dy * k;
  } else {
    cam.yaw += dx * 0.008;
    // Drag down looks down (bird's eye). Old sign only tilted toward a side view.
    cam.pitch = Math.max(PITCH_MIN, Math.min(PITCH_MAX, cam.pitch - dy * 0.014));
    camFitted = true;
  }
}

function onPointerUp(e) {
  e.stopPropagation();
  const wasDragging = dragging;
  const wasMoved = moved;
  dragging = false;
  panDrag = false;
  canvas.classList.remove("dragging");
  idleAt = performance.now();
  if (!wasDragging) return;
  const total = Math.hypot(e.clientX - ptrStart.x, e.clientY - ptrStart.y);
  if (Math.max(wasMoved, total) < 5) {
    const id = hitTest(e.clientX, e.clientY);
    if (id) onSelect(id);
  }
}

function onWheel(e) {
  e.preventDefault();
  e.stopPropagation();
  idleAt = performance.now();
  cam.dist = Math.max(DIST_MIN, Math.min(DIST_MAX, cam.dist * (e.deltaY > 0 ? 1.1 : 0.9)));
}

function onContext(e) {
  e.preventDefault();
}

export function mountSpace3d(flowEl, opts) {
  getGraph = (opts && opts.getGraph) || getGraph;
  onSelect = (opts && opts.onSelect) || onSelect;
  if (!flowEl) return;
  canvas = document.getElementById("dm-space3d");
  if (!canvas) {
    canvas = document.createElement("canvas");
    canvas.id = "dm-space3d";
    canvas.className = "sf-space3d";
    canvas.hidden = true;
    flowEl.appendChild(canvas);
  }
  canvas.classList.add("sf-space3d");
  ctx = canvas.getContext("2d");
  buildHud(flowEl);
  if (wired) unbind();
  on(canvas, "pointerdown", onPointerDown);
  on(canvas, "pointermove", onPointerMove);
  on(canvas, "pointerup", onPointerUp);
  on(canvas, "pointercancel", onPointerUp);
  on(canvas, "wheel", onWheel, { passive: false });
  on(canvas, "contextmenu", onContext);
  wired = true;
  idleAt = performance.now();
}

export function fitSpace3d() {
  lookTopSpace3d();
}

export function lookTopSpace3d() {
  const nodes = worldCache.length ? worldCache : layoutWorld(graphSnapshot());
  const b = stackBounds(nodes);
  cam.tx = (b.minX + b.maxX) / 2;
  cam.tz = (b.minZ + b.maxZ) / 2;
  let minY = Infinity;
  let maxY = -Infinity;
  for (const n of nodes) {
    if (!hasPos(n)) continue;
    minY = Math.min(minY, n.wy);
    maxY = Math.max(maxY, n.wy);
  }
  cam.ty = Number.isFinite(minY) ? (minY + maxY) / 2 : 340;
  const span = Math.max(b.maxX - b.minX, b.maxZ - b.minZ, 420);
  cam.dist = Math.max(DIST_MIN, Math.min(DIST_MAX, span * 2.15));
  cam.pitch = PITCH_TOP;
  cam.yaw = 0;
  camFitted = true;
  idleAt = performance.now();
}

export function zoomSpace3d(dir) {
  idleAt = performance.now();
  cam.dist = Math.max(DIST_MIN, Math.min(DIST_MAX, cam.dist * (dir === "in" ? 0.82 : 1.22)));
}

export function setSpace3dEnabled(on) {
  enabled = !!on;
  if (canvas) canvas.hidden = !enabled;
  if (hud) hud.hidden = !enabled;
  if (enabled) {
    sizeCanvas();
    if (!camFitted) fitSpace3d();
    idleAt = performance.now();
    if (!raf) raf = requestAnimationFrame(draw);
  } else if (raf) {
    cancelAnimationFrame(raf);
    raf = 0;
  }
}

export function isSpace3dEnabled() {
  return enabled;
}

export function pushPacket(fromId, toId, kind) {
  if (!fromId || !toId) return;
  packets.push({
    from: fromId,
    to: toId,
    kind: kind || "net",
    born: (performance.now() - t0) / 1000,
    life: 1.6 + Math.random() * 0.8,
  });
  if (packets.length > 80) packets = packets.slice(-60);
}

export function resizeSpace3d() {
  if (enabled) sizeCanvas();
}

export function destroySpace3d() {
  enabled = false;
  if (raf) cancelAnimationFrame(raf);
  raf = 0;
  packets = [];
  worldCache = [];
  extraTenants = [];
  layoutSig = "";
  hoverId = "";
  camFitted = false;
  unbind();
  if (hud && hud.parentElement) hud.parentElement.removeChild(hud);
  hud = null;
  canvas = null;
  ctx = null;
}
