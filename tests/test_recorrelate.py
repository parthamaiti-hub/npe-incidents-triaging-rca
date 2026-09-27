import asyncio
import datetime
import uuid

import pytest

import app.correlation as correlation_module
from app.db import Base, make_async_engine, make_async_session_factory, make_engine, make_session_factory
from app.models import CorrelationGroup, Incident, SourceSystem
from scripts.recorrelate import recorrelate

CATEGORY = "FUNCTIONAL DEFECT (QA/UAT)"
BASE_TIME = datetime.datetime(2026, 1, 1, 12, 0, 0)


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


def _make_incident(
    incident_id: str, external_id: str, received_at: datetime.datetime, classification_status: str = "resolved"
) -> Incident:
    return Incident(
        id=incident_id,
        source="jira",
        external_id=external_id,
        raw_text="x",
        jira_key=external_id,
        classification_status=classification_status,
        source_system_id="SYS_HSI" if classification_status in ("resolved", "llm_resolved") else None,
        category=CATEGORY if classification_status in ("resolved", "llm_resolved") else None,
        received_at=received_at,
    )


def test_dry_run_lists_candidates_without_mutating_db(seeded_db):
    async def _run():
        engine = make_async_engine(seeded_db)
        session_factory = make_async_session_factory(engine)
        try:
            since = BASE_TIME
            id1, id2 = str(uuid.uuid4()), str(uuid.uuid4())
            async with session_factory() as session:
                session.add(_make_incident(id1, "TT-D1", BASE_TIME))
                session.add(_make_incident(id2, "TT-D2", BASE_TIME + datetime.timedelta(minutes=1)))
                await session.commit()

            result = await recorrelate(session_factory, since=since, dry_run=True)
            assert result["dry_run"] is True
            assert result["candidates"] == 2
            assert set(result["incident_ids"]) == {id1, id2}

            async with session_factory() as session:
                fetched1 = await session.get(Incident, id1)
                fetched2 = await session.get(Incident, id2)
                assert fetched1.correlation_group_id is None
                assert fetched2.correlation_group_id is None
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_dry_run_excludes_unclassified_incidents(seeded_db):
    """Only fully-classified incidents (resolved/llm_resolved -- both have
    source_system_id and category set) are candidates."""

    async def _run():
        engine = make_async_engine(seeded_db)
        session_factory = make_async_session_factory(engine)
        try:
            resolved_id = str(uuid.uuid4())
            manual_triage_id = str(uuid.uuid4())
            async with session_factory() as session:
                session.add(_make_incident(resolved_id, "TT-R1", BASE_TIME))
                session.add(_make_incident(manual_triage_id, "TT-M1", BASE_TIME, classification_status="manual_triage"))
                await session.commit()

            result = await recorrelate(session_factory, since=BASE_TIME, dry_run=True)
            assert result["incident_ids"] == [resolved_id]
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_recorrelate_forms_a_group_from_candidates(seeded_db):
    async def _run():
        engine = make_async_engine(seeded_db)
        session_factory = make_async_session_factory(engine)
        try:
            since = BASE_TIME
            ids = [str(uuid.uuid4()) for _ in range(3)]
            async with session_factory() as session:
                for i, incident_id in enumerate(ids):
                    session.add(_make_incident(incident_id, f"TT-G{i}", BASE_TIME + datetime.timedelta(minutes=i * 2)))
                await session.commit()

            result = await recorrelate(session_factory, since=since, dry_run=False)
            assert result["candidates"] == 3
            assert result["reset"] == 0
            assert result["incidents_grouped"] == 3
            assert result["groups_touched"] == 1

            async with session_factory() as session:
                fetched = [await session.get(Incident, i) for i in ids]
                group_ids = {f.correlation_group_id for f in fetched}
                assert group_ids != {None}
                assert len(group_ids) == 1
                group = await session.get(CorrelationGroup, group_ids.pop())
                assert group.incident_count == 3
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_recorrelate_respects_since_until_bounds(seeded_db):
    async def _run():
        engine = make_async_engine(seeded_db)
        session_factory = make_async_session_factory(engine)
        try:
            id1, id2 = str(uuid.uuid4()), str(uuid.uuid4())
            boundary = BASE_TIME + datetime.timedelta(hours=1)
            async with session_factory() as session:
                session.add(_make_incident(id1, "TT-B1", BASE_TIME))
                session.add(_make_incident(id2, "TT-B2", boundary + datetime.timedelta(minutes=1)))
                await session.commit()

            before = await recorrelate(session_factory, since=BASE_TIME, until=boundary, dry_run=True)
            assert before["incident_ids"] == [id1]

            after = await recorrelate(session_factory, since=boundary, dry_run=True)
            assert after["incident_ids"] == [id2]
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_recorrelate_deletes_orphaned_groups_when_new_settings_no_longer_correlate(seeded_db, monkeypatch):
    """Simulates a threshold change: two incidents already grouped under the
    old CORRELATION_THRESHOLD no longer meet a stricter one, so replaying
    should reset both and delete the now-empty CorrelationGroup rather than
    leaving a dead row behind."""
    monkeypatch.setattr(correlation_module, "CORRELATION_THRESHOLD", 5)

    async def _run():
        engine = make_async_engine(seeded_db)
        session_factory = make_async_session_factory(engine)
        try:
            since = BASE_TIME
            id1, id2 = str(uuid.uuid4()), str(uuid.uuid4())
            group_id = str(uuid.uuid4())
            async with session_factory() as session:
                incident1 = _make_incident(id1, "TT-O1", BASE_TIME)
                incident2 = _make_incident(id2, "TT-O2", BASE_TIME + datetime.timedelta(minutes=1))
                session.add(incident1)
                session.add(incident2)
                await session.flush()
                session.add(
                    CorrelationGroup(
                        id=group_id,
                        source_system_id="SYS_HSI",
                        category=CATEGORY,
                        opened_at=BASE_TIME,
                        last_seen_at=BASE_TIME + datetime.timedelta(minutes=1),
                        incident_count=2,
                        representative_incident_id=id1,
                        status="open",
                    )
                )
                incident1.correlation_group_id = group_id
                incident2.correlation_group_id = group_id
                await session.commit()

            result = await recorrelate(session_factory, since=since, dry_run=False)
            assert result["reset"] == 2
            assert result["orphaned_groups_deleted"] == 1
            assert result["incidents_grouped"] == 0  # threshold 5, only 2 candidates -- no group re-forms

            async with session_factory() as session:
                assert await session.get(CorrelationGroup, group_id) is None
                fetched1 = await session.get(Incident, id1)
                fetched2 = await session.get(Incident, id2)
                assert fetched1.correlation_group_id is None
                assert fetched2.correlation_group_id is None
        finally:
            await engine.dispose()

    asyncio.run(_run())
