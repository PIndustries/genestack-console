// One next action, shared by Overview and Machines.
// The sentence changes as the cloud grows. Activity stays the log.
import { api, esc, toast } from "../api.js";
import { canAdmin, canRun, store } from "../store.js";
import { fetchMetalPath } from "../metal_path.js";
import { DEPLOY_STAGES as STAGES, stageStates } from "../deploy_stages.js?v=ls71";

let gen = 0;
let timer = null;
let currentEnv = "";
let currentModel = null;
let onDry = null;

export function nextStripHtml(place) {
  return `<section class="env-next" id="env-next-${place}" aria-live="polite"><p class="hint-row">Checking the next action.</p></section>`;
}

export function wireNextStrip(getEnvId) {
  if (onDry) window.removeEventListener("gsc-dry-run", onDry);
  onDry = () => {
    const id = typeof getEnvId === "function" ? getEnvId() : "";
    if (id) loadNextStrip(id);
  };
  window.addEventListener("gsc-dry-run", onDry);
}

export function destroyNextStrip() {
  gen += 1;
  currentEnv = "";
  if (timer) {
    clearTimeout(timer);
    timer = null;
  }
  if (onDry) {
    window.removeEventListener("gsc-dry-run", onDry);
    onDry = null;
  }
}

export function loadNextStrip(envId) {
  const mine = ++gen;
  currentEnv = envId || "";
  if (timer) {
    clearTimeout(timer);
    timer = null;
  }
  if (!envId) {
    currentModel = null;
    paint("Select an environment.", "", []);
    return;
  }
  paint("Checking the next action.", "", []);
  collect(envId).then((model) => {
    if (mine !== gen || currentEnv !== envId) return;
    currentModel = model;
    const view = describe(model);
    paint(view.title, view.detail, view.actions, view.log);
    if (view.live) timer = setTimeout(() => loadNextStrip(envId), 5000);
  }).catch(() => {
    if (mine !== gen) return;
    paint("The next action did not answer.", "Refresh the page.", []);
  });
}

function paint(title, detail, actions, log) {
  const html = render(title, detail, actions || [], log || null);
  for (const place of ["overview", "machines"]) {
    const el = document.getElementById(`env-next-${place}`);
    if (!el || el.dataset.key === html) continue;
    el.dataset.key = html;
    el.innerHTML = html;
    el.querySelectorAll("[data-next-act]").forEach((btn) => {
      btn.addEventListener("click", () => runAction(btn));
    });
  }
}

function render(title, detail, actions, log) {
  const buttons = actions.map((action) => {
    const cls = action.primary ? "btn-sm" : "secondary btn-sm";
    const stage = action.stage ? ` data-next-stage="${esc(action.stage)}"` : "";
    const titleAttr = action.title ? ` title="${esc(action.title)}"` : "";
    const disabled = action.disabled ? " disabled" : "";
    return `<button type="button" class="${cls}" data-next-act="${esc(action.act)}"${stage}${titleAttr}${disabled}>${esc(action.label)}</button>`;
  }).join("");
  const logLink = log && log.id
    ? `<a href="#/activity?tab=jobs&job=${esc(log.id)}">${esc(log.label || "Job log")}</a>`
    : "";
  return `<h2>${esc(title)}</h2><p class="hint-row">${esc(detail)}</p><div class="row">${buttons}${logLink}</div>`;
}

