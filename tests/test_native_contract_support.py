"""Native operation form metadata and Loki startup restoration contracts."""

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from app.services.catalog import PLAYBOOK_ALLOWLIST, get_operation
from app.services.observe import observe_logs
from app.services.redfish import RESET_TYPES


def test_operation_metadata_matches_supported_handlers():
    expected = {
        ("host.basic_ops", "action"): ["ping", "facts", "disk_check", "all"],
        ("ansible.playbook.run", "playbook"): sorted(PLAYBOOK_ALLOWLIST),
        ("baremetal.node.power", "action"): sorted(RESET_TYPES),
        ("genestack.tempest", "action"): ["install", "run", "install-run"],
        ("genestack.hyperconverged_lab", "platform"): ["kubespray", "talos"],
    }
    for (operation, name), values in expected.items():
        param = next(p for p in get_operation(operation).params if p.name == name)
        assert param.enum == values
    param = next(
        p for p in get_operation("genestack.tempest").params if p.name == "action"
    )
    assert param.default == "install-run"
    assert "bmc_password" in get_operation("baremetal.node.register").secret_params


def test_loki_query_uses_environment_context_redacts_and_cleans(monkeypatch):
    cleaned = []
    context = SimpleNamespace(
        kubeconfig="/test/environment-kubeconfig", cleanup=lambda: cleaned.append(True)
    )
    monkeypatch.setattr("app.services.observe.build_context", lambda *a: context)
    monkeypatch.setattr("shutil.which", lambda name: "/test/kubectl")
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return (
            json.dumps(
                {
                    "status": "success",
                    "data": {
                        "resultType": "streams",
                        "result": [
                            {
                                "values": [
                                    ["1", "password=secret-value"],
                                    ["2", "ready"],
                                ]
                            },
                        ],
                    },
                }
            ),
            None,
        )

    monkeypatch.setattr("app.services.livestate._run", run)
    result = observe_logs(None, None, namespace="openstack", since="5m", limit=2)
    assert result["ok"] and result["count"] == 2
    assert "secret-value" not in str(result)
    assert result["lines"][0] == "ready"
    assert cleaned == [True]
    cmd, kwargs = calls[0]
    assert cmd[:3] == ["/test/kubectl", "get", "--raw"]
    assert "/monitoring/services/http:loki-gateway:80/proxy/" in cmd[3]
    assert parse_qs(urlsplit(cmd[3]).query)["query"] == ['{namespace="openstack"}']
    assert kwargs["env"]["KUBECONFIG"] == "/test/environment-kubeconfig"


def test_loki_validation_and_no_kubeconfig_never_probe(monkeypatch):
    monkeypatch.setattr(
        "app.services.livestate._run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not probe")),
    )
    for options in (
        {"since": "bad"},
        {"since": "8d"},
        {"namespace": "../bad"},
        {"query": "secret", "pod": "pod"},
    ):
        assert not observe_logs(None, None, **options)["ok"]
    cleaned = []
    monkeypatch.setattr(
        "app.services.observe.build_context",
        lambda *a: SimpleNamespace(
            kubeconfig=None, cleanup=lambda: cleaned.append(True)
        ),
    )
    result = observe_logs(None, None)
    assert not result["ok"] and "kubeconfig" in result["error"]
    assert cleaned == [True]


def test_loki_error_does_not_echo_provider_credentials(monkeypatch):
    monkeypatch.setattr(
        "app.services.observe.build_context",
        lambda *a: SimpleNamespace(kubeconfig="test", cleanup=lambda: None),
    )
    monkeypatch.setattr("shutil.which", lambda name: "/test/kubectl")
    monkeypatch.setattr(
        "app.services.livestate._run", lambda *a, **k: (None, "password=LEAK")
    )
    result = observe_logs(None, None, query='{namespace="test"} |= "SECRET-FILTER"')
    assert (
        not result["ok"]
        and "LEAK" not in str(result)
        and "SECRET-FILTER" not in str(result)
    )
