"""Protocol-2 resync controls and persisted-topology invalidation."""

import asyncio
import json
import uuid

from app.db import SessionLocal
from app.models import BaremetalNode, EnvConfigVersion, Environment, MetricSample
from app.routers.stream import _event_stream, _payload_visible
from app.services import events
from app.services.relay import DBRelay


class Connected:
    async def is_disconnected(self):
        return False


def decode(frame):
    return json.loads(frame.removeprefix("data: "))


def test_v2_connect_overflow_sequence_and_reconnect(monkeypatch):
    monkeypatch.setattr(events, "_QUEUE_MAXSIZE", 1)

    async def run():
        queue = events.subscribe(["fleet"])
        gen = _event_stream(Connected(), queue, enhanced=True)
        first = decode(await anext(gen))
        assert first["topic"] == "stream" and first["payload"]["resync_required"]
        assert first["sequence"] == 1
        await events.publish("fleet", {"environment_id": "one"})
        await events.publish("fleet", {"environment_id": "two"})
        control = decode(await anext(gen))
        assert control["payload"]["reason"] == "overflow" and control["sequence"] == 2
        frame = decode(await anext(gen))
        assert frame["sequence"] == 3 and frame["epoch"] == first["epoch"]
        await gen.aclose()
        second_queue = events.subscribe(["fleet"])
        other = _event_stream(Connected(), second_queue, enhanced=True)
        reconnect = decode(await anext(other))
        assert reconnect["sequence"] == 1 and reconnect["epoch"] != first["epoch"]
        await other.aclose()

    asyncio.run(run())


def test_environment_scope_revocation_applies_to_direct_topics():
    assert _payload_visible("env:one", {}, {"one"})
    assert not _payload_visible("env:one", {}, set())
    assert _payload_visible("env:one", {}, None)


def test_topology_relay_detects_config_hardware_and_deletion():
    async def run():
        with SessionLocal() as db:
            env = Environment(name="stream-" + uuid.uuid4().hex)
            db.add(env)
            db.commit()
            eid = env.id
        relay = DBRelay(SessionLocal)
        await relay.initialize()
        queue = events.subscribe([f"env:{eid}"])
        try:
            await relay.poll_once()
            assert queue.empty()
            with SessionLocal() as db:
                db.add(
                    EnvConfigVersion(
                        environment_id=eid, version=1, yaml_text="servers: {}"
                    )
                )
                db.commit()
            await relay.poll_once()
            topic, payload = queue.get_nowait()
            assert topic == f"env:{eid}" and payload["type"] == "topology"
            assert set(payload) == {"type", "environment_id", "reason"}
            with SessionLocal() as db:
                row = BaremetalNode(
                    environment_id=eid,
                    name="machine",
                    bmc_host="secret-host",
                    bmc_username="secret-user",
                    bmc_password="SECRET",
                )
                db.add(row)
                db.commit()
                node_id = row.id
            await relay.poll_once()
            assert "SECRET" not in str(queue.get_nowait())
            with SessionLocal() as db:
                db.delete(db.get(BaremetalNode, node_id))
                db.commit()
            await relay.poll_once()
            assert queue.get_nowait()[1]["type"] == "topology"
        finally:
            events.unsubscribe(queue)

    asyncio.run(run())


def test_worker_metric_rows_are_relayed_without_labels_or_values():
    async def run():
        with SessionLocal() as db:
            env = Environment(name="metric-stream-" + uuid.uuid4().hex)
            db.add(env)
            db.commit()
            eid = env.id
        relay = DBRelay(SessionLocal)
        await relay.initialize()
        queue = events.subscribe(["metrics"])
        try:
            with SessionLocal() as db:
                db.add(
                    MetricSample(
                        environment_id=eid,
                        name="test",
                        value=1,
                        labels={"private": "SECRET"},
                    )
                )
                db.commit()
            await relay.poll_once()
            topic, payload = queue.get_nowait()
            assert topic == "metrics"
            assert payload == {"type": "metrics", "environment_id": eid, "samples": 1}
            await relay.poll_once()
            assert queue.empty()
        finally:
            events.unsubscribe(queue)

    asyncio.run(run())
