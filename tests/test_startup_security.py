"""Fail-closed startup: non-loopback bind with default credentials must not start."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings, _detect_genestack_root, assert_safe_bind, load_settings

# These tests import app.main which requires joserfc — skip gracefully when
# the module isn't installed (same as the _import_app pattern in conftest).
_app_available = False
try:
    import app.main as _main_test

    _app_available = _main_test.app is not None
except Exception:  # noqa: BLE001
    pass


# ---------------------------------------------------------------------------
# Multi-worker guard (M4): the console is single-process by design; detect
# GSC_UVICORN_WORKERS / WEB_CONCURRENCY > 1 and refuse to start.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _app_available, reason="joserfc not installed")
def test_multi_worker_guard_refuses_on_gsc_workers(monkeypatch):
    import app.main as main_module

    monkeypatch.setenv("GSC_UVICORN_WORKERS", "4")
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    with pytest.raises(RuntimeError, match="NOT safe with multiple uvicorn workers"):
        main_module._ensure_single_worker()


@pytest.mark.skipif(not _app_available, reason="joserfc not installed")
def test_multi_worker_guard_refuses_on_web_concurrency(monkeypatch):
    import app.main as main_module

    monkeypatch.delenv("GSC_UVICORN_WORKERS", raising=False)
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with pytest.raises(RuntimeError, match="NOT safe with multiple uvicorn workers"):
        main_module._ensure_single_worker()


@pytest.mark.skipif(not _app_available, reason="joserfc not installed")
def test_multi_worker_guard_allows_single(monkeypatch):
    import app.main as main_module

    monkeypatch.setenv("GSC_UVICORN_WORKERS", "1")
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    main_module._ensure_single_worker()


@pytest.mark.skipif(not _app_available, reason="joserfc not installed")
def test_multi_worker_guard_allows_when_unset(monkeypatch):
    import app.main as main_module

    monkeypatch.delenv("GSC_UVICORN_WORKERS", raising=False)
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    main_module._ensure_single_worker()


@pytest.mark.skipif(not _app_available, reason="joserfc not installed")
def test_multi_worker_guard_ignores_non_numeric(monkeypatch):
    import app.main as main_module

    monkeypatch.setenv("GSC_UVICORN_WORKERS", "auto")
    monkeypatch.setenv("WEB_CONCURRENCY", "garbage")
    main_module._ensure_single_worker()


def test_default_bind_with_dev_keys_refused():
    """0.0.0.0 + factory defaults (dev keys, default secret_key) -> startup error."""
    settings = Settings(host="0.0.0.0")
    with pytest.raises(RuntimeError, match="config.yaml"):
        assert_safe_bind(settings)


def test_non_loopback_with_default_secret_only_refused():
    settings = Settings(
        host="192.0.2.10",
        api_keys={"real-admin-key": "admin"},
    )
    with pytest.raises(RuntimeError, match="secret_key"):
        assert_safe_bind(settings)


def test_non_loopback_with_dev_keys_only_refused():
    settings = Settings(
        host="0.0.0.0",
        secret_key="a-real-unique-secret",
    )
    with pytest.raises(RuntimeError, match="dev API key"):
        assert_safe_bind(settings)


def test_loopback_stays_dev_friendly():
    """127.0.0.1/localhost with full defaults is fine (dev workflow)."""
    for host in ("127.0.0.1", "localhost", "::1"):
        assert_safe_bind(Settings(host=host))


def test_non_loopback_with_real_credentials_starts():
    settings = Settings(
        host="0.0.0.0",
        api_keys={"real-admin-key": "admin"},
        secret_key="a-real-unique-secret",
    )
    assert_safe_bind(settings)


def test_non_loopback_with_dev_auto_login_refused():
    """dev_auto_login turns any no-credential request into platform-admin."""
    settings = Settings(
        host="0.0.0.0",
        api_keys={"real-admin-key": "admin"},
        secret_key="a-real-unique-secret",
        dev_auto_login=True,
    )
    with pytest.raises(RuntimeError, match="dev_auto_login"):
        assert_safe_bind(settings)


def test_loopback_with_dev_auto_login_allowed():
    assert_safe_bind(Settings(host="127.0.0.1", dev_auto_login=True))


def test_non_loopback_with_terminal_command_override_refused():
    """The override replaces the terminal ssh argv wholesale."""
    settings = Settings(
        host="0.0.0.0",
        api_keys={"real-admin-key": "admin"},
        secret_key="a-real-unique-secret",
        terminal_command_override="/bin/cat",
    )
    with pytest.raises(RuntimeError, match="terminal.command_override"):
        assert_safe_bind(settings)


@pytest.mark.skipif(not _app_available, reason="joserfc not installed")
def test_create_app_raises_on_unsafe_bind(monkeypatch):
    """The app factory itself refuses to start on 0.0.0.0 + defaults."""
    import app.main as main_module

    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(host="0.0.0.0"))
    with pytest.raises(RuntimeError, match="Refusing to start"):
        main_module.create_app()


@pytest.mark.skipif(not _app_available, reason="joserfc not installed")
def test_create_app_starts_on_loopback_with_defaults(monkeypatch):
    import app.main as main_module

    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(host="127.0.0.1"))
    app = main_module.create_app()
    assert app is not None


# Placeholder (config.yaml.example) handling: shipped placeholders must be
# treated as UNSET so the fail-closed checks fire instead of a public
# "secret" being used to encrypt stored credentials.
def _write_config(tmp_path, text: str) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(text, encoding="utf-8")
    return cfg


def test_example_config_placeholders_resolve_to_defaults(tmp_path, monkeypatch):
    import yaml

    cfg = _write_config(
        tmp_path,
        yaml.safe_dump(
            {
                "secret_key": "REPLACE_ME",
                "auth": {
                    "api_keys": {
                        "REPLACE_ME_ADMIN_KEY": "admin",
                        "REPLACE_ME_OPERATOR_KEY": "operator",
                        "REPLACE_ME_VIEWER_KEY": "viewer",
                    }
                },
            }
        ),
    )
    monkeypatch.setenv("CONSOLE_CONFIG", str(cfg))
    from app.config import DEFAULT_SECRET_KEY

    settings = load_settings(cfg)
    assert settings.secret_key == DEFAULT_SECRET_KEY
    # Placeholder keys are dropped -> dev fallback keys, not the placeholders.
    assert "REPLACE_ME_ADMIN_KEY" not in settings.api_keys
    assert "dev-admin-key" in settings.api_keys


def test_replace_me_secret_on_non_loopback_is_refused(tmp_path, monkeypatch):
    """Bootstrapping from the example file verbatim cannot bind 0.0.0.0."""
    import yaml

    cfg = _write_config(
        tmp_path,
        yaml.safe_dump({"secret_key": "REPLACE_ME", "server": {"host": "0.0.0.0"}}),
    )
    monkeypatch.setenv("CONSOLE_CONFIG", str(cfg))
    settings = load_settings(cfg)
    with pytest.raises(RuntimeError, match="secret_key"):
        assert_safe_bind(settings)


# ---------------------------------------------------------------------------
# B1: genestack root detection must find the docker-compose mount at
# /genestack and must not fail silently when no candidate matches.
# ---------------------------------------------------------------------------


def _make_genestack_root(base: Path, name: str) -> Path:
    root = base / name
    (root / "bin").mkdir(parents=True)
    (root / "openstack-components.yaml").write_text("components: {}\n")
    return root


@pytest.fixture
def isolated_console(monkeypatch, tmp_path):
    """CONSOLE_DIR inside a sandbox parent that has no genestack markers."""
    import app.config as config_mod

    console_dir = tmp_path / "sandbox" / "console"
    console_dir.mkdir(parents=True)
    monkeypatch.setattr(config_mod, "CONSOLE_DIR", console_dir)
    monkeypatch.chdir(console_dir)
    return console_dir


@pytest.fixture
def hide_system_roots(monkeypatch):
    """Neutralize /genestack and /opt/genestack so tests don't depend on the host."""
    import app.config as config_mod

    real_is_dir = Path.is_dir

    def blocked_is_dir(self):
        if str(self) == "/opt/genestack" or str(self).startswith("/opt/genestack/"):
            return False
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", blocked_is_dir)
    real_candidates = config_mod._genestack_probe_candidates

    def filtered_candidates():
        return [p for p in real_candidates() if str(p) != "/genestack"]

    monkeypatch.setattr(config_mod, "_genestack_probe_candidates", filtered_candidates)


