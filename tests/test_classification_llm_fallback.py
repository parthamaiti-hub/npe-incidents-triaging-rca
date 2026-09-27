import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.classification as classification_module
from app.classification import (
    ANY_CATEGORY_PENDING_LLM,
    LLM_RESOLVED,
    MANUAL_TRIAGE,
    LlmClassificationSuggestion,
    classify_raw_text,
)
from app.db import Base, make_async_engine, make_async_session_factory, make_engine, make_session_factory
from app.models import Incident, IncidentMappingRule, SourceSystem, WorkflowDefinition

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


def _vector() -> list[float]:
    from app.config import OPENAI_EMBEDDING_DIMENSIONS

    # Non-zero -- pgvector's cosine distance is undefined for the zero
    # vector, so every mock embedding needs a real direction, not just a
    # placeholder value.
    v = [0.0] * OPENAI_EMBEDDING_DIMENSIONS
    v[0] = 1.0
    return v


async def _embeddings_create(model, input):
    texts = input if isinstance(input, list) else [input]
    response = MagicMock()
    response.data = [MagicMock(embedding=_vector()) for _ in texts]
    return response


def make_embeddings_only_client() -> AsyncMock:
    """A client whose embeddings.create works, but whose
    chat.completions.parse is left as a bare, assertable AsyncMock -- for
    tests asserting the LLM is never actually consulted."""
    client = AsyncMock()
    client.embeddings.create = _embeddings_create
    return client


def make_mock_client(suggestion: LlmClassificationSuggestion) -> AsyncMock:
    client = make_embeddings_only_client()
    completion = MagicMock()
    completion.choices = [MagicMock(message=MagicMock(parsed=suggestion))]
    client.chat.completions.parse = AsyncMock(return_value=completion)
    return client


async def test_rule_match_never_calls_llm(seeded_db, monkeypatch):
    monkeypatch.setattr(classification_module, "LLM_FALLBACK_ENABLED", True)
    engine = make_async_engine(seeded_db)
    session_factory = make_async_session_factory(engine)
    try:
        async with session_factory() as session:
            session.add(
                IncidentMappingRule(
                    id="IMR-1", source_system_id="SYS_HSI", category=CATEGORY, signal_type="keyword",
                    signal_pattern="HSI", priority=1, action="classify",
                )
            )
            await session.commit()

            client = make_embeddings_only_client()  # never touched if the rule wins outright
            result = await classify_raw_text(session, "incident mentioning HSI", client=client)
            assert result["status"] == "resolved"
            assert result["classification_method"] == "rule"
            client.chat.completions.parse.assert_not_called()
    finally:
        await engine.dispose()


async def test_manual_triage_resolves_above_threshold(seeded_db, monkeypatch):
    monkeypatch.setattr(classification_module, "LLM_FALLBACK_ENABLED", True)
    engine = make_async_engine(seeded_db)
    session_factory = make_async_session_factory(engine)
    try:
        async with session_factory() as session:
            # A resolved incident to embed as a retrievable candidate, plus
            # a catalog row so CATEGORY is in the closed set _classify_full
            # validates the LLM's pick against.
            session.add(WorkflowDefinition(id="WFD-1", source_system_id="SYS_HSI", category=CATEGORY))
            prior = Incident(
                id=str(uuid.uuid4()), source="jira", external_id="TT-0", raw_text="hsi outage text",
                classification_status="resolved", source_system_id="SYS_HSI", category=CATEGORY,
            )
            session.add(prior)
            await session.commit()

            from app.embeddings import embed_resolved_incident

            embed_client = make_embeddings_only_client()
            await embed_resolved_incident(session, embed_client, prior)
            await session.commit()

            client = make_mock_client(
                LlmClassificationSuggestion(source_system_id="SYS_HSI", category=CATEGORY, confidence=0.9, rationale="x")
            )
            result = await classify_raw_text(session, "no rule matches this text at all", client=client)
            assert result["status"] == LLM_RESOLVED
            assert result["source_system_id"] == "SYS_HSI"
            assert result["category"] == CATEGORY
            assert result["classification_method"] == "llm"
            assert result["llm_confidence"] == 0.9
    finally:
        await engine.dispose()


