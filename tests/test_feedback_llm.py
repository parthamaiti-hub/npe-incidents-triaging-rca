from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import app.routers.workflows as workflows_router_module
from app.db import Base, make_engine, make_session_factory
from app.models import FeedbackEmbedding, RcaPatternType, WorkflowExecution


@pytest.fixture()
def app(postgres_url, redis_url, rabbitmq_url):
    engine = make_engine(postgres_url)
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        session.add(
            RcaPatternType(
                id="environment_config", description="env/config drift", max_rca_status="Probable",
                status="active", created_by="test",
            )
        )
        session.add(
            RcaPatternType(
                id="retired_pattern", description="old", max_rca_status="Inconclusive", status="retired",
                created_by="test",
            )
        )
        session.commit()
    from app.main import create_app

    yield create_app(database_url=postgres_url, redis_url=redis_url, rabbitmq_url=rabbitmq_url)
    Base.metadata.drop_all(engine)
    engine.dispose()


def _seed_execution(postgres_url) -> str:
    engine = make_engine(postgres_url)
    with make_session_factory(engine)() as session:
        execution = WorkflowExecution(
            id="EXEC-1", workflow_definition_version_id=None, document_snapshot=[], evidence=[],
            rca={"matched_pattern": "environment_config"}, rca_status="Probable", triggered_by="rca", status="completed",
        )
        session.add(execution)
        session.commit()
    engine.dispose()
    return "EXEC-1"


def test_list_rca_patterns_returns_only_active(app):
    with TestClient(app) as client:
        response = client.get("/workflows/rca-patterns")
    assert response.status_code == 200
    ids = {p["id"] for p in response.json()}
    assert ids == {"environment_config"}  # retired_pattern excluded


def test_feedback_structured_correction_persists(app, postgres_url):
    execution_id = _seed_execution(postgres_url)
    with TestClient(app) as client:
        response = client.post(
            f"/workflows/executions/{execution_id}/feedback",
            json={
                "comment": "actually a deployment issue",
                "confidence_score": 2,
                "given_by": "alice",
                "corrected_pattern_id": "environment_config",
                "corrected_rca_status": "Inconclusive",
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["corrected_pattern_id"] == "environment_config"
        assert body["corrected_rca_status"] == "Inconclusive"

        listed = client.get(f"/workflows/executions/{execution_id}/feedback").json()
    assert listed[0]["corrected_pattern_id"] == "environment_config"


def test_feedback_without_correction_still_works(app, postgres_url):
    execution_id = _seed_execution(postgres_url)
    with TestClient(app) as client:
        response = client.post(
            f"/workflows/executions/{execution_id}/feedback",
            json={"comment": "looks right", "confidence_score": 5, "given_by": "bob"},
        )
    assert response.status_code == 201
    assert response.json()["corrected_pattern_id"] is None


def test_feedback_embeds_when_rca_synthesis_llm_enabled(app, postgres_url, monkeypatch):
    execution_id = _seed_execution(postgres_url)

    monkeypatch.setattr(workflows_router_module, "RCA_SYNTHESIS_LLM_ENABLED", True)

    mock_client = AsyncMock()

    async def embeddings_create(model, input):
        from app.config import OPENAI_EMBEDDING_DIMENSIONS

        texts = input if isinstance(input, list) else [input]
        v = [0.0] * OPENAI_EMBEDDING_DIMENSIONS
        v[0] = 1.0
        response = MagicMock()
        response.data = [MagicMock(embedding=v) for _ in texts]
        return response

    mock_client.embeddings.create = embeddings_create
    monkeypatch.setattr(workflows_router_module, "make_openai_client", lambda: mock_client)

    with TestClient(app) as client:
        response = client.post(
            f"/workflows/executions/{execution_id}/feedback",
            json={"comment": "wrong diagnosis", "confidence_score": 1, "given_by": "alice"},
        )
    assert response.status_code == 201

    engine = make_engine(postgres_url)
    with make_session_factory(engine)() as session:
        rows = session.query(FeedbackEmbedding).all()
        assert len(rows) == 1
        assert rows[0].confidence_score == 1
    engine.dispose()
