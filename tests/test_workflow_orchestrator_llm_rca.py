import datetime
import uuid

import pytest
from redis.asyncio import Redis

import app.workflow_orchestrator as workflow_orchestrator_module
from app.config import RCA_PENDING_STREAM, RCA_WORKER_CONSUMER_GROUP
from app.correlation import correlate_incident
from app.db import Base, make_async_engine, make_async_session_factory, make_engine, make_session_factory
from app.models import Incident, SourceSystem
from app.rca_worker import ensure_consumer_group
from app.workflow_orchestrator import approve_build_request, build_workflow_from_request, execute_and_record

CATEGORY = "FUNCTIONAL DEFECT (QA/UAT)"
BASE_TIME = datetime.datetime(2026, 1, 1, 12, 0, 0)

VALID_FUNCTIONS = [
    {"call": "error_logs", "with": {"env": "NPE", "app": "TESTAPP", "lookback_minutes": 240}},
]


@pytest.fixture()
def seeded_db(postgres_url):
    engine = make_engine(postgres_url)
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        session.add(
            SourceSystem(
                id="SYS_TESTAPP", name="Test App", code="TESTAPP", type="Application", description="x",
                owning_team="x", environment="NPE",
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


async def _approved_version(session):
    request = await build_workflow_from_request(session, "SYS_TESTAPP", CATEGORY, VALID_FUNCTIONS, "u1")
    return await approve_build_request(session, request.id, "u1")


async def test_llm_disabled_stays_synchronous_and_deterministic(session, monkeypatch):
    monkeypatch.setattr(workflow_orchestrator_module, "RCA_SYNTHESIS_LLM_ENABLED", False)
    version = await _approved_version(session)
    execution = await execute_and_record(session, version, jira_key="TT-1")
    assert execution.rca is not None
    assert execution.rca_status is not None


async def test_llm_enabled_not_correlated_publishes_and_leaves_rca_pending(session, monkeypatch, redis_url):
    monkeypatch.setattr(workflow_orchestrator_module, "RCA_SYNTHESIS_LLM_ENABLED", True)
    version = await _approved_version(session)
    redis = Redis.from_url(redis_url, decode_responses=True)
    try:
        await ensure_consumer_group(redis)
        execution = await execute_and_record(
            session, version, jira_key="TT-1", operator_context="known related change", redis_client=redis
        )
        assert execution.rca is None
        assert execution.rca_status is None
        assert execution.operator_context == "known related change"

        response = await redis.xreadgroup(
            RCA_WORKER_CONSUMER_GROUP, f"test-{uuid.uuid4()}", {RCA_PENDING_STREAM: ">"}, count=1, block=5000
        )
        assert response
        _stream, entries = response[0]
        entry_id, fields = entries[0]
        assert fields["execution_id"] == execution.id
        assert fields["operator_context"] == "known related change"
        await redis.xack(RCA_PENDING_STREAM, RCA_WORKER_CONSUMER_GROUP, entry_id)
    finally:
        await redis.aclose()


async def test_correlated_execution_stays_deterministic_even_with_llm_enabled(session, monkeypatch, redis_url):
    monkeypatch.setattr(workflow_orchestrator_module, "RCA_SYNTHESIS_LLM_ENABLED", True)
    version = await _approved_version(session)

    incident1 = Incident(
        id=str(uuid.uuid4()), source="jira", external_id="TT-1", raw_text="x", jira_key="TT-1",
        classification_status="resolved", source_system_id="SYS_TESTAPP", category=CATEGORY, received_at=BASE_TIME,
    )
    incident2 = Incident(
        id=str(uuid.uuid4()), source="jira", external_id="TT-2", raw_text="x", jira_key="TT-2",
        classification_status="resolved", source_system_id="SYS_TESTAPP", category=CATEGORY,
        received_at=BASE_TIME + datetime.timedelta(minutes=5),
    )
    session.add_all([incident1, incident2])
    await session.commit()
    await correlate_incident(session, incident1)  # forms nothing alone (below threshold)

    redis = Redis.from_url(redis_url, decode_responses=True)
    try:
        await ensure_consumer_group(redis)
        # incident2's own execution triggers correlate_incident() again inside
        # execute_and_record, forming the group with incident1 -> CORRELATED,
        # fully deterministic, no LLM/stream involvement despite the flag.
        execution = await execute_and_record(
            session, version, jira_key="TT-2", incident_id=incident2.id, redis_client=redis
        )
        assert execution.rca is not None
        assert execution.rca["matched_pattern"] == "correlated_systemic_issue"
        assert execution.rca_status == "Correlated"

        response = await redis.xreadgroup(
            RCA_WORKER_CONSUMER_GROUP, f"test-{uuid.uuid4()}", {RCA_PENDING_STREAM: ">"}, count=1, block=2000
        )
        assert not response  # nothing published for a correlated execution
    finally:
        await redis.aclose()
