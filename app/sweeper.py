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
from pymongo.asynchronous.database import AsyncDatabase

from app.config import RAG_CLASSIFICATION_RETENTION_MONTHS, SWEEP_INTERVAL_SECONDS, SWEEP_THRESHOLD_MINUTES
from app.db import ensure_indexes, get_database, make_mongo_client
from app.embeddings import prune_incident_vectors
from app.events import declare_incidents_raw, make_channel, make_connection, publish_incident_received
from app.models import Incident
from app.repositories.base import find
from app.vector_store import VectorStore, default_vector_store

logger = logging.getLogger(__name__)


async def sweep_once(
    db: AsyncDatabase,
    channel: AbstractChannel,
    threshold_minutes: int = SWEEP_THRESHOLD_MINUTES,
) -> int:
    """Re-publishes every Incident still "pending" older than
    threshold_minutes. Returns the number swept."""
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=threshold_minutes)
    stale = await find(db, Incident, {"classification_status": "pending", "received_at": {"$lt": cutoff}})
    for incident in stale:
        await publish_incident_received(channel, incident.id, incident.raw_text, incident.source)
    return len(stale)


async def prune_vectors_once(
    vs: VectorStore, retention_months: int | None = RAG_CLASSIFICATION_RETENTION_MONTHS
) -> bool:
    """Chroma doesn't prune itself: drops incident classification vectors
    older than retention_months (footprints are catalog data, never
    pruned). A no-op when retention is unset -- the default, which keeps
    everything, same as before the move to Chroma. Returns whether it ran."""
    if retention_months is None:
        return False
    await prune_incident_vectors(vs, datetime.now(timezone.utc) - timedelta(days=retention_months * 30))
    return True


async def run_sweeper() -> None:
    mongo = make_mongo_client()
    db = get_database(mongo)
    await ensure_indexes(db)
    vs = default_vector_store()
    connection = await make_connection()
    channel = await make_channel(connection)
    await declare_incidents_raw(channel)  # idempotent, same as Base.metadata.create_all

    try:
        while True:
            try:
                count = await sweep_once(db, channel)
                if count:
                    logger.info("Swept %d stale pending incident(s)", count)
            except Exception:
                logger.exception("Sweep cycle failed")
            try:
                await prune_vectors_once(vs)
            except Exception:
                logger.exception("Vector prune failed")
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
    finally:
        await connection.close()
        await mongo.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_sweeper())
