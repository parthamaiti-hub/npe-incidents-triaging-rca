import asyncio
import datetime
import uuid

from app.db import Base, make_async_engine, make_async_session_factory, make_engine
from app.models import Incident
from app.sweeper import sweep_once


def _make_incident(incident_id: str, external_id: str, received_at, classification_status: str = "pending") -> Incident:
    return Incident(
        id=incident_id,
        source="teams",
        external_id=external_id,
        raw_text=f"raw-{external_id}",
        classification_status=classification_status,
        received_at=received_at,
    )


def test_sweep_once_republishes_only_stale_pending_incidents(postgres_url, monkeypatch):
    """The sweeper is the safety net for a crash/failure between an Incident's
    DB commit and its RabbitMQ publish. Only incidents still "pending" and
    older than the threshold should be swept -- a fresh "pending" row (still
    within the normal ack/publish window) and any already-classified row
    must be left alone."""
    engine = make_engine(postgres_url)
    Base.metadata.create_all(engine)

    async def _run():
        async_engine = make_async_engine(postgres_url)
        session_factory = make_async_session_factory(async_engine)
        try:
            now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            stale_id = str(uuid.uuid4())
            fresh_id = str(uuid.uuid4())
            resolved_id = str(uuid.uuid4())
            async with session_factory() as session:
                session.add(_make_incident(stale_id, "T-STALE", now - datetime.timedelta(minutes=10)))
                session.add(_make_incident(fresh_id, "T-FRESH", now - datetime.timedelta(seconds=5)))
                session.add(
                    _make_incident(
                        resolved_id, "T-RESOLVED", now - datetime.timedelta(minutes=10), classification_status="resolved"
                    )
                )
                await session.commit()

            publish_calls: list[tuple[str, str, str]] = []

            async def fake_publish(channel, incident_id, raw_text, source):
                publish_calls.append((incident_id, raw_text, source))

            import app.sweeper as sweeper_module

            monkeypatch.setattr(sweeper_module, "publish_incident_received", fake_publish)

            count = await sweep_once(session_factory, channel=None, threshold_minutes=5)

            assert count == 1
            assert publish_calls == [(stale_id, "raw-T-STALE", "teams")]
        finally:
            await async_engine.dispose()

    try:
        asyncio.run(_run())
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
