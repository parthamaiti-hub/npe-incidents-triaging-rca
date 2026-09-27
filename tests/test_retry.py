import pytest
from fastapi.testclient import TestClient

from app.db import Base, make_engine, make_session_factory
from app.main import create_app
from app.models import Incident, SourceSystem, WorkflowDefinition, WorkflowDefinitionVersion

CATEGORY = "FUNCTIONAL DEFECT (QA/UAT)"


@pytest.fixture()
def app(postgres_url, redis_url, rabbitmq_url):
    engine = make_engine(postgres_url)
    Base.metadata.create_all(engine)
    yield create_app(database_url=postgres_url, redis_url=redis_url, rabbitmq_url=rabbitmq_url)
    Base.metadata.drop_all(engine)
    engine.dispose()


def _seed(postgres_url):
    """One incident whose active workflow is v2; v1 is superseded (still
    retryable) and v3 is a draft (never vetted, not retryable)."""
    engine = make_engine(postgres_url)
    with make_session_factory(engine)() as session:
        session.add(
            SourceSystem(
                id="SYS_X", name="X", code="X", type="Application", description="x", owning_team="x", environment="NPE"
            )
        )
        session.flush()
        session.add(
            Incident(
                id="INC-1",
                source="jira",
                external_id="TT-1",
                raw_text="x",
                jira_key="TT-1",
                classification_status="resolved",
                source_system_id="SYS_X",
                category=CATEGORY,
            )
        )
        session.add(WorkflowDefinition(id="WFD_X", source_system_id="SYS_X", category=CATEGORY))
        session.flush()
        session.add(
            WorkflowDefinitionVersion(
                id="WFDV_V1",
                workflow_definition_id="WFD_X",
                version_number=1,
                document=[{"call": "error_logs", "with": {"env": "NPE", "app": "X", "lookback_minutes": 60}}],
                status="superseded",
                source="static_authored",
                created_by="test",
            )
        )
        session.add(
            WorkflowDefinitionVersion(
                id="WFDV_V2",
                workflow_definition_id="WFD_X",
                version_number=2,
                document=[{"call": "error_logs", "with": {"env": "NPE", "app": "X", "lookback_minutes": 60}}],
                status="approved",
                source="static_authored",
                created_by="test",
            )
        )
        session.add(
            WorkflowDefinitionVersion(
                id="WFDV_V3",
                workflow_definition_id="WFD_X",
                version_number=3,
                document=[],
                status="draft",
                source="dynamic_generated",
                created_by="test",
            )
        )
        session.commit()
    engine.dispose()


def test_retry_with_no_body_uses_active_version(app, postgres_url):
    _seed(postgres_url)
    with TestClient(app) as client:
        response = client.post("/incidents/TT-1/retry", json={"requested_by": "u1"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["workflow_definition_version_id"] == "WFDV_V2"
    assert body["mapping_overridden"] is False
    assert body["triggered_by"] == "retry"
    assert body["requested_by"] == "u1"
    assert body["rca_status"] is not None


def test_retry_with_explicit_superseded_version_marks_overridden(app, postgres_url):
    _seed(postgres_url)
    with TestClient(app) as client:
        response = client.post(
            "/incidents/TT-1/retry", json={"workflow_definition_version_id": "WFDV_V1", "requested_by": "u1"}
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["workflow_definition_version_id"] == "WFDV_V1"
    assert body["mapping_overridden"] is True


def test_retry_against_draft_version_rejected(app, postgres_url):
    _seed(postgres_url)
    with TestClient(app) as client:
        response = client.post(
            "/incidents/TT-1/retry", json={"workflow_definition_version_id": "WFDV_V3", "requested_by": "u1"}
        )

    assert response.status_code == 422


def test_retry_unknown_jira_key_404s(app, postgres_url):
    _seed(postgres_url)
    with TestClient(app) as client:
        response = client.post("/incidents/NOPE-1/retry", json={"requested_by": "u1"})

    assert response.status_code == 404


def test_retry_history_shows_all_attempts_newest_first(app, postgres_url):
    _seed(postgres_url)
    with TestClient(app) as client:
        client.post("/incidents/TT-1/retry", json={"requested_by": "u1"})
        client.post(
            "/incidents/TT-1/retry", json={"workflow_definition_version_id": "WFDV_V1", "requested_by": "u2"}
        )
        history = client.get("/workflows/executions", params={"jira_key": "TT-1"})

    rows = history.json()
    assert len(rows) == 2
    assert rows[0]["started_at"] >= rows[1]["started_at"]
    assert {r["workflow_definition_version_id"] for r in rows} == {"WFDV_V1", "WFDV_V2"}
