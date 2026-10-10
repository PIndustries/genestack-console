// pages/environment_rack.js — one rack payload, three views.
// Flat is the elevation (U1 at the bottom). Space is a canvas orbit.
// Cloud is that same rack beside Metal, Kubernetes, and Instances.
import { api, esc, toast } from "../api.js";
import { canRun, gate } from "../store.js";

const GEAR_KINDS = new Set(["switch", "pdu", "layer"]);
const MODES = new Set(["flat", "space", "cloud"]);
const ROW_U = 48;

let getEnvId = () => "";
let activeEnvId = "";
let loadGen = 0;
let saving = false;
let payload = emptyPayload();
let logicalMap = new Map();

let spaceOn = false;
let raf = 0;
let spaceListeners = [];
let cam = { yaw: 0.62, pitch: 0.36, dist: 340, tx: 0, ty: 70, tz: 0, user: false };
let dragging = false;
let lastPtr = { x: 0, y: 0 };

let overlayFrame = 0;
let overlayLate = 0;
let resizeObserver = null;
let resizeHandler = null;

function emptyPayload() {
  return { racks: [], connections: [], logical: [], gear: [] };
}

function asList(value) {
  return Array.isArray(value) ? value : [];
}

function isGearKind(kind) {
  return GEAR_KINDS.has(String(kind || "").toLowerCase());
}

function isSide(item) {
  return !!item && (item.side === true || item.side === 1 || item.side === "true");
}

function isConflict(item) {
  return !!(item && item.conflict && item.conflict !== "false" && item.conflict !== "0");
}

function rackHeight(rack) {
  const raw = rack && (rack.height != null ? rack.height : rack.height_u);
  const n = Math.round(Number(raw));
  if (Number.isFinite(n) && n >= 1 && n <= 100) return n;
  return ROW_U;
}

function roleText(item) {
  const roles = item && item.roles;
  if (Array.isArray(roles)) return roles.map((r) => String(r)).filter(Boolean).join(", ");
  if (roles == null || roles === "") return "";
  return String(roles);
}

function itemName(item) {
  if (!item) return "";
  if (item.name != null && String(item.name) !== "") return String(item.name);
  if (item.hostname != null && String(item.hostname) !== "") return String(item.hostname);
  if (item.id != null && String(item.id) !== "") return String(item.id);
  return "";
}

function itemKey(item) {
  if (!item) return "";
  if (item.id != null && String(item.id) !== "") return "id:" + item.id;
  const name = itemName(item);
  return name ? "name:" + name : "";
}

function linkRows(src) {
  if (Array.isArray(src.links)) return src.links;
  return asList(src.connections);
}

function normalize(data) {
  const src = data && typeof data === "object" ? data : {};
  const connections = [];
  for (const row of linkRows(src)) {
    if (!row || typeof row !== "object") continue;
    const source = row.source != null ? String(row.source).trim() : "";
    const target = row.target != null ? String(row.target).trim() : "";
    if (!source || !target) continue;
    connections.push({
      source,
      target,
      kind: row.kind === "planned" ? "planned" : "seen",
      label: row.label != null ? String(row.label) : "",
      id: row.id != null ? String(row.id) : "",
    });
  }
  const logical = [];
  for (const row of asList(src.logical)) {
    if (!row || typeof row !== "object") continue;
    const hostname = row.hostname != null ? String(row.hostname).trim() : "";
    if (!hostname) continue;
    logical.push({
      hostname,
      rack: row.rack != null ? String(row.rack) : "",
      u_start: row.u_start,
      u_height: row.u_height,
      side: row.side === true,
      roles: Array.isArray(row.roles) ? row.roles : [],
      in_cluster: row.in_cluster === true,
      vms: asList(row.vms).map((vm) => String(vm).trim()).filter(Boolean),
    });
  }
  const racks = asList(src.racks).map((rack) => {
    const row = rack && typeof rack === "object" ? rack : {};
    const items = asList(row.items).filter((item) => item && typeof item === "object");
    const side = items.filter((item) => isSide(item));
    for (const extra of asList(row.side)) {
      if (extra && typeof extra === "object") side.push(extra);
    }
    return {
      name: row.name != null ? String(row.name) : "",
      height: row.height != null ? row.height : row.height_u,
      height_u: row.height != null ? row.height : row.height_u,
      items,
      side,
    };
  });
  return { racks, connections, logical, gear: [] };
}

function rebuildLogical() {
  logicalMap = new Map();
  for (const row of payload.logical) logicalMap.set(row.hostname, row);
}

function logicalFor(name) {
  if (name == null || name === "") return null;
  const key = String(name);
  if (logicalMap.has(key)) return logicalMap.get(key);
  const lower = key.toLowerCase();
  for (const [host, row] of logicalMap) {
    if (host.toLowerCase() === lower) return row;
  }
  return null;
}

function isReady(item) {
  const row = logicalFor(itemName(item)) || logicalFor(item && item.id);
  return !!(row && row.in_cluster);
}

function racksForView() {
  const seen = new Set();
  const racks = payload.racks.map((rack) => ({
    name: rack.name,
    height_u: rack.height_u,
    items: rack.items.slice(),
    side: rack.side.slice(),
  }));
  const note = (item) => {
    const key = itemKey(item);
    if (key) seen.add(key);
    const name = itemName(item);
    if (name) seen.add("name:" + name);
  };
  for (const rack of racks) {
    for (const item of rack.items) note(item);
    for (const item of rack.side) note(item);
  }
  for (const gear of payload.gear) {
    const key = itemKey(gear);
    const name = itemName(gear);
    if ((key && seen.has(key)) || (name && seen.has("name:" + name))) continue;
    if (key) seen.add(key);
    if (name) seen.add("name:" + name);
    let rack = racks.find((row) => row.name === String(gear.rack || ""));
    if (!rack) rack = racks[0];
    if (!rack) {
      rack = { name: gear.rack ? String(gear.rack) : "Rack 1", height_u: ROW_U, items: [], side: [] };
      racks.push(rack);
    }
    const item = {
      id: gear.id,
      name: gear.name,
      kind: gear.kind,
      u_start: gear.u_start,
      u_height: gear.u_height,
      side: isSide(gear),
      conflict: gear.conflict,
      roles: gear.roles || [],
    };
    if (item.side) rack.side.push(item);
    else rack.items.push(item);
  }
  return racks;
}

