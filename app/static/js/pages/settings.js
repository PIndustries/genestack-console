// Settings for this console: pick a version, then upgrade or roll back.
import { api, esc, toast, setConsoleRestarting, isConsoleRestarting } from "../api.js";
import { store, canAdmin, gate } from "../store.js";

export const title = "Settings";

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function labelOf(row) {
  if (!row) return "";
  return row.version || row.image || row.slot || "";
}

function calver(text) {
  const parts = String(text || "").replace(/^v/i, "").split(".").map((bit) => parseInt(bit, 10) || 0);
  while (parts.length < 4) parts.push(0);
  return parts.slice(0, 4);
}

function versionOlder(left, right) {
  const a = calver(left);
  const b = calver(right);
  for (let i = 0; i < 4; i += 1) {
    if (a[i] !== b[i]) return a[i] < b[i];
  }
  return false;
}

async function waitForConsole(previous) {
  const deadline = Date.now() + 180000;
  let sawGap = false;
  while (Date.now() < deadline) {
    await sleep(2000);
    try {
      const health = await api("/health", { timeout: 2500 });
      const version = health && health.version ? String(health.version) : "";
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

export async function render(root) {
  root.innerHTML = `
    <div class="card">
      <div class="toolbar">
        <h2>This console</h2>
      </div>
      <p class="muted" id="console-lead">Loading the versions on this machine.</p>
      <div id="console-err"></div>
      <label class="field" for="console-version"><span>Version</span>
        <select id="console-version"></select>
      </label>
      <div class="toolbar" style="margin-top:.75rem">
        <button type="button" id="console-upgrade" ${gate(canAdmin(), "admin")}>Upgrade</button>
        <button type="button" class="secondary" id="console-rollback" ${gate(canAdmin(), "admin")}>Rollback</button>
      </div>
      <p class="muted" id="console-note"></p>
    </div>`;

  const lead = root.querySelector("#console-lead");
  const note = root.querySelector("#console-note");
  const err = root.querySelector("#console-err");
  const select = root.querySelector("#console-version");
  const upgrade = root.querySelector("#console-upgrade");
  const rollback = root.querySelector("#console-rollback");
  let host = { bootc: false };
  let releases = [];
  let current = "";

  function showError(message) {
    err.innerHTML = message ? `<div class="error">${esc(message)}</div>` : "";
  }

  function fill(options) {
    select.innerHTML = options.map((opt) => (
      `<option value="${esc(opt.value)}">${esc(opt.label)}</option>`
    )).join("");
    if (!options.length) {
      select.innerHTML = `<option value="">No version is listed</option>`;
    }
  }

  function sync() {
    const choice = select.value;
    const admin = canAdmin();
    if (host.bootc) {
      upgrade.disabled = !admin || host.read_only || choice !== "upgrade";
      rollback.disabled = !admin || host.read_only || choice !== "rollback";
      if (choice === "current") {
        note.textContent = "This is the booted version.";
      } else if (choice === "rollback") {
        note.textContent = "Rollback reboots into the previous version. SSH stays off.";
      } else if (choice === "staged") {
        note.textContent = "Upgrade reboots into the staged image.";
      } else {
        note.textContent = "Upgrade pulls the image this machine tracks and reboots. The program file stays inside that image.";
      }
      return;
    }
    const selected = releases.find((row) => row && row.version === choice);
    const different = selected && selected.version && selected.version !== current;
    upgrade.disabled = !admin || !different || !!selected.older;
    rollback.disabled = !admin || !selected || !selected.older;
    if (!releases.length) {
      note.textContent = "Published versions are not listed yet.";
    } else if (choice === current) {
      note.textContent = "This install is on that version. Pick another one to upgrade or roll back the program file.";
    } else if (selected && selected.older) {
      note.textContent = "Rollback installs that older program file and restarts this console.";
    } else {
      note.textContent = "Upgrade installs that program file and restarts this console.";
    }
  }

  async function bootAction(action) {
    if (!canAdmin()) {
      toast("Ask an admin to change this console.", "bad");
      return;
    }
    const verb = action === "rollback" ? "Roll back" : "Upgrade";
    if (!confirm(`${verb} this console? The machine reboots.`)) return;
    setConsoleRestarting(true);
    toast(`${verb} started. This machine will reboot.`, "ok");
    let previous = current;
    try {
      const health = await api("/health");
      if (health && health.version) previous = String(health.version);
    } catch { /* the reboot still drops the page */ }
    try {
      const result = await api("/api/v1/update/bootc", {
        method: "POST",
        body: JSON.stringify({ action }),
        timeout: 180000,
      });
      if (result && result.ok === false) {
        setConsoleRestarting(false);
        toast(result.message || "bootc failed", "bad");
        return;
      }
    } catch (err) {
      if (!(err && (err.isNetwork || err.isTimeout || err.isRestarting))) {
        setConsoleRestarting(false);
        toast(err && err.message ? err.message : "bootc failed", "bad");
        return;
      }
    }
    const outcome = await waitForConsole(previous);
    if (outcome === "back") return;
    setConsoleRestarting(false);
    if (outcome === "down") toast("The console did not come back yet. Refresh in a moment.", "bad");
  }

  async function installSelected() {
    const version = select.value;
    if (!version || version === current) {
      toast("Already on that version.");
      return;
    }
    if (!canAdmin()) {
      toast("Ask an admin to change this console.", "bad");
      return;
    }
    if (!confirm(`Install Console ${version} and restart this console?`)) return;
    setConsoleRestarting(true);
    toast(`Installing ${version}. This console will restart.`, "ok");
    let previous = current;
    try {
      const health = await api("/health");
      if (health && health.version) previous = String(health.version);
    } catch { /* apply still reports the result */ }
    let result = null;
    try {
      result = await api("/api/v1/update/apply", {
        method: "POST",
        body: JSON.stringify({ version }),
        timeout: 180000,
      });
    } catch (err) {
      if (!(err && (err.isNetwork || err.isTimeout || err.isRestarting))) {
        setConsoleRestarting(false);
        toast(err && err.message ? err.message : "Update failed", "bad");
        return;
      }
      result = { applied: true };
    }
    if (!(result && (result.applied || String(result.message || "").startsWith("binary replaced")))) {
      setConsoleRestarting(false);
      toast((result && result.message) || "Already current", result && result.ok === false ? "bad" : "ok");
      return;
    }
    const outcome = await waitForConsole(previous);
    if (outcome === "back") return;
    setConsoleRestarting(false);
    if (outcome === "down") toast("The console did not come back yet. Refresh in a moment.", "bad");
  }

  upgrade.addEventListener("click", () => {
    if (host.bootc) bootAction(select.value === "staged" ? "upgrade" : "upgrade");
    else installSelected();
  });
  rollback.addEventListener("click", () => {
    if (host.bootc) bootAction("rollback");
    else installSelected();
  });
  select.addEventListener("change", sync);

  try {
    const health = await api("/health");
    current = health && health.version ? String(health.version) : "";
  } catch { /* the host call still paints the page */ }
  try {
    host = await api("/api/v1/update/host");
  } catch (err) {
    showError(err.message || "This machine did not report its version.");
    host = { bootc: false };
  }
  try {
    const feed = await api("/api/v1/update/feed");
    releases = Array.isArray(feed.releases) ? feed.releases : [];
  } catch {
    releases = [];
  }

  if (host.bootc) {
    const booted = labelOf(host.booted) || current || "this boot";
    const previous = labelOf(host.rollback);
    lead.textContent = host.ok === false
      ? (host.message || "bootc did not report a version.")
      : `This machine boots with bootc. SSH is off. This boot is ${booted}.`;
    const options = [{ value: "current", label: `This boot ${booted}` }];
    if (previous) options.push({ value: "rollback", label: `Previous ${previous}` });
    if (host.staged) options.push({ value: "staged", label: `Staged ${labelOf(host.staged)}` });
    options.push({ value: "upgrade", label: "Newer image" });
    fill(options);
    if (host.ok === false) showError(host.message || "");
  } else {
    lead.textContent = current
      ? `This install is ${current}. Pick a published version.`
      : "Pick a published version.";
    const options = [];
    const seen = new Set();
    if (current) {
      options.push({ value: current, label: `This install ${current}` });
      seen.add(current);
    }
    releases.forEach((row) => {
      const version = row && row.version ? String(row.version) : "";
      if (!version || seen.has(version)) return;
      seen.add(version);
      row.older = current ? versionOlder(version, current) : true;
      options.push({ value: version, label: version });
    });
    fill(options);
  }
  sync();
  if (isConsoleRestarting()) {
    note.textContent = "This console is restarting.";
  }
}

export function destroy() {}
