"""Test agents — critical path functions for install.

Focused unit tests for the install_agent function:
SSH options, error messages, token masking.

Tests the service function directly without API endpoints.
"""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock, patch


from app.services.agents import (
    TOKEN_PREFIX,
    generate_token,
    hash_token,
    proof_for,
    verify_proof,
    advertise_bases,
    install_agent,
    _run_over_ssh,
)


class TestTokenGeneration:
    """Token generation and hashing."""

    def test_token_starts_with_prefix(self):
        token = generate_token()
        assert token.startswith(TOKEN_PREFIX)

    def test_tokens_are_unique(self):
        tokens = {generate_token() for _ in range(10)}
        assert len(tokens) == 10

    def test_hash_deterministic(self):
        token = generate_token()
        h1 = hash_token(token)
        h2 = hash_token(token)
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex digest

    def test_hash_is_sha256_hex(self):
        token = generate_token()
        expected = hashlib.sha256(token.encode("utf-8")).hexdigest()
        assert hash_token(token) == expected


class TestHandshakeProof:
    """Challenge-response proof verification."""

    def test_valid_proof(self):
        token = generate_token()
        nonce = "test-nonce-123"
        proof = proof_for(token, nonce)
        assert verify_proof(token, nonce, proof) is True

    def test_wrong_proof_fails(self):
        token = generate_token()
        nonce = "test-nonce"
        assert verify_proof(token, nonce, "wrong-proof") is False

    def test_wrong_nonce_fails(self):
        token = generate_token()
        proof = proof_for(token, "correct-nonce")
        assert verify_proof(token, "wrong-nonce", proof) is False

    def test_proof_is_hex_digest(self):
        token = generate_token()
        proof = proof_for(token, "nonce")
        assert len(proof) == 64
        assert all(c in "0123456789abcdef" for c in proof)


class TestAdvertiseBases:
    """advertise_bases — URL parsing for agent install."""

    def test_http_to_ws(self):
        http, ws = advertise_bases("http://192.0.2.1:8080")
        assert http == "http://192.0.2.1:8080"
        assert ws == "ws://192.0.2.1:8080"

    def test_https_to_wss(self):
        http, ws = advertise_bases("https://console.example.com")
        assert http == "https://console.example.com"
        assert ws == "wss://console.example.com"

    def test_strips_trailing_slash(self):
        http, ws = advertise_bases("http://host:8080/")
        assert http == "http://host:8080"

    def test_scheme_less_becomes_http(self):
        http, ws = advertise_bases("host:8080")
        assert http == "http://host:8080"
        assert ws == "ws://host:8080"


class TestRunOverSSH:
    """_run_over_ssh — SSH option construction."""

    def test_default_port_uses_ssh_target(self):
        bridge = MagicMock()
        bridge.run_command.return_value = {"returncode": 0}

        _run_over_ssh(
            bridge,
            ["echo", "hello"],
            user="root",
            host="10.0.0.1",
            port=22,
            timeout=30,
            dry_run=False,
            log=lambda m: None,
        )
        call_kwargs = bridge.run_command.call_args[1]
        assert call_kwargs["ssh_target"] == "root@10.0.0.1"

    def test_custom_port_builds_ssh_argv(self):
        bridge = MagicMock()
        bridge.run_command.return_value = {"returncode": 0}

        _run_over_ssh(
            bridge,
            ["echo", "hello"],
            user="root",
            host="10.0.0.1",
            port=2222,
            timeout=30,
            dry_run=False,
            log=lambda m: None,
        )
        cmd = bridge.run_command.call_args[0][0]
        assert cmd[0] == "ssh"
        assert "-o" in cmd
        assert "StrictHostKeyChecking=accept-new" in cmd
        assert "-p" in cmd
        assert "2222" in cmd
        assert "root@10.0.0.1" in " ".join(cmd)


