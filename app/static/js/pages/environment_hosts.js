// A cluster that is already up. Machines is where servers are added.
// Ubuntu seed and MicroK8s stay on this card, behind the adopt form.
import { api, esc, toast } from "../api.js";

export function hostsCardHtml() {
  return `<div class="card" id="hosts-card">
    <div class="toolbar">
      <h2>Cluster already running</h2>
      <span class="muted" id="hosts-msg"></span>
    </div>
    <p class="muted" style="font-size:.78rem">
      Add servers on Machines. This card records a Genestack cluster that is already up.
      Paste the kubeconfig and choose Adopt Kubespray. That records the cluster.
      It does not clone Kubespray, run Ansible, or reinstall Kubernetes.
      Dry run must be off or the file is not stored. Do not Deploy until you mean to change that cloud.
    </p>
    <form id="hosts-kubespray" class="install-form">
      <textarea name="kubeconfig" rows="4" placeholder="kubeconfig" autocomplete="off"></textarea>
      <button class="secondary btn-sm" type="submit">Adopt Kubespray</button>
    </form>
    <details style="margin-top:.75rem">
      <summary>Ubuntu seed and MicroK8s</summary>
      <p class="muted" style="font-size:.78rem">
        Install Ubuntu on a Machines row is the boot for one machine.
        Prepare Ubuntu autoinstall writes that machine's seed. It does not install OpenStack.
      </p>
      <form id="hosts-ubuntu" class="install-form">
        <input name="hostname" type="text" placeholder="hostname" maxlength="63" autocomplete="off" />
        <input name="ssh_key" type="text" placeholder="SSH public key" autocomplete="off" />
        <button class="secondary btn-sm" type="submit">Prepare Ubuntu autoinstall</button>
      </form>
      <form id="hosts-microk8s" class="install-form">
        <input name="host" type="text" placeholder="host" maxlength="253" autocomplete="off" />
        <input name="ssh_user" type="text" placeholder="ssh user (ubuntu)" maxlength="32" autocomplete="off" />
        <button class="secondary btn-sm" type="submit">Install MicroK8s</button>
        <p class="muted" style="font-size:.78rem">Queues a job. SSH runs only when this environment is not in dry-run. The console default is a dry run.</p>
      </form>
      <p class="muted" style="font-size:.78rem">
        A machine that is already Ubuntu uses the existing agent.
        Run operation agent.install, or the one-liner on the Agents card.
        One command on each machine.
      </p>
    </details>
  </div>`;
}

export function wireHostsCard(getEnvId) {
  const card = document.getElementById("hosts-card");
  if (!card) return;
  card.addEventListener("submit", (e) => {
    const form = e.target;
    if (!(form instanceof HTMLFormElement)) return;
    e.preventDefault();
    const envId = getEnvId();
    if (!envId) return;
    if (form.id === "hosts-ubuntu") prepareUbuntu(envId, form);
    else if (form.id === "hosts-microk8s") installMicrok8s(envId, form);
    else if (form.id === "hosts-kubespray") adoptKubespray(envId, form);
  });
}

export function destroyHostsCard() {}

export function loadHostsCard(envId) {
  const msg = document.getElementById("hosts-msg");
  if (!msg) return;
  if (!envId) {
    msg.textContent = "";
    return;
  }
  msg.textContent = "";
}

async function postJob(envId, body) {
  return api(`/api/v1/environments/${encodeURIComponent(envId)}/jobs`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

async function prepareUbuntu(envId, form) {
  const hostname = form.elements.hostname.value.trim();
  const sshKey = form.elements.ssh_key.value.trim();
  if (!hostname || !sshKey) {
    toast("Hostname and an SSH public key are required", "bad");
    return;
  }
  try {
    const job = await postJob(envId, {
      operation: "hosts.ubuntu.prepare",
      params: { hostname, ssh_key: sshKey },
    });
    const id = job && job.id != null ? String(job.id) : "";
    const msg = document.getElementById("hosts-msg");
    if (msg) msg.textContent = `Ubuntu ${esc(id.slice(0, 8))}`;
    toast(`Ubuntu autoinstall queued ${id.slice(0, 8)}. This does not install OpenStack.`, "ok");
  } catch (e) {
    toast(e.message, "bad");
  }
}

async function installMicrok8s(envId, form) {
  const host = form.elements.host.value.trim();
  const sshUser = form.elements.ssh_user.value.trim() || "ubuntu";
  if (!host) {
    toast("Host is required", "bad");
    return;
  }
  try {
    const job = await postJob(envId, {
      operation: "hosts.microk8s.install",
      params: { host, ssh_user: sshUser },
    });
    const id = job && job.id != null ? String(job.id) : "";
    const msg = document.getElementById("hosts-msg");
    if (msg) msg.textContent = `MicroK8s ${esc(id.slice(0, 8))}`;
    toast(`MicroK8s job queued ${id.slice(0, 8)}. SSH runs only when dry-run is off.`, "ok");
  } catch (e) {
    toast(e.message, "bad");
  }
}

async function adoptKubespray(envId, form) {
  const kubeconfig = form.elements.kubeconfig.value;
  try {
    const job = await postJob(envId, {
      operation: "hosts.kubespray.adopt",
      params: { kubeconfig },
    });
    form.elements.kubeconfig.value = "";
    const id = job && job.id != null ? String(job.id) : "";
    const msg = document.getElementById("hosts-msg");
    if (msg) msg.textContent = `Kubespray ${esc(id.slice(0, 8))}`;
    toast(`Kubespray adopt queued ${id.slice(0, 8)}`, "ok");
  } catch (e) {
    toast(e.message, "bad");
  }
}
