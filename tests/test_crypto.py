"""Unit tests for app.services.crypto (Fernet at-rest encryption)."""

from __future__ import annotations

from app.services.crypto import FERNET_PREFIX, decrypt_secret, encrypt_secret


def test_round_trip():
    plain = "consumer-key:token-key:token-secret"
    token = encrypt_secret(plain)
    assert token is not None
    assert token.startswith(FERNET_PREFIX)
    assert plain not in token
    assert decrypt_secret(token) == plain


def test_encrypt_is_idempotent():
    token = encrypt_secret("secret")
    assert encrypt_secret(token) == token


def test_legacy_plaintext_passthrough():
    # Values stored before encryption existed still "decrypt" to themselves
    assert decrypt_secret("legacy-plain-value") == "legacy-plain-value"


def test_empty_values_pass_through():
    assert encrypt_secret(None) is None
    assert encrypt_secret("") == ""
    assert decrypt_secret(None) is None
    assert decrypt_secret("") == ""


def test_multiline_payload_round_trip():
    kubeconfig = "apiVersion: v1\nclusters:\n- cluster:\n    server: https://x\n"
    assert decrypt_secret(encrypt_secret(kubeconfig)) == kubeconfig
