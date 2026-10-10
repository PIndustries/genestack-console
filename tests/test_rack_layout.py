"""Rack layout: automatic U stacking, saved placement, and seen links."""

from __future__ import annotations

import uuid

import yaml

from app.services.rack_layout import layout

DOC = """\
provider: kubespray
components:
  keystone: true
servers:
  ctrl-1:
    ip: 10.20.0.11
    roles: [control]
  cmp-1:
    ip: 10.20.0.21
    roles: [compute]
network:
  gateway_domain: cluster.example.com
"""


def _servers(count: int) -> dict[str, dict]:
    return {f"n{index:02d}": {"roles": ["compute"]} for index in range(count)}


def _by_host(result: dict) -> dict[str, dict]:
    return {row["hostname"]: row for row in result["logical"]}


def test_unplaced_machines_stack_from_u1():
    result = layout(
        {"b-host": {"roles": ["compute"]}, "a-host": {"roles": ["control"]}},
        {},
        {},
    )
    logical = _by_host(result)
    assert logical["a-host"] == {
        "hostname": "a-host",
        "rack": "Rack 1",
        "u_start": 1,
        "u_height": 1,
        "side": False,
        "roles": ["control"],
        "in_cluster": False,
        "vms": [],
    }
    assert logical["b-host"]["u_start"] == 2
    assert logical["b-host"]["side"] is False
    assert result["racks"][0]["name"] == "Rack 1"
    assert result["racks"][0]["height"] == 48
    assert [item["hostname"] for item in result["racks"][0]["items"]] == [
        "a-host",
        "b-host",
    ]


def test_forty_ninth_machine_is_on_the_side_of_rack_1():
    result = layout(_servers(49), {}, {})
    logical = _by_host(result)
    assert logical["n00"]["u_start"] == 1
    assert logical["n00"]["side"] is False
    assert logical["n47"]["u_start"] == 48
    assert logical["n47"]["side"] is False
    assert logical["n48"]["side"] is True
    assert logical["n48"]["rack"] == "Rack 1"
    assert logical["n48"]["u_start"] is None
    assert [rack["name"] for rack in result["racks"]] == ["Rack 1"]
    side = [row["hostname"] for row in result["logical"] if row["side"]]
    assert side == ["n48"]


def test_saved_u_start_is_kept():
    result = layout(
        {"a-host": {"roles": ["control"]}, "b-host": {"roles": ["compute"]}},
        {
            "placements": {
                "b-host": {"rack": "Rack 2", "u_start": 10, "u_height": 2},
            }
        },
        {},
    )
    logical = _by_host(result)
    assert logical["b-host"]["rack"] == "Rack 2"
    assert logical["b-host"]["u_start"] == 10
    assert logical["b-host"]["u_height"] == 2
    assert logical["b-host"]["side"] is False
    assert logical["a-host"]["rack"] == "Rack 1"
    assert logical["a-host"]["u_start"] == 1
    names = [rack["name"] for rack in result["racks"]]
    assert names == ["Rack 1", "Rack 2"]


def test_overlap_marks_both_items():
    result = layout(
        {"a-host": {}, "b-host": {}},
        {
            "placements": {
                "a-host": {"u_start": 1, "u_height": 2},
                "b-host": {"u_start": 2, "u_height": 1},
            },
            "gear": [
                {
                    "id": "g1",
                    "name": "tor",
                    "kind": "switch",
                    "rack": "Rack 1",
                    "u_start": 2,
                    "u_height": 1,
                }
            ],
        },
        {},
    )
    items = result["racks"][0]["items"]
    assert len(items) == 3
    assert all(item["conflict"] is True for item in items)


def test_planned_link_kind_and_no_invented_gear_cables():
    result = layout(
        {"a-host": {"ip": "10.1.0.5"}},
        {
            "links": [
                {"id": "l1", "source": "a-host", "target": "tor", "label": "uplink"}
            ],
            "gear": [
                {
                    "id": "g1",
                    "name": "tor",
                    "kind": "switch",
                    "u_start": 48,
                    "u_height": 1,
                },
                {
                    "id": "g2",
                    "name": "pdu-a",
                    "kind": "pdu",
                    "u_start": 47,
                    "u_height": 1,
                },
            ],
        },
        {"ready_ips": {"10.1.0.5"}, "bmc_hosts": {"a-host"}},
    )
    planned = [link for link in result["links"] if link["kind"] == "planned"]
    assert planned == [
        {
            "id": "l1",
            "source": "a-host",
            "target": "tor",
            "label": "uplink",
            "kind": "planned",
        }
    ]
    seen = [link for link in result["links"] if link["kind"] == "seen"]
    assert all(link["target"] not in {"tor", "pdu-a", "g1", "g2"} for link in seen)