def test_detect_genestack_root_finds_console_parent(
    isolated_console, tmp_path, monkeypatch
):
    import app.config as config_mod

    checkout = _make_genestack_root(tmp_path, "checkout")
    console_dir = checkout / "genestack-console"
    console_dir.mkdir()
    monkeypatch.setattr(config_mod, "CONSOLE_DIR", console_dir)
    assert _detect_genestack_root() == checkout.resolve()


def test_detect_genestack_root_finds_container_mount(
    isolated_console, tmp_path, monkeypatch
):
    """The docker-compose /genestack mount is probed before /opt/genestack."""
    import app.config as config_mod

    mount = _make_genestack_root(tmp_path, "mount")
    real_candidates = config_mod._genestack_probe_candidates

    def mapped_candidates():
        # Replace the literal /genestack slot with a sandbox path so the test
        # never depends on whether this host actually has a /genestack mount.
        return [mount if str(p) == "/genestack" else p for p in real_candidates()]

    monkeypatch.setattr(config_mod, "_genestack_probe_candidates", mapped_candidates)
    assert _detect_genestack_root() == mount.resolve()


def test_detect_genestack_root_logs_error_when_missing(
    isolated_console, tmp_path, caplog, hide_system_roots
):
    import logging

    with caplog.at_level(logging.ERROR):
        result = _detect_genestack_root()
    assert result == (tmp_path / "sandbox").resolve()
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records, "expected a loud ERROR log on detection fallback"
    message = " ".join(r.getMessage() for r in error_records)
    assert "genestack.root" in message
    assert "openstack-components.yaml" in message
