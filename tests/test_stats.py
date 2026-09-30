import datetime

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Incident, SourceSystem, WorkflowExecution
from dataloadscripts.test_fixtures import insert_docs


@pytest.fixture()
def app(mongo_url, mongo_db_name, redis_url, rabbitmq_url, vector_store):
    return create_app(
        mongodb_url=mongo_url, mongodb_db=mongo_db_name, redis_url=redis_url, rabbitmq_url=rabbitmq_url,
        vector_store=vector_store,
    )


def _seed(db):
    """Four incidents, one per stats bucket, all processed "now" (within
    every period window) plus one old one outside a 24h window -- to prove
    period filtering actually excludes it."""
    now = datetime.datetime.now(datetime.timezone.utc)
    insert_docs(
        db,
        SourceSystem(
            id="SYS_X", name="X", code="X", type="Application", description="x", owning_team="x", environment="NPE"
        )
    )

    for i, (classification_status, exec_status, rca_status_value, started_offset_hours) in enumerate(
        [
            ("manual_triage", "completed", "NeedManualIntervention", -1),  # unidentified
            ("resolved", "failed", None, -1),  # failed (interpreter crash)
            ("resolved", "completed", "IssueCouldNotBeTraced", -1),  # failed (rca_status)
            ("resolved", "completed", "Probable", -1),  # succeeded
            ("resolved", "completed", "Probable", -48),  # outside a 24h window
        ]
    ):
        started = now + datetime.timedelta(hours=started_offset_hours)
        insert_docs(
            db,
            Incident(
                id=f"INC-{i}",
                source="jira",
                external_id=f"TT-{i}",
                raw_text="x",
                jira_key=f"TT-{i}",
                classification_status=classification_status,
                source_system_id="SYS_X",
                category="FUNCTIONAL DEFECT (QA/UAT)",
            )
        )
        insert_docs(
            db,
            WorkflowExecution(
                id=f"EXEC-{i}",
                jira_key=f"TT-{i}",
                incident_id=f"INC-{i}",
                workflow_definition_version_id=None,
                document_snapshot=[],
                evidence=[],
                rca={"matched_pattern": "x", "rca_status": rca_status_value} if rca_status_value else None,
                rca_status=rca_status_value,
                triggered_by="rca",
                status=exec_status,
                started_at=started,
                completed_at=started + datetime.timedelta(seconds=10) if exec_status == "completed" else None,
            )
        )


def test_stats_all_time_buckets(app, sync_db):
    _seed(sync_db)
    with TestClient(app) as client:
        response = client.get("/stats/incidents", params={"period": "all"})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 5
    assert body["unidentified"] == 1
    assert body["failed"] == 2
    assert body["succeeded"] == 2
    assert body["avg_process_seconds"] == pytest.approx(10.0)


def test_stats_period_excludes_older_executions(app, sync_db):
    _seed(sync_db)
    with TestClient(app) as client:
        response = client.get("/stats/incidents", params={"period": "24h"})

    body = response.json()
    assert body["total"] == 4  # the -48h one is excluded


def test_stats_default_period_is_7d(app, sync_db):
    _seed(sync_db)  # all 5 fixtures are within the last 7 days (oldest is -48h)
    with TestClient(app) as client:
        response = client.get("/stats/incidents")

    assert response.json()["total"] == 5
