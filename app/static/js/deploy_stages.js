// Deploy stage board. A job that starts at a later stage does not repeat
// earlier stages in its log. Those earlier stages are already in place.

export const DEPLOY_STAGES = [
  ["infrastructure", "infrastructure"],
  ["operators", "operators"],
  ["cni", "kube-ovn"],
  ["core", "core"],
  ["compute-network", "compute-network"],
  ["platform-extras", "platform-extras"],
];

const PIPE = [
  "hosts",
  "infrastructure",
  "operators",
  "cni",
  "core",
  "compute-network",
  "platform-extras",
  "observability",
  "testing",
];

function stageParam(job, key) {
  const params = job && job.params;
  const value = params && typeof params[key] === "string" ? params[key] : "";
  return PIPE.includes(value) ? value : "";
}

function markPrior(state, order, stageId) {
  const at = PIPE.indexOf(stageId);
  if (at < 0) return;
  order.forEach((id) => {
    const pos = PIPE.indexOf(id);
    if (pos >= 0 && pos < at && state[id] !== "failed") state[id] = "done";
  });
}

export function stageStates(job, stages = DEPLOY_STAGES) {
  const order = stages.map(([id]) => id);
  const state = {};
  order.forEach((id) => { state[id] = "not-started"; });
  if (!job) return state;
  const text = `${job.log_text || ""}\n${job.error || ""}`;
  order.forEach((id) => {
    if (text.includes(`[deploy] stage ${id} complete`)) state[id] = "done";
  });
  const status = String(job.status || "");
  const failed = text.match(/FAILED at ([a-z0-9_-]+)/i);
  if (status === "failed" && failed && order.includes(failed[1])) state[failed[1]] = "failed";
  const from = stageParam(job, "from_stage");
  const until = stageParam(job, "until_stage");
  const starts = [...text.matchAll(/=== stage \d+\/\d+: ([a-z0-9_-]+)/g)];
  const current = (starts.length ? starts[starts.length - 1][1] : "") || from;
  if (status === "queued" || status === "running" || status === "failed") {
    if (current) markPrior(state, order, current);
    else if (from) markPrior(state, order, from);
  }
  if ((status === "queued" || status === "running") && order.includes(current) && state[current] !== "done") {
    state[current] = "running";
  }
  if (status === "success") {
    const beginId = from || order[0] || "";
    const endId = until || order[order.length - 1] || "";
    const begin = PIPE.indexOf(beginId);
    const end = PIPE.indexOf(endId);
    if (beginId) markPrior(state, order, beginId);
    order.forEach((id) => {
      const pos = PIPE.indexOf(id);
      if (pos >= 0 && begin >= 0 && pos >= begin && (end < 0 || pos <= end) && state[id] !== "failed") {
        state[id] = "done";
      }
    });
  }
  return state;
}

export function hasRequiredRole(server, role) {
  const roles = Array.isArray(server && server.roles) ? server.roles.map((item) => String(item)) : [];
  if (role === "storage") {
    return roles.some((name) => name === "storage" || name === "storage-ceph" || name === "storage-cinder");
  }
  return roles.includes(role);
}
