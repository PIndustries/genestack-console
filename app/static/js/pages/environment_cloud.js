// pages/environment_cloud.js — first-class OpenStack control plane (Horizon in-console).
import { api, esc, toast } from "../api.js";
import { canAdmin, canRun } from "../store.js";

const PANELS = [
  ["instances", "Instances"],
  ["images", "Images"],
  ["volumes", "Volumes"],
  ["network", "Network"],
  ["access", "Access"],
  ["identity", "Identity"],
  ["quotas", "Quotas"],
  ["services", "Services"],
];
const REFRESH_MS = 20000;
const FETCH_TIMEOUT = 25000;

let activeEnvId = "";
let envIdGetter = null;
let cache = null;
let panel = "instances";
let inflight = null;
let inflightKey = "";
let refreshTimer = null;
let filter = "";
let selectedId = "";
let selectedSgId = "";
let loading = false;
let lastError = "";
let consoleEscBound = false;

function asList(v) {
  return Array.isArray(v) ? v : [];
}

function unavail(note) {
  return `<div class="muted">Unavailable${note ? " — " + esc(note) : ""}.</div>`;
}

function pill(status) {
  const s = String(status || "").toLowerCase();
  if (["active", "available", "running", "up", "enabled", "true", "in-use"].includes(s)) {
    return `<span class="pill ok">${esc(status)}</span>`;
  }
  if (["error", "down", "deleted", "false", "error_deleting"].includes(s)) {
    return `<span class="pill bad">${esc(status)}</span>`;
  }
  if (!status && status !== false) return '<span class="muted">—</span>';
  return `<span class="pill warn">${esc(status)}</span>`;
}

function sourcePillHtml(data) {
  if (!data || data.available === false) {
    return '<span class="pill bad">unavailable</span>';
  }
  if (data.source === "openstack-api") {
    return '<span class="pill ok">OpenStack API</span>';
  }
  return '<span class="pill warn">CLI fallback</span>';
}

function addrs(v) {
  const a = v && v.addresses;
  if (!a) return "—";
  if (typeof a === "string") return a;
  if (Array.isArray(a)) return a.join(", ") || "—";
  const bits = [];
  for (const [net, ips] of Object.entries(a)) {
    const list = Array.isArray(ips)
      ? ips
          .map((x) => (x && typeof x === "object" ? x.addr || x.ip || JSON.stringify(x) : String(x)))
          .join(", ")
      : String(ips);
    bits.push(`${net}: ${list}`);
  }
  return bits.join(" · ") || "—";
}

function options(items, valueKey, labelFn) {
  const rows = asList(items);
  return rows
    .map((it) => {
      const info = it && typeof it === "object" ? it : {};
      const val = info[valueKey] || info.name || info.id;
      if (!val) return "";
      const label = labelFn ? labelFn(info) : info.name || val;
      return `<option value="${esc(val)}">${esc(label)}</option>`;
    })
    .join("");
}

function ctaBtn(panelId, label) {
  return `<button type="button" class="secondary btn-sm" data-os-goto="${esc(panelId)}">${esc(label)}</button>`;
}

function emptyNote(kind, extra, cta) {
  const action = cta
    ? `<div class="os-empty-cta">${ctaBtn(cta.panel, cta.label)}</div>`
    : "";
  return `<div class="os-empty"><div>No ${esc(kind)}.${extra ? " " + extra : ""}</div>${action}</div>`;
}

function matchesFilter(info) {
  if (!filter) return true;
  const blob = JSON.stringify(info).toLowerCase();
  return blob.includes(filter.toLowerCase());
}

function statsHtml(data, { skeleton = false } = {}) {
  const n = (k) => asList(data && data[k]).length;
  const volGi = asList(data && data.volumes).reduce((s, v) => s + (Number(v && v.size) || 0), 0);
  const tiles = skeleton
    ? [
        ["—", "Instances"],
        ["—", "Images"],
        ["—", "Flavors"],
        ["—", "Volumes"],
        ["—", "Networks"],
        ["—", "Projects"],
      ]
    : [
        [n("servers"), "Instances"],
        [n("images"), "Images"],
        [n("flavors"), "Flavors"],
        [`${volGi} GiB`, "Volumes"],
        [n("networks"), "Networks"],
        [n("projects"), "Projects"],
      ];
  return `<div class="os-stats">${tiles
    .map(
      ([v, l]) =>
        `<div class="os-stat${skeleton ? " os-stat-skel" : ""}"><div class="os-stat-n">${
          skeleton ? '<span class="os-sk-bar"></span>' : esc(v)
        }</div><div class="os-stat-l">${esc(l)}</div></div>`
    )
    .join("")}</div>`;
}

function skeletonHtml() {
  return `<div class="os-skel">
    <div class="muted">Talking to OpenStack…</div>
    <div class="os-skel-rows">
      <div class="os-sk-bar"></div>
      <div class="os-sk-bar"></div>
      <div class="os-sk-bar"></div>
      <div class="os-sk-bar"></div>
    </div>
  </div>`;
}

function errorBannerHtml(err, { stale = false } = {}) {
  if (!err) return "";
  return `<div class="os-banner">
    <div>
      <div class="error">${esc(err)}</div>
      ${stale ? '<div class="muted">Showing last cached data.</div>' : ""}
    </div>
    <button type="button" class="secondary btn-sm" data-os-retry>Retry</button>
  </div>`;
}

function launchMissingHtml(data) {
  const nets = asList(data.networks);
  const imgs = asList(data.images);
  const flavs = asList(data.flavors);
  const bits = [];
  if (!nets.length) {
    bits.push(
      `<p>Launch is disabled because this project has no network. Create a tenant network first.</p>${ctaBtn(
        "network",
        "Create a network"
      )}`
    );
  }
  if (!imgs.length) {
    bits.push(`<p>No Glance image is available to boot from.</p>${ctaBtn("images", "View images")}`);
  }
  if (!flavs.length) {
    bits.push("<p>No Nova flavor is available.</p>");
  }
  if (!bits.length) return "";
  return `<div class="os-disabled">${bits.join("")}</div>`;
}

function instancesHtml(data) {
  const rows = asList(data.servers).filter(matchesFilter);
  const run = canRun();
  const admin = canAdmin();
  const nets = asList(data.networks);
  const imgs = asList(data.images);
  const flavs = asList(data.flavors);
  const canLaunch = !!(nets.length && imgs.length && flavs.length);
  let table = emptyNote(
    "instances",
    canLaunch ? "Launch one below." : "Create a network, then launch.",
    !canLaunch && !nets.length ? { panel: "network", label: "Create a network" } : null
  );
  if (rows.length) {
    table = `<table class="os-table">
      <thead><tr><th>Instance</th><th>Status</th><th>Flavor</th><th>Image</th><th>Addresses</th><th>Host</th><th></th></tr></thead>
      <tbody>${rows
        .map((s) => {
          const info = s && typeof s === "object" ? s : {};
          const id = info.id || "";
          const st = String(info.status || "").toUpperCase();
          const btns = [];
          if (run && st === "SHUTOFF") {
            btns.push(
              `<button type="button" class="secondary btn-sm" data-srv-act="start" data-id="${esc(id)}">Start</button>`
            );
          }
          if (run && st === "ACTIVE") {
            btns.push(
              `<button type="button" class="secondary btn-sm" data-srv-act="stop" data-id="${esc(id)}">Stop</button>`
            );
            btns.push(
              `<button type="button" class="secondary btn-sm" data-srv-act="reboot" data-id="${esc(id)}">Reboot</button>`
            );
            btns.push(
              `<button type="button" class="secondary btn-sm" data-srv-act="hard-reboot" data-id="${esc(id)}">Hard reboot</button>`
            );
            btns.push(
              `<button type="button" class="secondary btn-sm" data-srv-console data-id="${esc(id)}">Console</button>`
            );
          }
          if (admin && id) {
            btns.push(
              `<button type="button" class="secondary btn-sm" data-srv-del data-id="${esc(id)}">Delete</button>`
            );
          }
          const sel = id && id === selectedId ? ' class="os-row-sel"' : "";
          return `<tr${sel} data-srv-sel="${esc(id)}">
            <td><strong>${esc(info.name || "?")}</strong><div class="muted os-id"><code>${esc(id)}</code></div></td>
            <td>${pill(info.status)}</td>
            <td class="muted">${esc(info.flavor || "—")}</td>
            <td class="muted">${esc(info.image || "—")}</td>
            <td class="muted">${esc(addrs(info))}</td>
            <td class="muted">${esc(info.host || "—")}</td>
            <td class="os-actions">${btns.join(" ")}</td>
          </tr>`;
        })
        .join("")}</tbody>
    </table>`;
  }
  const keys = asList(data.keypairs);
  const vols = asList(data.volumes).filter(
    (v) => String((v && v.status) || "").toLowerCase() === "available"
  );
  const fips = asList(data.floating_ips).filter((f) => !f.port);
  let form = "";
  if (run) {
    form = `<div class="os-forms">
      <details class="os-form" ${canLaunch ? "open" : ""}>
        <summary>Launch instance</summary>
        ${
          canLaunch
            ? `<form id="os-launch" class="os-grid">
          <label>Name <input name="name" required maxlength="64" placeholder="demo-1"></label>
          <label>Image <select name="image" required>${options(imgs, "id", (i) => i.name || i.id)}</select></label>
          <label>Flavor <select name="flavor" required>${options(
            flavs,
            "id",
            (f) => `${f.name} · ${f.vcpus} vCPU / ${f.ram} MiB / ${f.disk} GiB`
          )}</select></label>
          <label>Network <select name="network" required>${options(nets, "id", (n) => n.name || n.id)}</select></label>
          <label>Key pair <select name="key_name"><option value="">(none)</option>${options(
            keys,
            "name",
            (k) => k.name
          )}</select></label>
          <button type="submit">Launch</button>
        </form>`
            : launchMissingHtml(data)
        }
      </details>
      ${
        rows.length && vols.length
          ? `<details class="os-form">
        <summary>Attach volume</summary>
        <form id="os-attach" class="os-grid">
          <label>Instance <select name="server_id" required>${options(rows, "id", (s) => s.name || s.id)}</select></label>
          <label>Volume <select name="volume_id" required>${options(
            vols,
            "id",
            (v) => `${v.name || v.id} (${v.size} GiB)`
          )}</select></label>
          <button type="submit">Attach</button>
        </form>
      </details>`
          : rows.length && !vols.length && asList(data.volumes).length
            ? `<div class="muted">No available volumes to attach. ${ctaBtn("volumes", "Create a volume")}</div>`
            : ""
      }
      ${
        rows.length && fips.length
          ? `<details class="os-form">
        <summary>Associate floating IP</summary>
        <form id="os-fip-assoc" class="os-grid">
          <label>Instance <select name="server_id" required>${options(rows, "id", (s) => s.name || s.id)}</select></label>
          <label>Address <select name="address" required>${options(fips, "ip", (f) => f.ip)}</select></label>
          <button type="submit">Associate</button>
        </form>
      </details>`
          : rows.length
            ? `<div class="muted">No free floating IPs. ${ctaBtn("access", "Allocate a floating IP")}</div>`
            : ""
      }
    </div>`;
  }
  const detail = selectedHtml(data);
  return table + form + detail;
}

