from app.services.updatecheck import is_newer, parse_calver


def test_calver_newer():
    assert is_newer("2026.09.01", "2026.08.06")
    assert not is_newer("2026.08.06", "2026.08.06")
    assert not is_newer("2026.07.01", "2026.08.06")
    assert is_newer("2026.08.06", "2026.08.06-rc1")
    assert parse_calver("2026.9.1")[1] == 9