async function collect(envId) {
  const [serversRes, jobs, facts, k8s, provider, env, registry, baremetal] = await Promise.all([
    api(`/api/v1/environments/${encodeURIComponent(envId)}/servers`).catch(() => null),
    api(`/api/v1/jobs?environment_id=${encodeURIComponent(envId)}&limit=20`).catch(() => []),
    api(`/api/v1/environments/${encodeURIComponent(envId)}/install-check`, { timeout: 8000 }).catch(() => null),
    api(`/api/v1/environments/${encodeURIComponent(envId)}/k8s/nodes`, { timeout: 8000 }).catch(() => null),
    fetchMetalPath(api, envId),
    api(`/api/v1/environments/${encodeURIComponent(envId)}`).catch(() => null),
    api(`/api/v1/environments/${encodeURIComponent(envId)}/registry`, { timeout: 4000 }).catch(() => null),
    api(`/api/v1/environments/${encodeURIComponent(envId)}/baremetal`, { timeout: 8000 }).catch(() => null),
  ]);
  const raw = Array.isArray(serversRes)
    ? serversRes
    : (serversRes && Array.isArray(serversRes.servers) ? serversRes.servers : []);
  const servers = raw.filter((row) => row && row.hostname && row.assigned !== false);
  const list = Array.isArray(jobs) ? jobs : [];
  const kept = facts && facts.client_pin
    ? facts
    : (facts ? { unavailable: true, boot_image: facts.boot_image || "" } : null);
  const bmcHosts = matchingBmcHosts(servers, baremetal);
  return {
    servers,
    provider: provider === "kubespray" ? "kubespray" : "talos",
    facts: kept,
    k8sOk: k8s != null,
    clusterIps: readyIps(k8s),
    bootstrap: list.find((job) => job && job.operation === "genestack.talos.bootstrap") || null,
    deploy: list.find((job) => job && job.operation === "genestack.deploy") || null,
    greenfield: list.find((job) => job && job.operation === "genestack.greenfield") || null,
    logsOnly: logsOnly(env),
    cacheDown: registry != null && registry.running === false,
    hasBmc: bmcHosts.length > 0,
    bmcHosts,
  };
}

function matchingBmcHosts(servers, baremetal) {
  const nodes = baremetal && Array.isArray(baremetal.nodes) ? baremetal.nodes : [];
  const names = new Set();
  for (const node of nodes) {
    const name = String((node && node.name) || "").trim().toLowerCase();
    if (name) names.add(name);
  }
  const hosts = [];
  for (const row of servers) {
    const host = String((row && row.hostname) || "").trim();
    if (host && names.has(host.toLowerCase())) hosts.push(host);
  }
  return hosts;
}

function readyIps(k8s) {
  const ips = new Set();
  for (const node of (k8s && k8s.nodes) || []) {
    const ip = String((node && node.internal_ip) || "");
    if (ip && String((node && node.status) || "").toLowerCase() === "ready") ips.add(ip);
  }
  return ips;
}

function logsOnly(env) {
  if (!env || env.dry_run == null) return store.health ? !!store.health.dry_run : true;
  return env.dry_run === true;
}

function serverIp(server) {
  return String((server && (server.private_ip || server.ip)) || "");
}

function bootImage(model) {
  const image = model && model.facts && model.facts.boot_image;
  return image ? String(image) : "v1.13.9";
}

function outsideCluster(model) {
  if (!model.k8sOk) return [];
  return model.servers.filter((row) => {
    const ip = serverIp(row);
    return ip && !model.clusterIps.has(ip);
  });
}

function fleetSentence(model) {
  const rows = model.servers;
  const noun = rows.length === 1 ? "machine is" : "machines are";
  if (!model.k8sOk) {
    return `${rows.length} ${noun} saved. The cluster list has not answered yet. Adding a machine does not join it.`;
  }
  const joined = rows.filter((row) => {
    const ip = serverIp(row);
    return ip && model.clusterIps.has(ip);
  }).length;
  const waiting = rows.length - joined;
  if (!waiting) return `${rows.length} ${noun} saved, and ${rows.length === 1 ? "it is" : "they are"} in the cluster.`;
  return `${rows.length} ${noun} saved. ${joined} in the cluster. ${waiting} not joined yet. Adding a machine does not join it.`;
}

function live(job) {
  const status = String((job && job.status) || "");
  return status === "queued" || status === "running";
}

function jobLog(job, label) {
  const id = job && job.id != null ? String(job.id) : "";
  return id ? { id, label } : null;
}

function identityMismatch(model) {
  const job = model.bootstrap;
  const err = String((job && job.error) || "");
  const step = job && job.user_step;
  const failed = job && job.status === "failed" && (
    (step && step.kind === "boot-installer")
    || /certificate|unknown authority|does not match|no talosconfig/i.test(err)
  );
  if (failed) return true;
  if (!model.k8sOk || !model.facts || model.facts.unavailable) return false;
  const roles = model.servers.filter((row) => Array.isArray(row.roles) && row.roles.length);
  if (!roles.length) return false;
  return !roles.some((row) => {
    const ip = serverIp(row);
    return ip && model.clusterIps.has(ip);
  });
}

function readyControlPlane(model) {
  if (!model.k8sOk) return false;
  return model.servers.some((row) => {
    const roles = Array.isArray(row.roles) ? row.roles : [];
    const control = roles.some((role) => String(role).toLowerCase() === "k8s_control_plane");
    const ip = serverIp(row);
    return control && ip && model.clusterIps.has(ip);
  });
}

