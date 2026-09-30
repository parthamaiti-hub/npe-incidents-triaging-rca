import pytest

from app.models import SourceSystem, WorkflowBuildRequest, WorkflowDefinitionVersion
from app.repositories.base import find, get
from app.workflow_orchestrator import (
    WorkflowValidationError,
    approve_build_request,
    build_workflow_from_request,
    execute_and_record,
    get_active_workflow,
    reject_build_request,
)
from dataloadscripts.test_fixtures import insert_docs, open_db

CATEGORY = "FUNCTIONAL DEFECT (QA/UAT)"

VALID_FUNCTIONS = [
    {"call": "error_logs", "with": {"env": "NPE", "app": "TESTAPP", "lookback_minutes": 240}},
    {"call": "recent_deployments", "with": {"env": "NPE", "app": "TESTAPP", "lookback_minutes": 1440}},
]


@pytest.fixture()
async def db(sync_db, mongo_url, mongo_db_name):
    insert_docs(
        sync_db,
        SourceSystem(
            id="SYS_TESTAPP",
            name="Test App",
            code="TESTAPP",
            type="Application",
            description="x",
            owning_team="x",
            environment="NPE",
        ),
    )
    async with open_db(mongo_url, mongo_db_name) as handle:
        yield handle


async def test_build_then_approve_creates_an_executable_version(db):
    request = await build_workflow_from_request(
        db, "SYS_TESTAPP", CATEGORY, VALID_FUNCTIONS, requested_by="neelburu@outlook.com"
    )
    assert request.status == "rendered"
    assert [t["call"] for t in request.generated_document] == ["error_logs", "recent_deployments"]

    assert await get_active_workflow(db, "SYS_TESTAPP", CATEGORY) is None  # not approved yet

    version = await approve_build_request(db, request.id, approved_by="neelburu@outlook.com")
    assert version.status == "approved"
    assert version.source == "dynamic_generated"
    assert version.build_request_id == request.id
    assert version.approved_by == "neelburu@outlook.com"
    assert version.approved_at is not None

    active = await get_active_workflow(db, "SYS_TESTAPP", CATEGORY)
    assert active.id == version.id


async def test_approving_a_second_request_supersedes_the_first(db):
    request1 = await build_workflow_from_request(db, "SYS_TESTAPP", CATEGORY, VALID_FUNCTIONS, "u1")
    version1 = await approve_build_request(db, request1.id, "u1")

    request2 = await build_workflow_from_request(db, "SYS_TESTAPP", CATEGORY, VALID_FUNCTIONS[:1], "u2")
    version2 = await approve_build_request(db, request2.id, "u2")

    version1 = await get(db, WorkflowDefinitionVersion, version1.id)
    assert version1.status == "superseded"
    assert version2.status == "approved"
    assert version2.version_number == version1.version_number + 1

    active = await get_active_workflow(db, "SYS_TESTAPP", CATEGORY)
    assert active.id == version2.id


async def test_reject_build_request_never_becomes_executable(db):
    request = await build_workflow_from_request(db, "SYS_TESTAPP", CATEGORY, VALID_FUNCTIONS, "u1")
    await reject_build_request(db, request.id, rejected_by="u1", reason="wrong functions")

    refreshed = await get(db, WorkflowBuildRequest, request.id)
    assert refreshed.status == "rejected"
    assert await get_active_workflow(db, "SYS_TESTAPP", CATEGORY) is None


async def test_unknown_function_rejected_before_anything_is_stored(db):
    with pytest.raises(WorkflowValidationError, match="Unknown function"):
        await build_workflow_from_request(
            db, "SYS_TESTAPP", CATEGORY, [{"call": "not_a_real_function", "with": {}}], "u1"
        )
    assert await find(db, WorkflowBuildRequest) == []


async def test_missing_required_param_rejected_before_anything_is_stored(db):
    with pytest.raises(WorkflowValidationError, match="missing required params"):
        await build_workflow_from_request(
            db, "SYS_TESTAPP", CATEGORY, [{"call": "error_logs", "with": {"env": "NPE"}}], "u1"
        )


async def test_execute_and_record_persists_a_reproducible_snapshot(db):
    request = await build_workflow_from_request(db, "SYS_TESTAPP", CATEGORY, VALID_FUNCTIONS, "u1")
    version = await approve_build_request(db, request.id, "u1")

    execution = await execute_and_record(db, version, jira_key="TT-1")

    assert execution.status == "completed"
    assert execution.jira_key == "TT-1"
    assert execution.document_snapshot == version.document
    assert [e["check"] for e in execution.evidence] == ["error_logs", "recent_deployments"]
    assert all(e["status"] in ("OK", "WARN", "ERROR") for e in execution.evidence)

    fetched = await get(db, WorkflowDefinitionVersion, version.id)
    assert fetched.id == version.id

    stored = await get(db, type(execution), execution.id)
    assert stored == execution  # what the caller got back is exactly what was persisted


async def test_concurrent_approvals_leave_exactly_one_approved_version(db):
    """The partial unique index makes "one approved version per definition"
    a DB invariant: racing approvals can't both win."""
    import asyncio

    requests = [await build_workflow_from_request(db, "SYS_TESTAPP", CATEGORY, VALID_FUNCTIONS, f"u{i}") for i in range(4)]
    results = await asyncio.gather(*(approve_build_request(db, r.id, "approver") for r in requests), return_exceptions=True)

    assert any(isinstance(r, WorkflowDefinitionVersion) for r in results)
    approved = await find(db, WorkflowDefinitionVersion, {"status": "approved"})
    assert len(approved) == 1
