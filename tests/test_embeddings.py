import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.config import OPENAI_EMBEDDING_DIMENSIONS
from app.db import Base, make_async_engine, make_async_session_factory, make_engine, make_session_factory
from app.embeddings import (
    embed_feedback,
    embed_footprint,
    embed_resolved_incident,
    retrieve_classification_candidates,
    retrieve_feedback_context,
)
from app.models import ClassificationEmbedding, Incident, RcaFeedback, SourceSystem, SystemFootprint, WorkflowExecution
from app.worker import _embed_incident_best_effort

CATEGORY = "FUNCTIONAL DEFECT (QA/UAT)"


@pytest.fixture()
def seeded_db(postgres_url):
    engine = make_engine(postgres_url)
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        session.add(
            SourceSystem(
                id="SYS_HSI", name="HSI", code="HSI", type="Application", description="x", owning_team="x", environment="NPE"
            )
        )
        session.commit()
    yield postgres_url
    Base.metadata.drop_all(engine)
    engine.dispose()


def _vector(seed: float) -> list[float]:
    """A deterministic-but-distinguishable embedding: mostly zeros with one
    seed-dependent value, so cosine distance between two different seeds is
    nonzero and stable -- exactly what these tests need to assert ordering
    without a real embedding model."""
    v = [0.0] * OPENAI_EMBEDDING_DIMENSIONS
    v[0] = seed
    v[1] = 1.0
    return v


def make_mock_client(embedding_for_text=None):
    """embedding_for_text: optional dict mapping substrings of the input
    text to a seed value, so different calls return distinguishable
    vectors. Falls back to a fixed vector otherwise."""
    client = AsyncMock()

    async def create(model, input):
        texts = input if isinstance(input, list) else [input]
        vectors = []
        for text in texts:
            seed = 0.0
            if embedding_for_text:
                for substr, s in embedding_for_text.items():
                    if substr in text:
                        seed = s
                        break
            vectors.append(_vector(seed))
        response = MagicMock()
        response.data = [MagicMock(embedding=v) for v in vectors]
        return response

    client.embeddings.create = create
    return client


async def test_embed_footprint_and_retrieve_classification_candidates(seeded_db):
    engine = make_async_engine(seeded_db)
    session_factory = make_async_session_factory(engine)
    try:
        async with session_factory() as session:
            footprint = SystemFootprint(
                id="FP-1", source_system_id="SYS_HSI", footprint_type="hostname", value="hsi.internal", notes=None
            )
            session.add(footprint)
            await session.commit()

            client = make_mock_client({"hsi.internal": 5.0, "query text": 5.0})
            await embed_footprint(session, client, footprint)
            await session.commit()

            candidates = await retrieve_classification_candidates(session, client, "query text about hsi.internal")
            assert len(candidates) == 1
            assert candidates[0].kind == "footprint"
            assert candidates[0].source_system_id == "SYS_HSI"
    finally:
        await engine.dispose()


async def test_embed_resolved_incident_skips_unclassified(seeded_db):
    engine = make_async_engine(seeded_db)
    session_factory = make_async_session_factory(engine)
    try:
        async with session_factory() as session:
            incident = Incident(
                id=str(uuid.uuid4()),
                source="jira",
                external_id="TT-1",
                raw_text="x",
                classification_status="manual_triage",
                source_system_id=None,
                category=None,
            )
            session.add(incident)
            await session.commit()

            client = make_mock_client()
            await embed_resolved_incident(session, client, incident)
            await session.commit()

            candidates = await retrieve_classification_candidates(session, client, "anything")
            assert candidates == []
    finally:
        await engine.dispose()