function deployBlock(model) {
  if (!model.facts) return "Checking the kubeconfig and the control plane.";
  if (model.facts.unavailable) return "The install check did not answer. Deploy stays closed.";
  const kube = !!model.facts.kubeconfig_readable;
  const ready = readyControlPlane(model);
  if (!kube && (!model.k8sOk || !ready)) {
    return "Deploy from infrastructure needs a kubeconfig this console can read and a Ready control plane.";
  }
  if (!kube) return "Deploy from infrastructure needs a kubeconfig this console can read.";
  if (!model.k8sOk) return "Deploy from infrastructure needs a Ready control plane. The cluster list has not answered yet.";
  if (!ready) return "Deploy from infrastructure needs a Ready control plane.";
  return "";
}

const NEED_PIPE = ["hosts"].concat(STAGES.map(([id]) => id));

function needsForStage(model, stage) {
  const facts = model && model.facts;
  const needs = facts && Array.isArray(facts.needs) ? facts.needs : [];
  const start = NEED_PIPE.indexOf(stage);
  if (start < 0) return [];
  const open = new Set(NEED_PIPE.slice(start));
  return needs.filter((item) => item && open.has(String(item.stage || "infrastructure")));
}

function blockingNeed(model, stage) {
  return needsForStage(model, stage).find((item) => item && item.blocks) || null;
}

function noticeText(model, stage) {
  return needsForStage(model, stage)
    .filter((item) => item && !item.blocks && item.detail)
    .map((item) => String(item.detail))
    .join(" ");
}

function logLine(model) {
  return model.logsOnly
    ? " This environment only logs. This run writes a log and does not change the machines."
    : "";
}

function applyAction(model) {
  if (!model.logsOnly) return [];
  return [{
    act: "apply",
    label: "Apply on this environment",
    primary: false,
    disabled: !canRun(),
    title: canRun() ? "Jobs from here change the machines." : "Requires operator role",
  }];
}

function cacheLine(model) {
  return model.cacheDown ? " Image cache is not up. Machines pull from it during install." : "";
}

function cacheAction(model) {
  if (!model.cacheDown) return [];
  return [{ act: "cache", label: "Open image cache", primary: false }];
}

function adminTitle() {
  return canAdmin() ? "" : "Requires admin role";
}

function cloudStagesOpen(model) {
  const states = stageStates(model.deploy);
  return STAGES.some(([id]) => states[id] !== "done");
}

// A management port can wipe disks. Offer it only before the cloud is up.
function managementAction(model) {
  if (!model.hasBmc || readyControlPlane(model) || !cloudStagesOpen(model)) return [];
  if (blockingNeed(model, "hosts")) return [];
  return [{
    act: "greenfield",
    label: "Install from the management port",
    primary: false,
    disabled: !canAdmin(),
    title: adminTitle() || "Replaces the running install and wipes the disks.",
  }];
}

function rackAction() {
  return { act: "rack", label: "Open rack", primary: false };
}

function osAction(model) {
  if (model.provider === "kubespray") {
    return { act: "ubuntu", label: "Open Ubuntu machines", primary: true };
  }
  return {
    act: "talos",
    label: "Talos is already installed",
    primary: true,
    disabled: !canAdmin(),
    title: adminTitle() || "Applies the Talos config. A machine already running Talos is replaced only after you confirm that machine by name.",
  };
}