function sideKeys(rack) {
  const keys = new Set();
  for (const item of asList(rack.side)) {
    const key = itemKey(item);
    if (key) keys.add(key);
    const name = itemName(item);
    if (name) keys.add("name:" + name);
  }
  return keys;
}

function besideItems(rack) {
  const out = [];
  const seen = new Set();
  const push = (item) => {
    const key = itemKey(item) || ("name:" + itemName(item));
    if (!key || seen.has(key)) return;
    seen.add(key);
    out.push(item);
  };
  for (const item of asList(rack.items)) {
    if (isSide(item)) push(item);
  }
  for (const item of asList(rack.side)) push(item);
  return out;
}

function faceItems(rack) {
  const beside = sideKeys(rack);
  return asList(rack.items).filter((item) => {
    if (!item || isSide(item)) return false;
    const key = itemKey(item);
    const name = itemName(item);
    if (key && beside.has(key)) return false;
    if (name && beside.has("name:" + name)) return false;
    return true;
  });
}

// A placed machine occupies u_start for u_height. Anything else stays off the rails.
function spanOf(item, heightU) {
  if (!item || isSide(item)) return null;
  const u = Number(item.u_start);
  const h = Number(item.u_height);
  if (!Number.isFinite(u) || !Number.isFinite(h) || u < 1 || h < 1) return null;
  const start = Math.round(u);
  if (start > heightU) return null;
  const end = Math.min(heightU, start + Math.round(h) - 1);
  return { u: start, h: Math.max(1, end - start + 1) };
}

function lanesFor(items, heightU) {
  const lanes = new Map();
  const placed = [];
  for (const item of items) {
    const span = spanOf(item, heightU);
    if (!span) continue;
    let lane = 0;
    const end = span.u + span.h - 1;
    for (const prev of placed) {
      const other = lanes.get(prev.item);
      if (other !== lane) continue;
      if (span.u <= prev.end && prev.span.u <= end) lane += 1;
    }
    lanes.set(item, lane);
    placed.push({ item, span, end });
  }
  return lanes;
}

function toneClass(item) {
  const conflict = isConflict(item) ? " is-conflict" : "";
  if (isGearKind(item.kind)) return conflict + " is-gear is-" + String(item.kind).toLowerCase();
  return conflict + (isReady(item) ? " is-ready" : " is-inventory");
}

function tip(item) {
  return [itemName(item), item.kind, roleText(item), isConflict(item) ? "overlap" : ""].filter(Boolean).join(" · ");
}

function blockInner(item) {
  const overlap = isConflict(item) ? `<span class="rack-overlap">overlap</span>` : "";
  const kind = isGearKind(item.kind) ? `<span class="rack-kind">${esc(item.kind)}</span>` : "";
  const roles = roleText(item);
  const role = roles ? `<span class="rack-roles">${esc(roles)}</span>` : "";
  return `${overlap}<span class="rack-name">${esc(itemName(item) || "Machine")}</span>${kind}${role}`;
}

function rawU(item) {
  if (!item || isSide(item)) return "";
  const u = Number(item.u_start);
  if (!Number.isFinite(u) || u < 1) return "";
  return String(Math.round(u));
}

function rawH(item) {
  const h = Number(item && item.u_height);
  if (!Number.isFinite(h) || h < 1) return "";
  return String(Math.round(h));
}

function controlHtml(item, rackName, className, style) {
  const host = itemName(item);
  const id = item && item.id != null ? String(item.id) : "";
  const gear = isGearKind(item.kind);
  const tag = gear ? "div" : "button";
  const type = gear ? "" : ` type="button"`;
  const hostAttr = gear ? "" : ` data-rack-host="${esc(host)}"`;
  const styleAttr = style ? ` style="${style}"` : "";
  return `<${tag}${type} class="${className}"${hostAttr}${styleAttr} data-rack-anchor="${esc(host)}" data-rack-anchor-id="${esc(id)}" data-rack-u-start="${esc(rawU(item))}" data-rack-u-height="${esc(rawH(item))}" data-rack-rack="${esc(rackName)}" title="${esc(tip(item))}">${blockInner(item)}</${tag}>`;
}

function railsHtml(heightU) {
  let html = "";
  for (let u = heightU; u >= 1; u -= 1) {
    html += `<div class="rack-unit"><span>${u}</span></div>`;
  }
  return html;
}

function rackSection(rack, compact) {
  const heightU = rackHeight(rack);
  const name = rack.name || "Rack";
  const faces = faceItems(rack);
  const lanes = lanesFor(faces, heightU);
  const blocks = [];
  const loose = [];
  for (const item of faces) {
    const span = spanOf(item, heightU);
    if (!span) {
      loose.push(item);
      continue;
    }
    const lane = lanes.get(item) || 0;
    blocks.push(controlHtml(item, name, "rack-block" + toneClass(item), `--u:${span.u};--h:${span.h};--lane:${lane}`));
  }
  const beside = besideItems(rack);
  const besideBody = beside.length
    ? beside.map((item) => controlHtml(item, name, "rack-side-btn" + toneClass(item), "")).join("")
    : `<p class="muted">Nothing beside it.</p>`;
  const looseBody = loose.length
    ? `<div class="rack-loose"><h3>Not placed</h3><div class="rack-loose-list">${loose
        .map((item) => controlHtml(item, name, "rack-side-btn" + toneClass(item), ""))
        .join("")}</div></div>`
    : "";
  return `<section class="rack-section${compact ? " is-compact" : ""}">
    <h3 class="rack-section-name">${esc(name)}</h3>
    <div class="rack-elev" style="--rows:${heightU}">${railsHtml(heightU)}${blocks.join("")}</div>
    <aside class="rack-beside">
      <h3>Beside the rack</h3>
      ${besideBody}
    </aside>
    ${looseBody}
  </section>`;
}

function flatInner(racks, compact) {
  if (!racks.length) return `<p class="muted">No rack yet.</p>`;
  return racks.map((rack) => rackSection(rack, compact)).join("");
}

function metalNames(racks) {
  const names = [];
  const seen = new Set();
  const push = (name) => {
    const text = String(name || "").trim();
    if (!text || seen.has(text.toLowerCase())) return;
    seen.add(text.toLowerCase());
    names.push(text);
  };
  for (const row of payload.logical) push(row.hostname);
  for (const rack of racks) {
    for (const item of faceItems(rack).concat(besideItems(rack))) {
      if (!isGearKind(item.kind)) push(itemName(item));
    }
  }
  return names;
}

