import datetime
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import OPENAI_EMBEDDING_DIMENSIONS
from app.embeddings import (
    embed_feedback,
    embed_footprint,
    embed_resolved_incident,
    incident_vector_id,
    retrieve_classification_candidates,
    retrieve_feedback_context,
)
from app.models import Incident, RcaFeedback, SourceSystem, SystemFootprint, WorkflowExecution
from app.sweeper import prune_vectors_once
from app.worker import _embed_incident_best_effort
from dataloadscripts.test_fixtures import insert_docs, open_db

CATEGORY = "FUNCTIONAL DEFECT (QA/UAT)"


@pytest.fixture()
def seeded_db(sync_db):
    insert_docs(
        sync_db,
        SourceSystem(
            id="SYS_HSI", name="HSI", code="HSI", type="Application", description="x", owning_team="x", environment="NPE"
        ),
    )
    return sync_db


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


async def test_embed_footprint_and_retrieve_classification_candidates(vector_store):
    footprint = SystemFootprint(
        id="FP-1", source_system_id="SYS_HSI", footprint_type="hostname", value="hsi.internal", notes=None
    )
    client = make_mock_client({"hsi.internal": 5.0, "query text": 5.0})
    await embed_footprint(vector_store, client, footprint)

    candidates = await retrieve_classification_candidates(vector_store, client, "query text about hsi.internal")
    assert len(candidates) == 1
    assert candidates[0].kind == "footprint"
    assert candidates[0].source_system_id == "SYS_HSI"
    assert candidates[0].category is None
    assert candidates[0].text == "hostname: hsi.internal"


async def test_retrieve_classification_candidates_on_an_empty_corpus_returns_nothing(vector_store):
    candidates = await retrieve_classification_candidates(vector_store, make_mock_client(), "anything")
    assert candidates == []


async def test_retrieval_orders_by_cosine_similarity(vector_store):
    client = make_mock_client({"near": 1.0, "far": -5.0, "query": 1.0})
    await embed_footprint(
        vector_store, client, SystemFootprint(id="FP-FAR", source_system_id="SYS_HSI", footprint_type="t", value="far")
    )
    await embed_footprint(
        vector_store, client, SystemFootprint(id="FP-NEAR", source_system_id="SYS_HSI", footprint_type="t", value="near")
    )

    candidates = await retrieve_classification_candidates(vector_store, client, "query")
    assert [c.id for c in candidates] == ["footprint:FP-NEAR", "footprint:FP-FAR"]


async def test_embed_resolved_incident_skips_unclassified(vector_store):
    incident = Incident(
        source="jira",
        external_id="TT-1",
        raw_text="x",
        classification_status="manual_triage",
        source_system_id=None,
        category=None,
    )
    client = make_mock_client()
    await embed_resolved_incident(vector_store, client, incident)

    candidates = await retrieve_classification_candidates(vector_store, client, "anything")
    assert candidates == []


async def test_retrieve_classification_candidates_scoped_by_source_system(vector_store):
    incident1 = Incident(
        source="jira", external_id="TT-1", raw_text="hsi text",
        classification_status="resolved", source_system_id="SYS_HSI", category=CATEGORY,
    )
    incident2 = Incident(
        source="jira", external_id="TT-2", raw_text="other text",
        classification_status="resolved", source_system_id="SYS_OTHER", category=CATEGORY,
    )
    client = make_mock_client()
    await embed_resolved_incident(vector_store, client, incident1)
    await embed_resolved_incident(vector_store, client, incident2)

    candidates = await retrieve_classification_candidates(vector_store, client, "query", source_system_id="SYS_HSI")
    assert len(candidates) == 1
    assert candidates[0].source_system_id == "SYS_HSI"
    assert candidates[0].category == CATEGORY
    assert candidates[0].id == incident_vector_id(incident1.id)


async def test_embed_feedback_and_retrieve_feedback_context(vector_store):
    execution = WorkflowExecution(
        document_snapshot=[], evidence=[], status="completed",
        rca={"matched_pattern": "environment_config", "root_cause_summary": "bad config"},
        rca_status="Probable",
    )
    feedback = RcaFeedback(
        workflow_execution_id=execution.id, comment="actually a deployment issue", confidence_score=2, given_by="alice"
    )
    client = make_mock_client()
    await embed_feedback(vector_store, client, feedback, execution)

    retrieved = await retrieve_feedback_context(vector_store, client, "evidence text")
    assert len(retrieved) == 1
    assert retrieved[0].id == f"feedback:{feedback.id}"
    assert retrieved[0].confidence_score == 2
    assert retrieved[0].workflow_execution_id == execution.id
    assert "deployment issue" in retrieved[0].text


