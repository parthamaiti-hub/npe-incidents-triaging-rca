from pathlib import Path

import httpx
import pytest
import respx

from app.db import Base, make_async_engine, make_async_session_factory
from app.e2e_pipeline import run_e2e_for_jira_key
from dataloadscripts.test_fixtures import JIRA_ISSUE_RS_173234, seed_catalog_db

REAL_CATALOG_PATH = Path(__file__).resolve().parents[1] / "dataloadscripts" / "npe_real_source_systems.yaml"

JIRA_ISSUE_IDS = {
    "key": "RS-169040",
    "fields": {
        "summary": "HSI NRF Ph#1 : QE Test Data not Present in IDS Source Views for Collections Process",
        "status": {"name": "In Progress"},
        "priority": {"name": "Low"},
        "issuetype": {"name": "THE Bug"},
        "reporter": {"displayName": "Venkata Subba Rao Pothuri"},
        "assignee": {"displayName": "Rajesh Kumar Jampala"},
        "labels": [],
        "resolution": None,
        "created": "2026-08-06T04:31:00.000-0700",
        "updated": "2026-08-21T14:49:00.000-0700",
    },
    "renderedFields": {
        "description": (
            "<p>As Part of E2E testing QE team Send few DO events to IDS, but for the 08/05/2026 "
            "Date provided events are not processed to IDS Views. "
            "CDW_IDW_DB_QAT.IDW_CORE_V.DIGITAL_SALES_ORDER and "
            "CDW_IDW_DB_QAT.IDW_CORE_V.DIGITAL_SALES_ORDER_LINE. Due to Data not available in the "
            "above Views, Collections team unable process the data.</p>"
        )
    },
}


@pytest.fixture(scope="module")
def real_catalog_db(postgres_url):
    engine = seed_catalog_db(postgres_url, catalog_path=REAL_CATALOG_PATH)
    yield postgres_url
    Base.metadata.drop_all(engine)
    engine.dispose()


@respx.mock
async def test_e2e_fiber_ticket_produces_functional_defect_rca(real_catalog_db, opa_url, monkeypatch):
    import app.opa_client as opa_client_module

    monkeypatch.setattr(opa_client_module, "OPA_URL", opa_url)
    respx.route(host="localhost").pass_through()
    respx.get("https://npetriage.atlassian.net/rest/api/3/issue/TT-9001").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_RS_173234)
    )

    engine = make_async_engine(real_catalog_db)
    session_factory = make_async_session_factory(engine)
    try:
        async with session_factory() as session, httpx.AsyncClient() as client:
            result = await run_e2e_for_jira_key(session, client, "TT-9001", jira_site="npetriage.atlassian.net")
    finally:
        await engine.dispose()

    print(f"\n[OUTCOME] {result}")

    assert result["classification"]["status"] == "resolved"
    assert result["classification"]["source_system_id"] == "SYS_FIBER"
    assert result["classification"]["category"] == "FUNCTIONAL DEFECT (QA/UAT)"

    assert result["evidence"] is not None
    checks_run = [e["check"] for e in result["evidence"]]
    assert checks_run == ["error_logs", "recent_deployments", "apm_traces", "dependent_services_health"]

    rca = result["rca"]
    assert rca["matched_pattern"] == "functional_defect_recent_release"
    assert "STUBBED" in rca["root_cause_summary"]  # honest about simulated evidence


@respx.mock
async def test_e2e_ids_ticket_produces_data_quality_rca(real_catalog_db, opa_url, monkeypatch):
    import app.opa_client as opa_client_module

    monkeypatch.setattr(opa_client_module, "OPA_URL", opa_url)
    respx.route(host="localhost").pass_through()
    respx.get("https://npetriage.atlassian.net/rest/api/3/issue/TT-9002").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_IDS)
    )

    engine = make_async_engine(real_catalog_db)
    session_factory = make_async_session_factory(engine)
    try:
        async with session_factory() as session, httpx.AsyncClient() as client:
            result = await run_e2e_for_jira_key(session, client, "TT-9002", jira_site="npetriage.atlassian.net")
    finally:
        await engine.dispose()

    print(f"\n[OUTCOME] {result}")

    assert result["classification"]["source_system_id"] == "SYS_IDS"
    assert result["classification"]["category"] == "DATA QUALITY / TEST DATA"

    rca = result["rca"]
    assert rca["matched_pattern"] == "data_quality_pipeline_change"


@respx.mock
async def test_e2e_reports_no_playbook_when_none_configured(real_catalog_db, opa_url, monkeypatch):
    """Most real systems have no playbook yet -- confirms this is reported
    honestly, not silently faked."""
    import app.opa_client as opa_client_module

    monkeypatch.setattr(opa_client_module, "OPA_URL", opa_url)
    respx.route(host="localhost").pass_through()

    no_playbook_issue = {
        "key": "TT-9003",
        "fields": {
            "summary": "RSP is not returning ban status in response",
            "status": {"name": "To Do"},
            "priority": {"name": "High"},
            "issuetype": {"name": "Bug"},
            "reporter": {"displayName": "x"},
            "assignee": None,
            "labels": [],
            "resolution": None,
            "created": "2026-08-22T00:00:00.000-0700",
            "updated": "2026-08-22T00:00:00.000-0700",
        },
        "renderedFields": {"description": "<p>RSP is failing.</p>"},
    }
    respx.get("https://npetriage.atlassian.net/rest/api/3/issue/TT-9003").mock(
        return_value=httpx.Response(200, json=no_playbook_issue)
    )

    engine = make_async_engine(real_catalog_db)
    session_factory = make_async_session_factory(engine)
    try:
        async with session_factory() as session, httpx.AsyncClient() as client:
            result = await run_e2e_for_jira_key(session, client, "TT-9003", jira_site="npetriage.atlassian.net")
    finally:
        await engine.dispose()

    print(f"\n[OUTCOME] {result}")

    assert result["classification"]["source_system_id"] == "SYS_RSP"
    assert result["evidence"] is None
    assert result["rca"]["matched_pattern"] == "no_playbook"
    assert "SYS_RSP" in result["rca"]["root_cause_summary"]


@respx.mock
async def test_e2e_post_rca_comment_posts_adf_body_to_the_issue(real_catalog_db, opa_url, monkeypatch):
    import app.opa_client as opa_client_module

    monkeypatch.setattr(opa_client_module, "OPA_URL", opa_url)
    respx.route(host="localhost").pass_through()
    respx.get("https://npetriage.atlassian.net/rest/api/3/issue/TT-9004").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_RS_173234)
    )
    comment_route = respx.post("https://npetriage.atlassian.net/rest/api/3/issue/TT-9004/comment").mock(
        return_value=httpx.Response(201, json={"id": "99001"})
    )

    engine = make_async_engine(real_catalog_db)
    session_factory = make_async_session_factory(engine)
    try:
        async with session_factory() as session, httpx.AsyncClient() as client:
            result = await run_e2e_for_jira_key(
                session, client, "TT-9004", jira_site="npetriage.atlassian.net", post_rca_comment=True
            )
    finally:
        await engine.dispose()

    print(f"\n[OUTCOME] {result}")

    assert result["comment_id"] == "99001"
    assert comment_route.called
    sent_body = comment_route.calls.last.request.content
    # functional_defect_recent_release -> rca_status.PROBABLE -> "warning" panel
    assert b'"panelType":"warning"' in sent_body or b'"panelType": "warning"' in sent_body
