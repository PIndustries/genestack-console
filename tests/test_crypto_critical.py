"""Test crypto — critical path functions.

Focused unit tests for encrypt_secret / decrypt_secret:
round-trip, idempotent, None handling.

Complements the existing test_crypto.py which covers basic cases.
"""

from __future__ import annotations

import pytest

from app.services.crypto import (
    FERNET_PREFIX,
    decrypt_secret,
    encrypt_secret,
    _fernet,
)


class TestEncryptSecret:
    """encrypt_secret — encryption, idempotency, edge cases."""

    def test_round_trip(self):
        plain = "consumer-key:token-key:token-secret"
        encrypted = encrypt_secret(plain)
        assert encrypted is not None
        assert encrypted.startswith(FERNET_PREFIX)
        assert plain not in encrypted
        assert decrypt_secret(encrypted) == plain

    def test_encrypt_is_idempotent(self):
        token = encrypt_secret("secret")
        assert encrypt_secret(token) == token

    def test_legacy_plaintext_passthrough(self):
        assert decrypt_secret("legacy-plain-value") == "legacy-plain-value"

    def test_none_handling(self):
        assert encrypt_secret(None) is None
        assert decrypt_secret(None) is None

    def test_empty_string_passthrough(self):
        assert encrypt_secret("") == ""
        assert decrypt_secret("") == ""

    def test_multiline_round_trip(self):
        payload = "apiVersion: v1\nclusters:\n- cluster:\n    server: https://x\n"
        assert decrypt_secret(encrypt_secret(payload)) == payload

    def test_unicode_round_trip(self):
        plain = "密码: secret-密钥"
        encrypted = encrypt_secret(plain)
        assert encrypted.startswith(FERNET_PREFIX)
        assert decrypt_secret(encrypted) == plain

    def test_binary_safe_base64_payload(self):
        """Encrypted value contains only safe characters."""
        plain = "secret-with-special-chars: @#$%^&*()"
        encrypted = encrypt_secret(plain)
        assert encrypted.startswith(FERNET_PREFIX)
        # Fernet tokens are base64url-safe
        token_part = encrypted[len(FERNET_PREFIX) :]
        assert "'" not in token_part

    def test_different_plaintexts_different_ciphertexts(self):
        enc1 = encrypt_secret("secret1")
        enc2 = encrypt_secret("secret2")
        assert enc1 != enc2

    def test_long_string_round_trip(self):
        plain = "x" * 10000
        assert decrypt_secret(encrypt_secret(plain)) == plain


class TestDecryptSecret:
    """decrypt_secret — decryption, edge cases, prefix handling."""

    def test_decrypt_encrypted_value(self):
        plain = "test-secret-value"
        encrypted = encrypt_secret(plain)
        assert decrypt_secret(encrypted) == plain

    def test_decrypt_without_prefix_returns_raw(self):
        """Values stored before encryption existed still pass through."""
        assert decrypt_secret("old-plain-text") == "old-plain-text"

    def test_decrypt_with_partial_prefix(self):
        """Value starting with 'fernet' but not 'fernet:' passes through."""
        assert decrypt_secret("fernetnotprefix") == "fernetnotprefix"

    def test_corrupt_fernet_token_raises(self):
        """Corrupt fernet: token raises an error."""
        with pytest.raises(Exception):
            decrypt_secret("fernet:not-a-valid-token")

    def test_none_returns_none(self):
        assert decrypt_secret(None) is None

    def test_empty_string_returns_empty(self):
        assert decrypt_secret("") == ""


class TestFernetInstance:
    """_fernet — Fernet instance construction."""

    def test_fernet_from_settings(self):
        from app.config import Settings

        s = Settings(secret_key="test-key-for-fernet")
        f = _fernet(s)
        assert f is not None

    def test_different_keys_produce_different_fernet(self):
        from app.config import Settings

        f1 = _fernet(Settings(secret_key="key1"))
        f2 = _fernet(Settings(secret_key="key2"))
        token1 = f1.encrypt(b"secret").decode()
        # f2 should not be able to decrypt f1's token
        with pytest.raises(Exception):
            f2.decrypt(token1.encode())
