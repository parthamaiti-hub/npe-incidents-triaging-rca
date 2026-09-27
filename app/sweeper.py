"""A periodic safety net for the gap between an Incident's DB commit and
its RabbitMQ publish on either ingestion path (webhooks.py commits then
publishes; poller.py collects and publishes only after its batch commits)
-- a crash or broker hiccup between those two steps would strand the row at
classification_status="pending" forever, since nothing else re-triggers
classification.

Re-publishing a still-pending Incident is safe to repeat: classify_raw_text
is deterministic and idempotent (overwrites the same fields), and the
embedding step it can trigger is made idempotent separately (see
app.worker's embed-after-commit split).

Standalone process, same shape as app.poller (own while True/asyncio.sleep
loop, own entry point) -- the only periodic-task convention this codebase
uses; see scripts/start.ps1.

Run standalone:
    uv run python -m app.sweeper
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aio_pika.abc import AbstractChannel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import SWEEP_INTERVAL_SECONDS, SWEEP_THRESHOLD_MINUTES
from app.db import make_async_engine, make_async_session_factory
from app.events import declare_incidents_raw, make_channel, make_connection, publish_incident_received
from app.models import Incident

logger = logging.getLogger(__name__)


async def sweep_once(
    session_factory: async_sessionmaker,
    channel: AbstractChannel,
    threshold_minutes: int = SWEEP_THRESHOLD_MINUTES,
) -> int:
    """Re-publishes every Incident still "pending" older than
    threshold_minutes. Returns the number swept."""
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=threshold_minutes)
    async with session_factory() as session:
        stale = (
            await session.scalars(
                select(Incident).where(Incident.classification_status == "pending", Incident.received_at < cutoff)
            )
        ).all()
        for incident in stale:
            await publish_incident_received(channel, incident.id, incident.raw_text, incident.source)
    return len(stale)


async def run_sweeper() -> None:
    engine = make_async_engine()
    session_factory = make_async_session_factory(engine)
    connection = await make_connection()
    channel = await make_channel(connection)
    await declare_incidents_raw(channel)  # idempotent, same as Base.metadata.create_all

    try:
        while True:
            try:
                count = await sweep_once(session_factory, channel)
                if count:
                    logger.info("Swept %d stale pending incident(s)", count)
            except Exception:
                logger.exception("Sweep cycle failed")
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
    finally:
        await connection.close()
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_sweeper())