class TestInstallAgent:
    """install_agent — critical path, SSH options, error messages."""

    def _make_env(self):
        env = MagicMock()
        env.id = "test-env-id"
        return env

    def _make_settings(self, advertise="http://192.0.2.1:8080"):
        settings = MagicMock()
        settings.hub_advertise_url = advertise
        return settings

    def test_requires_advertise_url(self):
        env = self._make_env()
        settings = self._make_settings(advertise="")
        logs: list[str] = []

        result = install_agent(
            MagicMock(),
            env,
            settings,
            host="10.0.0.1",
            dry_run=True,
            timeout=60,
            log=logs.append,
        )
        assert result["ok"] is False
        assert "hub.advertise_url" in result["error"]
        assert logs[-1].startswith("[denied]")

    def test_requires_host(self):
        env = self._make_env()
        settings = self._make_settings()
        logs: list[str] = []

        result = install_agent(
            MagicMock(),
            env,
            settings,
            host="",
            dry_run=True,
            timeout=60,
            log=logs.append,
        )
        assert result["ok"] is False
        assert "host" in result["error"].lower()

    def test_dry_run_returns_success(self):
        env = self._make_env()
        settings = self._make_settings()
        logs: list[str] = []

        result = install_agent(
            MagicMock(),
            env,
            settings,
            host="10.0.0.1",
            dry_run=True,
            timeout=60,
            log=logs.append,
        )
        assert result["ok"] is True
        assert result["dry_run"] is True
        assert result["host"] == "10.0.0.1"

    def test_dry_run_includes_ssh_options(self):
        env = self._make_env()
        settings = self._make_settings()
        logs: list[str] = []

        with patch("app.services.agents._run_over_ssh") as mock_run:
            mock_run.return_value = {"returncode": 0}
            install_agent(
                MagicMock(),
                env,
                settings,
                host="10.0.0.1",
                dry_run=True,
                timeout=60,
                log=logs.append,
            )
            assert mock_run.called
            call_kwargs = mock_run.call_args[1]
            assert call_kwargs["host"] == "10.0.0.1"

    def test_error_contains_hostname(self):
        env = self._make_env()
        settings = self._make_settings()
        logs: list[str] = []

        with patch("app.services.agents._run_over_ssh") as mock_run:
            mock_run.return_value = {
                "returncode": 1,
                "stderr": "Connection refused",
            }
            result = install_agent(
                MagicMock(),
                env,
                settings,
                host="10.0.0.5",
                dry_run=False,
                timeout=60,
                log=logs.append,
            )
            assert result["ok"] is False
            assert "10.0.0.5" in result["error"]
            assert "SSH" in result["error"]

    def test_error_contains_remediation_hint(self):
        env = self._make_env()
        settings = self._make_settings()
        logs: list[str] = []

        with patch("app.services.agents._run_over_ssh") as mock_run:
            mock_run.return_value = {
                "returncode": 1,
                "stderr": "Connection refused",
            }
            result = install_agent(
                MagicMock(),
                env,
                settings,
                host="10.0.0.5",
                dry_run=False,
                timeout=60,
                log=logs.append,
            )
            assert (
                "verify" in result["error"].lower()
                or "reachable" in result["error"].lower()
            )

    def test_ssh_failure_no_stdin_fallback(self):
        """When SSH connection itself fails, no stdin fallback is attempted."""
        env = self._make_env()
        settings = self._make_settings()
        logs: list[str] = []

        with patch("app.services.agents._run_over_ssh") as mock_run:
            mock_run.return_value = {
                "returncode": 1,
                "stderr": "Network is unreachable",
            }
            install_agent(
                MagicMock(),
                env,
                settings,
                host="10.0.0.5",
                dry_run=False,
                timeout=60,
                log=logs.append,
            )
            # Should only be called once (curl-pipe), not the stdin fallback
            assert mock_run.call_count == 1

    def test_dry_run_token_masked(self):
        """In dry_run mode the token literal is TOKEN_MASK, never raw."""
        from app.services.agents import TOKEN_MASK as TM

        env = self._make_env()
        settings = self._make_settings()
        logs: list[str] = []

        install_agent(
            MagicMock(),
            env,
            settings,
            host="10.0.0.1",
            dry_run=True,
            timeout=60,
            log=logs.append,
        )
        # The ssh command in dry_run carries the masked token
        full_log = "\n".join(logs)
        assert "--token" in full_log
        assert TM in full_log
        import re

        raw_re = re.compile(r"gsca_[A-Za-z0-9_-]{8,}")
        assert not raw_re.search(full_log), "raw token found in dry-run logs"

    def test_custom_ssh_user(self):
        env = self._make_env()
        settings = self._make_settings()
        logs: list[str] = []

        with patch("app.services.agents._run_over_ssh") as mock_run:
            mock_run.return_value = {"returncode": 0}
            install_agent(
                MagicMock(),
                env,
                settings,
                host="10.0.0.1",
                ssh_user="ubuntu",
                dry_run=True,
                timeout=60,
                log=logs.append,
            )
            call_kwargs = mock_run.call_args[1]
            assert call_kwargs["user"] == "ubuntu"

    def test_custom_ssh_port(self):
        env = self._make_env()
        settings = self._make_settings()
        logs: list[str] = []

        with patch("app.services.agents._run_over_ssh") as mock_run:
            mock_run.return_value = {"returncode": 0}
            install_agent(
                MagicMock(),
                env,
                settings,
                host="10.0.0.1",
                ssh_port=2222,
                dry_run=True,
                timeout=60,
                log=logs.append,
            )
            call_kwargs = mock_run.call_args[1]
            assert call_kwargs["port"] == 2222

    def test_error_has_returncode_2_for_ssh_failure(self):
        env = self._make_env()
        settings = self._make_settings()
        logs: list[str] = []

        with patch("app.services.agents._run_over_ssh") as mock_run:
            mock_run.return_value = {
                "returncode": 1,
                "stderr": "Connection refused",
            }
            result = install_agent(
                MagicMock(),
                env,
                settings,
                host="10.0.0.5",
                dry_run=False,
                timeout=60,
                log=logs.append,
            )
            assert result["returncode"] == 2
