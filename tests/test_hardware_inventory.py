"""Physical inventory discovery must not power, boot or change disks."""

import copy

import httpx
import pytest

from app.services import redfish


@pytest.fixture
def bmc(monkeypatch):
    paths = {
        "/redfish/v1/Systems": {"Members": [{"@odata.id": "/redfish/v1/Systems/A"}]},
        "/redfish/v1/Systems/A": {
            "UUID": "physical-system-a",
            "MemorySummary": {"TotalSystemMemoryGiB": 128},
            # This is package count, deliberately different from thread count.
            "ProcessorSummary": {"Count": 2},
            "Processors": {"@odata.id": "/redfish/v1/Processors"},
            "Storage": {"@odata.id": "/redfish/v1/Storage"},
            "EthernetInterfaces": {"@odata.id": "/redfish/v1/NICs"},
        },
        "/redfish/v1/Processors": {"Members": [{"@odata.id": "/redfish/v1/CPU/1"}]},
        "/redfish/v1/CPU/1": {"TotalThreads": 32, "TotalEnabledCores": 16},
        "/redfish/v1/Storage": {"Members": [{"@odata.id": "/redfish/v1/Controllers/1"}]},
        "/redfish/v1/Controllers/1": {"Drives": [{"@odata.id": "/redfish/v1/Drives/1"}]},
        "/redfish/v1/Drives/1": {
            "SerialNumber": "disk-a",
            "CapacityBytes": 1_000_000_000_000,
            "Status": {"Health": "OK", "State": "Enabled"},
        },
        "/redfish/v1/NICs": {"Members": [{"@odata.id": "/redfish/v1/NIC/1"}]},
        "/redfish/v1/NIC/1": {
            "Id": "1",
            "MACAddress": "aa:bb:cc:00:00:01",
            "LinkStatus": "LinkUp",
            "SpeedMbps": 10000,
        },
    }
    calls = []
    require_session = [False]

    def handle(request):
        calls.append((request.method, str(request.url)))
        assert request.url.host == "bmc.test"
        if request.url.path == redfish.SESSION_PATH:
            assert request.method == "POST"
            return httpx.Response(
                201,
                headers={
                    "X-Auth-Token": "test-session",
                    "Location": "/redfish/v1/SessionService/Sessions/1",
                },
            )
        if request.url.path == "/redfish/v1/SessionService/Sessions/1":
            assert request.method == "DELETE"
            return httpx.Response(204)
        assert request.method == "GET"
        if require_session[0] and not request.headers.get("X-Auth-Token"):
            return httpx.Response(401)
        return httpx.Response(200, json=paths[request.url.path])

    monkeypatch.setattr(
        redfish,
        "_client",
        lambda *args: httpx.Client(
            base_url="https://bmc.test", transport=httpx.MockTransport(handle)
        ),
    )
    return paths, calls, require_session


def test_capacity_measured_without_wipe_or_role_names(bmc):
    _, calls, _ = bmc
    report = redfish.hardware_inventory("bmc.test", "admin", "secret")
    assert report["cpu_threads"] == 32
    assert report["cpu_cores"] == 16
    assert report["memory_gib"] == 128
    assert report["drives"][0]["serial"] == "disk-a"
    assert report["hardware_modified"] is False
    assert all(method == "GET" or "/SessionService/Sessions" in path for method, path in calls)
    assert "secret" not in repr(report)


def test_one_auth_session_closed_after_reads(bmc):
    _, calls, session = bmc
    session[0] = True
    redfish.hardware_inventory("bmc.test", "admin", "secret")
    assert sum(method == "POST" for method, _ in calls) == 1
    assert calls[-1][0] == "DELETE"


@pytest.mark.parametrize(
    "link",
    [
        "https://other.test/redfish/v1/CPU/1",
        "//other.test/redfish/v1/CPU/1",
        "/redfish/v1/../../secrets",
        "/redfish/v1/%2e%2e/secrets",
    ],
)
def test_bmc_link_cannot_forward_credentials_elsewhere(bmc, link):
    paths, _, _ = bmc
    paths["/redfish/v1/Systems/A"]["Processors"] = {"@odata.id": link}
    with pytest.raises(redfish.RedfishError, match="unsafe"):
        redfish.hardware_inventory("bmc.test", "admin", "secret")


