import pytest
from sqlalchemy import select

from app.db import Base, make_async_engine, make_async_session_factory, make_engine, make_session_factory
from app.models import SourceSystem, WorkflowBuildRequest, WorkflowDefinitionVersion
from app.workflow_orchestrator import (
    WorkflowValidationError,
    approve_build_request,
    build_workflow_from_request,
    execute_and_record,
    get_active_workflow,
    reject_build_request,
)

CATEGORY = "FUNCTIONAL DEFECT (QA/UAT)"

VALID_FUNCTIONS = [
    {"call": "error_logs", "with": {"env": "NPE", "app": "TESTAPP", "lookback_minutes": 240}},
    {"call": "recent_deployments", "with": {"env": "NPE", "app": "TESTAPP", "lookback_minutes": 1440}},
]


@pytest.fixture()
def seeded_db(postgres_url):
    engine = make_engine(postgres_url)
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        session.add(
            SourceSystem(
                id="SYS_TESTAPP",
                name="Test App",
                code="TESTAPP",
                type="Application",
                description="x",
                owning_team="x",
                environment="NPE",
            )
        )
        session.commit()
    yield postgres_url
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
async def session(seeded_db):
    engine = make_async_engine(seeded_db)
    session_factory = make_async_session_factory(engine)
    async with session_factory() as s:
        yield s
    await engine.dispose()


async def test_build_then_approve_creates_an_executable_version(session):
    request = await build_workflow_from_request(
        session, "SYS_TESTAPP", CATEGORY, VALID_FUNCTIONS, requested_by="neelburu@outlook.com"
    )
    assert request.status == "rendered"
    assert [t["call"] for t in request.generated_document] == ["error_logs", "recent_deployments"]

    assert await get_active_workflow(session, "SYS_TESTAPP", CATEGORY) is None  # not approved yet

    version = await approve_build_request(session, request.id, approved_by="neelburu@outlook.com")
    assert version.status == "approved"
    assert version.source == "dynamic_generated"
    assert version.build_request_id == request.id
    assert version.approved_by == "neelburu@outlook.com"
    assert version.approved_at is not None

    active = await get_active_workflow(session, "SYS_TESTAPP", CATEGORY)
    assert active.id == version.id


async def test_approving_a_second_request_supersedes_the_first(session):
    request1 = await build_workflow_from_request(session, "SYS_TESTAPP", CATEGORY, VALID_FUNCTIONS, "u1")
    version1 = await approve_build_request(session, request1.id, "u1")

    request2 = await build_workflow_from_request(session, "SYS_TESTAPP", CATEGORY, VALID_FUNCTIONS[:1], "u2")
    version2 = await approve_build_request(session, request2.id, "u2")

    await session.refresh(version1)
    assert version1.status == "superseded"
    assert version2.status == "approved"
    assert version2.version_number == version1.version_number + 1

    active = await get_active_workflow(session, "SYS_TESTAPP", CATEGORY)
    assert active.id == version2.id


async def test_reject_build_request_never_becomes_executable(session):
    request = await build_workflow_from_request(session, "SYS_TESTAPP", CATEGORY, VALID_FUNCTIONS, "u1")
    await reject_build_request(session, request.id, rejected_by="u1", reason="wrong functions")

    refreshed = await session.get(WorkflowBuildRequest, request.id)
    assert refreshed.status == "rejected"
    assert await get_active_workflow(session, "SYS_TESTAPP", CATEGORY) is None


async def test_unknown_function_rejected_before_anything_is_stored(session):
    with pytest.raises(WorkflowValidationError, match="Unknown function"):
        await build_workflow_from_request(
            session, "SYS_TESTAPP", CATEGORY, [{"call": "not_a_real_function", "with": {}}], "u1"
        )
    count = (await session.scalars(select(WorkflowBuildRequest))).all()
    assert count == []


async def test_missing_required_param_rejected_before_anything_is_stored(session):
    with pytest.raises(WorkflowValidationError, match="missing required params"):
        await build_workflow_from_request(
            session, "SYS_TESTAPP", CATEGORY, [{"call": "error_logs", "with": {"env": "NPE"}}], "u1"
        )


async def test_execute_and_record_persists_a_reproducible_snapshot(session):
    request = await build_workflow_from_request(session, "SYS_TESTAPP", CATEGORY, VALID_FUNCTIONS, "u1")
    version = await approve_build_request(session, request.id, "u1")

    execution = await execute_and_record(session, version, jira_key="TT-1")

    assert execution.status == "completed"
    assert execution.jira_key == "TT-1"
    assert execution.document_snapshot == version.document
    assert [e["check"] for e in execution.evidence] == ["error_logs", "recent_deployments"]
    assert all(e["status"] in ("OK", "WARN", "ERROR") for e in execution.evidence)

    fetched = await session.scalars(
        select(WorkflowDefinitionVersion).where(WorkflowDefinitionVersion.id == version.id)
    )
    assert fetched.one().id == version.id