async def test_retrieve_classification_candidates_scoped_by_source_system(seeded_db):
    engine = make_async_engine(seeded_db)
    session_factory = make_async_session_factory(engine)
    try:
        async with session_factory() as session:
            session.add(
                SourceSystem(
                    id="SYS_OTHER", name="Other", code="OTH", type="Application", description="x", owning_team="x",
                    environment="NPE",
                )
            )
            await session.flush()
            incident1 = Incident(
                id=str(uuid.uuid4()), source="jira", external_id="TT-1", raw_text="hsi text",
                classification_status="resolved", source_system_id="SYS_HSI", category=CATEGORY,
            )
            incident2 = Incident(
                id=str(uuid.uuid4()), source="jira", external_id="TT-2", raw_text="other text",
                classification_status="resolved", source_system_id="SYS_OTHER", category=CATEGORY,
            )
            session.add_all([incident1, incident2])
            await session.commit()

            client = make_mock_client()
            await embed_resolved_incident(session, client, incident1)
            await embed_resolved_incident(session, client, incident2)
            await session.commit()

            candidates = await retrieve_classification_candidates(session, client, "query", source_system_id="SYS_HSI")
            assert len(candidates) == 1
            assert candidates[0].source_system_id == "SYS_HSI"
    finally:
        await engine.dispose()


async def test_embed_feedback_and_retrieve_feedback_context(seeded_db):
    engine = make_async_engine(seeded_db)
    session_factory = make_async_session_factory(engine)
    try:
        async with session_factory() as session:
            execution = WorkflowExecution(
                id=str(uuid.uuid4()), document_snapshot=[], evidence=[], status="completed",
                rca={"matched_pattern": "environment_config", "root_cause_summary": "bad config"},
                rca_status="Probable",
            )
            session.add(execution)
            await session.flush()
            feedback = RcaFeedback(
                workflow_execution_id=execution.id, comment="actually a deployment issue", confidence_score=2,
                given_by="alice",
            )
            session.add(feedback)
            await session.commit()

            client = make_mock_client()
            await embed_feedback(session, client, feedback, execution)
            await session.commit()

            retrieved = await retrieve_feedback_context(session, client, "evidence text")
            assert len(retrieved) == 1
            assert retrieved[0].confidence_score == 2
            assert "deployment issue" in retrieved[0].text
    finally:
        await engine.dispose()


async def test_embed_incident_best_effort_swallows_embedding_failure(seeded_db):
    """Embedding used to run inside the same transaction as the classification
    write -- an OpenAI failure rolled back an already-successful
    classification. _embed_incident_best_effort now runs after that
    transaction has committed, in its own; a failure here must be swallowed,
    never raised, and must leave no partial ClassificationEmbedding row."""
    engine = make_async_engine(seeded_db)
    session_factory = make_async_session_factory(engine)
    try:
        async with session_factory() as session:
            incident = Incident(
                id=str(uuid.uuid4()), source="jira", external_id="TT-E4-1", raw_text="x",
                classification_status="resolved", source_system_id="SYS_HSI", category=CATEGORY,
            )
            session.add(incident)
            await session.commit()
            incident_id = incident.id

        failing_client = AsyncMock()

        async def raise_on_create(model, input):
            raise RuntimeError("simulated OpenAI outage")

        failing_client.embeddings.create = raise_on_create

        await _embed_incident_best_effort(session_factory, failing_client, incident_id)  # must not raise

        async with session_factory() as session:
            rows = (
                await session.scalars(select(ClassificationEmbedding).where(ClassificationEmbedding.incident_id == incident_id))
            ).all()
            assert rows == []
    finally:
        await engine.dispose()


async def test_embed_incident_best_effort_is_idempotent_on_redelivery(seeded_db):
    """A redelivered/retried message must not insert a second
    ClassificationEmbedding row for the same incident -- the existence
    check (backed by the DB's partial unique index) makes a repeat call a
    no-op."""
    engine = make_async_engine(seeded_db)
    session_factory = make_async_session_factory(engine)
    try:
        async with session_factory() as session:
            incident = Incident(
                id=str(uuid.uuid4()), source="jira", external_id="TT-E4-2", raw_text="x",
                classification_status="resolved", source_system_id="SYS_HSI", category=CATEGORY,
            )
            session.add(incident)
            await session.commit()
            incident_id = incident.id

        client = make_mock_client()
        await _embed_incident_best_effort(session_factory, client, incident_id)
        await _embed_incident_best_effort(session_factory, client, incident_id)  # simulated redelivery

        async with session_factory() as session:
            rows = (
                await session.scalars(select(ClassificationEmbedding).where(ClassificationEmbedding.incident_id == incident_id))
            ).all()
            assert len(rows) == 1
    finally:
        await engine.dispose()
