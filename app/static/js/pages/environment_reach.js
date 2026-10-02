// How this console reaches one environment: WireGuard peer, Tailscale
// address, or a Cloudflare hostname. Private keys are shown once.
import { api, esc, toast } from "../api.js";

export function reachCardHtml() {
  return `<div class="card" id="reach-card">
    <div class="toolbar">
      <h2>Reach</h2>
      <span class="muted" id="reach-msg"></span>
    </div>
    <p class="muted" style="font-size:.78rem">
      A path from this deploy host to the environment. WireGuard assigns the address.
      Tailscale and Cloudflare use a name you already have. A private key is shown once.
    </p>
    <div id="reach-links" class="muted">Loading…</div>
    <form id="reach-wg" class="install-form" style="margin-top:.6rem">
      <label class="check"><input name="use_for_ssh" type="checkbox" /> Use as deploy host if none is set</label>
      <button class="secondary btn-sm" type="submit">Create WireGuard peer</button>
    </form>
    <pre id="reach-wg-config" hidden style="white-space:pre-wrap;font-size:.75rem"></pre>
    <form id="reach-ts" class="install-form">
      <input name="address" type="text" placeholder="tailnet address or MagicDNS name" maxlength="253" />
      <button class="secondary btn-sm" type="submit">Save Tailscale</button>
    </form>
    <form id="reach-cf" class="install-form">
      <input name="address" type="text" placeholder="published hostname" maxlength="253" />
      <input name="local_port" type="number" min="1" max="65535" placeholder="local port" />
      <button class="secondary btn-sm" type="submit">Save Cloudflare</button>
      <button class="secondary btn-sm" type="button" id="reach-cf-forward">Forward</button>
    </form>
  </div>`;
}

export function wireReachCard(getEnvId) {
  const card = document.getElementById("reach-card");
  if (!card) return;
  card.addEventListener("submit", (e) => {
    const form = e.target;
    if (!(form instanceof HTMLFormElement)) return;
    e.preventDefault();
    const envId = getEnvId();
    if (!envId) return;
    if (form.id === "reach-wg") saveWireguard(envId, form);
    else if (form.id === "reach-ts") saveAddress(envId, "tailscale", form);
    else if (form.id === "reach-cf") saveAddress(envId, "cloudflare", form);
  });
  document.getElementById("reach-cf-forward")?.addEventListener("click", () => {
    const envId = getEnvId();
    if (envId) forwardCloudflare(envId);
  });
}

export function destroyReachCard() {}

export async function loadReachCard(envId) {
  const host = document.getElementById("reach-links");
  if (!host || !envId) return;
  try {
    const rows = await api(`/api/v1/environments/${encodeURIComponent(envId)}/reach`);
    if (!rows.length) {
      host.innerHTML = '<div class="muted">No path saved yet.</div>';
      return;
    }
    host.innerHTML = rows
      .map((row) => {
        const bits = [row.kind, row.name, row.address || "", row.status || ""]
          .filter(Boolean)
          .map((part) => esc(part))
          .join(" · ");
        return `<div class="agent-row"><span>${bits}</span>
          <button class="secondary btn-sm" type="button" data-reach-delete="${esc(row.kind)}" data-reach-name="${esc(row.name)}">Remove</button></div>`;
      })
      .join("");
    host.querySelectorAll("[data-reach-delete]").forEach((btn) => {
      btn.addEventListener("click", () => removeLink(envId, btn.dataset.reachDelete, btn.dataset.reachName));
    });
  } catch (e) {
    host.innerHTML = `<div class="error">${esc(e.message)}</div>`;
  }
}

async function saveWireguard(envId, form) {
  const msg = document.getElementById("reach-msg");
  const box = document.getElementById("reach-wg-config");
  try {
    const row = await api(`/api/v1/environments/${encodeURIComponent(envId)}/reach/wireguard`, {
      method: "POST",
      body: JSON.stringify({
        name: "default",
        use_for_ssh: form.elements.use_for_ssh.checked,
      }),
    });
    if (box && row.client_config) {
      box.hidden = false;
      box.textContent = row.client_config;
    }
    if (msg) msg.textContent = row.address ? `Peer ${row.address}` : "Peer created";
    toast("WireGuard peer created. Copy the config now.", "ok");
    await loadReachCard(envId);
  } catch (e) {
    toast(e.message, "bad");
  }
}

async function saveAddress(envId, kind, form) {
  const address = form.elements.address.value.trim();
  const body = { name: "default", address, use_for_ssh: false };
  if (kind === "cloudflare" && form.elements.local_port && form.elements.local_port.value) {
    body.local_port = Number(form.elements.local_port.value);
  }
  try {
    await api(`/api/v1/environments/${encodeURIComponent(envId)}/reach/${kind}`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    toast("Saved", "ok");
    await loadReachCard(envId);
  } catch (e) {
    toast(e.message, "bad");
  }
}

async function forwardCloudflare(envId) {
  try {
    const row = await api(
      `/api/v1/environments/${encodeURIComponent(envId)}/reach/cloudflare/default/forward`,
      { method: "POST" }
    );
    toast(row.detail || row.status, "ok");
    await loadReachCard(envId);
  } catch (e) {
    toast(e.message, "bad");
  }
}

async function removeLink(envId, kind, name) {
  try {
    await api(
      `/api/v1/environments/${encodeURIComponent(envId)}/reach/${encodeURIComponent(kind)}/${encodeURIComponent(name)}`,
      { method: "DELETE" }
    );
    const box = document.getElementById("reach-wg-config");
    if (box && kind === "wireguard") {
      box.hidden = true;
      box.textContent = "";
    }
    await loadReachCard(envId);
  } catch (e) {
    toast(e.message, "bad");
  }
}