function extraServerActions(info) {
  if (!canRun() || !info || !info.id) return "";
  const st = String(info.status || "").toUpperCase();
  const id = info.id;
  const btns = [];
  const add = (act, label) =>
    btns.push(
      `<button type="button" class="secondary btn-sm" data-srv-act="${esc(act)}" data-id="${esc(id)}">${esc(label)}</button>`
    );
  if (st === "ACTIVE") {
    add("pause", "Pause");
    add("suspend", "Suspend");
    add("lock", "Lock");
    add("unlock", "Unlock");
    add("rescue", "Rescue");
    add("shelve", "Shelve");
  }
  if (st === "PAUSED") add("unpause", "Unpause");
  if (st === "SUSPENDED") add("resume", "Resume");
  if (st === "RESCUE") add("unrescue", "Unrescue");
  if (st === "SHELVED" || st === "SHELVED_OFFLOADED") add("unshelve", "Unshelve");
  if (st === "VERIFY_RESIZE") {
    add("confirm-resize", "Confirm resize");
    add("revert-resize", "Revert resize");
  }
  return btns.length ? `<div class="os-actions">${btns.join(" ")}</div>` : "";
}

function selectedHtml(data) {
  const rows = asList(data.servers);
  const info = rows.find((s) => s && s.id === selectedId);
  if (!info) return "";
  const flavs = asList(data.flavors);
  const imgs = asList(data.images);
  const sgs = asList(data.security_groups);
  const run = canRun();
  const forms = run
    ? `<div class="os-forms">
        ${extraServerActions(info)}
        <details class="os-form">
          <summary>Resize</summary>
          <form id="os-resize" class="os-grid" data-id="${esc(info.id)}">
            <label>Flavor <select name="flavor" required>${options(
              flavs,
              "id",
              (f) => `${f.name} · ${f.vcpus} vCPU / ${f.ram} MiB`
            )}</select></label>
            <button type="submit">Resize</button>
          </form>
        </details>
        <details class="os-form">
          <summary>Rebuild</summary>
          <form id="os-rebuild" class="os-grid" data-id="${esc(info.id)}">
            <label>Image <select name="image" required>${options(imgs, "id", (i) => i.name || i.id)}</select></label>
            <button type="submit">Rebuild</button>
          </form>
        </details>
        <details class="os-form">
          <summary>Snapshot instance</summary>
          <form id="os-srv-snap" class="os-grid" data-id="${esc(info.id)}">
            <label>Image name <input name="name" required maxlength="81" placeholder="${esc(info.name || "snap")}-snap"></label>
            <button type="submit">Create image</button>
          </form>
        </details>
        ${
          sgs.length
            ? `<details class="os-form">
          <summary>Security groups</summary>
          <form id="os-srv-sg-add" class="os-grid" data-id="${esc(info.id)}">
            <label>Add group <select name="name" required>${options(sgs, "name", (g) => g.name)}</select></label>
            <button type="submit">Add</button>
          </form>
          <form id="os-srv-sg-del" class="os-grid" data-id="${esc(info.id)}">
            <label>Remove group <select name="name" required>${options(sgs, "name", (g) => g.name)}</select></label>
            <button type="submit">Remove</button>
          </form>
        </details>`
            : ""
        }
      </div>`
    : "";
  return `<div class="os-detail">
    <h3 class="lc-title">Instance detail</h3>
    <div class="os-kv">
      <div><span class="k">Name</span> ${esc(info.name || "—")}</div>
      <div><span class="k">ID</span> <code>${esc(info.id || "")}</code></div>
      <div><span class="k">Status</span> ${pill(info.status)}</div>
      <div><span class="k">Power</span> ${esc(info.power_state != null ? String(info.power_state) : "—")}</div>
      <div><span class="k">Flavor</span> ${esc(info.flavor || "—")}</div>
      <div><span class="k">Image</span> ${esc(info.image || "—")}</div>
      <div><span class="k">Host</span> ${esc(info.host || "—")}</div>
      <div><span class="k">Key</span> ${esc(info.key_name || "—")}</div>
      <div><span class="k">Created</span> ${esc(info.created || "—")}</div>
      <div><span class="k">Project</span> <code>${esc(info.project_id || "—")}</code></div>
      <div class="os-kv-wide"><span class="k">Addresses</span> ${esc(addrs(info))}</div>
    </div>
    ${forms}
  </div>`;
}

function imagesHtml(data) {
  const rows = asList(data.images).filter(matchesFilter);
  const flavs = asList(data.flavors).filter(matchesFilter);
  const run = canRun();
  const imgTable = rows.length
    ? `<table class="os-table">
    <thead><tr><th>Image</th><th>Status</th><th>Visibility</th><th>Size</th><th>Id</th><th></th></tr></thead>
    <tbody>${rows
      .map((i) => {
        const info = i && typeof i === "object" ? i : {};
        const sz = info.size != null ? `${Math.round(Number(info.size) / (1024 * 1024))} MiB` : "—";
        const del =
          run && info.id
            ? `<button type="button" class="secondary btn-sm" data-img-del data-id="${esc(info.id)}">Delete</button>`
            : "";
        return `<tr>
          <td><strong>${esc(info.name || "?")}</strong></td>
          <td>${pill(info.status)}</td>
          <td class="muted">${esc(info.visibility || "—")}</td>
          <td class="muted">${esc(sz)}</td>
          <td class="muted"><code>${esc(info.id || "")}</code></td>
          <td class="os-actions">${del}</td>
        </tr>`;
      })
      .join("")}</tbody>
  </table>`
    : emptyNote("images", "Create one from a URL, or snapshot an instance.");
  const flavTable = flavs.length
    ? `<h3 class="lc-title">Flavors</h3><table class="os-table">
      <thead><tr><th>Flavor</th><th>vCPU</th><th>RAM</th><th>Disk</th><th></th></tr></thead>
      <tbody>${flavs
        .map((f) => {
          const info = f && typeof f === "object" ? f : {};
          const del =
            run && info.id
              ? `<button type="button" class="secondary btn-sm" data-flav-del data-id="${esc(info.id)}">Delete</button>`
              : "";
          return `<tr>
            <td><strong>${esc(info.name || info.id || "?")}</strong></td>
            <td class="muted">${esc(info.vcpus != null ? String(info.vcpus) : "—")}</td>
            <td class="muted">${info.ram != null ? esc(info.ram) + " MiB" : "—"}</td>
            <td class="muted">${info.disk != null ? esc(info.disk) + " GiB" : "—"}</td>
            <td class="os-actions">${del}</td>
          </tr>`;
        })
        .join("")}</tbody></table>`
    : emptyNote("flavors", "Create a flavor to launch instances.");
  let forms = "";
  if (run) {
    forms = `<div class="os-forms">
      <details class="os-form" ${rows.length ? "" : "open"}>
        <summary>Create image</summary>
        <form id="os-img" class="os-grid">
          <label>Name <input name="name" required maxlength="81" placeholder="cirros"></label>
          <label>Disk format
            <select name="disk_format">
              <option value="qcow2">qcow2</option>
              <option value="raw">raw</option>
              <option value="iso">iso</option>
              <option value="vmdk">vmdk</option>
            </select>
          </label>
          <label>Visibility
            <select name="visibility">
              <option value="private">private</option>
              <option value="public">public</option>
              <option value="shared">shared</option>
              <option value="community">community</option>
            </select>
          </label>
          <label>Source URL <input name="url" type="url" placeholder="https://…/image.qcow2"></label>
          <button type="submit">Create</button>
        </form>
      </details>
      ${
        rows.length
          ? `<details class="os-form">
        <summary>Update image</summary>
        <form id="os-img-patch" class="os-grid">
          <label>Image <select name="image_id" required>${options(rows, "id", (i) => i.name || i.id)}</select></label>
          <label>Name <input name="name" maxlength="81" placeholder="(unchanged)"></label>
          <label>Visibility
            <select name="visibility">
              <option value="">(unchanged)</option>
              <option value="private">private</option>
              <option value="public">public</option>
              <option value="shared">shared</option>
              <option value="community">community</option>
            </select>
          </label>
          <button type="submit">Save</button>
        </form>
      </details>`
          : ""
      }
      <details class="os-form" ${flavs.length ? "" : "open"}>
        <summary>Create flavor</summary>
        <form id="os-flav" class="os-grid">
          <label>Name <input name="name" required maxlength="64" placeholder="m1.small"></label>
          <label>vCPUs <input name="vcpus" type="number" min="1" max="256" value="1" required></label>
          <label>RAM (MiB) <input name="ram" type="number" min="1" value="1024" required></label>
          <label>Disk (GiB) <input name="disk" type="number" min="0" value="10" required></label>
          <button type="submit">Create</button>
        </form>
      </details>
    </div>`;
  }
  return imgTable + forms + flavTable;
}

