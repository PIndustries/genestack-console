// pages/activity.js — Activity page: Jobs / Alerts / Audit tabs. Each tab mounts
// the corresponding existing full-page module into the pane. Those modules find
// their controls by document-wide element ids — and share some (jobs, alerts,
// and audit all render a "f-env" select) — so only one is mounted at a time:
// switching tabs destroys the current module, clears the pane, and renders the
// next. Mounting is therefore also lazy (first open wires and loads the tab).
import * as jobs from "./jobs.js";
import * as alerts from "./alerts.js";
import * as auditPage from "./audit.js";

export const title = "Activity";

const TABS = [
  { id: "jobs", label: "Jobs", module: jobs },
  { id: "alerts", label: "Alerts", module: alerts },
  { id: "audit", label: "Audit", module: auditPage },
];

let mountedModule = null;
let jobParam = null; // deep-linked job id (?job=, or legacy #/jobs/<id> redirect)

export function destroy() {
  unmount();
  jobParam = null;
}

function unmount() {
  if (mountedModule && mountedModule.destroy) {
    try { mountedModule.destroy(); } catch { /* ignore */ }
  }
  mountedModule = null;
}

export async function render(root, { query } = {}) {
  destroy();
  const requested = query && query.get("tab");
  const initialTab = TABS.some((t) => t.id === requested) ? requested : "jobs";
  jobParam = query ? query.get("job") : null;

  root.innerHTML = `
  <div class="tab-bar" role="tablist">
    ${TABS.map(
      (t) =>
        `<button class="tab-btn" type="button" role="tab" data-act-tab="${t.id}">${t.label}</button>`
    ).join("")}
  </div>
  <div id="act-pane"></div>`;

  root.querySelectorAll("[data-act-tab]").forEach((btn) =>
    btn.addEventListener("click", () => activateTab(btn.dataset.actTab))
  );
  await activateTab(initialTab, { keepHash: true });
}

async function activateTab(id, { keepHash = false } = {}) {
  if (!keepHash) {
    history.replaceState(null, "", "#/activity" + (id === "jobs" ? "" : "?tab=" + id));
  }
  document.querySelectorAll("[data-act-tab]").forEach((b) =>
    b.classList.toggle("active", b.dataset.actTab === id)
  );
  unmount();
  const pane = document.getElementById("act-pane");
  if (!pane) return;
  pane.innerHTML = "";
  const tab = TABS.find((t) => t.id === id);
  if (!tab) return;
  mountedModule = tab.module;
  // jobs.render accepts { param } to pre-open a job detail; alerts/audit ignore it.
  await tab.module.render(pane, { param: id === "jobs" ? jobParam : null });
  jobParam = null; // consumed — don't re-open the same job on later tab switches
}
