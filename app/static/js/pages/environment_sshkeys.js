// pages/environment_sshkeys.js — SSH key management card for the Config tab.
import { api, esc, toast } from "../api.js";
import { roleAtLeast, gate } from "../store.js";

export function sshKeysCardHtml() {
  return `<div class="card" id="ssh-keys-card">
    <style>
      #ssh-keys-card .sshkey-toolbar { display: flex; align-items: center; gap: .5rem; justify-content: space-between; }
      #ssh-keys-card .sshkey-toolbar h2 { margin: 0; font-size: .95rem; }
      #ssh-keys-card .sshkey-body { margin-top: .5rem; }
      #ssh-keys-card .sshkey-key-box {
        background: rgba(0,0,0,.25); border-radius: .3rem; padding: .5rem .65rem;
        font-family: 'SF Mono', 'Fira Code', monospace; font-size: .75rem;
        word-break: break-all; margin: .4rem 0; line-height: 1.5;
        border: 1px solid var(--border, #2a2a2a);
      }
      #ssh-keys-card .sshkey-actions { display: flex; gap: .5rem; margin-top: .5rem; }
      #ssh-keys-card .sshkey-fp { font-size: .75rem; color: var(--fg-muted, #666); margin-top: .2rem; }
      #ssh-keys-card .sshkey-empty { padding: 1rem 0; }
    </style>
    <div class="sshkey-toolbar">
      <h2>SSH Keys</h2>
      <span class="muted" id="sshkey-msg"></span>
    </div>
    <div class="sshkey-body" id="sshkey-body">
      <div class="card-empty">Loading…</div>
    </div>
  </div>`;
}

let keyData = null;

export function wireSshKeysCard(getEnvId) {
  const card = document.getElementById("ssh-keys-card");
  if (!card) return;

  card.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-sshkey-action]");
    if (!btn) return;
    const envId = getEnvId();
    if (!envId) return;

    if (btn.dataset.sshkeyAction === "copy-pub") {
      if (keyData && keyData.public_key) {
        navigator.clipboard.writeText(keyData.public_key).then(() => {
          toast("Public key copied to clipboard", "ok");
        }).catch(() => {
          toast("Copy failed — select and copy manually", "warn");
        });
      }
    } else if (btn.dataset.sshkeyAction === "download") {
      downloadPrivateKey(envId);
    } else if (btn.dataset.sshkeyAction === "regenerate") {
      if (confirm("Regenerate the SSH key pair? Existing nodes using the old key will lose access.")) {
        regenerateKey(envId);
      }
    }
  });
}

export async function loadSshKeysCard(envId) {
  const body = document.getElementById("sshkey-body");
  const msg = document.getElementById("sshkey-msg");
  if (!body) return;
  if (!envId) {
    body.innerHTML = `<div class="card-empty">Select an environment.</div>`;
    keyData = null;
    return;
  }
  if (msg) msg.textContent = "Loading…";
  try {
    keyData = await api(`/api/v1/environments/${encodeURIComponent(envId)}/ssh-key/public`);
  } catch (e) {
    keyData = null;
    if (msg) msg.textContent = "";
    body.innerHTML = `<div class="card-empty">SSH key endpoint unavailable: ${esc(String(e.message))}</div>`;
    return;
  }
  if (msg) msg.textContent = "";
  render(keyData);
}

function render(data) {
  const body = document.getElementById("sshkey-body");
  if (!body) return;

  if (!data || !data.has_key) {
    body.innerHTML = `<div class="sshkey-empty">
      <div class="card-empty">
        <p class="muted">No SSH key pair generated yet.</p>
        <p class="muted">Keys are auto-generated when you create an environment. This may indicate an older environment.</p>
        <div class="sshkey-actions">
          <button class="btn-sm" data-sshkey-action="regenerate" ${gate(roleAtLeast("operator"), "operator")}>Generate key pair</button>
        </div>
      </div>
    </div>`;
    return;
  }

  const pubKey = data.public_key || "";
  const fp = data.fingerprint || "";

  body.innerHTML = `
    <div>
      <div style="font-size:.78rem;color:var(--fg-muted,#666);margin-bottom:.15rem">Public key (used by all nodes)</div>
      <div class="sshkey-key-box" id="sshkey-pub">${esc(pubKey)}</div>
      ${fp ? `<div class="sshkey-fp">Fingerprint: <code>${esc(fp)}</code></div>` : ""}
    </div>
    <div class="sshkey-actions">
      <button class="btn-sm" data-sshkey-action="copy-pub" ${gate(roleAtLeast("viewer"), "viewer")}>Copy public key</button>
      <button class="btn-sm secondary" data-sshkey-action="download" ${gate(roleAtLeast("operator"), "operator")}>Download private key</button>
      <span style="flex:1"></span>
      <button class="btn-sm warn" data-sshkey-action="regenerate" ${gate(roleAtLeast("operator"), "operator")}>Regenerate</button>
    </div>`;
}

async function regenerateKey(envId) {
  const msg = document.getElementById("sshkey-msg");
  const btn = document.querySelector('#ssh-keys-card [data-sshkey-action="regenerate"]');
  if (msg) msg.textContent = "Generating…";
  if (btn) btn.disabled = true;
  try {
    const result = await api(`/api/v1/environments/${encodeURIComponent(envId)}/ssh-key/regenerate`, {
      method: "POST",
    });
    toast(result.message || "Key pair regenerated", "ok");
    await loadSshKeysCard(envId);
  } catch (e) {
    let errMsg = e.message;
    if (e.isNetwork || e.isTimeout) errMsg = "Failed to reach server. Check your connection and try again.";
    toast(`SSH key regeneration failed: ${errMsg}`, "error");
    const m = document.getElementById("sshkey-msg");
    if (m) m.textContent = "";
  } finally {
    if (btn) btn.disabled = false;
    if (msg) msg.textContent = "";
  }
}

async function downloadPrivateKey(envId) {
  const msg = document.getElementById("sshkey-msg");
  if (msg) msg.textContent = "Fetching…";
  try {
    const result = await api(`/api/v1/environments/${encodeURIComponent(envId)}/ssh-key/private`);
    const content = `-----BEGIN OPENSSH PRIVATE KEY-----\n${(result.private_key || "").split("-----BEGIN OPENSSH PRIVATE KEY-----\n")[1] || result.private_key}`;
    const blob = new Blob([content], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `genestack-${envId.slice(0, 8)}-id_ed25519`;
    a.click();
    URL.revokeObjectURL(url);
    toast("Private key downloaded (store securely!)", "ok");
    if (msg) msg.textContent = "";
  } catch (e) {
    let msg = e.message;
    if (e.isNetwork || e.isTimeout) msg = "Failed to reach server. Check your connection and try again.";
    toast(`Download failed: ${msg}`, "error");
    if (msg) msg.textContent = "";
  }
}