async def test_manual_triage_below_threshold_stays_manual_triage(seeded_db, monkeypatch):
    monkeypatch.setattr(classification_module, "LLM_FALLBACK_ENABLED", True)
    monkeypatch.setattr(classification_module, "CLASSIFICATION_CONFIDENCE_THRESHOLD", 0.7)
    engine = make_async_engine(seeded_db)
    session_factory = make_async_session_factory(engine)
    try:
        async with session_factory() as session:
            prior = Incident(
                id=str(uuid.uuid4()), source="jira", external_id="TT-0", raw_text="hsi outage text",
                classification_status="resolved", source_system_id="SYS_HSI", category=CATEGORY,
            )
            session.add(prior)
            await session.commit()
            from app.embeddings import embed_resolved_incident

            embed_client = make_mock_client(
                LlmClassificationSuggestion(source_system_id="SYS_HSI", category=CATEGORY, confidence=0.9, rationale="x")
            )
            await embed_resolved_incident(session, embed_client, prior)
            await session.commit()

            low_confidence_client = make_mock_client(
                LlmClassificationSuggestion(source_system_id="SYS_HSI", category=CATEGORY, confidence=0.3, rationale="x")
            )
            result = await classify_raw_text(session, "ambiguous text", client=low_confidence_client)
            assert result["status"] == MANUAL_TRIAGE
            assert result["classification_method"] is None
            assert result["llm_confidence"] is None
    finally:
        await engine.dispose()


async def test_manual_triage_no_candidates_stays_manual_triage(seeded_db, monkeypatch):
    monkeypatch.setattr(classification_module, "LLM_FALLBACK_ENABLED", True)
    engine = make_async_engine(seeded_db)
    session_factory = make_async_session_factory(engine)
    try:
        async with session_factory() as session:
            client = make_embeddings_only_client()
            result = await classify_raw_text(session, "nothing embedded yet", client=client)
            assert result["status"] == MANUAL_TRIAGE
            client.chat.completions.parse.assert_not_called()
    finally:
        await engine.dispose()


async def test_any_category_pending_llm_resolves_category_only(seeded_db, monkeypatch):
    monkeypatch.setattr(classification_module, "LLM_FALLBACK_ENABLED", True)
    engine = make_async_engine(seeded_db)
    session_factory = make_async_session_factory(engine)
    try:
        async with session_factory() as session:
            session.add_all(
                [
                    IncidentMappingRule(
                        id="IMR-ANY", source_system_id="SYS_HSI", category="ANY", signal_type="keyword",
                        signal_pattern="HSI", priority=3, action="classify",
                    ),
                    WorkflowDefinition(id="WFD-1", source_system_id="SYS_HSI", category=CATEGORY),
                ]
            )
            await session.commit()

            client = make_mock_client(
                LlmClassificationSuggestion(category=CATEGORY, confidence=0.85, rationale="x")
            )
            result = await classify_raw_text(session, "HSI bug in UAT", client=client)
            assert result["status"] == LLM_RESOLVED
            assert result["source_system_id"] == "SYS_HSI"
            assert result["category"] == CATEGORY
            assert result["matched_rule_id"] == "IMR-ANY"
    finally:
        await engine.dispose()


async def test_any_category_pending_llm_invalid_category_rejected(seeded_db, monkeypatch):
    monkeypatch.setattr(classification_module, "LLM_FALLBACK_ENABLED", True)
    engine = make_async_engine(seeded_db)
    session_factory = make_async_session_factory(engine)
    try:
        async with session_factory() as session:
            session.add_all(
                [
                    IncidentMappingRule(
                        id="IMR-ANY", source_system_id="SYS_HSI", category="ANY", signal_type="keyword",
                        signal_pattern="HSI", priority=3, action="classify",
                    ),
                    WorkflowDefinition(id="WFD-1", source_system_id="SYS_HSI", category=CATEGORY),
                ]
            )
            await session.commit()

            # The LLM picks a category with no playbook/rule -- must never
            # be accepted, no matter the confidence.
            client = make_mock_client(
                LlmClassificationSuggestion(category="SOME MADE UP CATEGORY", confidence=0.99, rationale="x")
            )
            result = await classify_raw_text(session, "HSI bug in UAT", client=client)
            assert result["status"] == ANY_CATEGORY_PENDING_LLM
            assert result["category"] is None
    finally:
        await engine.dispose()


async def test_llm_fallback_disabled_stays_pre_llm_status(seeded_db):
    engine = make_async_engine(seeded_db)
    session_factory = make_async_session_factory(engine)
    try:
        async with session_factory() as session:
            result = await classify_raw_text(session, "no rule matches")
            assert result["status"] == MANUAL_TRIAGE
            assert result["classification_method"] is None
    finally:
        await engine.dispose()