function vmNames() {
  const names = [];
  const seen = new Set();
  for (const row of payload.logical) {
    for (const vm of row.vms) {
      const key = vm.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      names.push(vm);
    }
  }
  return names;
}

function cloudHtml(racks) {
  const inCluster = payload.logical.filter((row) => row.in_cluster).length;
  const metals = metalNames(racks);
  const vms = vmNames();
  const metalBody = metals.length
    ? metals
        .map((name) => {
          const row = logicalFor(name);
          const flag = row && row.in_cluster ? `<span class="pill ok">In cluster</span>` : "";
          return `<div class="rack-metal-row" data-rack-anchor="${esc(name)}"><span>${esc(name)}</span>${flag}</div>`;
        })
        .join("")
    : `<p class="muted">No machines.</p>`;
  const vmBody = vms.length
    ? vms.map((vm) => `<div class="rack-vm" data-rack-vm="${esc(vm)}" data-rack-anchor="${esc(vm)}">${esc(vm)}</div>`).join("")
    : `<p class="muted">No instances.</p>`;
  return `<div class="rack-cloud" id="rack-cloud">
    <div class="rack-cloud-cols">
      <div class="rack-cloud-left">
        <div class="rack-flat is-compact" id="rack-flat-embed">${flatInner(racks, true)}</div>
      </div>
      <div class="rack-stack">
        <section class="rack-layer" data-rack-layer="instances">
          <h3>Instances</h3>
          <div class="rack-vms">${vmBody}</div>
        </section>
        <section class="rack-layer" data-rack-layer="kubernetes" data-rack-anchor="kubernetes">
          <h3>Kubernetes</h3>
          <p class="muted">${inCluster ? `${inCluster} in the cluster` : "Nothing in the cluster yet."}</p>
        </section>
        <section class="rack-layer" data-rack-layer="metal">
          <h3>Metal</h3>
          ${metalBody}
          ${payload.connections.some((row) => row.target === "management-port" || row.source === "management-port")
            ? `<p class="muted" data-rack-anchor="management-port">Management port</p>`
            : ""}
        </section>
      </div>
    </div>
    <svg class="rack-overlay" aria-hidden="true"></svg>
    <p class="rack-caption">The rack is the machines. The cloud is what those machines are running.</p>
  </div>`;
}

function spaceHtml() {
  return `<div class="rack-space">
    <canvas id="rack-space" class="rack-canvas" aria-label="Rack in space. Drag to orbit."></canvas>
    <p class="rack-caption">This is the same machines, with a place.</p>
    <p class="muted rack-key">Green is in the cluster. Gray is only inventoried. Drag to orbit.</p>
  </div>`;
}

function flatWrap(racks) {
  return `<div class="rack-flat" id="rack-flat">${flatInner(racks, false)}<svg class="rack-overlay" aria-hidden="true"></svg></div>`;
}