function volumesHtml(data) {
  const rows = asList(data.volumes).filter(matchesFilter);
  let table = emptyNote("volumes", "Create a volume to attach to an instance.");
  if (rows.length) {
    table = `<table class="os-table">
      <thead><tr><th>Volume</th><th>Status</th><th>Size</th><th>Attached</th><th></th></tr></thead>
      <tbody>${rows
        .map((v) => {
          const info = v && typeof v === "object" ? v : {};
          const att = asList(info.attachments);
          const attLabel = att.length
            ? att
                .map((a) =>
                  a && typeof a === "object" ? a.server_id || a.serverId || JSON.stringify(a) : String(a)
                )
                .join(", ")
            : "—";
          const acts = [];
          if (canRun() && att.length) {
            const sid = att[0] && (att[0].server_id || att[0].serverId);
            if (sid) {
              acts.push(
                `<button type="button" class="secondary btn-sm" data-vol-detach data-id="${esc(
                  info.id || ""
                )}" data-server="${esc(sid)}">Detach</button>`
              );
            }
          }
          if (canRun() && info.id) {
            acts.push(
              `<button type="button" class="secondary btn-sm" data-vol-snap data-id="${esc(info.id)}">Snapshot</button>`
            );
            acts.push(
              `<button type="button" class="secondary btn-sm" data-vol-del data-id="${esc(info.id || "")}">Delete</button>`
            );
          }
          return `<tr>
            <td><strong>${esc(info.name || info.id || "?")}</strong></td>
            <td>${pill(info.status)}</td>
            <td class="muted">${info.size != null ? esc(info.size) + " GiB" : "—"}</td>
            <td class="muted">${esc(attLabel)}</td>
            <td class="os-actions">${acts.join(" ")}</td>
          </tr>`;
        })
        .join("")}</tbody>
    </table>`;
  }
  const snaps = asList(data.volume_snapshots).filter(matchesFilter);
  const snapTable = snaps.length
    ? `<h3 class="lc-title">Snapshots</h3><table class="os-table">
      <thead><tr><th>Snapshot</th><th>Status</th><th>Size</th><th>Volume</th><th></th></tr></thead>
      <tbody>${snaps
        .map((s) => {
          const info = s && typeof s === "object" ? s : {};
          const del =
            canRun() && info.id
              ? `<button type="button" class="secondary btn-sm" data-snap-del data-id="${esc(info.id)}">Delete</button>`
              : "";
          return `<tr>
            <td><strong>${esc(info.name || info.id || "?")}</strong></td>
            <td>${pill(info.status)}</td>
            <td class="muted">${info.size != null ? esc(info.size) + " GiB" : "—"}</td>
            <td class="muted"><code>${esc(info.volume_id || "—")}</code></td>
            <td class="os-actions">${del}</td>
          </tr>`;
        })
        .join("")}</tbody></table>`
    : "";
  const form = canRun()
    ? `<div class="os-forms">
      <details class="os-form" open>
        <summary>Create volume</summary>
        <form id="os-vol" class="os-grid">
          <label>Name <input name="name" required maxlength="64" placeholder="data-1"></label>
          <label>Size (GiB) <input name="size" type="number" min="1" max="16384" value="10" required></label>
          <button type="submit">Create</button>
        </form>
      </details>
      ${
        rows.length
          ? `<details class="os-form">
        <summary>Extend volume</summary>
        <form id="os-vol-ext" class="os-grid">
          <label>Volume <select name="volume_id" required>${options(
            rows,
            "id",
            (v) => `${v.name || v.id} (${v.size} GiB)`
          )}</select></label>
          <label>New size (GiB) <input name="size" type="number" min="1" max="16384" required></label>
          <button type="submit">Extend</button>
        </form>
      </details>
      <details class="os-form">
        <summary>Snapshot volume</summary>
        <form id="os-vol-snap" class="os-grid">
          <label>Volume <select name="volume_id" required>${options(rows, "id", (v) => v.name || v.id)}</select></label>
          <label>Name <input name="name" required maxlength="64" placeholder="data-1-snap"></label>
          <button type="submit">Snapshot</button>
        </form>
      </details>`
          : ""
      }
    </div>`
    : "";
  return table + snapTable + form;
}

function netName(data, id) {
  const hit = asList(data && data.networks).find((n) => n && (n.id === id || n.name === id));
  if (hit) return hit.name || id;
  return id || "—";
}

function gatewayLabel(data, router) {
  const gw = router && router.external_gateway;
  if (!gw) return "—";
  if (typeof gw === "string") return netName(data, gw);
  const nid = gw.network_id || gw.network || "";
  return nid ? netName(data, nid) : "—";
}

function networkHtml(data) {
  const nets = asList(data.networks).filter(matchesFilter);
  const subs = asList(data.subnets);
  const routers = asList(data.routers);
  const extNets = asList(data.networks).filter((n) => n && n.external);
  const run = canRun();
  const netTable = nets.length
    ? `<table class="os-table">
      <thead><tr><th>Network</th><th>Status</th><th>External</th><th>Subnets</th><th></th></tr></thead>
      <tbody>${nets
        .map((n) => {
          const info = n && typeof n === "object" ? n : {};
          const sn = Array.isArray(info.subnets) ? info.subnets.join(", ") : info.subnets || "—";
          const del =
            run && info.id
              ? `<button type="button" class="secondary btn-sm" data-net-del data-id="${esc(info.id)}">Delete</button>`
              : "";
          return `<tr>
            <td><strong>${esc(info.name || "?")}</strong><div class="muted os-id"><code>${esc(info.id || "")}</code></div></td>
            <td>${pill(info.status)}</td>
            <td>${info.external ? '<span class="pill ok">external</span>' : '<span class="muted">tenant</span>'}</td>
            <td class="muted">${esc(sn)}</td>
            <td class="os-actions">${del}</td>
          </tr>`;
        })
        .join("")}</tbody>
    </table>`
    : emptyNote("networks", "Create a tenant network to launch instances.");
  const subTable = subs.length
    ? `<h3 class="lc-title">Subnets</h3><table class="os-table">
      <thead><tr><th>Name</th><th>CIDR</th><th>Network</th><th></th></tr></thead>
      <tbody>${subs
        .map((s) => {
          const info = s && typeof s === "object" ? s : {};
          const del =
            run && info.id
              ? `<button type="button" class="secondary btn-sm" data-subnet-del data-id="${esc(info.id)}">Delete</button>`
              : "";
          return `<tr><td>${esc(info.name || "—")}</td><td><code>${esc(info.cidr || "—")}</code></td><td class="muted">${esc(
            info.network || "—"
          )}</td><td class="os-actions">${del}</td></tr>`;
        })
        .join("")}</tbody></table>`
    : "";
  const rTable = routers.length
    ? `<h3 class="lc-title">Routers</h3><table class="os-table">
      <thead><tr><th>Router</th><th>Status</th><th>Gateway network</th><th></th></tr></thead>
      <tbody>${routers
        .map((r) => {
          const info = r && typeof r === "object" ? r : {};
          const acts = [];
          if (run && info.id) {
            acts.push(
              `<button type="button" class="secondary btn-sm" data-router-del data-id="${esc(info.id)}">Delete</button>`
            );
          }
          return `<tr>
            <td><strong>${esc(info.name || "?")}</strong><div class="muted os-id"><code>${esc(info.id || "")}</code></div></td>
            <td>${pill(info.status)}</td>
            <td class="muted">${esc(gatewayLabel(data, info))}</td>
            <td class="os-actions">${acts.join(" ")}</td>
          </tr>`;
        })
        .join("")}</tbody></table>`
    : emptyNote("routers", "Create a router with an external gateway.");
  let form = "";
  if (canRun()) {
    form = `<div class="os-forms">
      <details class="os-form" ${nets.length ? "" : "open"}>
        <summary>Create network</summary>
        <form id="os-net" class="os-grid">
          <label>Name <input name="name" required maxlength="64" placeholder="demo-net"></label>
          <label>CIDR <input name="cidr" required placeholder="10.0.0.0/24" value="10.0.0.0/24"></label>
          <button type="submit">Create</button>
        </form>
      </details>
      <details class="os-form">
        <summary>Create external network</summary>
        <form id="os-net-ext" class="os-grid">
          <label>Name <input name="name" required maxlength="64" placeholder="public"></label>
          <label>CIDR (optional) <input name="cidr" placeholder="203.0.113.0/24"></label>
          <button type="submit">Create</button>
        </form>
      </details>
      <details class="os-form" ${routers.length ? "" : "open"}>
        <summary>Create router</summary>
        ${
          extNets.length
            ? `<form id="os-router" class="os-grid">
          <label>Name <input name="name" required maxlength="64" placeholder="demo-router"></label>
          <label>External network <select name="external_network" required>${options(
            extNets,
            "id",
            (n) => n.name || n.id
          )}</select></label>
          <button type="submit">Create</button>
        </form>`
            : `<div class="os-disabled"><p>Create an external network first, then attach it as the router gateway.</p></div>`
        }
      </details>
      ${
        nets.length
          ? `<details class="os-form">
        <summary>Create subnet</summary>
        <form id="os-subnet" class="os-grid">
          <label>Name <input name="name" maxlength="64" placeholder="demo-subnet"></label>
          <label>Network <select name="network" required>${options(nets, "id", (n) => n.name || n.id)}</select></label>
          <label>CIDR <input name="cidr" required placeholder="10.0.0.0/24" value="10.0.0.0/24"></label>
          <button type="submit">Create</button>
        </form>
      </details>`
          : ""
      }
      ${
        routers.length && subs.length
          ? `<details class="os-form">
        <summary>Add interface</summary>
        <form id="os-router-if" class="os-grid">
          <label>Router <select name="router_id" required>${options(routers, "id", (r) => r.name || r.id)}</select></label>
          <label>Subnet <select name="subnet_id" required>${options(
            subs,
            "id",
            (s) => `${s.name || s.id} ${s.cidr ? "(" + s.cidr + ")" : ""}`
          )}</select></label>
          <button type="submit">Add interface</button>
        </form>
      </details>
      <details class="os-form">
        <summary>Remove interface</summary>
        <form id="os-router-if-del" class="os-grid">
          <label>Router <select name="router_id" required>${options(routers, "id", (r) => r.name || r.id)}</select></label>
          <label>Subnet <select name="subnet_id" required>${options(
            subs,
            "id",
            (s) => `${s.name || s.id} ${s.cidr ? "(" + s.cidr + ")" : ""}`
          )}</select></label>
          <button type="submit">Remove</button>
        </form>
      </details>`
          : routers.length
            ? `<div class="muted">Add a subnet, then attach it as a router interface.</div>`
            : ""
      }
    </div>`;
  }
  return netTable + subTable + rTable + form;
}

function rulePorts(rule) {
  const lo = rule && rule.port_range_min;
  const hi = rule && rule.port_range_max;
  if (lo == null && hi == null) return "any";
  if (lo != null && hi != null && String(lo) !== String(hi)) return `${lo}–${hi}`;
  return String(lo != null ? lo : hi);
}

function sgRulesHtml(group) {
  const rules = asList(group && group.rules);
  const run = canRun();
  const gid = (group && group.id) || "";
  let table = emptyNote("rules", "Add a rule below.");
  if (rules.length) {
    table = `<table class="os-table">
      <thead><tr><th>Direction</th><th>Ether</th><th>Proto</th><th>Ports</th><th>Remote</th><th></th></tr></thead>
      <tbody>${rules
        .map((r) => {
          const info = r && typeof r === "object" ? r : {};
          const remote = info.remote_ip_prefix || info.remote_group_id || "—";
          const del =
            run && info.id
              ? `<button type="button" class="secondary btn-sm" data-sg-rule-del data-sg="${esc(gid)}" data-id="${esc(
                  info.id
                )}">Delete</button>`
              : "";
          return `<tr>
            <td>${esc(info.direction || "—")}</td>
            <td class="muted">${esc(info.ethertype || "—")}</td>
            <td class="muted">${esc(info.protocol || "any")}</td>
            <td class="muted">${esc(rulePorts(info))}</td>
            <td class="muted">${esc(remote)}</td>
            <td class="os-actions">${del}</td>
          </tr>`;
        })
        .join("")}</tbody>
    </table>`;
  }
  const form = run
    ? `<details class="os-form" open>
      <summary>Add rule</summary>
      <form id="os-sg-rule" class="os-grid" data-sg="${esc(gid)}">
        <input type="hidden" name="sg_id" value="${esc(gid)}">
        <label>Direction
          <select name="direction" required>
            <option value="ingress">ingress</option>
            <option value="egress">egress</option>
          </select>
        </label>
        <label>Protocol
          <select name="protocol">
            <option value="">any</option>
            <option value="tcp">tcp</option>
            <option value="udp">udp</option>
            <option value="icmp">icmp</option>
          </select>
        </label>
        <label>Port min <input name="port_range_min" type="number" min="0" max="65535" placeholder="22"></label>
        <label>Port max <input name="port_range_max" type="number" min="0" max="65535" placeholder="22"></label>
        <label>Remote CIDR <input name="remote_ip_prefix" placeholder="0.0.0.0/0"></label>
        <button type="submit">Add rule</button>
      </form>
    </details>`
    : "";
  return `<div class="os-detail">
    <h3 class="lc-title">Rules — ${esc((group && group.name) || gid || "group")}</h3>
    ${table}${form}
  </div>`;
}

function accessHtml(data) {
  const fips = asList(data.floating_ips).filter(matchesFilter);
  const sgs = asList(data.security_groups).filter(matchesFilter);
  const keys = asList(data.keypairs).filter(matchesFilter);
  const extNets = asList(data.networks).filter((n) => n && n.external);
  const run = canRun();
  const fipTable = fips.length
    ? `<table class="os-table">
      <thead><tr><th>Floating IP</th><th>Status</th><th>Fixed</th><th>Port</th><th></th></tr></thead>
      <tbody>${fips
        .map((f) => {
          const info = f && typeof f === "object" ? f : {};
          const acts = [];
          if (run && info.port && (info.ip || info.id)) {
            acts.push(
              `<button type="button" class="secondary btn-sm" data-fip-unassoc data-address="${esc(
                info.ip || ""
              )}" data-id="${esc(info.id || "")}">Disassociate</button>`
            );
          }
          if (run && info.id) {
            acts.push(
              `<button type="button" class="secondary btn-sm" data-fip-del data-id="${esc(info.id)}">Release</button>`
            );
          }
          return `<tr><td><code>${esc(info.ip || info.id || "—")}</code></td><td>${pill(info.status)}</td><td class="muted">${esc(
            info.fixed_ip || "—"
          )}</td><td class="muted">${esc(info.port || "—")}</td><td class="os-actions">${acts.join(" ")}</td></tr>`;
        })
        .join("")}</tbody></table>`
    : emptyNote(
        "floating IPs",
        extNets.length ? "Allocate one from an external network." : "Need an external network first."
      );
  let sg = emptyNote("security groups");
  if (sgs.length) {
    sg = `<h3 class="lc-title">Security groups</h3>
      <table class="os-table">
        <thead><tr><th>Group</th><th>Description</th><th>Rules</th><th></th></tr></thead>
        <tbody>${sgs
          .map((g) => {
            const info = g && typeof g === "object" ? g : {};
            const id = info.id || "";
            const n = asList(info.rules).length;
            const sel = id && id === selectedSgId ? ' class="os-row-sel"' : "";
            const del =
              run && id
                ? `<button type="button" class="secondary btn-sm" data-sg-del data-id="${esc(id)}">Delete</button>`
                : "";
            return `<tr${sel} data-sg-sel="${esc(id)}">
              <td><strong>${esc(info.name || "?")}</strong><div class="muted os-id"><code>${esc(id)}</code></div></td>
              <td class="muted">${esc(info.description || "—")}</td>
              <td class="muted">${esc(String(n))}</td>
              <td class="os-actions">${del}</td>
            </tr>`;
          })
          .join("")}</tbody>
      </table>`;
    const selected = sgs.find((g) => g && g.id === selectedSgId);
    if (selected) sg += sgRulesHtml(selected);
  }
  const kp = keys.length
    ? `<h3 class="lc-title">Key pairs</h3><table class="os-table">
      <thead><tr><th>Name</th><th>Fingerprint</th><th></th></tr></thead>
      <tbody>${keys
        .map((k) => {
          const info = k && typeof k === "object" ? k : {};
          const del =
            run && info.name
              ? `<button type="button" class="secondary btn-sm" data-kp-del data-name="${esc(info.name)}">Delete</button>`
              : "";
          return `<tr><td><strong>${esc(info.name || "?")}</strong></td><td class="muted"><code>${esc(
            info.fingerprint || "—"
          )}</code></td><td class="os-actions">${del}</td></tr>`;
        })
        .join("")}</tbody></table>`
    : emptyNote("key pairs");
  let form = "";
  if (run) {
    form = `<div class="os-forms">
      ${
        extNets.length
          ? `<details class="os-form" ${fips.length ? "" : "open"}>
        <summary>Allocate floating IP</summary>
        <form id="os-fip" class="os-grid">
          <label>External network <select name="network" required>${options(extNets, "id", (n) => n.name || n.id)}</select></label>
          <button type="submit">Allocate</button>
        </form>
      </details>`
          : `<div class="os-disabled"><p>Allocate a floating IP once an external network exists.</p></div>`
      }
      <details class="os-form">
        <summary>Create security group</summary>
        <form id="os-sg" class="os-grid">
          <label>Name <input name="name" required maxlength="64" placeholder="web"></label>
          <label>Description <input name="description" maxlength="255" placeholder="HTTP and SSH"></label>
          <button type="submit">Create</button>
        </form>
      </details>
      <details class="os-form">
        <summary>Create key pair</summary>
        <form id="os-kp" class="os-grid">
          <label>Name <input name="name" required maxlength="64" placeholder="laptop"></label>
          <label>Public key (optional) <textarea name="public_key" rows="3" placeholder="ssh-ed25519 AAAA… (leave empty to generate)"></textarea></label>
          <button type="submit">Create</button>
        </form>
      </details>
    </div>`;
  }
  return fipTable + form + sg + kp;
}

function quotaVal(v) {
  if (v == null || v === "") return "";
  return String(v);
}

function quotasHtml(data) {
  const q = data && data.quotas && typeof data.quotas === "object" ? data.quotas : {};
  const compute = q.compute && typeof q.compute === "object" ? q.compute : {};
  const network = q.network && typeof q.network === "object" ? q.network : {};
  const tiles = [
    [compute.instances, "Instances"],
    [compute.cores, "vCPU"],
    [compute.ram, "RAM MiB"],
    [network.network, "Networks"],
    [network.subnet, "Subnets"],
    [network.router, "Routers"],
    [network.floatingip, "Floating IPs"],
    [network.security_group, "Secgroups"],
    [network.port, "Ports"],
  ];
  const hasAny = tiles.some(([v]) => v != null && v !== "");
  const grid = hasAny
    ? `<div class="os-stats">${tiles
        .map(
          ([v, l]) =>
            `<div class="os-stat"><div class="os-stat-n">${esc(v != null && v !== "" ? v : "—")}</div><div class="os-stat-l">${esc(
              l
            )}</div></div>`
        )
        .join("")}</div>`
    : emptyNote("quotas", data.quotas_error || "OpenStack quota API did not return values.");
  const form = canRun()
    ? `<details class="os-form" ${hasAny ? "" : "open"}>
      <summary>Set quotas</summary>
      <form id="os-quotas" class="os-grid">
        <label>Instances <input name="instances" type="number" min="-1" value="${esc(quotaVal(compute.instances))}"></label>
        <label>vCPU (cores) <input name="cores" type="number" min="-1" value="${esc(quotaVal(compute.cores))}"></label>
        <label>RAM (MiB) <input name="ram" type="number" min="-1" value="${esc(quotaVal(compute.ram))}"></label>
        <label>Networks <input name="network" type="number" min="-1" value="${esc(quotaVal(network.network))}"></label>
        <label>Subnets <input name="subnet" type="number" min="-1" value="${esc(quotaVal(network.subnet))}"></label>
        <label>Routers <input name="router" type="number" min="-1" value="${esc(quotaVal(network.router))}"></label>
        <label>Floating IPs <input name="floatingip" type="number" min="-1" value="${esc(quotaVal(network.floatingip))}"></label>
        <label>Security groups <input name="security_group" type="number" min="-1" value="${esc(
          quotaVal(network.security_group)
        )}"></label>
        <label>Ports <input name="port" type="number" min="-1" value="${esc(quotaVal(network.port))}"></label>
        <button type="submit">Update quotas</button>
      </form>
    </details>`
    : "";
  return grid + form;
}

function identityHtml(data) {
  const projects = asList(data.projects).filter(matchesFilter);
  const users = asList(data.users).filter(matchesFilter);
  const run = canRun();
  const p = projects.length
    ? `<table class="os-table">
      <thead><tr><th>Project</th><th>Enabled</th><th>Id</th><th></th></tr></thead>
      <tbody>${projects
        .map((x) => {
          const info = x && typeof x === "object" ? x : {};
          const toggle =
            run && info.id
              ? `<button type="button" class="secondary btn-sm" data-proj-en data-id="${esc(info.id)}" data-enabled="${
                  info.enabled ? "0" : "1"
                }">${info.enabled ? "Disable" : "Enable"}</button>`
              : "";
          return `<tr><td><strong>${esc(info.name || "?")}</strong></td><td>${pill(info.enabled)}</td><td class="muted"><code>${esc(
            info.id || ""
          )}</code></td><td class="os-actions">${toggle}</td></tr>`;
        })
        .join("")}</tbody></table>`
    : emptyNote("projects");
  const u = users.length
    ? `<h3 class="lc-title">Users</h3><table class="os-table">
      <thead><tr><th>User</th><th>Enabled</th><th>Id</th><th></th></tr></thead>
      <tbody>${users
        .map((x) => {
          const info = x && typeof x === "object" ? x : {};
          const acts = [];
          if (run && info.id) {
            acts.push(
              `<button type="button" class="secondary btn-sm" data-user-en data-id="${esc(info.id)}" data-enabled="${
                info.enabled ? "0" : "1"
              }">${info.enabled ? "Disable" : "Enable"}</button>`
            );
          }
          return `<tr>
            <td><strong>${esc(info.name || "?")}</strong></td>
            <td>${pill(info.enabled)}</td>
            <td class="muted"><code>${esc(info.id || "")}</code></td>
            <td class="os-actions">${acts.join(" ")}</td>
          </tr>`;
        })
        .join("")}</tbody></table>`
    : emptyNote("users");
  const form = run
    ? `<div class="os-forms">
      <details class="os-form">
        <summary>Create project</summary>
        <form id="os-project" class="os-grid">
          <label>Name <input name="name" required maxlength="64" placeholder="demo"></label>
          <label>Description <input name="description" maxlength="255"></label>
          <label>Enabled
            <select name="enabled">
              <option value="true">enabled</option>
              <option value="false">disabled</option>
            </select>
          </label>
          <button type="submit">Create</button>
        </form>
      </details>
      <details class="os-form">
        <summary>Create user</summary>
        <form id="os-user" class="os-grid">
          <label>Name <input name="name" required maxlength="64" placeholder="alice"></label>
          <label>Password <input name="password" type="password" required minlength="8" maxlength="128" autocomplete="new-password"></label>
          <label>Project <select name="project"><option value="">(none)</option>${options(
            projects,
            "id",
            (pr) => pr.name || pr.id
          )}</select></label>
          <button type="submit">Create</button>
        </form>
      </details>
      ${
        users.length
          ? `<details class="os-form">
        <summary>Set user password</summary>
        <form id="os-user-pw" class="os-grid">
          <label>User <select name="user_id" required>${options(users, "id", (usr) => usr.name || usr.id)}</select></label>
          <label>New password <input name="password" type="password" required minlength="8" maxlength="128" autocomplete="new-password"></label>
          <button type="submit">Update</button>
        </form>
      </details>`
          : ""
      }
    </div>`
    : "";
  return p + u + form;
}

function servicesHtml(data) {
  const run = canRun();
  const lbs = asList(data.load_balancers).filter(matchesFilter);
  const zones = asList(data.dns_zones).filter(matchesFilter);
  const secrets = asList(data.secrets).filter(matchesFilter);
  const subs = asList(data.subnets);
  const lbAvail = data.load_balancers_available === true;
  const dnsAvail = data.dns_zones_available === true;
  const secretAvail = data.secrets_available === true;
  const lbTable = lbAvail
    ? lbs.length
      ? `<table class="os-table">
        <thead><tr><th>Load balancer</th><th>Provisioning</th><th>VIP</th><th></th></tr></thead>
        <tbody>${lbs
          .map((lb) => {
            const info = lb && typeof lb === "object" ? lb : {};
            const del =
              run && info.id
                ? `<button type="button" class="secondary btn-sm" data-lb-del data-id="${esc(info.id)}">Delete</button>`
                : "";
            return `<tr>
              <td><strong>${esc(info.name || info.id || "?")}</strong></td>
              <td>${pill(info.provisioning_status)}</td>
              <td class="muted"><code>${esc(info.vip_address || "—")}</code></td>
              <td class="os-actions">${del}</td>
            </tr>`;
          })
          .join("")}</tbody></table>`
      : emptyNote("load balancers", "Octavia is in the catalog. Create one below.")
    : `<div class="muted">Octavia (load-balancer) is not in this cloud's service catalog.</div>`;
  const zoneTable = dnsAvail
    ? zones.length
      ? `<h3 class="lc-title">DNS zones</h3><table class="os-table">
        <thead><tr><th>Zone</th><th>Status</th><th>Email</th><th></th></tr></thead>
        <tbody>${zones
          .map((z) => {
            const info = z && typeof z === "object" ? z : {};
            const del =
              run && info.id
                ? `<button type="button" class="secondary btn-sm" data-zone-del data-id="${esc(info.id)}">Delete</button>`
                : "";
            return `<tr>
              <td><strong>${esc(info.name || "?")}</strong></td>
              <td>${pill(info.status)}</td>
              <td class="muted">${esc(info.email || "—")}</td>
              <td class="os-actions">${del}</td>
            </tr>`;
          })
          .join("")}</tbody></table>`
      : `<h3 class="lc-title">DNS zones</h3>${emptyNote("DNS zones", "Designate is in the catalog.")}`
    : `<h3 class="lc-title">DNS zones</h3><div class="muted">Designate (dns) is not in this cloud's service catalog.</div>`;
  const secretTable = secretAvail
    ? secrets.length
      ? `<h3 class="lc-title">Secrets</h3><table class="os-table">
        <thead><tr><th>Name</th><th>Status</th><th></th></tr></thead>
        <tbody>${secrets
          .map((s) => {
            const info = s && typeof s === "object" ? s : {};
            const del =
              run && info.id
                ? `<button type="button" class="secondary btn-sm" data-secret-del data-id="${esc(info.id)}">Delete</button>`
                : "";
            return `<tr>
              <td><strong>${esc(info.name || info.id || "?")}</strong></td>
              <td>${pill(info.status)}</td>
              <td class="os-actions">${del}</td>
            </tr>`;
          })
          .join("")}</tbody></table>`
      : `<h3 class="lc-title">Secrets</h3>${emptyNote("secrets", "Barbican is in the catalog.")}`
    : `<h3 class="lc-title">Secrets</h3><div class="muted">Barbican (key-manager) is not in this cloud's service catalog.</div>`;
  let forms = "";
  if (run) {
    forms = `<div class="os-forms">
      ${
        lbAvail
          ? `<details class="os-form">
        <summary>Create load balancer</summary>
        <form id="os-lb" class="os-grid">
          <label>Name <input name="name" required maxlength="64" placeholder="lb-1"></label>
          <label>VIP subnet <select name="vip_subnet_id" required>${options(
            subs,
            "id",
            (s) => `${s.name || s.id} ${s.cidr ? "(" + s.cidr + ")" : ""}`
          )}</select></label>
          <button type="submit">Create</button>
        </form>
      </details>`
          : ""
      }
      ${
        dnsAvail
          ? `<details class="os-form">
        <summary>Create DNS zone</summary>
        <form id="os-zone" class="os-grid">
          <label>Name <input name="name" required maxlength="253" placeholder="example.com."></label>
          <label>Email <input name="email" type="email" required placeholder="hostmaster@example.com"></label>
          <button type="submit">Create</button>
        </form>
      </details>`
          : ""
      }
      ${
        secretAvail
          ? `<details class="os-form">
        <summary>Store secret</summary>
        <form id="os-secret" class="os-grid">
          <label>Name <input name="name" required maxlength="64" placeholder="api-token"></label>
          <label>Payload <input name="payload" required maxlength="16384" autocomplete="off"></label>
          <button type="submit">Store</button>
        </form>
      </details>`
          : ""
      }
    </div>`;
  }
  return `<h3 class="lc-title">Load balancers</h3>${lbTable}${zoneTable}${secretTable}${forms}`;
}

