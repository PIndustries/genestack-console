// roles.js — single source of truth for inventory role ids + display labels.
// Import from every module that renders role pickers (servers card, discovery
// card, env wizard) so the lists can't drift apart.
export const ROLES = [
  "k8s_control_plane",
  "etcd",
  "control",
  "compute",
  "network",
  "storage",
  "storage-ceph",
  "storage-cinder",
  "worker",
];

// Checkbox labels for roles whose inventory target benefits from a hint.
export const ROLE_LABELS = {
  storage: "storage (longhorn)",
  "storage-ceph": "storage-ceph (rook)",
  "storage-cinder": "storage-cinder (netapp)",
  worker: "worker",
};

export const REQUIRED_ROLES = ["k8s_control_plane", "etcd", "control"];
