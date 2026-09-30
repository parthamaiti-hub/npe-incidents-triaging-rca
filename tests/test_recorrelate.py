import asyncio
import datetime
import uuid

import pytest

import app.correlation as correlation_module
from app.models import CorrelationGroup, Incident, SourceSystem
from dataloadscripts.test_fixtures import get_doc, insert_docs, open_db
from scripts.recorrelate import recorrelate

CATEGORY = "FUNCTIONAL DEFECT (QA/UAT)"
BASE_TIME = datetime.datetime(2026, 1, 1, 12, 0, 0)


@pytest.fixture()
def seeded_db(sync_db):
    insert_docs(
        sync_db,
        SourceSystem(
            id="SYS_HSI", name="HSI", code="HSI", type="Application", description="x", owning_team="x", environment="NPE"
        ),
    )
    return sync_db


@pytest.fixture()
def run_recorrelate(mongo_url, mongo_db_name):
    """recorrelate() against this test's database, from sync test code."""

    def run(**kwargs) -> dict:
        async def _run():
            async with open_db(mongo_url, mongo_db_name) as db:
                return await recorrelate(db, **kwargs)

        return asyncio.run(_run())

    return run


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


def test_dry_run_lists_candidates_without_mutating_db(seeded_db, run_recorrelate):
    id1, id2 = str(uuid.uuid4()), str(uuid.uuid4())
    insert_docs(
        seeded_db,
        _make_incident(id1, "TT-D1", BASE_TIME),
        _make_incident(id2, "TT-D2", BASE_TIME + datetime.timedelta(minutes=1)),
    )

    result = run_recorrelate(since=BASE_TIME, dry_run=True)
    assert result["dry_run"] is True
    assert result["candidates"] == 2
    assert set(result["incident_ids"]) == {id1, id2}

    assert get_doc(seeded_db, Incident, id1).correlation_group_id is None
    assert get_doc(seeded_db, Incident, id2).correlation_group_id is None


def test_dry_run_excludes_unclassified_incidents(seeded_db, run_recorrelate):
    """Only fully-classified incidents (resolved/llm_resolved -- both have
    source_system_id and category set) are candidates."""
    resolved_id = str(uuid.uuid4())
    manual_triage_id = str(uuid.uuid4())
    insert_docs(
        seeded_db,
        _make_incident(resolved_id, "TT-R1", BASE_TIME),
        _make_incident(manual_triage_id, "TT-M1", BASE_TIME, classification_status="manual_triage"),
    )

    result = run_recorrelate(since=BASE_TIME, dry_run=True)
    assert result["incident_ids"] == [resolved_id]


def test_recorrelate_forms_a_group_from_candidates(seeded_db, run_recorrelate):
    ids = [str(uuid.uuid4()) for _ in range(3)]
    insert_docs(
        seeded_db,
        *(
            _make_incident(incident_id, f"TT-G{i}", BASE_TIME + datetime.timedelta(minutes=i * 2))
            for i, incident_id in enumerate(ids)
        ),
    )

    result = run_recorrelate(since=BASE_TIME, dry_run=False)
    assert result["candidates"] == 3
    assert result["reset"] == 0
    assert result["incidents_grouped"] == 3
    assert result["groups_touched"] == 1

    group_ids = {get_doc(seeded_db, Incident, i).correlation_group_id for i in ids}
    assert group_ids != {None}
    assert len(group_ids) == 1
    group = get_doc(seeded_db, CorrelationGroup, group_ids.pop())
    assert group.incident_count == 3


def test_recorrelate_respects_since_until_bounds(seeded_db, run_recorrelate):
    id1, id2 = str(uuid.uuid4()), str(uuid.uuid4())
    boundary = BASE_TIME + datetime.timedelta(hours=1)
    insert_docs(
        seeded_db,
        _make_incident(id1, "TT-B1", BASE_TIME),
        _make_incident(id2, "TT-B2", boundary + datetime.timedelta(minutes=1)),
    )

    before = run_recorrelate(since=BASE_TIME, until=boundary, dry_run=True)
    assert before["incident_ids"] == [id1]

    after = run_recorrelate(since=boundary, dry_run=True)
    assert after["incident_ids"] == [id2]


def test_recorrelate_deletes_orphaned_groups_when_new_settings_no_longer_correlate(
    seeded_db, run_recorrelate, monkeypatch
):
    """Simulates a threshold change: two incidents already grouped under the
    old CORRELATION_THRESHOLD no longer meet a stricter one, so replaying
    should reset both and delete the now-empty CorrelationGroup rather than
    leaving a dead document behind."""
    monkeypatch.setattr(correlation_module, "CORRELATION_THRESHOLD", 5)

    id1, id2 = str(uuid.uuid4()), str(uuid.uuid4())
    group_id = str(uuid.uuid4())
    incident1 = _make_incident(id1, "TT-O1", BASE_TIME)
    incident2 = _make_incident(id2, "TT-O2", BASE_TIME + datetime.timedelta(minutes=1))
    incident1.correlation_group_id = group_id
    incident2.correlation_group_id = group_id
    insert_docs(
        seeded_db,
        incident1,
        incident2,
        CorrelationGroup(
            id=group_id,
            source_system_id="SYS_HSI",
            category=CATEGORY,
            opened_at=BASE_TIME,
            last_seen_at=BASE_TIME + datetime.timedelta(minutes=1),
            incident_count=2,
            representative_incident_id=id1,
            status="open",
        ),
    )

    result = run_recorrelate(since=BASE_TIME, dry_run=False)
    assert result["reset"] == 2
    assert result["orphaned_groups_deleted"] == 1
    assert result["incidents_grouped"] == 0  # threshold 5, only 2 candidates -- no group re-forms

    assert get_doc(seeded_db, CorrelationGroup, group_id) is None
    assert get_doc(seeded_db, Incident, id1).correlation_group_id is None
    assert get_doc(seeded_db, Incident, id2).correlation_group_id is None
