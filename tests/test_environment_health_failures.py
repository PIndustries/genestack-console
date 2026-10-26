"""An incomplete environment must produce an actionable state in the apps."""

from types import SimpleNamespace

from tests.test_baremetal import _create_env


def test_missing_environment_access_is_unknown_not_http_500(client, admin_headers, monkeypatch):
    from app.routers import livestate

    env = _create_env(client, admin_headers, dry_run=False)

    def unavailable(*args):
        raise FileNotFoundError("Environment access has not been configured")

    monkeypatch.setattr(livestate, "build_context", unavailable)
    response = client.get(f"/api/v1/environments/{env['id']}/health", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["error"]


def test_cleanup_failure_does_not_hide_probe_result(client, admin_headers, monkeypatch):
    from app.routers import livestate

    env = _create_env(client, admin_headers, dry_run=False)

    def bad_cleanup():
        raise OSError("Could not remove temporary access file")

    monkeypatch.setattr(livestate, "build_context", lambda *a: SimpleNamespace(cleanup=bad_cleanup))
    monkeypatch.setattr(
        livestate.livestate, "environment_health", lambda *a: {"proof": "real probe result"}
    )
    response = client.get(f"/api/v1/environments/{env['id']}/health", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["proof"] == "real probe result"
