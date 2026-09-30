import re
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Incident, SourceSystem, WorkflowDefinition, WorkflowDefinitionVersion
from dataloadscripts.test_fixtures import insert_docs, set_fields

CATEGORY = "FUNCTIONAL DEFECT (QA/UAT)"

RESOLVED_SYS_X = {
    "status": "resolved",
    "matched_rule_id": None,
    "source_system_id": "SYS_X",
    "category": CATEGORY,
    "classification_method": "rule",
    "llm_confidence": None,
}
MANUAL_TRIAGE = {
    "status": "manual_triage",
    "matched_rule_id": None,
    "source_system_id": None,
    "category": None,
    "classification_method": None,
    "llm_confidence": None,
}


@pytest.fixture()
def app(mongo_url, mongo_db_name, redis_url, rabbitmq_url, vector_store):
    return create_app(
        mongodb_url=mongo_url, mongodb_db=mongo_db_name, redis_url=redis_url, rabbitmq_url=rabbitmq_url,
        vector_store=vector_store,
    )


@pytest.fixture(autouse=True)
def classifier(monkeypatch):
    """Retry re-classifies the stored text first. These tests are about
    version selection, so the classifier is stubbed to the seeded
    classification unless a test changes it."""
    import app.routers.incidents as incidents_router

    mock = AsyncMock(return_value=RESOLVED_SYS_X)
    monkeypatch.setattr(incidents_router, "classify_raw_text", mock)
    return mock


def _seed(db):
    """One incident whose active workflow is v2; v1 is superseded (still
    retryable) and v3 is a draft (never vetted, not retryable)."""
    insert_docs(
        db,
        SourceSystem(
            id="SYS_X", name="X", code="X", type="Application", description="x", owning_team="x", environment="NPE"
        ),
        Incident(
            id="INC-1",
            source="jira",
            external_id="TT-1",
            raw_text="x",
            jira_key="TT-1",
            classification_status="resolved",
            source_system_id="SYS_X",
            category=CATEGORY,
        ),
        WorkflowDefinition(id="WFD_X", source_system_id="SYS_X", category=CATEGORY),
        WorkflowDefinitionVersion(
            id="WFDV_V1",
            workflow_definition_id="WFD_X",
            version_number=1,
            document=[{"call": "error_logs", "with": {"env": "NPE", "app": "X", "lookback_minutes": 60}}],
            status="superseded",
            source="static_authored",
            created_by="test",
        ),
        WorkflowDefinitionVersion(
            id="WFDV_V2",
            workflow_definition_id="WFD_X",
            version_number=2,
            document=[{"call": "error_logs", "with": {"env": "NPE", "app": "X", "lookback_minutes": 60}}],
            status="approved",
            source="static_authored",
            created_by="test",
        ),
        WorkflowDefinitionVersion(
            id="WFDV_V3",
            workflow_definition_id="WFD_X",
            version_number=3,
            document=[],
            status="draft",
            source="dynamic_generated",
            created_by="test",
        ),
    )


def test_retry_with_no_body_uses_active_version(app, sync_db):
    _seed(sync_db)
    with TestClient(app) as client:
        response = client.post("/incidents/TT-1/retry", json={"requested_by": "u1"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["workflow_definition_version_id"] == "WFDV_V2"
    assert body["mapping_overridden"] is False
    assert body["triggered_by"] == "retry"
    assert body["requested_by"] == "u1"
    assert body["rca_status"] is not None


def test_retry_with_explicit_superseded_version_marks_overridden(app, sync_db):
    _seed(sync_db)
    with TestClient(app) as client:
        response = client.post(
            "/incidents/TT-1/retry", json={"workflow_definition_version_id": "WFDV_V1", "requested_by": "u1"}
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["workflow_definition_version_id"] == "WFDV_V1"
    assert body["mapping_overridden"] is True


def test_retry_against_draft_version_rejected(app, sync_db):
    _seed(sync_db)
    with TestClient(app) as client:
        response = client.post(
            "/incidents/TT-1/retry", json={"workflow_definition_version_id": "WFDV_V3", "requested_by": "u1"}
        )

    assert response.status_code == 422


def test_retry_unknown_jira_key_404s(app, sync_db):
    _seed(sync_db)
    with TestClient(app) as client:
        response = client.post("/incidents/NOPE-1/retry", json={"requested_by": "u1"})

    assert response.status_code == 404


def test_retry_history_shows_all_attempts_newest_first(app, sync_db):
    _seed(sync_db)
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


def test_retry_reclassifies_so_a_fixed_mapping_rule_takes_effect(app, sync_db, classifier):
    """Stored as unclassifiable; after the mapping rule is fixed (the
    classifier now resolves it), retry updates the incident and runs the
    playbook for the new classification."""
    _seed(sync_db)
    _set_classification(sync_db, "INC-1", MANUAL_TRIAGE)
    classifier.return_value = RESOLVED_SYS_X
    with TestClient(app) as client:
        response = client.post("/incidents/TT-1/retry", json={"requested_by": "u1"})
        incident = client.get("/incidents/INC-1").json()

    assert response.status_code == 200, response.text
    assert response.json()["workflow_definition_version_id"] == "WFDV_V2"
    assert classifier.await_args.args[1] == "x"  # the stored incident text
    assert incident["classification_status"] == "resolved"
    assert incident["source_system_id"] == "SYS_X"


def test_retry_records_not_classified_instead_of_rejecting(app, sync_db, classifier):
    _seed(sync_db)
    classifier.return_value = MANUAL_TRIAGE
    with TestClient(app) as client:
        response = client.post("/incidents/TT-1/retry", json={"requested_by": "u1"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["workflow_definition_version_id"] is None
    assert body["rca"]["matched_pattern"] == "not_classified"
    assert body["rca_status"] == "NeedManualIntervention"
    assert body["requested_by"] == "u1"


def test_incident_without_jira_key_gets_a_generated_key_and_can_be_retried(app, sync_db):
    _seed(sync_db)
    first = Incident(source="teams", external_id="msg-1", raw_text="x")
    second = Incident(source="teams", external_id="msg-2", raw_text="x")
    insert_docs(sync_db, first, second)
    keys = [first.incident_key, second.incident_key]

    pattern = re.compile(r"^int_\d{14}_\d{5}$")
    assert all(pattern.match(k) for k in keys), keys
    assert keys[0] != keys[1]
    assert int(keys[1][-5:]) == int(keys[0][-5:]) + 1  # running number

    with TestClient(app) as client:
        response = client.post(f"/incidents/{keys[0]}/retry", json={"requested_by": "u1"})
        history = client.get("/workflows/executions", params={"incident_key": keys[0]}).json()

    assert response.status_code == 200, response.text
    assert response.json()["incident_key"] == keys[0]
    assert [r["id"] for r in history] == [response.json()["id"]]


def test_jira_incident_key_is_its_jira_key(app, sync_db):
    _seed(sync_db)
    with TestClient(app) as client:
        assert client.get("/incidents/INC-1").json()["incident_key"] == "TT-1"


def _set_classification(db, incident_id: str, result: dict) -> None:
    set_fields(
        db,
        Incident,
        incident_id,
        classification_status=result["status"],
        source_system_id=result["source_system_id"],
        category=result["category"],
    )
