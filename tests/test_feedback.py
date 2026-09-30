import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import RcaPatternType, WorkflowExecution
from dataloadscripts.test_fixtures import insert_docs


@pytest.fixture()
def app(mongo_url, mongo_db_name, redis_url, rabbitmq_url, vector_store):
    return create_app(
        mongodb_url=mongo_url, mongodb_db=mongo_db_name, redis_url=redis_url, rabbitmq_url=rabbitmq_url,
        vector_store=vector_store,
    )


def _seed_execution(db) -> str:
    insert_docs(
        db,
        WorkflowExecution(
            id="EXEC-1",
            workflow_definition_version_id=None,
            document_snapshot=[],
            evidence=[],
            rca={"matched_pattern": "inconclusive"},
            rca_status="Inconclusive",
            triggered_by="rca",
            status="completed",
        ),
    )
    return "EXEC-1"


def test_post_and_list_feedback(app, sync_db):
    execution_id = _seed_execution(sync_db)
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


def test_feedback_confidence_score_out_of_range_rejected(app, sync_db):
    execution_id = _seed_execution(sync_db)
    with TestClient(app) as client:
        response = client.post(
            f"/workflows/executions/{execution_id}/feedback",
            json={"comment": "x", "confidence_score": 6, "given_by": "alice"},
        )
    assert response.status_code == 422


def test_feedback_with_unknown_corrected_pattern_rejected(app, sync_db):
    """What the rca_pattern_type foreign key used to refuse is now an
    explicit check."""
    execution_id = _seed_execution(sync_db)
    insert_docs(
        sync_db,
        RcaPatternType(id="environment_config", description="d", max_rca_status="Probable", created_by="t"),
    )
    with TestClient(app) as client:
        unknown = client.post(
            f"/workflows/executions/{execution_id}/feedback",
            json={"confidence_score": 2, "given_by": "alice", "corrected_pattern_id": "no_such_pattern"},
        )
        known = client.post(
            f"/workflows/executions/{execution_id}/feedback",
            json={"confidence_score": 2, "given_by": "alice", "corrected_pattern_id": "environment_config"},
        )

    assert unknown.status_code == 422
    assert known.status_code == 201, known.text
    assert known.json()["corrected_pattern_id"] == "environment_config"