export function describe(model) {
  const fleet = model.servers.length ? fleetSentence(model) : "";
  const logging = logLine(model);
  if (!model.servers.length) {
    return {
      title: "Add the machines.",
      detail: "A hostname and an address are enough. Adding a machine does not boot it and does not install OpenStack.",
      actions: [
        {
          act: "add",
          label: "Add a machine",
          primary: true,
          disabled: !canRun(),
          title: canRun() ? "" : "Requires operator role",
        },
        rackAction(),
      ],
      live: false,
    };
  }
  if (live(model.bootstrap) || live(model.deploy) || live(model.greenfield)) {
    const job = live(model.greenfield) ? model.greenfield : (live(model.deploy) ? model.deploy : model.bootstrap);
    const which = live(model.greenfield) ? "Install from the management port" : (live(model.deploy) ? "Deploy" : "Talos bootstrap");
    return {
      title: `${which} is running.`,
      detail: `${fleet} The log is in Activity.${logging}`,
      actions: [],
      log: jobLog(job, "Job log"),
      live: true,
    };
  }
  if (model.provider === "talos" && identityMismatch(model)) {
    return {
      title: "The saved Talos identity does not match these machines.",
      detail: `${fleet} Adopt the running cluster's talosconfig, or replace one named machine. Replacing reboots that machine into the installer and removes the running install. A Ready control plane is replaced only when you pick it.${logging}`,
      actions: [
        { act: "adopt", label: "Adopt talosconfig", primary: true },
        { act: "replace", label: "Choose the machine", primary: false },
        rackAction(),
        ...managementAction(model),
        ...applyAction(model),
      ],
      log: jobLog(model.bootstrap, "Confirm on the job"),
      live: false,
    };
  }
  const block = deployBlock(model);
  const states = stageStates(model.deploy);
  const pending = STAGES.find(([id]) => states[id] !== "done");
  const started = STAGES.some(([id]) => states[id] !== "not-started");
  if (model.provider === "kubespray" && !started) {
    if (!pending) return operate(model, fleet);
    const unrecorded = model.servers.some((row) => !String((row && row.adopt) || "").trim());
    if (unrecorded) {
      return {
        title: "Record Ubuntu on each machine.",
        detail: `${fleet} Select the rows on the Ubuntu tab, then Already have an OS. That records the machine. It does not reboot it.${cacheLine(model)}${logging}`,
        actions: [
          { act: "ubuntu", label: "Open Ubuntu machines", primary: true },
          rackAction(),
          ...managementAction(model),
          ...cacheAction(model),
          ...applyAction(model),
        ],
        live: false,
      };
    }
    const sshBlock = blockingNeed(model, "hosts");
    const sshNotes = noticeText(model, "hosts");
    const sshNote = sshNotes ? ` ${sshNotes}` : "";
    if (sshBlock) {
      return {
        title: String(sshBlock.missing || sshBlock.detail),
        detail: `${fleet} ${sshBlock.detail}${sshNote}${cacheLine(model)}${logging}`,
        actions: [
          { act: "config", label: "Open config", primary: true },
          {
            act: "deploy",
            label: "Deploy over SSH",
            primary: false,
            disabled: true,
            title: String(sshBlock.detail || ""),
          },
          rackAction(),
          ...cacheAction(model),
          ...applyAction(model),
        ],
        live: false,
      };
    }
    return {
      title: "Deploy over SSH.",
      detail: `${fleet} Already have an OS recorded these machines. Deploy installs Kubernetes and OpenStack over SSH. It does not reboot them.${sshNote}${cacheLine(model)}${logging}`,
      actions: [
        {
          act: "deploy",
          label: "Deploy over SSH",
          primary: true,
          disabled: !canAdmin(),
          title: adminTitle() || "Installs Kubernetes and OpenStack over SSH. It does not reboot the machines.",
        },
        rackAction(),
        ...managementAction(model),
        ...cacheAction(model),
        ...applyAction(model),
      ],
      live: false,
    };
  }
  if (model.provider === "kubespray" && !pending) return operate(model, fleet);
  if (model.provider !== "kubespray" && block && (!model.deploy || !pending || states[pending[0]] === "not-started")) {
    const boot = model.bootstrap && model.bootstrap.status === "success";
    const installing = `Boot the Talos ISO on each machine yourself. This button does not boot it. The boot image default is ${bootImage(model)}. The install disk defaults to /dev/sda. When the machine is waiting for a config, use Talos is already installed. A machine already running Talos is replaced only after that machine is named.`;
    const ahead = blockingNeed(model, "infrastructure");
    const aheadNotes = noticeText(model, "infrastructure");
    const aheadText = `${ahead ? ` ${ahead.detail}` : ""}${aheadNotes ? ` ${aheadNotes}` : ""}`;
    return {
      title: boot ? block : "Install Talos on the machines you added.",
      detail: `${fleet} ${boot ? "Talos bootstrap already finished." : installing}${aheadText}${cacheLine(model)}${logging}`,
      actions: [
        {
          act: "talos",
          label: "Talos is already installed",
          primary: true,
          disabled: !canAdmin(),
          title: adminTitle() || "Applies the Talos config. A machine already running Talos is replaced only after you confirm that machine by name.",
        },
        rackAction(),
        ...managementAction(model),
        ...cacheAction(model),
        ...applyAction(model),
      ],
      log: jobLog(model.bootstrap, "Job log"),
      live: false,
    };
  }
  if (!pending) return operate(model, fleet);
  const failed = STAGES.find(([id]) => states[id] === "failed");
  const next = failed || pending;
  const title = failed
    ? `Stopped at ${next[1]}.`
    : (started ? `Continue from ${next[1]}.` : "Install the cloud from infrastructure.");
  const label = failed || started ? `Continue from ${next[1]}` : "Deploy from infrastructure";
  const offered = next[0];
  const fact = blockingNeed(model, offered);
  const notes = noticeText(model, offered);
  const noteBit = notes ? ` ${notes}` : "";
  if (fact) {
    return {
      title: String(fact.missing || fact.detail),
      detail: `${fleet} ${fact.detail}${noteBit}${cacheLine(model)}${logging}`,
      actions: [
        { act: "config", label: "Open config", primary: true },
        {
          act: "deploy",
          label,
          stage: offered,
          primary: false,
          disabled: true,
          title: String(fact.detail || ""),
        },
        rackAction(),
        ...cacheAction(model),
        ...applyAction(model),
      ],
      log: jobLog(model.deploy, "Job log"),
      live: false,
    };
  }
  return {
    title,
    detail: `${fleet} ${block || "Kubernetes stays as it is. Talos bootstrap does not run again."}${noteBit} A disk wipe is separate. It is Greenfield metal wipe under Maintenance on Overview.${cacheLine(model)}${logging}`,
    actions: [
      {
        act: "deploy",
        label,
        stage: offered,
        primary: true,
        disabled: !canAdmin() || !!block,
        title: !canAdmin() ? "Requires admin role" : (block || `Start at ${next[1]}`),
      },
      rackAction(),
      ...managementAction(model),
      ...cacheAction(model),
      ...applyAction(model),
    ],
    log: jobLog(model.deploy, "Job log"),
    live: false,
  };
}

