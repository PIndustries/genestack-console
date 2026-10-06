"""iLO HTML5 console session store, JS rewrite, and HTTP proxy."""

from __future__ import annotations

import uuid

import pytest

from app.services import ilo_console as ilo_svc


@pytest.fixture(autouse=True)
def _clean_sessions():
    yield
    ilo_svc._reset()


def test_rewrite_iframe_relative_roots_keeps_decoder_worker_on_session_path():
    src = 'var ngl, path = window.top === window.self ? "" : "../", me = this'
    out = ilo_svc.rewrite_iframe_relative_roots(src)
    assert "../" not in out
    assert 'path = ""' in out or 'path = ""' in out


def test_rewrite_iframe_icons_extendedicons_stays_on_session_path():
    """icons.js uses a different top/self ternary than renderer.js."""
    src = (
        '}, oemIconpath = window.top === window.self ? "js/extendedIcons.js" '
        ': "../js/extendedIcons.js";\n\n$.getScript(oemIconpath)'
    )
    out = ilo_svc.rewrite_iframe_relative_roots(src)
    assert "../js/extendedIcons.js" not in out
    assert 'oemIconpath = "js/extendedIcons.js"' in out


def test_rewrite_json_roots_rewrites_getcache_absolute_json_url():
    """getCache builds reqUrl: "/json/" + name — must stay under session prefix."""
    prefix = "/api/v1/environments/env/baremetal/console/ilo_sess"
    src = (
        'var jsonReq = {\n'
        '        callName: req.name,\n'
        '        reqUrl: "/json/" + req.name,\n'
        '        httpMethod: "GET"\n'
        "      };"
    )
    out = ilo_svc.rewrite_json_roots(src, prefix)
    assert 'reqUrl: "/json/" + req.name' not in out
    assert f'reqUrl: "{prefix}/json/" + req.name' in out
    # suffix match literals must remain so login_session detection still works
    match_src = '"/json/login_session" == my_url.match("/json/login_session$")'
    assert match_src in ilo_svc.rewrite_json_roots(match_src, prefix)


def test_apply_rewrites_icons_and_getcache():
    prefix = "/api/v1/environments/e/baremetal/console/ilo_x"
    icons = (
        b'oemIconpath = window.top === window.self ? "js/extendedIcons.js" '
        b': "../js/extendedIcons.js";'
    )
    out = ilo_svc.apply_rewrites("js/icons.js", icons, prefix).decode()
    assert "../js/extendedIcons.js" not in out
    assert '"js/extendedIcons.js"' in out
    ilo_js = (
        b'reqUrl: "/json/" + req.name,\n'
        b'"json/" == my_url.match("^json/") && (my_url = "/" + my_url)'
    )
    out2 = ilo_svc.apply_rewrites("js/iLO.js", ilo_js, prefix).decode()
    assert f'reqUrl: "{prefix}/json/" + req.name' in out2
    assert f'(my_url = "{prefix}/" + my_url)' in out2


def test_rewrite_irc_renderer_global_binds_window_renderer():
    src = (
        "me.renderer = new Renderer(settings), renderer.onready = function() {\n"
        "    $(\"#remote_console_thumbnail_wait\").hide();\n"
        "  }, renderer.onclose = function() { htmlIrcClose(); };"
    )
    out = ilo_svc.rewrite_irc_renderer_global(src)
    assert "window.renderer = me.renderer = new Renderer(settings), renderer.onready" in out
    assert "me.renderer = new Renderer(settings), renderer.onready" not in out.replace(
        "window.renderer = me.renderer = new Renderer(settings), renderer.onready", ""
    )


def test_apply_rewrites_irc_js_binds_renderer_global():
    src = b"me.renderer = new Renderer(settings), renderer.onready = function(){};"
    out = ilo_svc.apply_rewrites("js/irc.js", src, "/api/v1/environments/e/baremetal/console/s").decode()
    assert out.startswith("window.renderer = me.renderer = new Renderer(settings), renderer.onready")




def test_rewrite_socket_js_keeps_kvm_on_console_origin():
    src = 'this.sessionKey = options.sessionKey, this.sockaddr = "wss://" + options.host + "/wss/ircport", this.ie = 0'
    out = ilo_svc.rewrite_socket_js(src)
    assert "wss://" + " + options.host" not in out
    assert "self.location" in out
    assert "wss/ircport" in out
    assert "ws://" in out
    assert "window.location" not in out
    # A missing slash here is a SyntaxError and the console canvas stays black.
    assert '.replace(/\\/js\\/$/,"/")' in out
    assert '.replace(/\\/js\\/$,"/")' not in out


def test_rewrite_json_roots_keeps_relative_json_under_session_prefix():
    src = (
        '"json/" == my_url.match("^json/") && (my_url = "/" + my_url), '
        '"rest/" == my_url.match("^rest/") && (my_url = "/" + my_url), '
        '"/json/login_session" == my_url.match("/json/login_session$")'
    )
    prefix = "/api/v1/environments/e/baremetal/console/s"
    out = ilo_svc.rewrite_json_roots(src, prefix)
    assert f'(my_url = "{prefix}/" + my_url)' in out
    assert '"rest/" == my_url.match("^rest/") && (my_url = "/" + my_url)' not in out
    # Suffix checks must stay unprefixed so they still match the rewritten URL.
    assert '"/json/login_session" == my_url.match("/json/login_session$")' in out


