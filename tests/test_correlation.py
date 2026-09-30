import asyncio
import datetime
import uuid

import pytest

import app.correlation as correlation_module
from app.correlation import build_correlation_context, correlate_incident
from app.models import CorrelationGroup, Incident, SourceSystem
from app.repositories.base import get
from app.repositories.incidents import insert_incident
from dataloadscripts.test_fixtures import insert_docs, open_db

CATEGORY = "FUNCTIONAL DEFECT (QA/UAT)"


@pytest.fixture()
async def db(sync_db, mongo_url, mongo_db_name):
    insert_docs(
        sync_db,
        SourceSystem(
            id="SYS_HSI", name="HSI", code="HSI", type="Application", description="x", owning_team="x", environment="NPE"
        ),
    )
    async with open_db(mongo_url, mongo_db_name) as handle:
        yield handle


def _incident(jira_key: str, received_at: datetime.datetime, source_system_id="SYS_HSI", category=CATEGORY) -> Incident:
    return Incident(
        id=str(uuid.uuid4()),
        source="jira",
        external_id=jira_key,
        raw_text="x",
        jira_key=jira_key,
        classification_status="resolved" if source_system_id else "manual_triage",
        source_system_id=source_system_id,
        category=category,
        received_at=received_at,
    )


BASE_TIME = datetime.datetime(2026, 1, 1, 12, 0, 0)


async def test_third_incident_within_window_forms_a_group_of_three(db):
    incident1 = _incident("TT-1", BASE_TIME)
    await insert_incident(db, incident1)
    assert await correlate_incident(db, incident1) is None  # alone -- no group yet

    incident2 = _incident("TT-2", BASE_TIME + datetime.timedelta(minutes=5))
    await insert_incident(db, incident2)
    group2 = await correlate_incident(db, incident2)
    assert group2 is not None
    assert group2.incident_count == 2

    incident3 = _incident("TT-3", BASE_TIME + datetime.timedelta(minutes=10))
    await insert_incident(db, incident3)
    group3 = await correlate_incident(db, incident3)
    assert group3.id == group2.id  # joined the existing group, not a new one
    assert group3.incident_count == 3

    incident1 = await get(db, Incident, incident1.id)
    incident2 = await get(db, Incident, incident2.id)
    incident3 = await get(db, Incident, incident3.id)
    assert incident1.correlation_group_id == group3.id
    assert incident2.correlation_group_id == group3.id
    assert incident3.correlation_group_id == group3.id
    assert group3.representative_incident_id == incident1.id  # earliest of the three


async def test_different_category_does_not_correlate(db):
    incident1 = _incident("TT-1", BASE_TIME, category=CATEGORY)
    incident2 = _incident("TT-2", BASE_TIME + datetime.timedelta(minutes=5), category="DATA QUALITY / TEST DATA")
    await insert_incident(db, incident1)
    await insert_incident(db, incident2)

    assert await correlate_incident(db, incident1) is None
    assert await correlate_incident(db, incident2) is None


async def test_incidents_outside_the_window_do_not_correlate(db, monkeypatch):
    # Production default is 1440 minutes (24h) -- explicitly narrowed here so this test's 45-minute gap stays
    # "outside the window" regardless of what the production config default
    # is set to.
    monkeypatch.setattr(correlation_module, "CORRELATION_WINDOW_MINUTES", 30)
    incident1 = _incident("TT-1", BASE_TIME)
    incident2 = _incident("TT-2", BASE_TIME + datetime.timedelta(minutes=45))
    await insert_incident(db, incident1)
    await insert_incident(db, incident2)

    assert await correlate_incident(db, incident1) is None
    assert await correlate_incident(db, incident2) is None
    incident1 = await get(db, Incident, incident1.id)
    incident2 = await get(db, Incident, incident2.id)
    assert incident1.correlation_group_id is None
    assert incident2.correlation_group_id is None


async def test_unclassified_incident_never_correlates(db):
    incident = _incident("TT-1", BASE_TIME, source_system_id=None, category=None)
    await insert_incident(db, incident)
    assert await correlate_incident(db, incident) is None


async def test_build_correlation_context_excludes_self_and_finds_representative(db):
    incident1 = _incident("TT-1", BASE_TIME)
    await insert_incident(db, incident1)
    await correlate_incident(db, incident1)

    incident2 = _incident("TT-2", BASE_TIME + datetime.timedelta(minutes=5))
    await insert_incident(db, incident2)
    group = await correlate_incident(db, incident2)

    context = await build_correlation_context(db, group, exclude_incident_id=incident2.id)
    assert context.sibling_jira_keys == ["TT-1"]
    assert context.representative_jira_key == "TT-1"
    assert context.incident_count == 2
    assert context.source_system_id == "SYS_HSI"


async def test_concurrent_joins_never_lose_a_count(db):
    """incident_count moves with $inc, not read-modify-write: incidents
    joining an existing group concurrently each count exactly once."""
    first = _incident("TT-1", BASE_TIME)
    second = _incident("TT-2", BASE_TIME + datetime.timedelta(minutes=1))
    await insert_incident(db, first)
    await insert_incident(db, second)
    group = await correlate_incident(db, second)
    assert group.incident_count == 2

    late = [_incident(f"TT-{n}", BASE_TIME + datetime.timedelta(minutes=n)) for n in range(3, 9)]
    for incident in late:
        await insert_incident(db, incident)
    await asyncio.gather(*(correlate_incident(db, incident) for incident in late))

    stored = await get(db, CorrelationGroup, group.id)
    assert stored.incident_count == 8
    assert stored.last_seen_at == BASE_TIME + datetime.timedelta(minutes=8)