export function rackCardHtml() {
  const op = gate(canRun(), "operator");
  return `<div class="card" id="rack-card" data-rack-mode="flat">
    <style>
      #rack-card .rack-forms { display: grid; grid-template-columns: 1fr 1fr; gap: .75rem; margin: .7rem 0; }
      #rack-card .rack-form { display: grid; gap: .4rem; padding: .65rem; border: 1px solid var(--border, #1a3d28); border-radius: .5rem; }
      #rack-card .rack-form h3 { margin: 0; font-size: .82rem; }
      #rack-card .rack-form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: .4rem .6rem; }
      #rack-card .rack-form label { display: grid; gap: .15rem; font-size: .72rem; color: var(--muted, #7e9b86); }
      #rack-card .rack-form input, #rack-card .rack-form select { width: 100%; }
      #rack-card .rack-modes { display: flex; gap: .4rem; margin: 0 0 .7rem; }
      #rack-card .rack-place { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)) auto; gap: .45rem .6rem; align-items: end; margin: 0 0 .7rem; padding: .6rem; border: 1px solid var(--border, #1a3d28); border-radius: .5rem; background: rgba(0,0,0,.18); }
      #rack-card .rack-place[hidden] { display: none; }
      #rack-card .rack-place-title { grid-column: 1 / -1; font-size: .82rem; }
      #rack-card .rack-place label { display: grid; gap: .15rem; font-size: .72rem; color: var(--muted, #7e9b86); }
      #rack-card .rack-place-actions { display: flex; gap: .35rem; }
      #rack-card .rack-flat { --row: 16px; position: relative; }
      #rack-card .rack-flat.is-compact { --row: 10px; }
      #rack-card .rack-section { display: flex; flex-wrap: wrap; gap: .75rem 1rem; align-items: flex-start; margin-bottom: .9rem; }
      #rack-card .rack-section-name { flex: 1 0 100%; margin: 0; font-size: .9rem; }
      #rack-card .rack-elev { position: relative; width: min(100%, 36rem); background: #070e09; border: 1px solid var(--border, #1a3d28); border-radius: .4rem; }
      #rack-card .rack-flat.is-compact .rack-elev { width: min(100%, 18rem); }
      #rack-card .rack-unit { height: var(--row); box-sizing: border-box; border-bottom: 1px solid rgba(26, 61, 40, .85); color: var(--muted, #7e9b86); font-size: 9px; line-height: var(--row); padding-left: .3rem; }
      #rack-card .rack-block { position: absolute; left: calc(2.35rem + var(--lane, 0) * 12px); right: 4px; bottom: calc((var(--u) - 1) * var(--row)); height: calc(var(--h) * var(--row) - 1px); z-index: calc(2 + var(--lane, 0)); display: flex; align-items: center; gap: .25rem; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; padding: 0 .35rem; border-radius: .25rem; border: 1px solid var(--ok-border, #14532d); background: rgba(63, 185, 80, .2); color: var(--text, #eafaef); font-size: 11px; text-align: left; }
      #rack-card .rack-side-btn { display: flex; align-items: center; gap: .3rem; width: 100%; margin: 0 0 .3rem; text-align: left; font-size: .78rem; }
      #rack-card .is-ready { background: rgba(63, 185, 80, .28); border-color: var(--ok-border, #14532d); }
      #rack-card .is-inventory { background: rgba(100, 116, 139, .22); border-color: #334155; }
      #rack-card .is-switch { background: rgba(125, 211, 252, .16); border-color: #155e75; }
      #rack-card .is-pdu { background: rgba(210, 153, 34, .2); border-color: #78350f; }
      #rack-card .is-layer { background: rgba(167, 139, 250, .18); border-color: #5b21b6; }
      #rack-card .is-conflict { box-shadow: inset 0 0 0 1px var(--danger, #f85149); }
      #rack-card .is-selected { outline: 2px solid var(--accent, #34c759); outline-offset: 1px; }
      #rack-card .rack-overlap { flex: none; padding: 0 .28rem; border-radius: 999px; background: var(--bad-bg, #2a0b0b); color: var(--bad-text, #fecaca); border: 1px solid var(--bad-border, #7f1d1d); font-size: 9px; line-height: 1.35; }
      #rack-card .rack-kind, #rack-card .rack-roles { color: var(--muted, #7e9b86); font-size: 10px; }
      #rack-card .rack-beside { min-width: 10rem; flex: 1; }
      #rack-card .rack-beside h3, #rack-card .rack-loose h3, #rack-card .rack-layer h3 { margin: 0 0 .35rem; font-size: .82rem; }
      #rack-card .rack-loose { flex: 1 0 100%; }
      #rack-card .rack-loose-list { display: flex; flex-wrap: wrap; gap: .35rem; }
      #rack-card .rack-loose-list .rack-side-btn { width: auto; }
      #rack-card .rack-canvas { width: 100%; height: 28rem; display: block; border: 1px solid var(--border, #1a3d28); border-radius: .5rem; background: #070e09; cursor: grab; touch-action: none; }
      #rack-card .rack-canvas.is-dragging { cursor: grabbing; }
      #rack-card .rack-caption { margin: .55rem 0 .15rem; font-size: .86rem; }
      #rack-card .rack-key { margin: 0; font-size: .75rem; }
      #rack-card .rack-cloud { position: relative; }
      #rack-card .rack-cloud-cols { display: grid; grid-template-columns: minmax(16rem, .9fr) minmax(16rem, 1.1fr); gap: 1rem; align-items: start; }
      #rack-card .rack-stack { display: flex; flex-direction: column; gap: .5rem; }
      #rack-card .rack-layer { border: 1px solid var(--border, #1a3d28); border-radius: .5rem; padding: .55rem .7rem; background: rgba(0,0,0,.18); }
      #rack-card .rack-layer[data-rack-layer="metal"] { border-color: #14532d; }
      #rack-card .rack-layer[data-rack-layer="kubernetes"] { border-color: #155e75; }
      #rack-card .rack-layer[data-rack-layer="instances"] { border-color: #78350f; }
      #rack-card .rack-metal-row, #rack-card .rack-vm { display: flex; justify-content: space-between; gap: .4rem; align-items: center; padding: .2rem 0; font-size: .8rem; }
      #rack-card .rack-vms { display: flex; flex-wrap: wrap; gap: .3rem; }
      #rack-card .rack-vm { border: 1px solid #78350f; border-radius: 999px; padding: .1rem .45rem; }
      #rack-card .rack-overlay { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; overflow: visible; }
      #rack-card .rack-cable { stroke: #8df06a; stroke-width: 1.7; fill: none; }
      #rack-card .rack-cable.is-planned { stroke: #d29922; }
      #rack-card .rack-logic { stroke: rgba(125, 211, 252, .85); stroke-width: 1.25; fill: none; }
      #rack-card .rack-cable-label { fill: var(--muted, #7e9b86); font-size: 10px; }
      @media (max-width: 860px) {
        #rack-card .rack-forms, #rack-card .rack-cloud-cols, #rack-card .rack-place { grid-template-columns: 1fr; }
      }
    </style>
    <div class="toolbar">
      <h2>Rack</h2>
      <span class="muted" id="rack-msg"></span>
    </div>
    <div class="rack-forms">
      <form id="rack-gear-form" class="rack-form">
        <h3>Add gear</h3>
        <div class="rack-form-grid">
          <label>Name <input name="name" type="text" autocomplete="off"></label>
          <label>Kind
            <select name="kind">
              <option value="switch">switch</option>
              <option value="pdu">pdu</option>
              <option value="layer">layer</option>
            </select>
          </label>
          <label>Height in U <input name="u_height" type="number" min="1" step="1" value="1"></label>
          <label>Starting U <input name="u_start" type="number" min="1" step="1" placeholder="optional"></label>
          <label>Rack name <input name="rack" type="text" autocomplete="off"></label>
        </div>
        <div><button type="submit" class="btn-sm" ${op}>Add gear</button></div>
      </form>
      <form id="rack-link-form" class="rack-form">
        <h3>Planned connection</h3>
        <div class="rack-form-grid">
          <label>Source <input name="source" type="text" autocomplete="off"></label>
          <label>Target <input name="target" type="text" autocomplete="off"></label>
          <label>Label <input name="label" type="text" autocomplete="off"></label>
        </div>
        <div><button type="submit" class="btn-sm" ${op}>Add connection</button></div>
      </form>
    </div>
    <div class="rack-modes" role="tablist" aria-label="Rack view">
      <button type="button" class="btn-sm" data-rack-pick="flat" aria-pressed="true">Flat</button>
      <button type="button" class="btn-sm secondary" data-rack-pick="space" aria-pressed="false">Space</button>
      <button type="button" class="btn-sm secondary" data-rack-pick="cloud" aria-pressed="false">Cloud</button>
    </div>
    <form id="rack-place-form" class="rack-place" hidden>
      <div class="rack-place-title">Place <strong data-rack-place-name></strong></div>
      <label>Height in U <input name="u_height" type="number" min="1" step="1"></label>
      <label>Starting U <input name="u_start" type="number" min="1" step="1" placeholder="empty clears placement"></label>
      <label>Rack name <input name="rack" type="text" autocomplete="off"></label>
      <div class="rack-place-actions">
        <button type="submit" class="btn-sm" ${op}>Save</button>
        <button type="button" class="btn-sm secondary" data-rack-place-cancel>Cancel</button>
      </div>
      <p class="muted" style="grid-column:1/-1;margin:0">Leave starting U empty to clear placement.</p>
    </form>
    <div id="rack-view"><div class="muted">Loading…</div></div>
  </div>`;
}

function currentMode() {
  const card = document.getElementById("rack-card");
  const mode = card && card.dataset.rackMode;
  return MODES.has(mode) ? mode : "flat";
}