async def test_retrieve_feedback_context_excludes_feedback_older_than_retention(vector_store):
    execution = WorkflowExecution(document_snapshot=[], rca={}, rca_status="Probable")
    old = RcaFeedback(
        workflow_execution_id=execution.id, comment="ancient", confidence_score=3, given_by="a",
        created_at=datetime.datetime(2000, 1, 1),
    )
    recent = RcaFeedback(workflow_execution_id=execution.id, comment="recent", confidence_score=4, given_by="b")
    client = make_mock_client()
    await embed_feedback(vector_store, client, old, execution)
    await embed_feedback(vector_store, client, recent, execution)

    retrieved = await retrieve_feedback_context(vector_store, client, "evidence text")
    assert [r.id for r in retrieved] == [f"feedback:{recent.id}"]


async def test_embedding_is_idempotent_by_deterministic_id(vector_store):
    """Replaces the old partial unique index: re-embedding the same
    incident upserts one vector, never a second."""
    incident = Incident(
        source="jira", external_id="TT-1", raw_text="x",
        classification_status="resolved", source_system_id="SYS_HSI", category=CATEGORY,
    )
    client = make_mock_client()
    await embed_resolved_incident(vector_store, client, incident)
    await embed_resolved_incident(vector_store, client, incident)

    assert await (await vector_store.classification()).count() == 1


async def test_prune_drops_old_incident_vectors_but_keeps_footprints(vector_store):
    client = make_mock_client()
    old = Incident(
        source="jira", external_id="TT-OLD", raw_text="old", received_at=datetime.datetime(2000, 1, 1),
        classification_status="resolved", source_system_id="SYS_HSI", category=CATEGORY,
    )
    recent = Incident(
        source="jira", external_id="TT-NEW", raw_text="new",
        classification_status="resolved", source_system_id="SYS_HSI", category=CATEGORY,
    )
    await embed_resolved_incident(vector_store, client, old)
    await embed_resolved_incident(vector_store, client, recent)
    await embed_footprint(vector_store, client, SystemFootprint(id="FP-1", source_system_id="SYS_HSI", footprint_type="t", value="v"))

    assert await prune_vectors_once(vector_store, retention_months=None) is False  # unset = keep everything
    assert await (await vector_store.classification()).count() == 3

    assert await prune_vectors_once(vector_store, retention_months=6) is True
    remaining = (await (await vector_store.classification()).get())["ids"]
    assert sorted(remaining) == sorted(["footprint:FP-1", incident_vector_id(recent.id)])


async def test_embed_incident_best_effort_swallows_embedding_failure(seeded_db, mongo_url, mongo_db_name, vector_store):
    """Embedding used to run inside the same transaction as the classification
    write -- an OpenAI failure rolled back an already-successful
    classification. _embed_incident_best_effort now runs after classification
    has been persisted; a failure here must be swallowed, never raised, and
    must leave no partial vector."""
    incident = Incident(
        id=str(uuid.uuid4()), source="jira", external_id="TT-E4-1", raw_text="x",
        classification_status="resolved", source_system_id="SYS_HSI", category=CATEGORY,
    )
    insert_docs(seeded_db, incident)

    failing_client = AsyncMock()

    async def raise_on_create(model, input):
        raise RuntimeError("simulated OpenAI outage")

    failing_client.embeddings.create = raise_on_create

    async with open_db(mongo_url, mongo_db_name) as db:
        await _embed_incident_best_effort(db, failing_client, incident.id, vector_store)  # must not raise

    assert (await (await vector_store.classification()).get(ids=[incident_vector_id(incident.id)]))["ids"] == []


async def test_embed_incident_best_effort_is_idempotent_on_redelivery(seeded_db, mongo_url, mongo_db_name, vector_store):
    """A redelivered/retried message must not produce a second vector for
    the same incident -- nor a second (paid) OpenAI embedding call."""
    incident = Incident(
        id=str(uuid.uuid4()), source="jira", external_id="TT-E4-2", raw_text="x",
        classification_status="resolved", source_system_id="SYS_HSI", category=CATEGORY,
    )
    insert_docs(seeded_db, incident)

    calls = []
    client = make_mock_client()
    original_create = client.embeddings.create

    async def counting_create(model, input):
        calls.append(input)
        return await original_create(model, input)

    client.embeddings.create = counting_create

    async with open_db(mongo_url, mongo_db_name) as db:
        await _embed_incident_best_effort(db, client, incident.id, vector_store)
        await _embed_incident_best_effort(db, client, incident.id, vector_store)  # simulated redelivery

    assert await (await vector_store.classification()).count() == 1
    assert len(calls) == 1
