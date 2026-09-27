"""Redis Stream consumer: LLM/RAG-primary RCA synthesis, decoupled from
workflow_orchestrator.execute_and_record's request path. Its own consumer
group ("npe-rca-worker") on stream:rca-pending (Valkey, already deployed
for idempotency) -- this is "reliably hand a job to a background worker,"
not "retained log with replay/fan-out," so a Redis Stream is sufficient.

Concurrency for burst absorption (target scale: 3,000 incidents/24h
across 300 applications -- one application's bad day can plausibly burst
a few hundred entries at once): run multiple instances of this process
sharing the same consumer group. Redis Streams' consumer-group semantics
claim each pending entry to exactly one consumer, so this needs no
in-process task pool to get real concurrency -- `uv run python -m
app.rca_worker` more than once, same group, same stream.

Run standalone:
    uv run python -m app.rca_worker
"""

import asyncio
import datetime
import json
import logging
import uuid

from openai import AsyncOpenAI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import RCA_PENDING_STREAM, RCA_PENDING_STREAM_DLQ, RCA_WORKER_CONSUMER_GROUP
from app.db import make_async_engine, make_async_session_factory
from app.idempotency import make_redis
from app.llm_client import make_openai_client
from app.models import WorkflowExecution
from app.rca_llm_synthesizer import synthesize_rca_via_llm

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


async def ensure_consumer_group(redis: Redis, stream: str = RCA_PENDING_STREAM, group: str = RCA_WORKER_CONSUMER_GROUP) -> None:
    """Idempotent group creation -- mkstream=True so the stream exists even
    before the first publish, id='0' so a fresh group sees every entry
    already on the stream rather than only future ones (matches this
    module's own "don't lose work" intent)."""
    try:
        await redis.xgroup_create(stream, group, id="0", mkstream=True)
    except Exception as exc:  # noqa: BLE001 -- BUSYGROUP means it already exists, anything else re-raises
        if "BUSYGROUP" not in str(exc):
            raise


async def publish_rca_pending(
    redis: Redis,
    execution_id: str,
    incident_id: str | None,
    jira_key: str | None,
    evidence: list[dict],
    operator_context: str | None,
) -> None:
    await redis.xadd(
        RCA_PENDING_STREAM,
        {
            "execution_id": execution_id,
            "incident_id": incident_id or "",
            "jira_key": jira_key or "",
            "evidence": json.dumps(evidence),
            "operator_context": operator_context or "",
            "schema_version": "1",
            "emitted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        },
    )


async def process_entry(
    session_factory: async_sessionmaker, client: AsyncOpenAI, fields: dict
) -> None:
    execution_id = fields["execution_id"]
    async with session_factory() as session:
        execution = await session.get(WorkflowExecution, execution_id)
        if execution is None:
            logger.warning("WorkflowExecution %s not found, skipping", execution_id)
            return

        evidence = json.loads(fields["evidence"])
        operator_context = fields.get("operator_context") or None
        result = await synthesize_rca_via_llm(session, client, evidence, operator_context)

        execution.rca = {k: v for k, v in result.items() if k != "rca_meta"}
        execution.rca_status = result["rca_status"]
        execution.rca_meta = result["rca_meta"]
        await session.commit()


async def process_with_retry(
    session_factory: async_sessionmaker,
    client: AsyncOpenAI,
    redis: Redis,
    entry_id: str,
    fields: dict,
    max_attempts: int = MAX_ATTEMPTS,
) -> None:
    """Mirrors app.playbook_engine._run_with_retry's retry philosophy: a bad
    entry doesn't crash the worker or get lost. After exhausting retries,
    publish to the DLQ stream, log, and let the caller XACK regardless --
    at-least-once, deliberately, same as app.worker."""
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            await process_entry(session_factory, client, fields)
            return
        except Exception as exc:  # noqa: BLE001 -- deliberately broad, see app.playbook_engine
            last_error = exc
            logger.warning(
                "RCA synthesis attempt %d/%d failed for execution %s: %s",
                attempt,
                max_attempts,
                fields.get("execution_id"),
                exc,
            )
            if attempt < max_attempts:
                await asyncio.sleep(2 ** (attempt - 1))

    logger.error(
        "RCA synthesis permanently failed for execution %s after %d attempts (%s) -- publishing to DLQ",
        fields.get("execution_id"),
        max_attempts,
        last_error,
    )
    await redis.xadd(RCA_PENDING_STREAM_DLQ, {**fields, "error": str(last_error)})


async def consume_one(
    redis: Redis, consumer_name: str, block_ms: int = 3000
) -> tuple[str, dict] | None:
    """Reads exactly one new entry (or None on timeout) -- used both by the
    standalone worker loop and by tests, which need a bounded read rather
    than an infinite block. block_ms must stay safely under this redis-py
    version's own ~5s client-side socket read timeout (see run_rca_worker's
    comment) -- too close to that boundary risks an occasional spurious
    TimeoutError instead of a clean empty-response timeout."""
    response = await redis.xreadgroup(
        RCA_WORKER_CONSUMER_GROUP, consumer_name, {RCA_PENDING_STREAM: ">"}, count=1, block=block_ms
    )
    if not response:
        return None
    _stream_name, entries = response[0]
    if not entries:
        return None
    entry_id, fields = entries[0]
    return entry_id, fields


async def run_rca_worker(consumer_name: str | None = None) -> None:
    consumer_name = consumer_name or f"rca-worker-{uuid.uuid4().hex[:8]}"
    engine = make_async_engine()
    session_factory = make_async_session_factory(engine)
    redis = make_redis()
    client = make_openai_client()
    await ensure_consumer_group(redis)
    try:
        while True:
            # Bounded block + loop, not BLOCK 0 (infinite) -- this redis-py
            # version applies its own ~5s client-side socket read timeout
            # regardless of the server-side BLOCK duration (visible as
            # `orig_socket_timeout` on the connection pool's kwargs), so a
            # true indefinite block raises redis.exceptions.TimeoutError
            # the moment the stream sits idle past that window. Looping on
            # a finite block matches the pattern consume_one's own test
            # usage already relies on, and is more resilient to a network
            # blip than a single indefinite read would be anyway.
            entry = await consume_one(redis, consumer_name, block_ms=3000)
            if entry is not None:
                entry_id, fields = entry
                await process_with_retry(session_factory, client, redis, entry_id, fields)
                await redis.xack(RCA_PENDING_STREAM, RCA_WORKER_CONSUMER_GROUP, entry_id)  # at-least-once, deliberately
    finally:
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_rca_worker())
