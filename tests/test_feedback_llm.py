import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import app.routers.workflows as workflows_router_module
from app.models import RcaFeedback, RcaPatternType, WorkflowExecution
from dataloadscripts.test_fixtures import find_docs, insert_docs


@pytest.fixture()
def app(sync_db, mongo_url, mongo_db_name, redis_url, rabbitmq_url, vector_store):
    insert_docs(
        sync_db,
        RcaPatternType(
            id="environment_config", description="env/config drift", max_rca_status="Probable",
            status="active", created_by="test",
        ),
        RcaPatternType(
            id="retired_pattern", description="old", max_rca_status="Inconclusive", status="retired",
            created_by="test",
        ),
    )
    from app.main import create_app

    return create_app(
        mongodb_url=mongo_url, mongodb_db=mongo_db_name, redis_url=redis_url, rabbitmq_url=rabbitmq_url,
        vector_store=vector_store,
    )


def _seed_execution(db) -> str:
    insert_docs(
        db,
        WorkflowExecution(
            id="EXEC-1", workflow_definition_version_id=None, document_snapshot=[], evidence=[],
            rca={"matched_pattern": "environment_config"}, rca_status="Probable", triggered_by="rca", status="completed",
        ),
    )
    return "EXEC-1"


def test_list_rca_patterns_returns_only_active(app):
    with TestClient(app) as client:
        response = client.get("/workflows/rca-patterns")
    assert response.status_code == 200
    ids = {p["id"] for p in response.json()}
    assert ids == {"environment_config"}  # retired_pattern excluded


def test_feedback_structured_correction_persists(app, sync_db):
    execution_id = _seed_execution(sync_db)
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


def test_feedback_without_correction_still_works(app, sync_db):
    execution_id = _seed_execution(sync_db)
    with TestClient(app) as client:
        response = client.post(
            f"/workflows/executions/{execution_id}/feedback",
            json={"comment": "looks right", "confidence_score": 5, "given_by": "bob"},
        )
    assert response.status_code == 201
    assert response.json()["corrected_pattern_id"] is None


def test_feedback_embeds_when_rca_synthesis_llm_enabled(app, sync_db, vector_store, monkeypatch):
    execution_id = _seed_execution(sync_db)

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

    async def stored_vectors():
        return await (await vector_store.feedback()).get(include=["metadatas"])

    vectors = asyncio.run(stored_vectors())
    assert vectors["ids"] == [f"feedback:{response.json()['id']}"]
    assert vectors["metadatas"][0]["confidence_score"] == 1
    assert vectors["metadatas"][0]["workflow_execution_id"] == execution_id


def test_feedback_is_stored_even_when_embedding_fails(app, sync_db, monkeypatch):
    """The vector is derived data: an OpenAI/Chroma failure must not lose
    (or fail) the operator's feedback -- rebuild_vector_index fills the gap."""
    execution_id = _seed_execution(sync_db)
    monkeypatch.setattr(workflows_router_module, "RCA_SYNTHESIS_LLM_ENABLED", True)

    failing_client = AsyncMock()

    async def raise_on_create(model, input):
        raise RuntimeError("simulated OpenAI outage")

    failing_client.embeddings.create = raise_on_create
    monkeypatch.setattr(workflows_router_module, "make_openai_client", lambda: failing_client)

    with TestClient(app) as client:
        response = client.post(
            f"/workflows/executions/{execution_id}/feedback",
            json={"comment": "x", "confidence_score": 3, "given_by": "alice"},
        )

    assert response.status_code == 201
    assert [f.id for f in find_docs(sync_db, RcaFeedback)] == [response.json()["id"]]
