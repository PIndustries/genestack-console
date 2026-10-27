"""A mixed-disk fleet must not fall back to one global /dev/sda target."""
import base64
import shlex

import pytest
import yaml

from app.models import Environment
from app.services.envconfig import ConfigValidationError, parse_document
from app.services.talos import build_talos_plan


def document():
    return {
        "provider": "talos",
        "servers": {
            "cp1": {"ip": "192.0.2.10", "roles": ["k8s_control_plane"]},
            "worker1": {"ip": "192.0.2.11", "roles": ["compute"]},
        },
        "talos": {"install_disk_selectors": {
            "cp1": "naa.60000000000000000000000000000001",
            "worker1": "naa.60000000000000000000000000000002",
        }},
    }


def plan(doc, tmp_path):
    parsed, warnings = parse_document(yaml.safe_dump(doc))
    assert not warnings
    return build_talos_plan(parsed, Environment(name="isolated", genestack_config_dir=str(tmp_path)))


def test_each_machine_gets_exact_disk_selector_and_base_has_no_fallback(tmp_path):
    doc = document()
    result = plan(doc, tmp_path)
    gen = next(c["argv"] for c in result["commands"] if c["phase"] == "gen-config")
    assert gen[gen.index("--install-disk") + 1] == ""
    patches = [c for c in result["commands"] if c["phase"] == "write-node-net"]
    assert len(patches) == 2
    for command in patches:
        # Decode the emitted file payload without executing its shell capsule.
        tokens = shlex.split(command["argv"][-1])
        documents = list(yaml.safe_load_all(base64.b64decode(tokens[2])))
        host = next(d["hostname"] for d in documents if d.get("kind") == "HostnameConfig")
        config = next(d for d in documents if d.get("kind") == "UnattendedInstallConfig")
        assert config["installer"]["image"] == result["install_image"]
        assert config["provisioning"]["diskSelector"]["match"] == (
            'disk.wwid == "' + doc["talos"]["install_disk_selectors"][host] + '"'
        )
        assert not any(d.get("machine", {}).get("install") for d in documents)
        address = doc["servers"][host]["ip"]
        apply = next(c["argv"] for c in result["commands"]
                     if c["phase"] in ("apply-controlplane", "apply-worker") and address in c["argv"])
        assert "@node-" + host + ".yaml" in apply
    guard = next(c for c in result["commands"] if c["phase"] == "write-network-patch")
    guarded = list(yaml.safe_load_all(base64.b64decode(shlex.split(guard["argv"][-1])[2])))
    install = next(d for d in guarded if d.get("kind") == "UnattendedInstallConfig")
    assert install["provisioning"]["diskSelector"]["match"] == "false"
    assert result["install_disk_selectors"] == doc["talos"]["install_disk_selectors"]


@pytest.mark.parametrize("selectors", [
    None, {}, {"cp1": "naa.60000000000000000000000000000001"},
    {"cp1": "*", "worker1": "naa.60000000000000000000000000000002"},
    {"cp1": "naa.60000000000000000000000000000001", "worker1": "naa.60000000000000000000000000000001"},
    {"cp1": "naa.60000000000000000000000000000001", "worker1": "naa.60000000000000000000000000000002", "other": "naa.60000000000000000000000000000003"},
    ["naa.60000000000000000000000000000001"],
])
def test_incomplete_ambiguous_or_unknown_targets_are_rejected(selectors, tmp_path):
    doc = document(); doc["talos"]["install_disk_selectors"] = selectors
    with pytest.raises(ConfigValidationError, match="install_disk_selectors"):
        parse_document(yaml.safe_dump(doc))


def test_global_device_cannot_override_per_node_selection(tmp_path):
    doc = document(); doc["talos"]["install_disk"] = "/dev/sda"
    with pytest.raises(ConfigValidationError, match="install_disk"):
        plan(doc, tmp_path)


def test_api_rejects_partial_selector_update_without_replacing_saved_config(client, admin_headers):
    from tests.test_baremetal import _create_env

    env = _create_env(client, admin_headers, dry_run=False)
    url = f"/api/v1/environments/{env['id']}/config"
    doc = document()
    saved = client.put(url, headers=admin_headers, json={"yaml_text": yaml.safe_dump(doc)})
    assert saved.status_code == 201
    version = saved.json()["version"]
    del doc["talos"]["install_disk_selectors"]["worker1"]
    invalid = client.put(url, headers=admin_headers, json={"yaml_text": yaml.safe_dump(doc), "expected_version": version})
    assert invalid.status_code == 422
    current = client.get(url, headers=admin_headers).json()
    assert current["version"] == version
    assert set(yaml.safe_load(current["yaml"])["talos"]["install_disk_selectors"]) == {"cp1", "worker1"}


def test_fresh_boot_image_and_generated_contract_match_pinned_client(tmp_path):
    from app.services.talos import DEFAULT_TALOS_VERSION, TALOSCTL_VERSION

    assert DEFAULT_TALOS_VERSION == TALOSCTL_VERSION
    result = plan(document(), tmp_path)
    command = next(c["argv"] for c in result["commands"] if c["phase"] == "gen-config")
    assert command[command.index("--talos-version") + 1] == DEFAULT_TALOS_VERSION
    assert result["install_image"].endswith(":" + DEFAULT_TALOS_VERSION)
