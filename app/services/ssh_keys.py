"""SSH key pair generation for per-environment host access."""

from __future__ import annotations

from typing import Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.services.crypto import encrypt_secret, decrypt_secret


def _ed25519_to_openssh_private(key: Ed25519PrivateKey) -> str:
    """Serialize Ed25519 private key as OpenSSH PEM (unencrypted)."""
    der = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return der.decode()


def _ed25519_to_openssh_public(key: Ed25519PrivateKey, comment: str = "") -> str:
    """Serialize Ed25519 public key as OpenSSH one-liner."""
    der = key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    )
    parts = der.decode().strip().split()
    return f"{parts[0]} {parts[1]} {comment}".strip()


def generate_ed25519_key(comment: str = "genestack-env") -> dict[str, str]:
    """Generate a new Ed25519 SSH key pair in pure Python (no ssh-keygen binary needed).

    Returns {"private": ..., "public": ...}
    """
    key = Ed25519PrivateKey.generate()
    return {
        "private": _ed25519_to_openssh_private(key),
        "public": _ed25519_to_openssh_public(key, comment),
    }


def store_key_pair(env_obj, comment: str = "genestack-env") -> None:
    """Generate and store a new key pair on the Environment ORM object."""
    keys = generate_ed25519_key(f"{comment} ({env_obj.id[:8]})")
    env_obj.ssh_private_key_encrypted = encrypt_secret(keys["private"])
    env_obj.ssh_public_key = keys["public"]


def get_decrypted_private_key(env_obj) -> Optional[str]:
    """Decrypt and return the environment's SSH private key."""
    if not env_obj.ssh_private_key_encrypted:
        return None
    return decrypt_secret(env_obj.ssh_private_key_encrypted)