function bodyHtml(data) {
  if (panel === "images") return imagesHtml(data);
  if (panel === "volumes") return volumesHtml(data);
  if (panel === "network") return networkHtml(data);
  if (panel === "access") return accessHtml(data);
  if (panel === "identity") return identityHtml(data);
  if (panel === "quotas") return quotasHtml(data);
  if (panel === "services") return servicesHtml(data);
  return instancesHtml(data);
}

export function cloudCardHtml() {
  return `
  <div class="card span-12 os-cloud" id="os-cloud-card">
    <div class="toolbar">
      <h2>OpenStack</h2>
      <span id="os-cloud-pill"></span>
      <span id="os-cloud-msg" class="muted"></span>
      <input id="os-cloud-filter" class="os-filter" type="search" placeholder="Filter…" autocomplete="off">
      <button class="secondary btn-sm" id="os-cloud-refresh" type="button">Refresh</button>
    </div>
    <div id="os-cloud-stats">${statsHtml({}, { skeleton: true })}</div>
    <div class="tab-bar" id="os-cloud-tabs">
      ${PANELS.map(
        ([id, label], i) =>
          `<button type="button" class="tab-btn${i === 0 ? " active" : ""}" data-os-panel="${esc(id)}">${esc(label)}</button>`
      ).join("")}
    </div>
    <div id="os-cloud-err"></div>
    <div id="os-cloud-body">${skeletonHtml()}</div>
  </div>`;
}