def test_ready_ip_is_a_seen_kubernetes_connection():
    result = layout(
        {
            "a-host": {"ip": "10.1.0.5", "roles": ["control"]},
            "b-host": {"ip": "10.1.0.6"},
        },
        {},
        {"ready_ips": {"10.1.0.5"}, "vms": [{"name": "vm-a", "host": "a-host"}]},
    )
    logical = _by_host(result)
    assert logical["a-host"]["in_cluster"] is True
    assert logical["b-host"]["in_cluster"] is False
    assert logical["a-host"]["vms"] == ["vm-a"]
    seen = [link for link in result["links"] if link["kind"] == "seen"]
    assert {
        "source": "a-host",
        "target": "kubernetes",
        "kind": "seen",
        "label": "kubernetes",
    } in seen
    assert {
        "source": "a-host",
        "target": "vm-a",
        "kind": "seen",
        "label": "vm",
    } in seen
    assert all(link["source"] != "b-host" for link in seen)


def test_bmc_hostname_is_a_seen_management_port_connection():
    result = layout(
        {"a-host": {}, "b-host": {}},
        {},
        {"bmc_hosts": {"a-host"}},
    )
    seen = [link for link in result["links"] if link["kind"] == "seen"]
    assert seen == [
        {
            "source": "a-host",
            "target": "management-port",
            "kind": "seen",
            "label": "management-port",
        }
    ]


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def test_placement_write_does_not_drop_servers(client, admin_headers):
    created = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={"name": f"rack-{_suffix()}"},
    )
    assert created.status_code == 201, created.text
    env_id = created.json()["id"]
    base = f"/api/v1/environments/{env_id}"

    put = client.put(
        f"{base}/config",
        headers=admin_headers,
        json={"yaml_text": DOC},
    )
    assert put.status_code == 201, put.text
    before = yaml.safe_load(
        client.get(f"{base}/config", headers=admin_headers).json()["yaml"]
    )

    placed = client.put(
        f"{base}/rack/placement",
        headers=admin_headers,
        json={"hostname": "ctrl-1", "rack": "Rack 1", "u_start": 12, "u_height": 1},
    )
    assert placed.status_code == 200, placed.text
    assert placed.json()["placement"]["u_start"] == 12

    linked = client.post(
        f"{base}/rack/links",
        headers=admin_headers,
        json={"source": "ctrl-1", "target": "cmp-1", "label": "east"},
    )
    assert linked.status_code == 201, linked.text

    after = yaml.safe_load(
        client.get(f"{base}/config", headers=admin_headers).json()["yaml"]
    )
    assert after["servers"] == before["servers"]
    assert after["components"] == before["components"]
    assert after["network"] == before["network"]
    assert after["provider"] == before["provider"]
    assert after["rack"]["placements"]["ctrl-1"]["u_start"] == 12
    assert after["rack"]["links"][0]["label"] == "east"
    assert "kind" not in after["rack"]["links"][0]

    rack = client.get(f"{base}/rack", headers=admin_headers)
    assert rack.status_code == 200, rack.text
    body = rack.json()
    logical = _by_host(body)
    assert logical["ctrl-1"]["u_start"] == 12
    assert logical["cmp-1"]["u_start"] == 1
    assert logical["cmp-1"]["rack"] == "Rack 1"
    planned = [link for link in body["links"] if link["kind"] == "planned"]
    assert planned[0]["source"] == "ctrl-1"
    assert planned[0]["kind"] == "planned"


def test_viewer_cannot_write_placement(client, admin_headers, viewer_headers):
    created = client.post(
        "/api/v1/environments",
        headers=admin_headers,
        json={"name": f"rack-view-{_suffix()}"},
    )
    assert created.status_code == 201, created.text
    env_id = created.json()["id"]
    resp = client.put(
        f"/api/v1/environments/{env_id}/rack/placement",
        headers=viewer_headers,
        json={"hostname": "ctrl-1", "u_start": 1},
    )
    assert resp.status_code == 403
