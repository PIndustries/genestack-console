"""Move the console database between engines without touching the live one."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
import yaml
from sqlalchemy.orm import Session

from app import models  # noqa: F401
from app.config import get_settings
from app.db import Base, create_db_engine
from app.models import Tenant
from app.services.db_move import database_kind, mask_database_url, move_database

_SECRET = "fernet-secret-do-not-log"


def _mount() -> None:
    """Attach the router. main.py is left for the integrator."""
    from app.main import app
    from app.routers.database import router

    for route in app.routes:
        if getattr(route, "path", None) == "/api/v1/database":
            return
    app.include_router(router)


def _config_text() -> str:
    return Path(os.environ["CONSOLE_CONFIG"]).read_text(encoding="utf-8")


def _counts(url: str) -> dict[str, int]:
    from sqlalchemy import func, select

    engine = create_db_engine(url)
    try:
        counts: dict[str, int] = {}
        with engine.connect() as conn:
            for table in Base.metadata.sorted_tables:
                counts[table.name] = conn.execute(
                    select(func.count()).select_from(table)
                ).scalar_one()
        return counts
    finally:
        engine.dispose()


def _tenant_names(url: str) -> list[str]:
    from sqlalchemy import select

    engine = create_db_engine(url)
    try:
        with Session(engine) as session:
            rows = session.scalars(select(Tenant.name).order_by(Tenant.name))
            return list(rows)
    finally:
        engine.dispose()


def test_mask_hides_password():
    masked = mask_database_url(
        "postgresql+psycopg://console:s3cret@db.example:5432/console"
    )
    assert "s3cret" not in masked
    assert "***" in masked
    assert database_kind(
        "postgresql+psycopg://console:s3cret@db.example:5432/console"
    ) == "postgresql"


def test_mysql_url_raises(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("marker: keep-me\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mysql") as caught:
        move_database(
            f"sqlite:///{tmp_path / 'a.db'}",
            "mysql://user:s3cret@localhost/db",
            config,
        )
    assert "s3cret" not in str(caught.value)
    text = config.read_text(encoding="utf-8")
    assert "keep-me" in text
    assert "s3cret" not in text


def test_same_url_raises(tmp_path):
    url = f"sqlite:///{tmp_path / 'a.db'}"
    config = tmp_path / "config.yaml"
    config.write_text("marker: keep-me\ndatabase_url: stay\n", encoding="utf-8")
    with pytest.raises(ValueError, match="same"):
        move_database(url, f"  {url}  ", config)
    text = config.read_text(encoding="utf-8")
    assert "keep-me" in text
    assert "stay" in text


def test_round_trip_sqlite(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    url_a = f"sqlite:///{tmp_path / 'a.db'}"
    url_b = f"sqlite:///{tmp_path / 'b.db'}"
    url_c = f"sqlite:///{tmp_path / 'c.db'}"
    config = tmp_path / "config.yaml"
    config.write_text(
        "# keep this comment\nmarker: keep-me\ndatabase_url: unused\ndry_run: true\n",
        encoding="utf-8",
    )
    engine = create_db_engine(url_a)
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        session.add(Tenant(name="kept", description=_SECRET))
        session.commit()
    engine.dispose()

    import app.db as app_db

    live = app_db.engine
    settings_url = get_settings().database_url
    first = move_database(url_a, url_b, config)
    assert app_db.engine is live
    assert get_settings().database_url == settings_url
    assert first["restart_required"] is True
    assert first["rows"] == 1
    assert first["tables"] == len(Base.metadata.sorted_tables)
    assert _SECRET not in str(first)
    text = config.read_text(encoding="utf-8")
    assert "# keep this comment" in text
    loaded = yaml.safe_load(text)
    assert loaded["database_url"] == url_b
    assert loaded["marker"] == "keep-me"
    assert loaded["dry_run"] is True

    second = move_database(url_b, url_c, config)
    assert app_db.engine is live
    assert get_settings().database_url == settings_url
    assert second["rows"] == 1
    assert _counts(url_a) == _counts(url_b) == _counts(url_c)
    assert _tenant_names(url_c) == ["kept"]
    loaded = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert loaded["database_url"] == url_c
    assert loaded["marker"] == "keep-me"
    assert loaded["dry_run"] is True
    assert _SECRET not in caplog.text


def test_target_with_rows_is_refused(tmp_path):
    url_a = f"sqlite:///{tmp_path / 'a.db'}"
    url_b = f"sqlite:///{tmp_path / 'b.db'}"
    config = tmp_path / "config.yaml"
    original = "# keep\ndatabase_url: stay\n"
    config.write_text(original, encoding="utf-8")
    for url in (url_a, url_b):
        engine = create_db_engine(url)
        Base.metadata.create_all(bind=engine)
        with Session(engine) as session:
            session.add(Tenant(name=f"row-{url[-4]}"))
            session.commit()
        engine.dispose()
    with pytest.raises(ValueError, match="already has rows"):
        move_database(url_a, url_b, config)
    assert config.read_text(encoding="utf-8") == original


def test_get_masks_password(client, admin_headers, monkeypatch):
    _mount()
    settings = get_settings()
    original = settings.database_url
    secret = "s3cret"
    before = _config_text()
    monkeypatch.setattr(
        settings,
        "database_url",
        f"postgresql+psycopg://console:{secret}@db.example:5432/console",
    )
    try:
        response = client.get("/api/v1/database", headers=admin_headers)
        assert response.status_code == 200, response.text
        assert secret not in response.text
        body = response.json()
        assert body["kind"] == "postgresql"
        assert "***" in body["url"]
        assert secret not in body["url"]
    finally:
        settings.database_url = original
    assert get_settings().database_url == original
    assert _config_text() == before


def test_post_mysql_is_400(client, admin_headers):
    _mount()
    before = _config_text()
    response = client.post(
        "/api/v1/database/move",
        headers=admin_headers,
        json={"target_url": "mysql://user:s3cret@localhost/db"},
    )
    assert response.status_code == 400
    assert "s3cret" not in response.text
    assert _config_text() == before


def test_post_same_url_is_400(client, admin_headers):
    _mount()
    before = _config_text()
    current = get_settings().database_url
    response = client.post(
        "/api/v1/database/move",
        headers=admin_headers,
        json={"target_url": current},
    )
    assert response.status_code == 400
    assert _config_text() == before
    assert get_settings().database_url == current


def test_post_operator_is_403(client, operator_headers):
    _mount()
    before = _config_text()
    response = client.post(
        "/api/v1/database/move",
        headers=operator_headers,
        json={"target_url": "mysql://user:s3cret@localhost/db"},
    )
    assert response.status_code == 403
    assert _config_text() == before


def test_post_viewer_is_403(client, viewer_headers):
    _mount()
    before = _config_text()
    response = client.post(
        "/api/v1/database/move",
        headers=viewer_headers,
        json={"target_url": "mysql://user:s3cret@localhost/db"},
    )
    assert response.status_code == 403
    assert _config_text() == before
