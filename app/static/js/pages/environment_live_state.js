// First-class live environment store. Snapshot + event patches.
// Polls may reconcile; they must never clobber a good frame with empty.

const listeners = new Set();

export const live = {
  envId: "",
  snap: null,
  pipe: null,
  job: null,
  recentJobs: [],
  workloads: null,
  osVms: [],
  bmLive: [],
  catalog: [],
  cluster: null,
  observe: null,
  platform: null,
  osCloud: null,
  k8sIngress: [],
  k8sServices: [],
  k8sGateways: [],
  k8sRoutes: [],
  k8sPools: [],
  gen: 0,
};

export function subscribeLive(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function notify() {
  live.gen += 1;
  listeners.forEach((fn) => {
    try {
      fn(live);
    } catch {
      /* one subscriber must not kill the others */
    }
  });
}

function nonemptyArray(v) {
  return Array.isArray(v) && v.length > 0;
}

function hasPods(wl) {
  return !!(wl && Array.isArray(wl.pods) && wl.pods.length);
}

const OS_CLOUD_LISTS = [
  "servers",
  "networks",
  "projects",
  "floating_ips",
  "routers",
  "subnets",
  "load_balancers",
  "ports",
];

function hasOsCloud(cloud) {
  if (!cloud) return false;
  if (cloud.available) return true;
  return OS_CLOUD_LISTS.some((k) => nonemptyArray(cloud[k]));
}

export function bindLiveEnv(envId) {
  const id = envId || "";
  if (live.envId && live.envId !== id) {
    live.snap = null;
    live.pipe = null;
    live.job = null;
    live.recentJobs = [];
    live.workloads = null;
    live.osVms = [];
    live.bmLive = [];
    live.catalog = [];
    live.cluster = null;
    live.observe = null;
    live.platform = null;
    live.osCloud = null;
    live.k8sIngress = [];
    live.k8sServices = [];
    live.k8sGateways = [];
    live.k8sRoutes = [];
    live.k8sPools = [];
  }
  live.envId = id;
}

export function mergeSnapshot(state, patch) {
  if (!state || !patch) return state;
  if (patch.snap) {
    const prevN = ((state.snap && state.snap.nodes) || []).length;
    const nextN = ((patch.snap.nodes) || []).length;
    state.snap = nextN || !prevN ? patch.snap : { ...patch.snap, nodes: state.snap.nodes };
  }
  if (patch.pipe) state.pipe = patch.pipe;
  if (patch.job) state.job = { ...(state.job || {}), ...patch.job };
  if (nonemptyArray(patch.recentJobs)) state.recentJobs = patch.recentJobs;
  if (hasPods(patch.workloads) || (patch.workloads && !state.workloads)) {
    state.workloads = patch.workloads;
  }
  if (nonemptyArray(patch.osVms)) state.osVms = patch.osVms;
  if (nonemptyArray(patch.bmLive)) state.bmLive = patch.bmLive;
  if (nonemptyArray(patch.catalog)) state.catalog = patch.catalog;
  if (patch.cluster && (patch.cluster.ok !== false || !state.cluster)) state.cluster = patch.cluster;
  if (patch.observe) state.observe = patch.observe;
  if (patch.platform) state.platform = patch.platform;
  if (hasOsCloud(patch.osCloud)) state.osCloud = patch.osCloud;
  if (nonemptyArray(patch.k8sIngress) || (patch.k8sIngress && !nonemptyArray(state.k8sIngress))) {
    state.k8sIngress = patch.k8sIngress;
  }
  if (nonemptyArray(patch.k8sServices) || (patch.k8sServices && !nonemptyArray(state.k8sServices))) {
    state.k8sServices = patch.k8sServices;
  }
  if (nonemptyArray(patch.k8sGateways) || (patch.k8sGateways && !nonemptyArray(state.k8sGateways))) {
    state.k8sGateways = patch.k8sGateways;
  }
  if (nonemptyArray(patch.k8sRoutes) || (patch.k8sRoutes && !nonemptyArray(state.k8sRoutes))) {
    state.k8sRoutes = patch.k8sRoutes;
  }
  if (nonemptyArray(patch.k8sPools) || (patch.k8sPools && !nonemptyArray(state.k8sPools))) {
    state.k8sPools = patch.k8sPools;
  }
  return state;
}

export function applyLive(patch) {
  mergeSnapshot(live, patch);
  notify();
  return live;
}

export function graphSignature(nodes, edges) {
  const n = (nodes || []).map((x) => x && x.id).filter(Boolean).join("\0");
  const e = (edges || []).map((x) => x.id || `${x.source}>${x.target}`).join("\0");
  return n + "#" + e;
}
