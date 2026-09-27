import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.asyncio import Redis

from app import rca_status
from app.config import RCA_PENDING_STREAM, RCA_PENDING_STREAM_DLQ, RCA_WORKER_CONSUMER_GROUP
from app.db import Base, make_async_engine, make_async_session_factory, make_engine, make_session_factory
from app.models import RcaPatternType, WorkflowExecution
from app.rca_llm_synthesizer import LlmRcaSuggestion
from app.rca_worker import ensure_consumer_group, process_entry, process_with_retry, publish_rca_pending


@pytest.fixture()
def seeded_db(postgres_url):
    engine = make_engine(postgres_url)
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        session.add(
            RcaPatternType(
                id="environment_config", description="env/config drift", max_rca_status=rca_status.PROBABLE,
                status="active", created_by="test",
            )
        )
        session.commit()
    yield postgres_url
    Base.metadata.drop_all(engine)
    engine.dispose()


def make_mock_client(suggestion: LlmRcaSuggestion | None = None) -> AsyncMock:
    client = AsyncMock()
    if suggestion is not None:
        completion = MagicMock()
        completion.choices = [MagicMock(message=MagicMock(parsed=suggestion))]
        client.chat.completions.parse = AsyncMock(return_value=completion)

    async def embeddings_create(model, input):
        from app.config import OPENAI_EMBEDDING_DIMENSIONS

        texts = input if isinstance(input, list) else [input]
        v = [0.0] * OPENAI_EMBEDDING_DIMENSIONS
        v[0] = 1.0
        response = MagicMock()
        response.data = [MagicMock(embedding=v) for _ in texts]
        return response

    client.embeddings.create = embeddings_create
    return client


EVIDENCE = [{"check": "service_health", "params": {}, "status": "WARN", "details": "degraded"}]


def test_publish_and_consume_rca_pending(redis_url):
    async def _run():
        redis = Redis.from_url(redis_url, decode_responses=True)
        try:
            await ensure_consumer_group(redis)
            await publish_rca_pending(redis, "exec-1", "inc-1", "TT-1", EVIDENCE, "some context")
            response = await redis.xreadgroup(
                RCA_WORKER_CONSUMER_GROUP, f"test-{uuid.uuid4()}", {RCA_PENDING_STREAM: ">"}, count=1, block=5000
            )
            assert response
            _stream, entries = response[0]
            entry_id, fields = entries[0]
            assert fields["execution_id"] == "exec-1"
            assert fields["jira_key"] == "TT-1"
            assert fields["operator_context"] == "some context"
            await redis.xack(RCA_PENDING_STREAM, RCA_WORKER_CONSUMER_GROUP, entry_id)
        finally:
            await redis.aclose()

    import asyncio

    asyncio.run(_run())


async def test_process_entry_writes_back_rca(seeded_db):
    engine = make_async_engine(seeded_db)
    session_factory = make_async_session_factory(engine)
    try:
        async with session_factory() as session:
            execution = WorkflowExecution(
                id=str(uuid.uuid4()), document_snapshot=[], evidence=EVIDENCE, status="completed",
            )
            session.add(execution)
            await session.commit()
            execution_id = execution.id

        client = make_mock_client(
            LlmRcaSuggestion(
                matched_pattern="environment_config", self_assessed_status=rca_status.PROBABLE,
                root_cause_summary="likely config drift", contributing_factors=["x"], recommended_actions=["y"],
            )
        )
        fields = {"execution_id": execution_id, "evidence": __import__("json").dumps(EVIDENCE), "operator_context": ""}
        await process_entry(session_factory, client, fields)

        async with session_factory() as session:
            fetched = await session.get(WorkflowExecution, execution_id)
            assert fetched.rca_status == rca_status.PROBABLE
            assert fetched.rca["matched_pattern"] == "environment_config"
            assert fetched.rca_meta is not None
    finally:
        await engine.dispose()


async def test_process_entry_missing_execution_is_a_noop(seeded_db):
    engine = make_async_engine(seeded_db)
    session_factory = make_async_session_factory(engine)
    try:
        client = make_mock_client()
        fields = {"execution_id": "does-not-exist", "evidence": "[]", "operator_context": ""}
        await process_entry(session_factory, client, fields)  # must not raise
    finally:
        await engine.dispose()


def test_process_with_retry_publishes_to_dlq_after_exhausting_attempts(seeded_db, redis_url):
    async def _run():
        engine = make_async_engine(seeded_db)
        session_factory = make_async_session_factory(engine)
        redis = Redis.from_url(redis_url, decode_responses=True)
        try:
            # No "execution_id" key -> process_entry raises KeyError every attempt.
            bad_fields = {"evidence": "[]"}
            client = make_mock_client()
            await process_with_retry(session_factory, client, redis, "0-1", bad_fields, max_attempts=2)

            response = await redis.xrange(RCA_PENDING_STREAM_DLQ, count=1)
            assert response
            _entry_id, dlq_fields = response[0]
            assert "error" in dlq_fields
        finally:
            await redis.aclose()
            await engine.dispose()

    import asyncio

    asyncio.run(_run())
