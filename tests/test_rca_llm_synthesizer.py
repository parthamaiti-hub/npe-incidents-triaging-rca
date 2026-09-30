from unittest.mock import AsyncMock, MagicMock

import pytest

from app import rca_status
from app.embeddings import embed_feedback
from app.models import RcaFeedback, RcaPatternType, WorkflowExecution
from app.rca_llm_synthesizer import NOVEL_PATTERN, LlmRcaSuggestion, synthesize_rca_via_llm
from dataloadscripts.test_fixtures import insert_docs, open_db


@pytest.fixture()
async def db(sync_db, mongo_url, mongo_db_name):
    insert_docs(
        sync_db,
        RcaPatternType(
            id="environment_config", description="env/config drift", max_rca_status=rca_status.PROBABLE,
            status="active", created_by="test",
        ),
        RcaPatternType(
            id="generic_issues_in_checks", description="generic warn/error", max_rca_status=rca_status.INCONCLUSIVE,
            status="active", created_by="test",
        ),
    )
    async with open_db(mongo_url, mongo_db_name) as handle:
        yield handle


def _vector() -> list[float]:
    from app.config import OPENAI_EMBEDDING_DIMENSIONS

    v = [0.0] * OPENAI_EMBEDDING_DIMENSIONS
    v[0] = 1.0
    return v


def make_mock_client(suggestion: LlmRcaSuggestion) -> AsyncMock:
    client = AsyncMock()
    completion = MagicMock()
    completion.choices = [MagicMock(message=MagicMock(parsed=suggestion))]
    client.chat.completions.parse = AsyncMock(return_value=completion)

    async def embeddings_create(model, input):
        texts = input if isinstance(input, list) else [input]
        response = MagicMock()
        response.data = [MagicMock(embedding=_vector()) for _ in texts]
        return response

    client.embeddings.create = embeddings_create
    return client


EVIDENCE = [{"check": "service_health", "params": {}, "status": "WARN", "details": "degraded"}]


async def test_clamp_downgrades_over_confident_llm_output(db, vector_store):
    # LLM claims Identified, but environment_config's ceiling is Probable.
    client = make_mock_client(
        LlmRcaSuggestion(
            matched_pattern="environment_config", self_assessed_status=rca_status.IDENTIFIED,
            root_cause_summary="x", contributing_factors=["x"], recommended_actions=["x"],
        )
    )
    result = await synthesize_rca_via_llm(db, client, EVIDENCE, vs=vector_store)
    assert result["matched_pattern"] == "environment_config"
    assert result["rca_status"] == rca_status.PROBABLE  # clamped down, not Identified


async def test_clamp_never_upgrades_below_ceiling(db, vector_store):
    # LLM is itself uncertain (Inconclusive) -- must stay Inconclusive,
    # never bumped up to the pattern's Probable ceiling.
    client = make_mock_client(
        LlmRcaSuggestion(
            matched_pattern="environment_config", self_assessed_status=rca_status.INCONCLUSIVE,
            root_cause_summary="x", contributing_factors=[], recommended_actions=["x"],
        )
    )
    result = await synthesize_rca_via_llm(db, client, EVIDENCE, vs=vector_store)
    assert result["rca_status"] == rca_status.INCONCLUSIVE


async def test_unrecognized_pattern_becomes_novel_pending_review(db, vector_store):
    client = make_mock_client(
        LlmRcaSuggestion(
            matched_pattern="some_pattern_not_in_catalog", self_assessed_status=rca_status.IDENTIFIED,
            root_cause_summary="x", contributing_factors=[], recommended_actions=["x"],
        )
    )
    result = await synthesize_rca_via_llm(db, client, EVIDENCE, vs=vector_store)
    assert result["matched_pattern"] == NOVEL_PATTERN
    # Never invented as a new label with an unearned high confidence.
    assert result["rca_status"] == rca_status.INCONCLUSIVE


async def test_categorical_outcomes_pass_through_unclamped(db, vector_store):
    client = make_mock_client(
        LlmRcaSuggestion(
            matched_pattern="environment_config", self_assessed_status=rca_status.NEED_MANUAL_INTERVENTION,
            root_cause_summary="x", contributing_factors=[], recommended_actions=["x"],
        )
    )
    result = await synthesize_rca_via_llm(db, client, EVIDENCE, vs=vector_store)
    assert result["rca_status"] == rca_status.NEED_MANUAL_INTERVENTION


async def test_rca_meta_carries_audit_trail(db, vector_store):
    client = make_mock_client(
        LlmRcaSuggestion(
            matched_pattern="generic_issues_in_checks", self_assessed_status=rca_status.INCONCLUSIVE,
            root_cause_summary="x", contributing_factors=[], recommended_actions=["x"],
        )
    )
    result = await synthesize_rca_via_llm(db, client, EVIDENCE, operator_context="known related change", vs=vector_store)
    assert "model" in result["rca_meta"]
    assert "generated_at" in result["rca_meta"]
    assert "retrieved_context_ids" in result["rca_meta"]


async def test_retrieved_context_ids_are_the_feedback_vector_ids(db, vector_store):
    """rca_meta.retrieved_context_ids records the deterministic Chroma ids
    (feedback:<rca_feedback_id>) of the grounding it was given."""
    execution = WorkflowExecution(document_snapshot=[], rca={"matched_pattern": "environment_config"}, rca_status="Probable")
    feedback = RcaFeedback(workflow_execution_id=execution.id, comment="config", confidence_score=4, given_by="alice")
    client = make_mock_client(
        LlmRcaSuggestion(
            matched_pattern="environment_config", self_assessed_status=rca_status.PROBABLE,
            root_cause_summary="x", contributing_factors=[], recommended_actions=["x"],
        )
    )
    await embed_feedback(vector_store, client, feedback, execution)

    result = await synthesize_rca_via_llm(db, client, EVIDENCE, vs=vector_store)
    assert result["rca_meta"]["retrieved_context_ids"] == [f"feedback:{feedback.id}"]
