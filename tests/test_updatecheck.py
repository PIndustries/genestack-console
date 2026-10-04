from app.services.updatecheck import is_newer, parse_calver


def test_calver_newer():
    assert is_newer("2026.09.01", "2026.08.06")
    assert not is_newer("2026.08.06", "2026.08.06")
    assert not is_newer("2026.07.01", "2026.08.06")
    assert is_newer("2026.08.06", "2026.08.06-rc1")
    assert parse_calver("2026.9.1")[1] == 9


def test_calver_build_number():
    assert is_newer("2026.10.04.3", "2026.10.04.2")
    assert not is_newer("2026.10.04.2", "2026.10.04.3")
    assert is_newer("2026.10.04.1", "2026.10.04.1-rc1")
    assert is_newer("2026.10.05.1", "2026.10.04.9")
    assert not is_newer("2026.10.04.3", "2026.10.04.3")
    assert is_newer("2026.10.04.1", "2026.10.03")
    assert not is_newer("v2026.10.04.3", "2026.10.04.3")
    assert parse_calver("2026.10.04.3")[3] == 3
    assert parse_calver("2026.10.03")[3] == 0
    assert parse_calver("2026.08.06-rc1")[4] == 0
    assert parse_calver("2026.08.06")[4] == 1
