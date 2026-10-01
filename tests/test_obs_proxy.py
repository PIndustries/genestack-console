"""Observability dashboard proxy: sessions, path guards, kinds."""

from __future__ import annotations

from app.services import obs_proxy as ob
from app.services.horizon_proxy import is_safe_path


def setup_function() -> None:
    ob._reset()


def test_obs_kinds_include_grafana_stack():
    assert set(ob.KINDS) >= {"grafana", "prometheus", "alertmanager", "tempo"}
    assert ob.KINDS["grafana"]["login"] is True
    assert ob.KINDS["prometheus"]["login"] is False


def test_session_create_bound_to_env_and_kind():
    sid = ob.create_session(env_id="env-1", actor="ops", kind="grafana")
    assert sid.startswith("ob_")
    got = ob.get_session(sid, "env-1")
    assert got is not None
    assert got.kind == "grafana"
    assert ob.get_session(sid, "other") is None
    assert ob.get_session("ob_nope", "env-1") is None


def test_proxy_prefix_and_safe_path():
    prefix = ob.proxy_prefix("e1", "grafana", "ob_abc")
    assert prefix.endswith("/cloud/grafana/ob_abc")
    assert is_safe_path("d/foo")
    assert not is_safe_path("../secret")
