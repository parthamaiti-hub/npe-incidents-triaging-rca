"""RabbitMQ consumer worker: classifies each raw incident event, writes the
result back onto its Incident row, then runs the incident's first RCA
automatically (the active playbook, or a recorded 'no playbook' / 'not
classified' outcome). Later RCAs are operator retries.

Each message is retried with backoff; once attempts are exhausted it is
nack'ed to the dead-letter queue via RabbitMQ's native ack/nack. The
automatic RCA is best-effort: its failure never fails the message.

Correlation is not done here: it runs synchronously, inline in
app.workflow_orchestrator, with no broker dependency.

Run standalone:
    uv run python -m app.worker
"""

import asyncio
import logging

from aio_pika.abc import AbstractIncomingMessage
from openai import AsyncOpenAI
from pymongo.asynchronous.database import AsyncDatabase

from app.classification import FULLY_CLASSIFIED_STATUSES, apply_classification, classify_raw_text
from app.config import LLM_FALLBACK_ENABLED
from app.db import ensure_indexes, get_database, make_mongo_client
from app.embeddings import embed_resolved_incident, incident_is_embedded
from app.events import consume_one, decode, declare_incidents_raw, make_channel, make_connection
from app.function_registry import refresh_function_registry_from_db
from app.llm_client import make_openai_client
from app.models import Incident, WorkflowExecution
from app.repositories.base import exists, get
from app.vector_store import VectorStore, default_vector_store
from app.workflow_orchestrator import run_rca_for_incident

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


class IncidentNotFoundError(Exception):
    """Logging a warning and returning normally (which the caller then
    acks) would silently discard a message referencing an Incident row that
    doesn't exist yet -- e.g. a publish-before-commit race, or simply a bad
    payload. Raising here lets it flow
    through process_with_retry's existing retry-with-backoff loop (a few
    bounded attempts, covering any lingering commit-visibility race) before
    exhausting and dead-lettering, instead of acking success on missing
    work."""


async def process_message(
    db: AsyncDatabase,
    payload: dict,
    openai_client: AsyncOpenAI | None = None,
    vs: VectorStore | None = None,
) -> None:
    incident = await get(db, Incident, payload["incident_id"])
    if incident is None:
        raise IncidentNotFoundError(payload["incident_id"])

    result = await classify_raw_text(db, payload["raw_text"], client=openai_client, vs=vs)

    # Classification is persisted on its own here -- embedding is a
    # separate, later write (see _embed_incident_best_effort below). It
    # used to share one transaction with classification, so an OpenAI
    # hiccup rolled back a classification that had already succeeded, and
    # every one of process_with_retry's retries re-ran the whole thing.
    await apply_classification(db, incident, result)

    if LLM_FALLBACK_ENABLED and result["status"] in FULLY_CLASSIFIED_STATUSES:
        await _embed_incident_best_effort(db, openai_client, incident.id, vs)

    await _auto_rca_best_effort(db, incident.id)


async def _auto_rca_best_effort(db: AsyncDatabase, incident_id: str) -> None:
    """The incident's first RCA, right after classification has been
    persisted; a failure is logged and swallowed (the execution document
    records it), never re-raised into process_with_retry. Skipped if an
    automatic RCA already exists for this incident, so a redelivered message
    doesn't run it twice."""
    try:
        if await exists(db, WorkflowExecution, {"incident_id": incident_id, "triggered_by": "auto"}):
            return
        incident = await get(db, Incident, incident_id)
        if incident is None:
            return
        await run_rca_for_incident(db, incident, triggered_by="auto")
    except Exception as exc:  # noqa: BLE001 -- best-effort, see docstring
        logger.warning("Automatic RCA failed for incident %s: %s", incident_id, exc)