function syncModeButtons(mode) {
  document.querySelectorAll("#rack-card [data-rack-pick]").forEach((btn) => {
    const on = btn.dataset.rackPick === mode;
    btn.classList.toggle("secondary", !on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
  });
}

function fillGearRackDefault() {
  const input = document.querySelector("#rack-gear-form [name=rack]");
  if (!input || input.value.trim()) return;
  const racks = racksForView();
  if (racks[0] && racks[0].name) input.value = racks[0].name;
}

function paint() {
  stopSpace();
  stopOverlay();
  const card = document.getElementById("rack-card");
  const view = document.getElementById("rack-view");
  if (!card || !view) return;
  rebuildLogical();
  const mode = currentMode();
  if (card.dataset.rackMode !== mode) card.dataset.rackMode = mode;
  syncModeButtons(mode);
  fillGearRackDefault();
  const racks = racksForView();
  if (mode === "space") {
    view.innerHTML = spaceHtml();
    startSpace();
    return;
  }
  if (mode === "cloud") {
    view.innerHTML = cloudHtml(racks);
    scheduleOverlay();
    return;
  }
  view.innerHTML = flatWrap(racks);
  scheduleOverlay();
}

function envIdNow() {
  const id = getEnvId ? getEnvId() : "";
  return id || activeEnvId || "";
}

function rackApi(envId, suffix) {
  const base = `/api/v1/environments/${encodeURIComponent(envId)}/rack`;
  return suffix ? `${base}/${suffix}` : base;
}

function parseU(value) {
  const raw = String(value ?? "").trim();
  if (!raw) return null;
  if (!/^\d+$/.test(raw)) return NaN;
  return Number(raw);
}

function openPlace(hostEl) {
  const form = document.getElementById("rack-place-form");
  if (!form) return;
  const hostname = hostEl.dataset.rackHost || "";
  form.hidden = false;
  form.dataset.hostname = hostname;
  const name = form.querySelector("[data-rack-place-name]");
  if (name) name.textContent = hostname;
  form.u_height.value = hostEl.dataset.rackUHeight || "1";
  form.u_start.value = hostEl.dataset.rackUStart || "";
  form.rack.value = hostEl.dataset.rackRack || "";
  document.querySelectorAll("#rack-card .is-selected").forEach((el) => el.classList.remove("is-selected"));
  hostEl.classList.add("is-selected");
  form.scrollIntoView({ block: "nearest" });
}

function closePlace() {
  const form = document.getElementById("rack-place-form");
  if (form) form.hidden = true;
  document.querySelectorAll("#rack-card .is-selected").forEach((el) => el.classList.remove("is-selected"));
}

function onCardClick(e) {
  const pick = e.target.closest("[data-rack-pick]");
  if (pick) {
    const mode = pick.dataset.rackPick;
    const card = document.getElementById("rack-card");
    if (card && MODES.has(mode)) {
      card.dataset.rackMode = mode;
      paint();
    }
    return;
  }
  if (e.target.closest("[data-rack-place-cancel]")) {
    closePlace();
    return;
  }
  const host = e.target.closest("[data-rack-host]");
  if (host) openPlace(host);
}

function onCardKey(e) {
  if (e.key === "Escape") closePlace();
}

async function onCardSubmit(e) {
  const form = e.target;
  if (!(form instanceof HTMLFormElement)) return;
  if (form.id !== "rack-gear-form" && form.id !== "rack-link-form" && form.id !== "rack-place-form") return;
  e.preventDefault();
  if (form.id === "rack-gear-form") await submitGear(form);
  else if (form.id === "rack-link-form") await submitLink(form);
  else await submitPlace(form);
}

async function submitGear(form) {
  if (!canRun()) {
    toast("Requires operator role", "bad");
    return;
  }
  const envId = envIdNow();
  if (!envId || saving) return;
  const name = form.name.value.trim();
  const kind = String(form.kind.value || "");
  const uHeight = parseU(form.u_height.value);
  const uStart = parseU(form.u_start.value);
  let rack = form.rack.value.trim();
  if (!rack) {
    const first = racksForView()[0];
    rack = (first && first.name) || "Rack 1";
  }
  if (!name) {
    toast("Name the gear.", "bad");
    return;
  }
  if (!GEAR_KINDS.has(kind)) {
    toast("Kind is switch, pdu, or layer.", "bad");
    return;
  }
  if (!Number.isFinite(uHeight) || uHeight < 1) {
    toast("Height must be at least 1 U.", "bad");
    return;
  }
  if (uStart !== null && (!Number.isFinite(uStart) || uStart < 1)) {
    toast("Starting U must be a unit on the rack.", "bad");
    return;
  }
  saving = true;
  try {
    await api(rackApi(envId, "gear"), {
      method: "POST",
      body: JSON.stringify({ name, kind, u_height: uHeight, u_start: uStart, rack }),
    });
    form.name.value = "";
    form.u_start.value = "";
    toast("Gear added", "ok");
    await loadRackCard(envId);
  } catch (err) {
    toast(err.message || "Could not add gear", "bad");
  } finally {
    saving = false;
  }
}

async function submitLink(form) {
  if (!canRun()) {
    toast("Requires operator role", "bad");
    return;
  }
  const envId = envIdNow();
  if (!envId || saving) return;
  const source = form.source.value.trim();
  const target = form.target.value.trim();
  const label = form.label.value.trim();
  if (!source || !target) {
    toast("A connection needs a source and a target.", "bad");
    return;
  }
  saving = true;
  try {
    await api(rackApi(envId, "links"), {
      method: "POST",
      body: JSON.stringify({ source, target, label }),
    });
    form.source.value = "";
    form.target.value = "";
    form.label.value = "";
    toast("Connection added", "ok");
    await loadRackCard(envId);
  } catch (err) {
    toast(err.message || "Could not add the connection", "bad");
  } finally {
    saving = false;
  }
}

async function submitPlace(form) {
  if (!canRun()) {
    toast("Requires operator role", "bad");
    return;
  }
  const envId = envIdNow();
  if (!envId || saving) return;
  const hostname = form.dataset.hostname || "";
  const rack = form.rack.value.trim();
  const uStart = parseU(form.u_start.value);
  const uHeight = parseU(form.u_height.value);
  if (!hostname) return;
  if (!rack) {
    toast("Enter a rack name.", "bad");
    return;
  }
  if (uStart !== null && (!Number.isFinite(uStart) || uStart < 1)) {
    toast("Starting U must be a unit on the rack, or empty to clear placement.", "bad");
    return;
  }
  if (uHeight !== null && (!Number.isFinite(uHeight) || uHeight < 1)) {
    toast("Height must be at least 1 U.", "bad");
    return;
  }
  saving = true;
  try {
    await api(rackApi(envId, "placement"), {
      method: "PUT",
      body: JSON.stringify({ hostname, rack, u_start: uStart, u_height: uHeight }),
    });
    closePlace();
    toast(uStart == null ? "Placement cleared" : "Placement saved", "ok");
    await loadRackCard(envId);
  } catch (err) {
    toast(err.message || "Could not save placement", "bad");
  } finally {
    saving = false;
  }
}

function cssEscape(value) {
  const text = String(value ?? "");
  if (typeof CSS !== "undefined" && CSS.escape) return CSS.escape(text);
  return text.replace(/["\\]/g, "\\$&");
}

function findEnd(root, name) {
  const key = cssEscape(name);
  if (!key) return null;
  return (
    root.querySelector(`[data-rack-anchor="${key}"]`) ||
    root.querySelector(`[data-rack-anchor-id="${key}"]`) ||
    root.querySelector(`[data-rack-vm="${key}"]`)
  );
}

function svgLine(a, b, cls, dashed, label) {
  if (!a || !b || !Number.isFinite(a.x) || !Number.isFinite(b.x)) return "";
  const dash = dashed ? ` stroke-dasharray="7 5"` : "";
  let text = "";
  if (label) {
    const x = ((a.x + b.x) / 2).toFixed(1);
    const y = ((a.y + b.y) / 2).toFixed(1);
    text = `<text class="rack-cable-label" x="${x}" y="${y}">${esc(label)}</text>`;
  }
  return `<line class="${cls}" x1="${a.x.toFixed(1)}" y1="${a.y.toFixed(1)}" x2="${b.x.toFixed(1)}" y2="${b.y.toFixed(1)}"${dash}></line>${text}`;
}

// Cables are only the connections array. Cluster and instance lines are not cables.
function drawOverlay(root, logicalLines) {
  if (!root) return;
  const svg = root.querySelector(":scope > .rack-overlay");
  if (!svg) return;
  const box = root.getBoundingClientRect();
  if (box.width < 2 || box.height < 2) return;
  svg.setAttribute("viewBox", `0 0 ${box.width} ${box.height}`);
  svg.setAttribute("width", String(box.width));
  svg.setAttribute("height", String(box.height));
  const pt = (el) => {
    const rect = el.getBoundingClientRect();
    return { x: rect.left + rect.width / 2 - box.left, y: rect.top + rect.height / 2 - box.top };
  };
  const parts = [];
  const linked = new Set(payload.connections.map((row) => `${row.source}\0${row.target}`));
  if (logicalLines) {
    const k8s = root.querySelector('[data-rack-layer="kubernetes"]');
    for (const row of payload.logical) {
      const from = findEnd(root, row.hostname);
      if (!from) continue;
      if (row.in_cluster === true && k8s && !linked.has(`${row.hostname}\0kubernetes`)) {
        parts.push(svgLine(pt(from), pt(k8s), "rack-logic", false, ""));
      }
      for (const vm of row.vms) {
        if (linked.has(`${row.hostname}\0${vm}`)) continue;
        const to = root.querySelector(`[data-rack-vm="${cssEscape(vm)}"]`);
        if (!to) continue;
        parts.push(svgLine(pt(from), pt(to), "rack-logic", false, ""));
      }
    }
  }
  for (const conn of payload.connections) {
    const aEl = findEnd(root, conn.source);
    const bEl = findEnd(root, conn.target);
    if (!aEl || !bEl || aEl === bEl) continue;
    const planned = conn.kind === "planned";
    parts.push(svgLine(pt(aEl), pt(bEl), planned ? "rack-cable is-planned" : "rack-cable", planned, conn.label));
  }
  svg.innerHTML = parts.join("");
}

function stopOverlay() {
  if (overlayFrame) cancelAnimationFrame(overlayFrame);
  overlayFrame = 0;
  if (overlayLate) clearTimeout(overlayLate);
  overlayLate = 0;
  if (resizeObserver) {
    resizeObserver.disconnect();
    resizeObserver = null;
  }
  if (resizeHandler) {
    window.removeEventListener("resize", resizeHandler);
    resizeHandler = null;
  }
}

function scheduleOverlay() {
  stopOverlay();
  const run = () => {
    const mode = currentMode();
    if (mode === "cloud") drawOverlay(document.getElementById("rack-cloud"), true);
    else if (mode === "flat") drawOverlay(document.getElementById("rack-flat"), false);
  };
  overlayFrame = requestAnimationFrame(() => {
    overlayFrame = 0;
    run();
  });
  overlayLate = setTimeout(run, 80);
  resizeHandler = () => run();
  window.addEventListener("resize", resizeHandler);
  const el = document.getElementById("rack-cloud") || document.getElementById("rack-flat");
  if (el && typeof ResizeObserver !== "undefined") {
    resizeObserver = new ResizeObserver(() => run());
    resizeObserver.observe(el);
  }
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
  return { x: w / 2 + rx * f, y: h / 2 - ry * f, s: f, d: raw, behind: raw < 8 };
}

const QUADS = [
  [0, 1, 2, 3],
  [5, 4, 7, 6],
  [4, 0, 3, 7],
  [1, 5, 6, 2],
  [3, 2, 6, 7],
  [4, 5, 1, 0],
];
const SHADES = [0.55, 0.72, 0.42, 0.86, 1, 0.34];

function corners(box) {
  const { x0, y0, z0, x1, y1, z1 } = box;
  return [
    [x0, y0, z0],
    [x1, y0, z0],
    [x1, y1, z0],
    [x0, y1, z0],
    [x0, y0, z1],
    [x1, y0, z1],
    [x1, y1, z1],
    [x0, y1, z1],
  ];
}

function boxColor(box) {
  if (box.gear) {
    if (box.itemKind === "pdu") return { fill: "rgba(210,153,34,0.86)", stroke: "#fbbf24" };
    if (box.itemKind === "layer") return { fill: "rgba(167,139,250,0.8)", stroke: "#c4b5fd" };
    return { fill: "rgba(125,211,252,0.8)", stroke: "#7dd3fc" };
  }
  if (box.ready) return { fill: "rgba(63,185,80,0.92)", stroke: box.conflict ? "#f85149" : "#b6f79a" };
  return { fill: "rgba(100,116,139,0.9)", stroke: box.conflict ? "#f85149" : "#cbd5e1" };
}

function buildScene() {
  const racks = racksForView();
  const list = racks.length ? racks : [];
  const width = 70;
  const depth = 46;
  const unit = 3.1;
  const gap = 96;
  const boxes = [];
  const shells = [];
  if (!list.length) return { boxes, shells };
  const total = list.length * width + (list.length - 1) * gap;
  let origin = -total / 2 + width / 2;
  for (const rack of list) {
    const heightU = rackHeight(rack);
    const H = heightU * unit;
    const x0 = origin - width / 2;
    const x1 = origin + width / 2;
    const z0 = -depth / 2;
    const z1 = depth / 2;
    shells.push({ name: rack.name || "Rack", x0, y0: 0, z0, x1, y1: H, z1, heightU });
    for (const item of faceItems(rack)) {
      const span = spanOf(item, heightU);
      if (!span) continue;
      const y0 = (span.u - 1) * unit;
      const y1 = y0 + span.h * unit;
      boxes.push({
        kind: "machine",
        name: itemName(item),
        id: item.id != null ? String(item.id) : "",
        gear: isGearKind(item.kind),
        itemKind: String(item.kind || "").toLowerCase(),
        ready: isReady(item),
        conflict: isConflict(item),
        x0: x0 + 7,
        x1: x1 - 7,
        y0,
        y1,
        z0: z0 + 5,
        z1: z1 - 5,
      });
    }
    besideItems(rack).forEach((item, index) => {
      const size = 14;
      const y0 = 6 + index * (size + 7);
      boxes.push({
        kind: "side",
        name: itemName(item),
        id: item.id != null ? String(item.id) : "",
        gear: isGearKind(item.kind),
        itemKind: String(item.kind || "").toLowerCase(),
        ready: isReady(item),
        conflict: isConflict(item),
        x0: x1 + 18,
        x1: x1 + 18 + size,
        y0,
        y1: y0 + size,
        z0: -size / 2,
        z1: size / 2,
      });
    });
    origin += width + gap;
  }
  return { boxes, shells };
}

function fitCamera(scene) {
  let maxH = ROW_U * 3.1;
  let minX = Infinity;
  let maxX = -Infinity;
  for (const shell of scene.shells) {
    maxH = Math.max(maxH, shell.y1);
    minX = Math.min(minX, shell.x0);
    maxX = Math.max(maxX, shell.x1);
  }
  for (const box of scene.boxes) {
    if (box.kind === "side") maxX = Math.max(maxX, box.x1);
  }
  if (!Number.isFinite(minX)) {
    minX = -40;
    maxX = 40;
  }
  cam.yaw = 0.62;
  cam.pitch = 0.36;
  cam.tx = (minX + maxX) / 2;
  cam.ty = maxH * 0.42;
  cam.tz = 0;
  cam.dist = Math.max(260, maxH * 2.05, (maxX - minX) * 1.35);
}

function sizeSpace(canvas, ctx) {
  const rect = canvas.getBoundingClientRect();
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = Math.max(10, Math.floor(rect.width));
  const h = Math.max(10, Math.floor(rect.height));
  const pw = Math.floor(w * dpr);
  const ph = Math.floor(h * dpr);
  if (canvas.width !== pw) canvas.width = pw;
  if (canvas.height !== ph) canvas.height = ph;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { w, h };
}

function drawShell(ctx, shell, w, h) {
  const c = corners(shell);
  const edges = [
    [0, 1],
    [1, 2],
    [2, 3],
    [3, 0],
    [4, 5],
    [5, 6],
    [6, 7],
    [7, 4],
    [0, 4],
    [1, 5],
    [2, 6],
    [3, 7],
  ];
  ctx.beginPath();
  ctx.strokeStyle = "rgba(141,240,106,0.55)";
  ctx.lineWidth = 1.25;
  for (const [a, b] of edges) {
    const pa = project(c[a][0], c[a][1], c[a][2], w, h);
    const pb = project(c[b][0], c[b][1], c[b][2], w, h);
    if (!pa || !pb || pa.behind || pb.behind) continue;
    ctx.moveTo(pa.x, pa.y);
    ctx.lineTo(pb.x, pb.y);
  }
  ctx.stroke();
  const foot = project(shell.x0, 0, shell.z1, w, h);
  if (foot && !foot.behind) {
    ctx.fillStyle = "#7e9b86";
    ctx.font = "11px ui-sans-serif, system-ui";
    ctx.fillText("U1", foot.x + 4, foot.y + 12);
  }
  const cap = project((shell.x0 + shell.x1) / 2, shell.y1, 0, w, h);
  if (cap && !cap.behind && shell.name) {
    ctx.fillStyle = "#eafaef";
    ctx.font = "600 12px ui-sans-serif, system-ui";
    ctx.fillText(shell.name, cap.x + 6, cap.y);
  }
}

function drawBoxes(ctx, boxes, w, h) {
  const faces = [];
  for (const box of boxes) {
    const cols = boxColor(box);
    const c = corners(box);
    QUADS.forEach((quad, index) => {
      const pts = quad.map((idx) => project(c[idx][0], c[idx][1], c[idx][2], w, h));
      if (pts.some((p) => !p || p.behind)) return;
      const d = pts.reduce((sum, p) => sum + p.d, 0) / pts.length;
      faces.push({ pts, d, shade: SHADES[index], cols, wide: box.conflict });
    });
  }
  faces.sort((a, b) => b.d - a.d);
  for (const face of faces) {
    ctx.beginPath();
    ctx.moveTo(face.pts[0].x, face.pts[0].y);
    for (let i = 1; i < face.pts.length; i += 1) ctx.lineTo(face.pts[i].x, face.pts[i].y);
    ctx.closePath();
    ctx.globalAlpha = 0.5 + face.shade * 0.5;
    ctx.fillStyle = face.cols.fill;
    ctx.fill();
    ctx.globalAlpha = 1;
    ctx.strokeStyle = face.cols.stroke;
    ctx.lineWidth = face.wide ? 2 : 1;
    ctx.stroke();
  }
}

function boxCenter(box) {
  return [(box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2, (box.z0 + box.z1) / 2];
}

function findBox(boxes, name) {
  const key = String(name || "");
  if (!key) return null;
  return boxes.find((box) => box.name === key || box.id === key) || null;
}

function drawSpaceCables(ctx, boxes, w, h) {
  ctx.lineWidth = 1.6;
  for (const conn of payload.connections) {
    const a = findBox(boxes, conn.source);
    const b = findBox(boxes, conn.target);
    if (!a || !b || a === b) continue;
    const ac = boxCenter(a);
    const bc = boxCenter(b);
    const pa = project(ac[0], ac[1], ac[2], w, h);
    const pb = project(bc[0], bc[1], bc[2], w, h);
    if (!pa || !pb || pa.behind || pb.behind) continue;
    ctx.beginPath();
    ctx.strokeStyle = conn.kind === "planned" ? "rgba(210,153,34,0.95)" : "rgba(141,240,106,0.95)";
    ctx.setLineDash(conn.kind === "planned" ? [7, 5] : []);
    ctx.moveTo(pa.x, pa.y);
    ctx.lineTo(pb.x, pb.y);
    ctx.stroke();
  }
  ctx.setLineDash([]);
}

function drawLabels(ctx, boxes, w, h) {
  ctx.font = "11px ui-sans-serif, system-ui";
  ctx.fillStyle = "#eafaef";
  const used = [];
  for (const box of boxes) {
    if (!box.name) continue;
    const c = boxCenter(box);
    const p = project(c[0], c[1], c[2], w, h);
    if (!p || p.behind || p.s < 0.7) continue;
    const near = used.some((pt) => Math.hypot(pt.x - p.x, pt.y - p.y) < 14);
    if (near) continue;
    used.push(p);
    ctx.fillText(box.name.slice(0, 28), p.x + 8, p.y - 6);
  }
}

function drawSpace() {
  const canvas = document.getElementById("rack-space");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  const { w, h } = sizeSpace(canvas, ctx);
  if (w < 12 || h < 12) return;
  const scene = buildScene();
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "#070e09";
  ctx.fillRect(0, 0, w, h);
  for (const shell of scene.shells) drawShell(ctx, shell, w, h);
  drawBoxes(ctx, scene.boxes, w, h);
  drawSpaceCables(ctx, scene.boxes, w, h);
  drawLabels(ctx, scene.boxes, w, h);
}

function onPointerDown(e) {
  if (e.button !== 0) return;
  dragging = true;
  cam.user = true;
  lastPtr = { x: e.clientX, y: e.clientY };
  try {
    e.currentTarget.setPointerCapture(e.pointerId);
  } catch {
    /* ignore */
  }
  e.currentTarget.classList.add("is-dragging");
}

function onPointerMove(e) {
  if (!dragging) return;
  const dx = e.clientX - lastPtr.x;
  const dy = e.clientY - lastPtr.y;
  lastPtr = { x: e.clientX, y: e.clientY };
  cam.yaw += dx * 0.008;
  cam.pitch = Math.max(-1.4, Math.min(1.4, cam.pitch - dy * 0.008));
}

function onPointerUp(e) {
  dragging = false;
  if (e.currentTarget && e.currentTarget.classList) e.currentTarget.classList.remove("is-dragging");
}

function onWheel(e) {
  e.preventDefault();
  cam.user = true;
  cam.dist = Math.max(90, Math.min(2200, cam.dist * (e.deltaY > 0 ? 1.08 : 0.92)));
}

function unbindSpace() {
  for (const [el, type, fn, opts] of spaceListeners) {
    try {
      el.removeEventListener(type, fn, opts);
    } catch {
      /* ignore */
    }
  }
  spaceListeners = [];
}

function bindSpace(canvas) {
  unbindSpace();
  const add = (type, fn, opts) => {
    canvas.addEventListener(type, fn, opts);
    spaceListeners.push([canvas, type, fn, opts]);
  };
  add("pointerdown", onPointerDown);
  add("pointermove", onPointerMove);
  add("pointerup", onPointerUp);
  add("pointercancel", onPointerUp);
  add("wheel", onWheel, { passive: false });
  add("contextmenu", (e) => e.preventDefault());
}

function startSpace() {
  const canvas = document.getElementById("rack-space");
  if (!canvas) return;
  spaceOn = true;
  bindSpace(canvas);
  if (!cam.user) fitCamera(buildScene());
  const loop = () => {
    if (!spaceOn) return;
    drawSpace();
    raf = requestAnimationFrame(loop);
  };
  if (raf) cancelAnimationFrame(raf);
  raf = requestAnimationFrame(loop);
}

function stopSpace() {
  spaceOn = false;
  dragging = false;
  if (raf) cancelAnimationFrame(raf);
  raf = 0;
  unbindSpace();
}

export function wireRackCard(getId) {
  getEnvId = typeof getId === "function" ? getId : () => "";
  const card = document.getElementById("rack-card");
  if (!card || card.dataset.rackWired === "1") return;
  card.dataset.rackWired = "1";
  card.addEventListener("click", onCardClick);
  card.addEventListener("keydown", onCardKey);
  card.addEventListener("submit", onCardSubmit);
}

export async function loadRackCard(envId) {
  const view = document.getElementById("rack-view");
  const msg = document.getElementById("rack-msg");
  if (!view) return;
  const gen = ++loadGen;
  activeEnvId = envId || "";
  if (!envId) {
    payload = emptyPayload();
    if (msg) msg.textContent = "";
    view.innerHTML = `<div class="muted">Select an environment.</div>`;
    return;
  }
  if (msg) msg.textContent = "Loading…";
  try {
    const data = await api(rackApi(envId));
    if (gen !== loadGen) return;
    payload = normalize(data);
  } catch (err) {
    if (gen !== loadGen) return;
    payload = emptyPayload();
    if (msg) msg.textContent = "";
    stopSpace();
    stopOverlay();
    view.innerHTML = `<div class="error">${esc(err.message || "Rack could not be loaded.")}</div>`;
    return;
  }
  if (msg) msg.textContent = "";
  paint();
}

export function destroyRackCard() {
  loadGen += 1;
  stopSpace();
  stopOverlay();
  closePlace();
  cam.user = false;
  const card = document.getElementById("rack-card");
  if (card) {
    card.removeEventListener("click", onCardClick);
    card.removeEventListener("keydown", onCardKey);
    card.removeEventListener("submit", onCardSubmit);
    delete card.dataset.rackWired;
  }
  payload = emptyPayload();
  logicalMap = new Map();
  activeEnvId = "";
  saving = false;
}