function operate(model, fleet) {
  const outside = outsideCluster(model);
  const shared = [
    { act: "config", label: "Change a service", primary: false },
    { act: "check", label: "Check the cloud", primary: false, disabled: !canRun(), title: canRun() ? "Short verify. It does not install OpenStack again." : "Requires operator role" },
    { act: "openstack", label: "Open OpenStack", primary: false },
    rackAction(),
    ...cacheAction(model),
  ];
  if (outside.length) {
    const named = outside.map((row) => row.hostname).filter(Boolean).join(", ");
    return {
      title: "A machine is not in the cloud yet.",
      detail: `${fleet} ${named} is not in the cloud yet. It still needs an operating system, then the compute stage. Adding it did not join it. Change a service in Settings, Config. Save stores a version. Push writes it. Check the cloud runs a short verify.${cacheLine(model)} Kubeconfig and talosconfig are in the vault. Renew them from Machines before the client certificate is a year old.`,
      actions: [
        osAction(model),
        {
          act: "deploy",
          label: "Continue from compute-network",
          stage: "compute-network",
          primary: false,
          disabled: !canAdmin(),
          title: adminTitle() || "Start at compute-network",
        },
        { act: "capacity", label: "Add capacity", primary: false, disabled: !canRun(), title: canRun() ? "" : "Requires operator role" },
        ...shared,
      ],
      live: false,
    };
  }
  return {
    title: "The cloud is installed.",
    detail: `${fleet} Add capacity records a machine. It still needs an operating system, then the compute stage. Change a service in Settings, Config. Save stores a version. Push writes it. Check the cloud runs a short verify.${cacheLine(model)} Kubeconfig and talosconfig are in the vault. Renew them from Machines before the client certificate is a year old.`,
    actions: [
      { act: "capacity", label: "Add capacity", primary: true, disabled: !canRun(), title: canRun() ? "" : "Requires operator role" },
      ...shared,
    ],
    live: false,
  };
}

function clickTab(selector) {
  const el = document.querySelector(selector);
  if (el) el.click();
}

