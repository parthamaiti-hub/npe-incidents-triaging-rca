import datetime

import pytest
from fastapi.testclient import TestClient

from app.db import Base, make_engine, make_session_factory
from app.main import create_app
from app.models import Incident, SourceSystem, WorkflowDefinition, WorkflowDefinitionVersion, WorkflowExecution

CATEGORY = "FUNCTIONAL DEFECT (QA/UAT)"


@pytest.fixture()
def app(postgres_url, redis_url, rabbitmq_url):
    engine = make_engine(postgres_url)
    Base.metadata.create_all(engine)
    yield create_app(database_url=postgres_url, redis_url=redis_url, rabbitmq_url=rabbitmq_url)
    Base.metadata.drop_all(engine)
    engine.dispose()


def _seed(postgres_url):
    engine = make_engine(postgres_url)
    with make_session_factory(engine)() as session:
        session.add(
            SourceSystem(
                id="SYS_X", name="X", code="X", type="Application", description="x", owning_team="x", environment="NPE"
            )
        )
        session.add(WorkflowDefinition(id="WFD_X", source_system_id="SYS_X", category=CATEGORY))
        session.flush()
        session.add(
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
        session.add(
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
        session.add(
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
        session.flush()
        # Three executions for INC-1: two older, one newest -- only the
        # newest should surface on the dashboard row.
        for i, (started, rca_status_value, pattern) in enumerate(
            [
                (datetime.datetime(2026, 1, 1, 9, 0), "Inconclusive", "inconclusive"),
                (datetime.datetime(2026, 1, 1, 10, 0), "Inconclusive", "generic_issues_in_checks"),
                (datetime.datetime(2026, 1, 2, 10, 0), "Probable", "functional_defect_recent_release"),
            ]
        ):
            session.add(
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
        session.commit()
    engine.dispose()


def test_dashboard_shows_latest_execution_per_incident(app, postgres_url):
    _seed(postgres_url)
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


def test_dashboard_newest_processed_first(app, postgres_url):
    _seed(postgres_url)
    with TestClient(app) as client:
        response = client.get("/incidents/dashboard")

    jira_keys = [row["jira_key"] for row in response.json()["items"]]
    assert jira_keys == ["TT-2", "TT-1"]  # INC-2 received 2026-01-03 > INC-1's latest execution 2026-01-02


def test_dashboard_q_filters_by_jira_key_or_subject(app, postgres_url):
    _seed(postgres_url)
    with TestClient(app) as client:
        response = client.get("/incidents/dashboard", params={"q": "TT-1"})

    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["jira_key"] == "TT-1"


def test_dashboard_rca_status_filter(app, postgres_url):
    _seed(postgres_url)
    with TestClient(app) as client:
        response = client.get("/incidents/dashboard", params={"rca_status": "Probable"})

    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["jira_key"] == "TT-1"


def test_dashboard_total_independent_of_limit(app, postgres_url):
    _seed(postgres_url)
    with TestClient(app) as client:
        response = client.get("/incidents/dashboard", params={"limit": 1})

    body = response.json()
    assert body["total"] == 2
    assert len(body["items"]) == 1