export function wireCloudCard(getEnvId) {
  envIdGetter = typeof getEnvId === "function" ? getEnvId : null;
  const refresh = document.getElementById("os-cloud-refresh");
  if (refresh && !refresh.dataset.wired) {
    refresh.dataset.wired = "1";
    refresh.addEventListener("click", () => retryCloud());
  }
  const filt = document.getElementById("os-cloud-filter");
  if (filt && !filt.dataset.wired) {
    filt.dataset.wired = "1";
    filt.addEventListener("input", () => {
      filter = filt.value || "";
      renderBody();
    });
  }
  const tabs = document.getElementById("os-cloud-tabs");
  if (tabs && !tabs.dataset.wired) {
    tabs.dataset.wired = "1";
    tabs.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-os-panel]");
      if (!btn) return;
      setPanel(btn.dataset.osPanel || "instances");
    });
  }
  const card = document.getElementById("os-cloud-card");
  if (card && !card.dataset.wired) {
    card.dataset.wired = "1";
    card.addEventListener("click", onCardClick);
    card.addEventListener("submit", onCardSubmit);
  }
  startRefresh();
}

function setPanel(id) {
  panel = id || "instances";
  const tabs = document.getElementById("os-cloud-tabs");
  if (tabs) {
    tabs.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.osPanel === panel));
  }
  renderBody();
}

