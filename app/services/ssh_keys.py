"""SSH key pair generation for per-environment host access."""

from __future__ import annotations

from typing import Optional

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    load_pem_private_key,
    load_ssh_private_key,
)

from app.services.crypto import decrypt_secret, encrypt_secret


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


def apply_private_key(env_obj, private: str) -> None:
    """Store an unencrypted SSH private key and the matching public key.

    Raises ValueError with a fixed message. The message does not include the key.
    """
    text = (private or "").strip()
    public = _public_from_private(text)
    stored = encrypt_secret(text)
    if not stored:
        raise ValueError("That value is not an unencrypted SSH private key.")
    env_obj.ssh_private_key_encrypted = stored
    env_obj.ssh_public_key = public


def _public_from_private(private: str) -> str:
    blob = private.encode()
    if not blob.strip():
        raise ValueError("That value is not an unencrypted SSH private key.")
    key = _load_private(blob)
    try:
        raw = key.public_key().public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise ValueError("That value is not an unencrypted SSH private key.") from None
    text = raw.decode().strip()
    if not text:
        raise ValueError("That value is not an unencrypted SSH private key.")
    return text


def _load_private(blob: bytes):
    errors = (ValueError, TypeError, UnsupportedAlgorithm)
    try:
        return load_ssh_private_key(blob, password=None)
    except errors:
        pass
    try:
        return load_pem_private_key(blob, password=None)
    except errors:
        raise ValueError("That value is not an unencrypted SSH private key.") from None
