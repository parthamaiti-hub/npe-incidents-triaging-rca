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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.classification import FULLY_CLASSIFIED_STATUSES, apply_classification, classify_raw_text
from app.config import LLM_FALLBACK_ENABLED
from app.db import make_async_engine, make_async_session_factory
from app.embeddings import embed_resolved_incident
from app.events import consume_one, decode, declare_incidents_raw, make_channel, make_connection
from app.llm_client import make_openai_client
from app.models import ClassificationEmbedding, Incident, WorkflowExecution
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
    session_factory: async_sessionmaker,
    payload: dict,
    openai_client: AsyncOpenAI | None = None,
) -> None:
    async with session_factory() as session:
        incident = await session.get(Incident, payload["incident_id"])
        if incident is None:
            raise IncidentNotFoundError(payload["incident_id"])

        result = await classify_raw_text(session, payload["raw_text"], client=openai_client)
        apply_classification(incident, result)

        # Classification commits on its own here -- embedding no
        # longer shares this transaction (see _embed_incident_best_effort
        # below). It used to run inline in this same `async with` block, so
        # an OpenAI hiccup rolled back a classification that had already
        # succeeded, and every one of process_with_retry's retries re-ran
        # the whole thing, inserting a duplicate ClassificationEmbedding row
        # each time it got further than the failure point.
        await session.commit()
        incident_id = incident.id
        status = result["status"]

    if LLM_FALLBACK_ENABLED and status in FULLY_CLASSIFIED_STATUSES:
        await _embed_incident_best_effort(session_factory, openai_client, incident_id)

    await _auto_rca_best_effort(session_factory, incident_id)


async def _auto_rca_best_effort(session_factory: async_sessionmaker, incident_id: str) -> None:
    """The incident's first RCA, right after classification. Runs in its own
    transaction after classification has committed; a failure is logged and
    swallowed (the execution row records it), never re-raised into
    process_with_retry. Skipped if an automatic RCA already exists for this
    incident, so a redelivered message doesn't run it twice."""
    try:
        async with session_factory() as session:
            already = await session.scalar(
                select(WorkflowExecution.id).where(
                    WorkflowExecution.incident_id == incident_id, WorkflowExecution.triggered_by == "auto"
                )
            )
            if already is not None:
                return
            incident = await session.get(Incident, incident_id)
            if incident is None:
                return
            await run_rca_for_incident(session, incident, triggered_by="auto")
    except Exception as exc:  # noqa: BLE001 -- best-effort, see docstring
        logger.warning("Automatic RCA failed for incident %s: %s", incident_id, exc)


async def _embed_incident_best_effort(
    session_factory: async_sessionmaker,
    openai_client: AsyncOpenAI | None,
    incident_id: str,
) -> None:
    """A non-essential enhancement (the RAG corpus) must never fail
    the message that already durably classified this incident. Runs in its
    own transaction, after classification has committed; any failure here
    (OpenAI outage, etc.) is logged and swallowed, never re-raised -- so it
    never triggers process_with_retry's retry/DLQ path. The existence check
    (backed by the partial unique index on ClassificationEmbedding) makes a
    redelivered/retried embed attempt a no-op instead of inserting a
    duplicate row."""
    try:
        async with session_factory() as session:
            already = await session.scalar(
                select(ClassificationEmbedding.id).where(ClassificationEmbedding.incident_id == incident_id)
            )
            if already is not None:
                return
            incident = await session.get(Incident, incident_id)
            if incident is None:
                return
            client = openai_client or make_openai_client()
            await embed_resolved_incident(session, client, incident)
            await session.commit()
    except Exception as exc:  # noqa: BLE001 -- deliberately swallowed, see docstring
        logger.warning("Embedding failed for incident %s (classification already committed): %s", incident_id, exc)


async def process_with_retry(
    session_factory: async_sessionmaker,
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
            await process_message(session_factory, payload, openai_client)
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
    session_factory: async_sessionmaker,
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
        succeeded = await process_with_retry(session_factory, payload, openai_client)
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

    engine = make_async_engine()
    session_factory = make_async_session_factory(engine)
    openai_client = make_openai_client()  # construction is lazy/local -- no network call until first use

    try:
        async with queue.iterator() as iterator:
            while True:
                message = await consume_one(iterator, timeout=None)
                if message is None:
                    continue
                await handle_message(message, session_factory, openai_client)
    finally:
        await connection.close()
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker())