function retryCloud() {
  const id = (envIdGetter && envIdGetter()) || activeEnvId;
  if (id) loadCloudCard(id, { refresh: true });
}

function cloudPanelActive() {
  const p = document.querySelector('.tab-panel[data-panel="platform"]');
  if (!(p && p.classList.contains("active"))) return false;
  const sub = p.querySelector('.ptab-panel[data-ppanel="openstack"]');
  return !!(sub && sub.classList.contains("active"));
}

function startRefresh() {
  stopRefresh();
  refreshTimer = setInterval(() => {
    if (document.hidden || !cloudPanelActive()) return;
    const id = (envIdGetter && envIdGetter()) || activeEnvId;
    if (id && !loading) loadCloudCard(id, { silent: true });
  }, REFRESH_MS);
}

function stopRefresh() {
  if (refreshTimer) {
    clearInterval(refreshTimer);
    refreshTimer = null;
  }
}

function renderError(err, { stale = false } = {}) {
  const el = document.getElementById("os-cloud-err");
  if (!el) return;
  el.innerHTML = errorBannerHtml(err, { stale });
}

function renderBody() {
  const body = document.getElementById("os-cloud-body");
  if (!body) return;
  if (!cache || typeof cache !== "object") return;
  body.classList.remove("muted");
  body.innerHTML = bodyHtml(cache);
}

function renderStats(data, skeleton) {
  const el = document.getElementById("os-cloud-stats");
  if (!el) return;
  if (!data) {
    el.innerHTML = skeleton ? statsHtml({}, { skeleton: true }) : "";
    return;
  }
  el.innerHTML = statsHtml(data);
}

function renderSource(data) {
  const pillEl = document.getElementById("os-cloud-pill");
  if (!pillEl) return;
  if (!data) {
    pillEl.innerHTML = "";
    return;
  }
  pillEl.innerHTML = sourcePillHtml(data);
}

function fetchCloud(envId, refresh) {
  const q = refresh ? "?refresh=true" : "";
  if (inflight && inflightKey === envId) return inflight;
  inflightKey = envId;
  inflight = api(`/api/v1/environments/${encodeURIComponent(envId)}/cloud${q}`, {
    timeout: FETCH_TIMEOUT,
  }).finally(() => {
    if (inflightKey === envId) {
      inflight = null;
      inflightKey = "";
    }
  });
  return inflight;
}