async def _embed_incident_best_effort(
    db: AsyncDatabase,
    openai_client: AsyncOpenAI | None,
    incident_id: str,
    vs: VectorStore | None = None,
) -> None:
    """A non-essential enhancement (the RAG corpus) must never fail
    the message that already durably classified this incident. Runs after
    classification has been persisted; any failure here (OpenAI or Chroma
    outage, etc.) is logged and swallowed, never re-raised -- so it never
    triggers process_with_retry's retry/DLQ path. The vector id is
    deterministic (incident:<id>) and written with upsert, so a redelivered
    message can't duplicate it; the existence check just avoids paying for
    a second OpenAI embedding call."""
    vs = vs or default_vector_store()
    try:
        if await incident_is_embedded(vs, incident_id):
            return
        incident = await get(db, Incident, incident_id)
        if incident is None:
            return
        client = openai_client or make_openai_client()
        await embed_resolved_incident(vs, client, incident)
    except Exception as exc:  # noqa: BLE001 -- deliberately swallowed, see docstring
        logger.warning("Embedding failed for incident %s (classification already persisted): %s", incident_id, exc)


async def process_with_retry(
    db: AsyncDatabase,
    payload: dict,
    openai_client: AsyncOpenAI | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> bool:
    """Mirrors app.rca_worker's retry philosophy (itself mirroring
    app.playbook_engine._run_with_retry). Returns True on success (caller
    acks), False once attempts are exhausted (caller nack(requeue=False) --
    RabbitMQ's dead-letter-exchange routes it to incidents.raw.dlq
    automatically, no manual DLQ-message construction needed)."""
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            await process_message(db, payload, openai_client)
            return True
        except Exception as exc:  # noqa: BLE001 -- deliberately broad, see app.playbook_engine
            last_error = exc
            logger.warning(
                "Classification attempt %d/%d failed for incident %s: %s",
                attempt,
                max_attempts,
                payload.get("incident_id"),
                exc,
            )
            if attempt < max_attempts:
                await asyncio.sleep(2 ** (attempt - 1))

    logger.error(
        "Classification permanently failed for incident %s after %d attempts (%s) -- dead-lettering",
        payload.get("incident_id"),
        max_attempts,
        last_error,
    )
    return False


async def handle_message(
    message: AbstractIncomingMessage,
    db: AsyncDatabase,
    openai_client: AsyncOpenAI | None = None,
) -> None:
    """Both decode() and process_with_retry are guarded here: an unguarded
    decode exception (a malformed body, a schema change) would propagate
    through run_worker's `finally`, close the connection, and kill the whole
    classification pipeline over one poison message. Instead a single bad
    message is nacked to the DLQ and the consume loop keeps running."""
    try:
        payload = decode(message)
    except Exception as exc:  # noqa: BLE001 -- any decode failure is unrecoverable for this message
        logger.error("Failed to decode message, dead-lettering: %s", exc)
        await message.nack(requeue=False)
        return

    try:
        succeeded = await process_with_retry(db, payload, openai_client)
    except Exception as exc:  # noqa: BLE001 -- backstop; process_with_retry already catches internally
        logger.error("Unexpected error handling message, dead-lettering: %s", exc)
        await message.nack(requeue=False)
        return

    if succeeded:
        await message.ack()
    else:
        await message.nack(requeue=False)


async def run_worker() -> None:
    connection = await make_connection()
    channel = await make_channel(connection)
    queue = await declare_incidents_raw(channel)

    mongo = make_mongo_client()
    db = get_database(mongo)
    await ensure_indexes(db)
    # The worker runs RCAs itself, so it needs the DB's function versions
    # (pinned contracts, retry policies), not just the built-in defaults.
    await refresh_function_registry_from_db(db)
    openai_client = make_openai_client()  # construction is lazy/local -- no network call until first use

    try:
        async with queue.iterator() as iterator:
            while True:
                message = await consume_one(iterator, timeout=None)
                if message is None:
                    continue
                await handle_message(message, db, openai_client)
    finally:
        await connection.close()
        await mongo.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker())
