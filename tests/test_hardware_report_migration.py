"""Existing node/boot data survives addition of non-destructive inventory."""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session


def test_sqlite_hardware_report_upgrade_preserves_node(tmp_path, monkeypatch):
    from app import db
    from app.models import BaremetalNode

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    db.Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BaremetalNode(
                id="old-node",
                environment_id="existing-env",
                name="existing",
                bmc_host="bmc.test",
                bmc_username="admin",
                bmc_password="fixture",
                next_boot="disk",
                expected_ip="192.0.2.10",
            )
        )
        session.commit()
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE baremetal_nodes DROP COLUMN hardware_report")
    monkeypatch.setattr(db, "engine", engine)
    db._ensure_columns_sqlite()
    db._ensure_columns_sqlite()
    with Session(engine) as session:
        node = session.get(BaremetalNode, "old-node")
        assert node.hardware_report is None
        assert node.next_boot == "disk"
        assert node.expected_ip == "192.0.2.10"
        assert node.bmc_password == "fixture"
    engine.dispose()