async function onCardClick(e) {
  const envId = (envIdGetter && envIdGetter()) || activeEnvId;
  const retry = e.target.closest("[data-os-retry]");
  if (retry) {
    retryCloud();
    return;
  }
  const go = e.target.closest("[data-os-goto]");
  if (go) {
    setPanel(go.dataset.osGoto || "instances");
    return;
  }
  if (!envId) return;
  const row = e.target.closest("[data-srv-sel]");
  if (row && !e.target.closest("button")) {
    const id = row.dataset.srvSel || "";
    selectedId = selectedId === id ? "" : id;
    renderBody();
    return;
  }
  const act = e.target.closest("[data-srv-act]");
  if (act) {
    const action = act.dataset.srvAct;
    const id = act.dataset.id;
    if (!action || !id) return;
    await mutate("POST", `/cloud/servers/${encodeURIComponent(id)}/${encodeURIComponent(action)}`);
    return;
  }
  const cons = e.target.closest("[data-srv-console]");
  if (cons) {
    const id = cons.dataset.id;
    if (!id) return;
    await openConsoleModal(envId, id);
    return;
  }
  const del = e.target.closest("[data-srv-del]");
  if (del) {
    const id = del.dataset.id;
    if (!id || !window.confirm("Delete this instance? This cannot be undone.")) return;
    await mutate("DELETE", `/cloud/servers/${encodeURIComponent(id)}`);
    return;
  }
  const vdel = e.target.closest("[data-vol-del]");
  if (vdel) {
    const id = vdel.dataset.id;
    if (!id || !window.confirm("Delete this volume?")) return;
    await mutate("DELETE", `/cloud/volumes/${encodeURIComponent(id)}`);
    return;
  }
  const vdet = e.target.closest("[data-vol-detach]");
  if (vdet) {
    const id = vdet.dataset.id;
    const serverId = vdet.dataset.server;
    if (!id || !serverId) return;
    await mutate("POST", "/cloud/volumes/detach", { server_id: serverId, volume_id: id });
    return;
  }
  const sgRow = e.target.closest("[data-sg-sel]");
  if (sgRow && !e.target.closest("button")) {
    const id = sgRow.dataset.sgSel || "";
    selectedSgId = selectedSgId === id ? "" : id;
    renderBody();
    return;
  }
  const rdel = e.target.closest("[data-sg-rule-del]");
  if (rdel) {
    const sg = rdel.dataset.sg;
    const id = rdel.dataset.id;
    if (!sg || !id || !window.confirm("Delete this security-group rule?")) return;
    await mutate(
      "DELETE",
      `/cloud/security-groups/${encodeURIComponent(sg)}/rules/${encodeURIComponent(id)}`
    );
    return;
  }
  const imgDel = e.target.closest("[data-img-del]");
  if (imgDel) {
    const id = imgDel.dataset.id;
    if (!id || !window.confirm("Delete this image?")) return;
    await mutate("DELETE", `/cloud/images/${encodeURIComponent(id)}`);
    return;
  }
  const flavDel = e.target.closest("[data-flav-del]");
  if (flavDel) {
    const id = flavDel.dataset.id;
    if (!id || !window.confirm("Delete this flavor?")) return;
    await mutate("DELETE", `/cloud/flavors/${encodeURIComponent(id)}`);
    return;
  }
  const snapDel = e.target.closest("[data-snap-del]");
  if (snapDel) {
    const id = snapDel.dataset.id;
    if (!id || !window.confirm("Delete this volume snapshot?")) return;
    await mutate("DELETE", `/cloud/volume-snapshots/${encodeURIComponent(id)}`);
    return;
  }
  const volSnap = e.target.closest("[data-vol-snap]");
  if (volSnap) {
    const id = volSnap.dataset.id;
    const name = window.prompt("Snapshot name");
    if (!id || !name) return;
    await mutate("POST", `/cloud/volumes/${encodeURIComponent(id)}/snapshot`, { name });
    return;
  }
  const netDel = e.target.closest("[data-net-del]");
  if (netDel) {
    const id = netDel.dataset.id;
    if (!id || !window.confirm("Delete this network?")) return;
    await mutate("DELETE", `/cloud/networks/${encodeURIComponent(id)}`);
    return;
  }
  const subDel = e.target.closest("[data-subnet-del]");
  if (subDel) {
    const id = subDel.dataset.id;
    if (!id || !window.confirm("Delete this subnet?")) return;
    await mutate("DELETE", `/cloud/subnets/${encodeURIComponent(id)}`);
    return;
  }
  const rtrDel = e.target.closest("[data-router-del]");
  if (rtrDel) {
    const id = rtrDel.dataset.id;
    if (!id || !window.confirm("Delete this router?")) return;
    await mutate("DELETE", `/cloud/routers/${encodeURIComponent(id)}`);
    return;
  }
  const fipUn = e.target.closest("[data-fip-unassoc]");
  if (fipUn) {
    const body = {};
    if (fipUn.dataset.id) body.id = fipUn.dataset.id;
    if (fipUn.dataset.address) body.address = fipUn.dataset.address;
    await mutate("POST", "/cloud/floating-ips/disassociate", body);
    return;
  }
  const fipDel = e.target.closest("[data-fip-del]");
  if (fipDel) {
    const id = fipDel.dataset.id;
    if (!id || !window.confirm("Release this floating IP?")) return;
    await mutate("DELETE", `/cloud/floating-ips/${encodeURIComponent(id)}`);
    return;
  }
  const sgDel = e.target.closest("[data-sg-del]");
  if (sgDel) {
    const id = sgDel.dataset.id;
    if (!id || !window.confirm("Delete this security group?")) return;
    await mutate("DELETE", `/cloud/security-groups/${encodeURIComponent(id)}`);
    return;
  }
  const kpDel = e.target.closest("[data-kp-del]");
  if (kpDel) {
    const name = kpDel.dataset.name;
    if (!name || !window.confirm("Delete this key pair?")) return;
    await mutate("DELETE", `/cloud/keypairs/${encodeURIComponent(name)}`);
    return;
  }
  const projEn = e.target.closest("[data-proj-en]");
  if (projEn) {
    const id = projEn.dataset.id;
    if (!id) return;
    await mutate("PATCH", `/cloud/projects/${encodeURIComponent(id)}`, {
      enabled: projEn.dataset.enabled === "1",
    });
    return;
  }
  const userEn = e.target.closest("[data-user-en]");
  if (userEn) {
    const id = userEn.dataset.id;
    if (!id) return;
    await mutate("PATCH", `/cloud/users/${encodeURIComponent(id)}`, {
      enabled: userEn.dataset.enabled === "1",
    });
    return;
  }
  const lbDel = e.target.closest("[data-lb-del]");
  if (lbDel) {
    const id = lbDel.dataset.id;
    if (!id || !window.confirm("Delete this load balancer?")) return;
    await mutate("DELETE", `/cloud/load-balancers/${encodeURIComponent(id)}`);
    return;
  }
  const zoneDel = e.target.closest("[data-zone-del]");
  if (zoneDel) {
    const id = zoneDel.dataset.id;
    if (!id || !window.confirm("Delete this DNS zone?")) return;
    await mutate("DELETE", `/cloud/dns-zones/${encodeURIComponent(id)}`);
    return;
  }
  const secretDel = e.target.closest("[data-secret-del]");
  if (secretDel) {
    const id = secretDel.dataset.id;
    if (!id || !window.confirm("Delete this secret?")) return;
    await mutate("DELETE", `/cloud/secrets/${encodeURIComponent(id)}`);
  }
}

async function onCardSubmit(e) {
  const form = e.target;
  if (!(form instanceof HTMLFormElement)) return;
  const known = {
    "os-launch": "/cloud/servers",
    "os-vol": "/cloud/volumes",
    "os-net": "/cloud/networks",
    "os-net-ext": "/cloud/networks",
    "os-router": "/cloud/routers",
    "os-attach": "/cloud/volumes/attach",
    "os-fip": "/cloud/floating-ips",
    "os-fip-assoc": "/cloud/floating-ips/associate",
    "os-img": "/cloud/images",
    "os-flav": "/cloud/flavors",
    "os-sg": "/cloud/security-groups",
    "os-kp": "/cloud/keypairs",
    "os-subnet": "/cloud/subnets",
    "os-project": "/cloud/projects",
    "os-user": "/cloud/users",
    "os-lb": "/cloud/load-balancers",
    "os-zone": "/cloud/dns-zones",
    "os-secret": "/cloud/secrets",
  };
  const special = [
    "os-sg-rule",
    "os-router-if",
    "os-router-if-del",
    "os-quotas",
    "os-img-patch",
    "os-vol-ext",
    "os-vol-snap",
    "os-resize",
    "os-rebuild",
    "os-srv-snap",
    "os-srv-sg-add",
    "os-srv-sg-del",
    "os-user-pw",
  ].includes(form.id);
  if (!known[form.id] && !special) return;
  e.preventDefault();
  const envId = (envIdGetter && envIdGetter()) || activeEnvId;
  if (!envId) return;
  const fd = new FormData(form);
  const body = Object.fromEntries(fd.entries());
  if (form.id === "os-vol" || form.id === "os-vol-ext" || form.id === "os-flav") {
    if (body.size != null && body.size !== "") body.size = Number(body.size);
    if (body.vcpus != null && body.vcpus !== "") body.vcpus = Number(body.vcpus);
    if (body.ram != null && body.ram !== "") body.ram = Number(body.ram);
    if (body.disk != null && body.disk !== "") body.disk = Number(body.disk);
  }
  if (form.id === "os-launch" && !body.key_name) delete body.key_name;
  if (form.id === "os-img" && !body.url) delete body.url;
  if (form.id === "os-kp" && !body.public_key) delete body.public_key;
  if (form.id === "os-user" && !body.project) delete body.project;
  if (form.id === "os-project") body.enabled = body.enabled !== "false";
  if (form.id === "os-subnet" && !body.name) delete body.name;
  if (form.id === "os-net-ext") {
    body.external = true;
    if (!body.cidr) delete body.cidr;
    await mutate("POST", known[form.id], body);
    return;
  }
  if (form.id === "os-sg-rule") {
    const sg = body.sg_id || form.dataset.sg;
    delete body.sg_id;
    if (!body.protocol) body.protocol = null;
    if (body.port_range_min === "") delete body.port_range_min;
    else if (body.port_range_min != null) body.port_range_min = Number(body.port_range_min);
    if (body.port_range_max === "") delete body.port_range_max;
    else if (body.port_range_max != null) body.port_range_max = Number(body.port_range_max);
    if (!body.remote_ip_prefix) delete body.remote_ip_prefix;
    if (!sg) return;
    await mutate("POST", `/cloud/security-groups/${encodeURIComponent(sg)}/rules`, body);
    return;
  }
  if (form.id === "os-router-if") {
    const rid = body.router_id;
    const subnetId = body.subnet_id;
    if (!rid || !subnetId) return;
    await mutate("POST", `/cloud/routers/${encodeURIComponent(rid)}/interfaces`, { subnet_id: subnetId });
    return;
  }
  if (form.id === "os-img-patch") {
    const id = body.image_id;
    if (!id) return;
    const payload = {};
    if (body.name) payload.name = body.name;
    if (body.visibility) payload.visibility = body.visibility;
    if (!Object.keys(payload).length) return;
    await mutate("PATCH", `/cloud/images/${encodeURIComponent(id)}`, payload);
    return;
  }
  if (form.id === "os-vol-ext") {
    const id = body.volume_id;
    if (!id) return;
    await mutate("POST", `/cloud/volumes/${encodeURIComponent(id)}/extend`, { size: Number(body.size) });
    return;
  }
  if (form.id === "os-vol-snap") {
    const id = body.volume_id;
    if (!id) return;
    await mutate("POST", `/cloud/volumes/${encodeURIComponent(id)}/snapshot`, { name: body.name });
    return;
  }
  if (form.id === "os-resize") {
    const id = form.dataset.id;
    if (!id) return;
    await mutate("POST", `/cloud/servers/${encodeURIComponent(id)}/resize`, { flavor: body.flavor });
    return;
  }
  if (form.id === "os-rebuild") {
    const id = form.dataset.id;
    if (!id) return;
    await mutate("POST", `/cloud/servers/${encodeURIComponent(id)}/rebuild`, { image: body.image });
    return;
  }
  if (form.id === "os-srv-snap") {
    const id = form.dataset.id;
    if (!id) return;
    await mutate("POST", `/cloud/servers/${encodeURIComponent(id)}/snapshot`, { name: body.name });
    return;
  }
  if (form.id === "os-srv-sg-add") {
    const id = form.dataset.id;
    if (!id) return;
    await mutate("POST", `/cloud/servers/${encodeURIComponent(id)}/security-groups`, { name: body.name });
    return;
  }
  if (form.id === "os-srv-sg-del") {
    const id = form.dataset.id;
    if (!id || !body.name) return;
    await mutate(
      "DELETE",
      `/cloud/servers/${encodeURIComponent(id)}/security-groups/${encodeURIComponent(body.name)}`
    );
    return;
  }
  if (form.id === "os-router-if-del") {
    const rid = body.router_id;
    const subnetId = body.subnet_id;
    if (!rid || !subnetId) return;
    await mutate(
      "DELETE",
      `/cloud/routers/${encodeURIComponent(rid)}/interfaces/${encodeURIComponent(subnetId)}`
    );
    return;
  }
  if (form.id === "os-user-pw") {
    const id = body.user_id;
    if (!id) return;
    await mutate("PATCH", `/cloud/users/${encodeURIComponent(id)}`, { password: body.password });
    return;
  }
  if (form.id === "os-quotas") {
    const num = (k) => {
      const v = body[k];
      if (v === "" || v == null) return undefined;
      const n = Number(v);
      return Number.isFinite(n) ? n : undefined;
    };
    const payload = {
      compute: { instances: num("instances"), cores: num("cores"), ram: num("ram") },
      network: {
        network: num("network"),
        subnet: num("subnet"),
        router: num("router"),
        floatingip: num("floatingip"),
        security_group: num("security_group"),
        port: num("port"),
      },
    };
    const dropEmpty = (obj) => {
      const out = {};
      for (const [k, v] of Object.entries(obj)) {
        if (v !== undefined) out[k] = v;
      }
      return out;
    };
    payload.compute = dropEmpty(payload.compute);
    payload.network = dropEmpty(payload.network);
    if (!Object.keys(payload.compute).length) delete payload.compute;
    if (!Object.keys(payload.network).length) delete payload.network;
    await mutate("PUT", "/cloud/quotas", payload);
    return;
  }
  await mutate("POST", known[form.id], body);
}