async function runAction(btn) {
  const act = btn.dataset.nextAct || "";
  const envId = currentEnv;
  if (act === "add" || act === "capacity") {
    clickTab('.tab[data-tab="platform"][data-ptab="machines"]');
    setTimeout(() => {
      const open = document.getElementById("srv-add-open");
      if (open && !open.disabled) open.click();
    }, 0);
    return;
  }
  if (act === "ubuntu") {
    clickTab('.tab[data-tab="platform"][data-ptab="machines"]');
    setTimeout(() => {
      const tab = document.querySelector('[data-ostab="ubuntu"]');
      if (tab) tab.click();
    }, 0);
    return;
  }
  if (act === "replace") {
    clickTab('.tab[data-tab="platform"][data-ptab="machines"]');
    setTimeout(() => {
      const card = document.getElementById("srv-identity");
      if (card) card.scrollIntoView({ block: "center" });
    }, 400);
    return;
  }
  if (act === "adopt") {
    clickTab('.tab[data-tab="vault"]');
    return;
  }
  if (act === "cache") {
    clickTab('.tab[data-tab="cache"]');
    return;
  }
  if (act === "config") {
    clickTab('.tab[data-tab="settings"]');
    setTimeout(() => {
      const tab = document.querySelector('[data-stab="config"]');
      if (tab) tab.click();
    }, 0);
    return;
  }
  if (act === "openstack") {
    clickTab('.tab[data-tab="platform"][data-ptab="openstack"]');
    return;
  }
  if (act === "rack") {
    clickTab('.tab[data-tab="rack"]');
    return;
  }
  if (act === "apply") {
    const apply = document.querySelector('[data-env-mode="apply"]');
    if (apply) apply.click();
    return;
  }
  if (!envId) return;
  if (act === "talos") {
    if (!window.confirm(
      "Point this console at Talos that is already installed?\n\nThis uses every saved host, not one row. Each machine must already be running Talos and waiting for a config.\n\nThe console does not power a machine until you confirm that machine by name. Confirming replaces the running install. A management port can do that reboot after the confirm.\n\nIf this environment only logs, the job writes a log and does not change the machines."
    )) return;
    await enqueue(envId, "genestack.talos.bootstrap", {}, "Talos bootstrap");
    return;
  }
  if (act === "greenfield") {
    const hosts = currentModel && Array.isArray(currentModel.bmcHosts) ? currentModel.bmcHosts : [];
    const named = hosts.join(", ");
    if (!window.confirm(
      `Install from the management port on ${named}?\n\nThe running install will be replaced and the disks are wiped.\n\nIf this environment only logs, the job writes a log and does not change the machines.`
    )) return;
    await enqueue(envId, "genestack.greenfield", {
      skip_push: true,
      boot: "auto",
      parallelism: 2,
    }, "Install from the management port");
    return;
  }
  if (act === "deploy") {
    const stage = btn.dataset.nextStage || "";
    if (!stage || stage === "hosts") {
      const notes = noticeText(currentModel, "hosts");
      const extra = notes ? `\n\n${notes}` : "";
      if (!window.confirm(
        `Deploy over SSH?\n\nDeploy installs Kubernetes and OpenStack over SSH. It does not reboot the machines.\n\nIf this environment only logs, the job writes a log and does not change the machines.${extra}`
      )) return;
      await enqueue(envId, "genestack.deploy", { parallelism: 2 }, "Deploy over SSH");
      return;
    }
    const label = (STAGES.find(([id]) => id === stage) || ["", stage])[1];
    const notes = noticeText(currentModel, stage);
    const extra = notes ? `\n\n${notes}` : "";
    if (!window.confirm(
      `Continue from ${label}?\n\nKubernetes stays as it is. This does not run Talos bootstrap again.\n\nIf this environment only logs, the job writes a log and does not change the machines.${extra}`
    )) return;
    await enqueue(envId, "genestack.deploy", { from_stage: stage, parallelism: 2 }, `Deploy from ${label}`);
    return;
  }
  if (act === "check") {
    if (!window.confirm(
      "Check the cloud?\n\nThis runs a short verify. It does not install OpenStack again.\n\nIf this environment only logs, the job writes a log and does not change the machines."
    )) return;
    await enqueue(envId, "genestack.verify", { level: "quick" }, "Verify");
  }
}

async function enqueue(envId, operation, params, label) {
  try {
    const job = await api(`/api/v1/environments/${encodeURIComponent(envId)}/jobs`, {
      method: "POST",
      body: JSON.stringify({ operation, params }),
    });
    const id = job && job.id != null ? String(job.id) : "";
    toast(id ? `${label} queued ${id.slice(0, 8)}` : `${label} queued`, "ok");
    loadNextStrip(envId);
  } catch (err) {
    toast(err && err.message ? err.message : `${label} did not start`, "bad");
  }
}
