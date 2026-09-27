import datetime
import uuid

import pytest

import app.correlation as correlation_module
from app.correlation import build_correlation_context, correlate_incident
from app.db import Base, make_async_engine, make_async_session_factory, make_engine, make_session_factory
from app.models import CorrelationGroup, Incident, SourceSystem

CATEGORY = "FUNCTIONAL DEFECT (QA/UAT)"


@pytest.fixture()
def seeded_db(postgres_url):
    engine = make_engine(postgres_url)
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        session.add(
            SourceSystem(
                id="SYS_HSI", name="HSI", code="HSI", type="Application", description="x", owning_team="x", environment="NPE"
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


async def test_third_incident_within_window_forms_a_group_of_three(session):
    incident1 = _incident("TT-1", BASE_TIME)
    session.add(incident1)
    await session.commit()
    assert await correlate_incident(session, incident1) is None  # alone -- no group yet

    incident2 = _incident("TT-2", BASE_TIME + datetime.timedelta(minutes=5))
    session.add(incident2)
    await session.commit()
    group2 = await correlate_incident(session, incident2)
    assert group2 is not None
    assert group2.incident_count == 2

    incident3 = _incident("TT-3", BASE_TIME + datetime.timedelta(minutes=10))
    session.add(incident3)
    await session.commit()
    group3 = await correlate_incident(session, incident3)
    assert group3.id == group2.id  # joined the existing group, not a new one
    assert group3.incident_count == 3

    await session.refresh(incident1)
    await session.refresh(incident2)
    await session.refresh(incident3)
    assert incident1.correlation_group_id == group3.id
    assert incident2.correlation_group_id == group3.id
    assert incident3.correlation_group_id == group3.id
    assert group3.representative_incident_id == incident1.id  # earliest of the three


async def test_different_category_does_not_correlate(session):
    incident1 = _incident("TT-1", BASE_TIME, category=CATEGORY)
    incident2 = _incident("TT-2", BASE_TIME + datetime.timedelta(minutes=5), category="DATA QUALITY / TEST DATA")
    session.add_all([incident1, incident2])
    await session.commit()

    assert await correlate_incident(session, incident1) is None
    assert await correlate_incident(session, incident2) is None


async def test_incidents_outside_the_window_do_not_correlate(session, monkeypatch):
    # Production default is 1440 minutes (24h) -- explicitly narrowed here so this test's 45-minute gap stays
    # "outside the window" regardless of what the production config default
    # is set to.
    monkeypatch.setattr(correlation_module, "CORRELATION_WINDOW_MINUTES", 30)
    incident1 = _incident("TT-1", BASE_TIME)
    incident2 = _incident("TT-2", BASE_TIME + datetime.timedelta(minutes=45))
    session.add_all([incident1, incident2])
    await session.commit()

    assert await correlate_incident(session, incident1) is None
    assert await correlate_incident(session, incident2) is None
    await session.refresh(incident1)
    await session.refresh(incident2)
    assert incident1.correlation_group_id is None
    assert incident2.correlation_group_id is None


async def test_unclassified_incident_never_correlates(session):
    incident = _incident("TT-1", BASE_TIME, source_system_id=None, category=None)
    session.add(incident)
    await session.commit()
    assert await correlate_incident(session, incident) is None


async def test_build_correlation_context_excludes_self_and_finds_representative(session):
    incident1 = _incident("TT-1", BASE_TIME)
    session.add(incident1)
    await session.commit()
    await correlate_incident(session, incident1)

    incident2 = _incident("TT-2", BASE_TIME + datetime.timedelta(minutes=5))
    session.add(incident2)
    await session.commit()
    group = await correlate_incident(session, incident2)

    context = await build_correlation_context(session, group, exclude_incident_id=incident2.id)
    assert context.sibling_jira_keys == ["TT-1"]
    assert context.representative_jira_key == "TT-1"
    assert context.incident_count == 2
    assert context.source_system_id == "SYS_HSI"