def test_missing_cpu_measurement_not_replaced_with_package_count(bmc):
    paths, _, _ = bmc
    paths["/redfish/v1/CPU/1"] = {"TotalCores": 16}
    report = redfish.hardware_inventory("bmc.test", "admin", "secret")
    assert report["cpu_threads"] is None
    assert report["issues"]


def test_multiple_systems_not_silently_selected(bmc):
    paths, _, _ = bmc
    paths["/redfish/v1/Systems"]["Members"].append(
        copy.deepcopy(paths["/redfish/v1/Systems"]["Members"][0])
    )
    with pytest.raises(redfish.RedfishError, match="single physical"):
        redfish.hardware_inventory("bmc.test", "admin", "secret")


def test_pagination_cannot_report_partial_capacity_as_complete(bmc):
    paths, _, _ = bmc
    paths["/redfish/v1/Storage"]["Members@odata.nextLink"] = "/redfish/v1/Storage?page=2"
    with pytest.raises(redfish.RedfishError, match="Paginated"):
        redfish.hardware_inventory("bmc.test", "admin", "secret")


def test_ilo_legacy_processor_summary_is_not_a_resource_link(bmc):
    paths, _, _ = bmc
    system = paths["/redfish/v1/Systems/A"]
    system["Processors"] = {"Count": 2}
    system["links"] = {"Processors": {"href": "/redfish/v1/Processors"}}
    assert redfish.hardware_inventory("bmc.test", "admin", "secret")["cpu_threads"] == 32


def test_absolute_same_bmc_links_supported(bmc):
    paths, _, _ = bmc
    paths["/redfish/v1/Systems/A"]["Processors"] = {
        "@odata.id": "https://bmc.test/redfish/v1/Processors"
    }
    assert redfish.hardware_inventory("bmc.test", "admin", "secret")["cpu_threads"] == 32


def test_session_closed_when_inventory_is_invalid(bmc):
    paths, calls, session = bmc
    session[0] = True
    paths["/redfish/v1/Systems/A"]["Processors"] = {
        "@odata.id": "https://other.test/redfish/v1/Processors"
    }
    with pytest.raises(redfish.RedfishError):
        redfish.hardware_inventory("bmc.test", "admin", "secret")
    assert calls[-1][0] == "DELETE"


@pytest.mark.parametrize(
    "field,value,expected", [("CapacityGB", 1000, 10**12), ("CapacityMiB", 1024, 1024**3)]
)
def test_hpe_smartstorage_capacity_units(bmc, field, value, expected):
    paths, _, _ = bmc
    system = paths["/redfish/v1/Systems/A"]
    del system["Storage"]
    system["Oem"] = {"Hp": {"links": {"SmartStorage": {"href": "/redfish/v1/SmartStorage"}}}}
    paths["/redfish/v1/SmartStorage"] = {
        "links": {"ArrayControllers": {"href": "/redfish/v1/Storage"}}
    }
    paths["/redfish/v1/Controllers/1"] = {
        "links": {"PhysicalDrives": {"href": "/redfish/v1/PhysicalDrives"}}
    }
    paths["/redfish/v1/PhysicalDrives"] = {"Members": [{"@odata.id": "/redfish/v1/Drives/1"}]}
    paths["/redfish/v1/Drives/1"] = {
        "@odata.id": "/redfish/v1/Drives/1",
        "SerialNumber": "actual-disk",
        field: value,
    }
    result = redfish.hardware_inventory("bmc.test", "admin", "secret")
    assert result["drives"][0]["capacity_bytes"] == expected
    # Capacity does not imply disk health or eligibility.
    assert result["drives"][0]["health"] is None