function ensureConsoleModal() {
  let el = document.getElementById("os-console-modal");
  if (el) return el;
  el = document.createElement("div");
  el.id = "os-console-modal";
  el.className = "os-console-modal";
  el.hidden = true;
  el.innerHTML = `<div class="os-console-panel" role="dialog" aria-modal="true" aria-labelledby="os-console-title">
      <div class="os-console-head">
        <h2 id="os-console-title">Console</h2>
        <button type="button" class="secondary btn-sm" data-os-console-close>Close</button>
      </div>
      <iframe id="os-console-frame" title="Instance console" sandbox="allow-scripts" referrerpolicy="no-referrer"></iframe>
      <p class="muted os-console-help">VNC via console proxy (nova-novncproxy). Token stays on the server.</p>
    </div>`;
  document.body.appendChild(el);
  el.addEventListener("click", (e) => {
    if (e.target === el || e.target.closest("[data-os-console-close]")) closeConsoleModal();
  });
  return el;
}

function onConsoleKey(e) {
  if (e.key === "Escape") {
    e.preventDefault();
    closeConsoleModal();
  }
}

function closeConsoleModal() {
  const frame = document.getElementById("os-console-frame");
  if (frame) frame.src = "about:blank";
  const modal = document.getElementById("os-console-modal");
  if (modal) modal.hidden = true;
  if (consoleEscBound) {
    document.removeEventListener("keydown", onConsoleKey);
    consoleEscBound = false;
  }
}

async function openConsoleModal(envId, serverId) {
  const modal = ensureConsoleModal();
  const title = document.getElementById("os-console-title");
  const frame = document.getElementById("os-console-frame");
  const short = String(serverId || "").slice(0, 8) || "instance";
  if (title) title.textContent = `Console — ${short}`;
  if (frame) frame.src = "about:blank";
  modal.hidden = true;
  try {
    const d = await api(
      `/api/v1/environments/${encodeURIComponent(envId)}/cloud/servers/${encodeURIComponent(serverId)}/console/session`,
      { method: "POST", timeout: 25000 }
    );
    if (!d || !d.ok || !d.embed_url) {
      toast((d && (d.error || d.message)) || "Console session unavailable", "error");
      return;
    }
    const embed = String(d.embed_url);
    if (!embed.startsWith("/") || embed.startsWith("//")) {
      toast("Console session unavailable", "error");
      return;
    }
    if (frame) frame.src = embed;
    modal.hidden = false;
    if (!consoleEscBound) {
      document.addEventListener("keydown", onConsoleKey);
      consoleEscBound = true;
    }
    const closeBtn = modal.querySelector("[data-os-console-close]");
    if (closeBtn) closeBtn.focus();
  } catch (err) {
    toast(err && err.message ? err.message : "console failed", "error");
  }
}

async function mutate(method, suffix, body) {
  const envId = (envIdGetter && envIdGetter()) || activeEnvId;
  if (!envId) return;
  const msg = document.getElementById("os-cloud-msg");
  if (msg) msg.textContent = "Working…";
  try {
    const res = await api(`/api/v1/environments/${encodeURIComponent(envId)}${suffix}`, {
      method,
      body: body ? JSON.stringify(body) : undefined,
      timeout: 60000,
    });
    const privateKey = res && res.keypair && res.keypair.private_key;
    if (res && res.ok === false) toast(res.error || res.message || "action failed", "error");
    else toast(res && res.message ? res.message : "accepted", "ok");
    if (privateKey) {
      window.prompt("Save this private key now; it will not be shown again.", privateKey);
    }
  } catch (err) {
    toast(err && err.message ? err.message : "action failed", "error");
  }
  await loadCloudCard(envId, { refresh: true });
}

function failMessage(err) {
  if (err && err.isTimeout) return "OpenStack inventory timed out after 25s.";
  return (err && err.message) || "unreachable";
}

export async function loadCloudCard(envId, { silent = false, refresh = false } = {}) {
  if (activeEnvId && envId && activeEnvId !== envId) {
    closeConsoleModal();
    selectedSgId = "";
    selectedId = "";
  }
  if (!envId) closeConsoleModal();
  activeEnvId = envId || "";
  const body = document.getElementById("os-cloud-body");
  const msg = document.getElementById("os-cloud-msg");
  if (!body) return;
  if (!activeEnvId) {
    lastError = "";
    if (msg) msg.textContent = "";
    renderSource(null);
    renderError("");
    renderStats(null);
    body.classList.add("muted");
    body.innerHTML = "Select an environment.";
    cache = null;
    return;
  }
  if (!silent) {
    if (msg) msg.textContent = cache ? "Refreshing…" : "Talking to OpenStack…";
    if (!cache) {
      renderStats(null, true);
      body.classList.remove("muted");
      body.innerHTML = skeletonHtml();
    }
  } else if (!cache) {
    renderStats(null, true);
    body.classList.remove("muted");
    if (!body.querySelector(".os-skel")) body.innerHTML = skeletonHtml();
  }
  loading = true;
  let d;
  try {
    d = await fetchCloud(activeEnvId, refresh);
  } catch (e) {
    if (activeEnvId !== envId) return;
    lastError = failMessage(e);
    if (msg) msg.textContent = cache ? "showing cache" : "";
    renderSource(cache);
    renderError(lastError, { stale: !!cache });
    if (!cache) {
      body.classList.remove("muted");
      body.innerHTML = unavail(lastError);
    }
    if (cache) toast(lastError, "error");
    return;
  } finally {
    loading = false;
  }
  if (activeEnvId !== envId) return;
  lastError = "";
  cache = d && typeof d === "object" ? d : {};
  if (msg) msg.textContent = cache.cached ? "cached" : "";
  renderSource(cache);
  renderStats(cache);
  renderError(cache.available === false ? cache.error || "OpenStack API not available" : "");
  body.classList.remove("muted");
  body.innerHTML = bodyHtml(cache);
}

export function destroyCloudCard() {
  closeConsoleModal();
  stopRefresh();
  envIdGetter = null;
  activeEnvId = "";
  cache = null;
  inflight = null;
  inflightKey = "";
  panel = "instances";
  filter = "";
  selectedId = "";
  selectedSgId = "";
  loading = false;
  lastError = "";
}
