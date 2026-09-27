import pytest
from fastapi.testclient import TestClient

from app.db import Base, make_engine, make_session_factory
from app.main import create_app
from app.models import WorkflowExecution


@pytest.fixture()
def app(postgres_url, redis_url, rabbitmq_url):
    engine = make_engine(postgres_url)
    Base.metadata.create_all(engine)
    yield create_app(database_url=postgres_url, redis_url=redis_url, rabbitmq_url=rabbitmq_url)
    Base.metadata.drop_all(engine)
    engine.dispose()


def _seed_execution(postgres_url) -> str:
    engine = make_engine(postgres_url)
    with make_session_factory(engine)() as session:
        execution = WorkflowExecution(
            id="EXEC-1",
            workflow_definition_version_id=None,
            document_snapshot=[],
            evidence=[],
            rca={"matched_pattern": "inconclusive"},
            rca_status="Inconclusive",
            triggered_by="rca",
            status="completed",
        )
        session.add(execution)
        session.commit()
    engine.dispose()
    return "EXEC-1"


def test_post_and_list_feedback(app, postgres_url):
    execution_id = _seed_execution(postgres_url)
    with TestClient(app) as client:
        first = client.post(
            f"/workflows/executions/{execution_id}/feedback",
            json={"comment": "looks about right", "confidence_score": 4, "given_by": "alice"},
        )
        assert first.status_code == 201, first.text

        second = client.post(
            f"/workflows/executions/{execution_id}/feedback",
            json={"comment": None, "confidence_score": 2, "given_by": "bob"},
        )
        assert second.status_code == 201

        listed = client.get(f"/workflows/executions/{execution_id}/feedback")

    assert listed.status_code == 200
    rows = listed.json()
    assert [r["given_by"] for r in rows] == ["alice", "bob"]  # submission order
    assert rows[0]["confidence_score"] == 4
    assert rows[1]["comment"] is None


def test_feedback_against_unknown_execution_404s(app):
    with TestClient(app) as client:
        response = client.post(
            "/workflows/executions/NOPE/feedback",
            json={"comment": "x", "confidence_score": 3, "given_by": "alice"},
        )
    assert response.status_code == 404


def test_feedback_confidence_score_out_of_range_rejected(app, postgres_url):
    execution_id = _seed_execution(postgres_url)
    with TestClient(app) as client:
        response = client.post(
            f"/workflows/executions/{execution_id}/feedback",
            json={"comment": "x", "confidence_score": 6, "given_by": "alice"},
        )
    assert response.status_code == 422