def test_is_safe_console_path():
    assert ilo_svc.is_safe_console_path("irc.html")
    assert ilo_svc.is_safe_console_path("js/socket.js")
    assert ilo_svc.is_safe_console_path("json/login_session")
    assert ilo_svc.is_safe_console_path("json/ui_events/12")
    assert ilo_svc.is_safe_console_path("rest/v1/Systems/1")
    assert not ilo_svc.is_safe_console_path("../etc/passwd")
    assert not ilo_svc.is_safe_console_path("/json/login_session")
    assert not ilo_svc.is_safe_console_path("js/foo.js?x=1")


def test_bmc_origin_adds_https():
    assert ilo_svc.bmc_origin("192.0.2.5") == "https://192.0.2.5"
    assert ilo_svc.bmc_origin("https://ilo.example/") == "https://ilo.example"


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _create_env(client, headers):
    resp = client.post(
        "/api/v1/environments", headers=headers, json={"name": f"ilo-{_suffix()}"}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _make_node(env_id):
    from app.db import SessionLocal
    from app.models import BaremetalNode
    from app.services.crypto import encrypt_secret

    db = SessionLocal()
    try:
        node = BaremetalNode(
            environment_id=env_id,
            name=f"node-{_suffix()}",
            bmc_host="192.0.2.5",
            bmc_username="maas",
            bmc_password=encrypt_secret("secret"),
        )
        db.add(node)
        db.commit()
        db.refresh(node)
        return node.id, node.name
    finally:
        db.close()


def test_console_session_unknown_node(client, admin_headers):
    env = _create_env(client, admin_headers)
    resp = client.post(
        f"/api/v1/environments/{env['id']}/baremetal/nodes/{uuid.uuid4()}/console/session",
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["embed_url"] is None


def test_console_session_mints_embed_url(client, admin_headers, monkeypatch):
    monkeypatch.setattr(ilo_svc, "json_login", lambda *a, **k: "SESSIONKEY")
    env = _create_env(client, admin_headers)
    node_id, name = _make_node(env["id"])
    resp = client.post(
        f"/api/v1/environments/{env['id']}/baremetal/nodes/{node_id}/console/session",
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["node_name"] == name
    assert body["session_id"].startswith("ilo_")
    assert body["embed_url"].startswith(
        f"/api/v1/environments/{env['id']}/baremetal/console/{body['session_id']}/irc.html"
    )
    assert "token=SESSIONKEY" in body["embed_url"]


def test_console_session_login_error(client, admin_headers, monkeypatch):
    def boom(*a, **k):
        raise ilo_svc.IloConsoleError("iLO login HTTP 401")

    monkeypatch.setattr(ilo_svc, "json_login", boom)
    env = _create_env(client, admin_headers)
    node_id, _name = _make_node(env["id"])
    resp = client.post(
        f"/api/v1/environments/{env['id']}/baremetal/nodes/{node_id}/console/session",
        headers=admin_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "401" in body["error"]


def test_asset_proxy_rewrites_socket_js(client, admin_headers, monkeypatch):
    monkeypatch.setattr(ilo_svc, "json_login", lambda *a, **k: "SESSIONKEY")

    def fake_fetch(origin, path, session_key, **kwargs):
        assert path == "js/socket.js"
        src = b'this.sockaddr = "wss://" + options.host + "/wss/ircport"'
        return 200, src, "application/javascript"

    monkeypatch.setattr(ilo_svc, "fetch_ilo_asset", fake_fetch)
    env = _create_env(client, admin_headers)
    node_id, _name = _make_node(env["id"])
    minted = client.post(
        f"/api/v1/environments/{env['id']}/baremetal/nodes/{node_id}/console/session",
        headers=admin_headers,
    ).json()
    sid = minted["session_id"]
    resp = client.get(
        f"/api/v1/environments/{env['id']}/baremetal/console/{sid}/js/socket.js"
    )
    assert resp.status_code == 200
    text = resp.text
    assert "options.host" not in text or "window.location.host" in text
    assert "self.location" in text


def test_asset_proxy_rejects_unknown_session(client, admin_headers):
    env = _create_env(client, admin_headers)
    resp = client.get(
        f"/api/v1/environments/{env['id']}/baremetal/console/ilo_notarealsession/irc.html"
    )
    assert resp.status_code == 404


def test_asset_proxy_posts_json_to_bmc(client, admin_headers, monkeypatch):
    monkeypatch.setattr(ilo_svc, "json_login", lambda *a, **k: "SESSIONKEY")
    seen: dict[str, object] = {}

    def fake_fetch(origin, path, session_key, **kwargs):
        seen["path"] = path
        seen["method"] = kwargs.get("method")
        seen["body"] = kwargs.get("body")
        seen["headers"] = kwargs.get("extra_headers")
        return 200, b'{"ok":true}', "application/json"

    monkeypatch.setattr(ilo_svc, "fetch_ilo_asset", fake_fetch)
    env = _create_env(client, admin_headers)
    node_id, _name = _make_node(env["id"])
    minted = client.post(
        f"/api/v1/environments/{env['id']}/baremetal/nodes/{node_id}/console/session",
        headers=admin_headers,
    ).json()
    sid = minted["session_id"]
    resp = client.post(
        f"/api/v1/environments/{env['id']}/baremetal/console/{sid}/json/ilo_status",
        headers={**admin_headers, "Content-Type": "application/json"},
        content=b'{"method":"reset_ilo"}',
    )
    assert resp.status_code == 200, resp.text
    assert seen["path"] == "json/ilo_status"
    assert seen["method"] == "POST"
    assert seen["body"] == b'{"method":"reset_ilo"}'
    assert resp.json()["ok"] is True
