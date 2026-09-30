import datetime

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Incident, SourceSystem, WorkflowDefinition, WorkflowDefinitionVersion, WorkflowExecution
from dataloadscripts.test_fixtures import insert_docs

CATEGORY = "FUNCTIONAL DEFECT (QA/UAT)"


@pytest.fixture()
def app(mongo_url, mongo_db_name, redis_url, rabbitmq_url, vector_store):
    return create_app(
        mongodb_url=mongo_url, mongodb_db=mongo_db_name, redis_url=redis_url, rabbitmq_url=rabbitmq_url,
        vector_store=vector_store,
    )


def _seed(db):
    insert_docs(
        db,
        SourceSystem(
            id="SYS_X", name="X", code="X", type="Application", description="x", owning_team="x", environment="NPE"
        )
    )
    insert_docs(db, WorkflowDefinition(id="WFD_X", source_system_id="SYS_X", category=CATEGORY))
    insert_docs(
        db,
        WorkflowDefinitionVersion(
            id="WFDV_X1",
            workflow_definition_id="WFD_X",
            version_number=1,
            document=[],
            status="approved",
            source="static_authored",
            created_by="test",
        )
    )
    insert_docs(
        db,
        Incident(
            id="INC-1",
            source="jira",
            external_id="TT-1",
            raw_text="x",
            subject="Dealer code validation failing",
            jira_key="TT-1",
            classification_status="resolved",
            source_system_id="SYS_X",
            category=CATEGORY,
            received_at=datetime.datetime(2026, 1, 1),
        )
    )
    insert_docs(
        db,
        Incident(
            id="INC-2",
            source="jira",
            external_id="TT-2",
            raw_text="x",
            subject="Unrelated ticket",
            jira_key="TT-2",
            classification_status="manual_triage",
            received_at=datetime.datetime(2026, 1, 3),
        )
    )
    # Three executions for INC-1: two older, one newest -- only the
    # newest should surface on the dashboard row.
    for i, (started, rca_status_value, pattern) in enumerate(
        [
            (datetime.datetime(2026, 1, 1, 9, 0), "Inconclusive", "inconclusive"),
            (datetime.datetime(2026, 1, 1, 10, 0), "Inconclusive", "generic_issues_in_checks"),
            (datetime.datetime(2026, 1, 2, 10, 0), "Probable", "functional_defect_recent_release"),
        ]
    ):
        insert_docs(
            db,
            WorkflowExecution(
                id=f"EXEC-1-{i}",
                jira_key="TT-1",
                incident_id="INC-1",
                workflow_definition_version_id="WFDV_X1",
                document_snapshot=[],
                evidence=[],
                rca={"matched_pattern": pattern},
                rca_status=rca_status_value,
                triggered_by="rca",
                status="completed",
                started_at=started,
                completed_at=started + datetime.timedelta(seconds=5),
            )
        )


def test_dashboard_shows_latest_execution_per_incident(app, sync_db):
    _seed(sync_db)
    with TestClient(app) as client:
        response = client.get("/incidents/dashboard")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2  # INC-1 and INC-2, regardless of INC-1 having 3 executions

    by_jira_key = {row["jira_key"]: row for row in body["items"]}
    inc1 = by_jira_key["TT-1"]
    assert inc1["latest_execution_id"] == "EXEC-1-2"
    assert inc1["latest_rca_status"] == "Probable"
    assert inc1["latest_matched_pattern"] == "functional_defect_recent_release"

    inc2 = by_jira_key["TT-2"]
    assert inc2["latest_execution_id"] is None
    assert inc2["latest_rca_status"] is None


def test_dashboard_newest_processed_first(app, sync_db):
    _seed(sync_db)
    with TestClient(app) as client:
        response = client.get("/incidents/dashboard")

    jira_keys = [row["jira_key"] for row in response.json()["items"]]
    assert jira_keys == ["TT-2", "TT-1"]  # INC-2 received 2026-01-03 > INC-1's latest execution 2026-01-02


def test_dashboard_q_filters_by_jira_key_or_subject(app, sync_db):
    _seed(sync_db)
    with TestClient(app) as client:
        response = client.get("/incidents/dashboard", params={"q": "TT-1"})

    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["jira_key"] == "TT-1"


def test_dashboard_rca_status_filter(app, sync_db):
    _seed(sync_db)
    with TestClient(app) as client:
        response = client.get("/incidents/dashboard", params={"rca_status": "Probable"})

    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["jira_key"] == "TT-1"


def test_dashboard_total_independent_of_limit(app, sync_db):
    _seed(sync_db)
    with TestClient(app) as client:
        response = client.get("/incidents/dashboard", params={"limit": 1})

    body = response.json()
    assert body["total"] == 2
    assert len(body["items"]) == 1


def test_dashboard_q_is_a_literal_case_insensitive_substring_not_a_regex(app, sync_db):
    """q goes into a Mongo $regex; it must be escaped, or user input becomes
    a pattern (injection/ReDoS) -- and "(" would be a 500."""
    _seed(sync_db)
    with TestClient(app) as client:
        by_subject = client.get("/incidents/dashboard", params={"q": "dealer CODE"})
        metachars = client.get("/incidents/dashboard", params={"q": "validation ("})
        wildcard = client.get("/incidents/dashboard", params={"q": ".*"})

    assert [r["jira_key"] for r in by_subject.json()["items"]] == ["TT-1"]
    assert metachars.status_code == 200
    assert metachars.json()["total"] == 0
    assert wildcard.json()["total"] == 0
