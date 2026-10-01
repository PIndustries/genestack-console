"""Secret encryption at rest (Fernet, key derived from settings.secret_key).

Stored values are prefixed with ``fernet:`` so legacy plaintext values in
existing dev databases keep working (decrypt falls through to the raw value).
Rotating ``secret_key`` requires re-encrypting stored secrets.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet

from app.config import Settings, get_settings

FERNET_PREFIX = "fernet:"


def _fernet(settings: Settings | None = None) -> Fernet:
    settings = settings or get_settings()
    key = base64.urlsafe_b64encode(
        hashlib.sha256(settings.secret_key.encode()).digest()
    )
    return Fernet(key)


def encrypt_secret(plain: str | None, settings: Settings | None = None) -> str | None:
    """Encrypt a secret for storage. Idempotent for already-encrypted values."""
    if not plain:
        return plain
    if plain.startswith(FERNET_PREFIX):
        return plain
    token = _fernet(settings).encrypt(plain.encode()).decode()
    return FERNET_PREFIX + token


def decrypt_secret(value: str | None, settings: Settings | None = None) -> str | None:
    """Decrypt a stored secret; legacy plaintext (no prefix) passes through."""
    if not value:
        return value
    if not value.startswith(FERNET_PREFIX):
        return value
    return _fernet(settings).decrypt(value[len(FERNET_PREFIX) :].encode()).decode()
