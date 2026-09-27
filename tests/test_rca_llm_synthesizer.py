from unittest.mock import AsyncMock, MagicMock

import pytest

from app import rca_status
from app.db import Base, make_async_engine, make_async_session_factory, make_engine, make_session_factory
from app.models import RcaPatternType
from app.rca_llm_synthesizer import NOVEL_PATTERN, LlmRcaSuggestion, synthesize_rca_via_llm


@pytest.fixture()
def seeded_db(postgres_url):
    engine = make_engine(postgres_url)
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        session.add_all(
            [
                RcaPatternType(
                    id="environment_config", description="env/config drift", max_rca_status=rca_status.PROBABLE,
                    status="active", created_by="test",
                ),
                RcaPatternType(
                    id="generic_issues_in_checks", description="generic warn/error", max_rca_status=rca_status.INCONCLUSIVE,
                    status="active", created_by="test",
                ),
            ]
        )
        session.commit()
    yield postgres_url
    Base.metadata.drop_all(engine)
    engine.dispose()


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


async def test_clamp_downgrades_over_confident_llm_output(seeded_db):
    engine = make_async_engine(seeded_db)
    session_factory = make_async_session_factory(engine)
    try:
        async with session_factory() as session:
            # LLM claims Identified, but environment_config's ceiling is Probable.
            client = make_mock_client(
                LlmRcaSuggestion(
                    matched_pattern="environment_config", self_assessed_status=rca_status.IDENTIFIED,
                    root_cause_summary="x", contributing_factors=["x"], recommended_actions=["x"],
                )
            )
            result = await synthesize_rca_via_llm(session, client, EVIDENCE)
            assert result["matched_pattern"] == "environment_config"
            assert result["rca_status"] == rca_status.PROBABLE  # clamped down, not Identified
    finally:
        await engine.dispose()


async def test_clamp_never_upgrades_below_ceiling(seeded_db):
    engine = make_async_engine(seeded_db)
    session_factory = make_async_session_factory(engine)
    try:
        async with session_factory() as session:
            # LLM is itself uncertain (Inconclusive) -- must stay Inconclusive,
            # never bumped up to the pattern's Probable ceiling.
            client = make_mock_client(
                LlmRcaSuggestion(
                    matched_pattern="environment_config", self_assessed_status=rca_status.INCONCLUSIVE,
                    root_cause_summary="x", contributing_factors=[], recommended_actions=["x"],
                )
            )
            result = await synthesize_rca_via_llm(session, client, EVIDENCE)
            assert result["rca_status"] == rca_status.INCONCLUSIVE
    finally:
        await engine.dispose()


async def test_unrecognized_pattern_becomes_novel_pending_review(seeded_db):
    engine = make_async_engine(seeded_db)
    session_factory = make_async_session_factory(engine)
    try:
        async with session_factory() as session:
            client = make_mock_client(
                LlmRcaSuggestion(
                    matched_pattern="some_pattern_not_in_catalog", self_assessed_status=rca_status.IDENTIFIED,
                    root_cause_summary="x", contributing_factors=[], recommended_actions=["x"],
                )
            )
            result = await synthesize_rca_via_llm(session, client, EVIDENCE)
            assert result["matched_pattern"] == NOVEL_PATTERN
            # Never invented as a new label with an unearned high confidence.
            assert result["rca_status"] == rca_status.INCONCLUSIVE
    finally:
        await engine.dispose()


async def test_categorical_outcomes_pass_through_unclamped(seeded_db):
    engine = make_async_engine(seeded_db)
    session_factory = make_async_session_factory(engine)
    try:
        async with session_factory() as session:
            client = make_mock_client(
                LlmRcaSuggestion(
                    matched_pattern="environment_config", self_assessed_status=rca_status.NEED_MANUAL_INTERVENTION,
                    root_cause_summary="x", contributing_factors=[], recommended_actions=["x"],
                )
            )
            result = await synthesize_rca_via_llm(session, client, EVIDENCE)
            assert result["rca_status"] == rca_status.NEED_MANUAL_INTERVENTION
    finally:
        await engine.dispose()


async def test_rca_meta_carries_audit_trail(seeded_db):
    engine = make_async_engine(seeded_db)
    session_factory = make_async_session_factory(engine)
    try:
        async with session_factory() as session:
            client = make_mock_client(
                LlmRcaSuggestion(
                    matched_pattern="generic_issues_in_checks", self_assessed_status=rca_status.INCONCLUSIVE,
                    root_cause_summary="x", contributing_factors=[], recommended_actions=["x"],
                )
            )
            result = await synthesize_rca_via_llm(session, client, EVIDENCE, operator_context="known related change")
            assert "model" in result["rca_meta"]
            assert "generated_at" in result["rca_meta"]
            assert "retrieved_context_ids" in result["rca_meta"]
    finally:
        await engine.dispose()
