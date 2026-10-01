"""Parse and attach [timing] scoreboard lines from deploy/greenfield logs."""

from app.services.deploy_timing import attach_stage_times, fmt_duration, parse_timings


def test_parse_timings_stages_items_hosts_and_total():
    log = """
[2026-09-05T21:16:31Z] [greenfield] DESTRUCTIVE
[timing] phase=pxe seconds=12.0
[timing] phase=metal host=controller1 seconds=261.2
[timing] phase=metal host=compute1 seconds=451.0
[timing] phase=metal seconds=975.4
[timing] stage=operators item=mariadb-operator seconds=18.2
[timing] stage=operators item=cert-manager seconds=40.1
[timing] stage=operators seconds=146.0
[timing] stage=core item=keystone seconds=280.4
[timing] stage=core seconds=412.1
[timing] phase=total seconds=1533.5
"""
    parsed = parse_timings(log)
    assert parsed["total_s"] == 1533.5
    assert parsed["phases"][0] == {"id": "pxe", "seconds": 12.0}
    assert parsed["phases"][1]["id"] == "metal"
    assert parsed["hosts"][0]["host"] == "controller1"
    assert parsed["hosts"][0]["seconds"] == 261.2
    core = next(s for s in parsed["stages"] if s["id"] == "core")
    assert core["seconds"] == 412.1
    assert core["items"][0] == {"name": "keystone", "seconds": 280.4}


def test_parse_timings_sums_when_total_missing():
    log = "[timing] stage=core seconds=10\n[timing] stage=nova seconds=20.5\n"
    parsed = parse_timings(log)
    assert parsed["total_s"] == 30.5


def test_attach_stage_times_copies_seconds():
    stages = [
        {
            "id": "core",
            "name": "OpenStack Core",
            "state": "done",
            "items": [
                {"name": "keystone", "state": "done"},
                {"name": "glance", "state": "done"},
            ],
        }
    ]
    attach_stage_times(
        stages,
        {
            "stages": [
                {
                    "id": "core",
                    "seconds": 412.1,
                    "items": [{"name": "keystone", "seconds": 280.4}],
                }
            ]
        },
    )
    assert stages[0]["seconds"] == 412.1
    assert stages[0]["items"][0]["seconds"] == 280.4
    assert "seconds" not in stages[0]["items"][1]


def test_fmt_duration():
    assert fmt_duration(12) == "12s"
    assert fmt_duration(75) == "1m15s"
    assert fmt_duration(3723) == "1h02m"
    assert fmt_duration(None) == ""
