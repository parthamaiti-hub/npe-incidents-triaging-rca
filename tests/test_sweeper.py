import asyncio
import datetime
import uuid

from app.models import Incident
from app.sweeper import sweep_once
from dataloadscripts.test_fixtures import insert_docs, open_db


def _make_incident(incident_id: str, external_id: str, received_at, classification_status: str = "pending") -> Incident:
    return Incident(
        id=incident_id,
        source="teams",
        external_id=external_id,
        raw_text=f"raw-{external_id}",
        classification_status=classification_status,
        received_at=received_at,
    )


def test_sweep_once_republishes_only_stale_pending_incidents(sync_db, mongo_url, mongo_db_name, monkeypatch):
    """The sweeper is the safety net for a crash/failure between an Incident's
    DB commit and its RabbitMQ publish. Only incidents still "pending" and
    older than the threshold should be swept -- a fresh "pending" row (still
    within the normal ack/publish window) and any already-classified row
    must be left alone."""
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    stale_id = str(uuid.uuid4())
    fresh_id = str(uuid.uuid4())
    resolved_id = str(uuid.uuid4())
    insert_docs(
        sync_db,
        _make_incident(stale_id, "T-STALE", now - datetime.timedelta(minutes=10)),
        _make_incident(fresh_id, "T-FRESH", now - datetime.timedelta(seconds=5)),
        _make_incident(resolved_id, "T-RESOLVED", now - datetime.timedelta(minutes=10), classification_status="resolved"),
    )

    async def _run():
        async with open_db(mongo_url, mongo_db_name) as db:
            publish_calls: list[tuple[str, str, str]] = []

            async def fake_publish(channel, incident_id, raw_text, source):
                publish_calls.append((incident_id, raw_text, source))

            import app.sweeper as sweeper_module

            monkeypatch.setattr(sweeper_module, "publish_incident_received", fake_publish)

            count = await sweep_once(db, channel=None, threshold_minutes=5)

            assert count == 1
            assert publish_calls == [(stale_id, "raw-T-STALE", "teams")]

    asyncio.run(_run())