def test_hpe_physical_network_port_does_not_invent_link_state(bmc):
    paths, _, _ = bmc
    paths["/redfish/v1/Systems/A"]["Oem"] = {
        "Hp": {"links": {"NetworkAdapters": {"href": "/redfish/v1/Adapters"}}}
    }
    paths["/redfish/v1/Adapters"] = {"Members": [{"@odata.id": "/redfish/v1/Adapters/1"}]}
    paths["/redfish/v1/Adapters/1"] = {
        "PhysicalPorts": [{"MacAddress": "aa:bb:cc:00:00:02", "SpeedMbps": 10000}]
    }
    result = redfish.hardware_inventory("bmc.test", "admin", "secret")
    port = next(p for p in result["nics"] if p["mac"] == "aa:bb:cc:00:00:02")
    assert port["link_status"] is None


def test_cancel_during_discovery_closes_session_without_hardware_writes(bmc):
    _, calls, _ = bmc
    checkpoints = []

    class Cancelled(Exception):
        pass

    def check():
        checkpoints.append(True)
        if len(checkpoints) == 3:
            raise Cancelled()

    with pytest.raises(Cancelled):
        redfish.hardware_inventory('bmc.test', 'operator', 'secret', check=check)
    assert calls[-1] == ('DELETE', 'https://bmc.test/redfish/v1/SessionService/Sessions/1')
    assert not any(method in ('PATCH', 'PUT') for method, _ in calls)


@pytest.mark.parametrize("legacy", [False, True])
def test_logical_storage_preserves_os_identity_and_physical_membership(bmc, legacy):
    paths, calls, _ = bmc
    controller = paths["/redfish/v1/Controllers/1"]
    if legacy:
        system = paths["/redfish/v1/Systems/A"]
        del system["Storage"]
        system["Oem"] = {"Hp": {"links": {"SmartStorage": {"href": "/redfish/v1/SmartStorage"}}}}
        paths["/redfish/v1/SmartStorage"] = {"links": {"ArrayControllers": {"href": "/redfish/v1/Storage"}}}
        controller["links"] = {"LogicalDrives": {"href": "/redfish/v1/Volumes"}}
    else:
        controller["Volumes"] = {"@odata.id": "/redfish/v1/Volumes"}
    paths["/redfish/v1/Volumes"] = {"Members": [{"@odata.id": "/redfish/v1/Volumes/1"}]}
    volume = {"@odata.id": "/redfish/v1/Volumes/1", "Status": {"Health": "OK", "State": "Enabled"}}
    if legacy:
        volume.update(VolumeUniqueIdentifier="60000000000000000000000000000001", CapacityMiB=1024, Raid="1")
        volume["links"] = {"DataDrives": {"href": "/redfish/v1/VolumeDrives"}}
        paths["/redfish/v1/VolumeDrives"] = {"Members": [{"@odata.id": "/redfish/v1/Drives/1"}]}
    else:
        volume.update(Identifiers=[{"DurableNameFormat": "NAA", "DurableName": "60000000000000000000000000000001"}], CapacityBytes=1024**3, RAIDType="RAID1")
        volume["Links"] = {"Drives": [{"@odata.id": "/redfish/v1/Drives/1"}]}
    paths["/redfish/v1/Volumes/1"] = volume
    report = redfish.hardware_inventory("bmc.test", "operator", "secret")
    assert len(report["logical_volumes"]) == 1
    found = report["logical_volumes"][0]
    assert found["unique_identifier"] == "60000000000000000000000000000001"
    assert found["capacity_bytes"] == 1024**3
    assert found["physical_drive_serials"] == ["disk-a"]
    assert found["os_mapping_verified"] is False
    assert report["hardware_modified"] is False
    assert all(method == "GET" or "/SessionService/Sessions" in path for method, path in calls)


def test_volume_links_cannot_forward_bmc_credentials(bmc):
    paths, _, _ = bmc
    paths["/redfish/v1/Controllers/1"]["Volumes"] = {"@odata.id": "https://other.test/redfish/v1/Volumes"}
    with pytest.raises(redfish.RedfishError, match="unsafe"):
        redfish.hardware_inventory("bmc.test", "operator", "secret")
