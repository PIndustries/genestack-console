// metal_path.js — Talos or Kubespray for one environment.
// Talos is the preferred direct boot. Kubespray adopts machines that
// already have an OS. Genestack after that is the same work.
// An empty string means the provider has not been read.

export function metalTabLabel(provider) {
  if (provider === "kubespray") return "Kubespray";
  if (provider === "talos") return "Talos";
  return "Cluster";
}

export function metalLead(provider) {
  if (provider === "kubespray") {
    return "Kubespray on machines that already have an OS. Kubernetes and OpenStack stay the same.";
  }
  if (provider === "talos") {
    return "Talos on the metal, the preferred direct boot. Versions, Ready machines, logs, and day-2 upgrades.";
  }
  return "Cluster machines. Talos is the preferred direct boot. Kubespray adopts machines that already have an OS.";
}

export function metalPathSentence(provider) {
  if (provider === "kubespray") {
    return "Cluster path is Kubespray. That adopts machines that already have an OS. On a row, Reinstall keeps that OS. The menu beside it installs Ubuntu or Talos.";
  }
  if (provider === "talos") {
    return "Cluster path is Talos, the preferred direct boot. On a row, Reinstall keeps that OS. The menu beside it installs Ubuntu or Talos.";
  }
  return "Reading the metal path for this environment…";
}

export function metalPathLine(provider) {
  if (provider === "kubespray") return "Kubespray — adopts machines that already have an OS. Genestack on top is the same.";
  if (provider === "talos") return "Talos — the preferred direct boot onto the metal.";
  return "Reading the metal path…";
}

export function osMark(kind) {
  if (kind === "ubuntu") {
    return `<svg class="os-mark" viewBox="0 0 16 16" aria-hidden="true" focusable="false"><circle cx="8" cy="8" r="8" fill="#E95420"/><circle cx="8" cy="4.35" r="1.35" fill="#fff"/><circle cx="4.75" cy="10.15" r="1.35" fill="#fff"/><circle cx="11.25" cy="10.15" r="1.35" fill="#fff"/></svg>`;
  }
  if (kind === "talos") {
    return `<svg class="os-mark" viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path fill="#e8eef5" d="M8 1.15 14.35 4.75v6.5L8 14.85 1.65 11.25v-6.5L8 1.15z"/><path fill="#172033" d="M8 4.05 11.45 6v3.95L8 11.9 4.55 9.95V6L8 4.05z"/></svg>`;
  }
  return "";
}

export function osNameHtml(kind, label) {
  const mark = osMark(kind);
  const text = label || (kind === "ubuntu" ? "Ubuntu" : kind === "talos" ? "Talos" : "");
  if (!mark) return text;
  return `<span class="os-name">${mark}<span>${text}</span></span>`;
}

export function clusterOsHtml(provider) {
  if (provider === "kubespray") {
    return `<span class="pill ok">Kubespray</span> <span class="muted">in the cluster</span>`;
  }
  if (provider === "talos") {
    return `<span class="pill ok">${osNameHtml("talos")}</span> <span class="muted">in the cluster</span>`;
  }
  return `<span class="pill ok">In cluster</span> <span class="muted">metal path unread</span>`;
}

export async function fetchMetalPath(api, envId) {
  if (!envId || typeof api !== "function") return "";
  try {
    const data = await api(`/api/v1/environments/${encodeURIComponent(envId)}/config/provider`);
    const provider = data && data.provider;
    if (provider === "talos" || provider === "kubespray") return provider;
  } catch {
    /* leave unread */
  }
  return "";
}
