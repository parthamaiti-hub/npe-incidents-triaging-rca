from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

import app.poller as poller_module
from app.db import make_sync_mongo_client
from app.events import consume_one, decode, declare_incidents_raw, make_channel, make_connection
from app.idempotency import make_redis
from app.models import Incident
from app.poller import poll_once
from app.worker import process_message
from dataloadscripts.test_fixtures import (
    GRAPH_MESSAGES_PAGE_RESPONSE,
    JIRA_ISSUE_RS_173234,
    find_docs,
    get_doc,
    open_db,
    seeded_catalog_database,
)

REAL_CATALOG_PATH = Path(__file__).resolve().parents[1] / "dataloadscripts" / "npe_real_source_systems.yaml"


@pytest.fixture(scope="module")
def real_catalog_db(mongo_url):
    """(mongo_url, database name) -- shared by the whole module."""
    with seeded_catalog_database(mongo_url, REAL_CATALOG_PATH) as name:
        yield mongo_url, name


@pytest.fixture(scope="module")
def catalog_sync_db(real_catalog_db):
    mongo_url, name = real_catalog_db
    client = make_sync_mongo_client(mongo_url)
    yield client[name]
    client.close()


@respx.mock
async def test_poll_once_upserts_incident_with_real_jira_fields(real_catalog_db, catalog_sync_db, redis_url, rabbitmq_url):
    respx.route(host="localhost").pass_through()  # let real testcontainer calls through unmocked

    respx.get(
        "https://graph.microsoft.com/v1.0/teams/team1/channels/channel1/messages",
        params={"$top": "50"},
    ).mock(return_value=httpx.Response(200, json=GRAPH_MESSAGES_PAGE_RESPONSE))
    respx.get("https://t-mobile.atlassian.net/rest/api/3/search").mock(
        return_value=httpx.Response(200, json={"issues": [JIRA_ISSUE_RS_173234]})
    )

    redis = make_redis(redis_url)
    connection = await make_connection(rabbitmq_url)
    channel = await make_channel(connection)
    await declare_incidents_raw(channel)

    try:
        async with open_db(*real_catalog_db) as db, httpx.AsyncClient() as http_client:
            count = await poll_once(
                db,
                channel,
                redis,
                http_client,
                access_token="fake-token",
                team_id="team1",
                channel_id="channel1",
                since=datetime(2026, 8, 1, tzinfo=timezone.utc),
                jira_site="t-mobile.atlassian.net",
            )
    finally:
        await connection.close()
        await redis.aclose()

    assert count == 1

    [incident] = find_docs(catalog_sync_db, Incident, {"jira_key": "RS-173234"})
    print(
        f"\n[OUTCOME] jira_key={incident.jira_key} status={incident.status} "
        f"priority={incident.priority} issue_type={incident.issue_type} "
        f"assignee={incident.assignee} labels={incident.labels} "
        f"teams_mentions={len(incident.teams_mentions or [])} "
        f"addressed_team_raw={incident.addressed_team_raw} environment_raw={incident.environment_raw}"
    )
    assert incident.source == "jira"
    assert incident.status == "In Progress"
    assert incident.priority == "Urgent/Blocker"
    assert incident.issue_type == "THE Bug"
    assert incident.assignee == "Jvalant Dave"
    assert incident.reporter == "Indrani Akula"
    assert incident.labels == ["FIBER_QE_AUG_QLAB03", "Fiber_Automation"]
    assert len(incident.teams_mentions) == 1
    assert incident.teams_mentions[0]["author"] == "Dongale, Kapil"
    # addressed_team is reporting metadata only, captured
    # here but must not have influenced classification below.
    assert incident.addressed_team_raw == "dpo-dice"
    assert incident.environment_raw == "QLAB03"


@respx.mock
async def test_poll_once_incident_classifies_against_real_catalog_unchanged(
    real_catalog_db, catalog_sync_db, redis_url, rabbitmq_url, opa_url, monkeypatch
):
    """A mocked Graph message -> extracted Jira key -> mocked Jira issue ->
    real Incident fields -> the SAME classification pipeline (extract_signals/OPA/classify_raw_text),
    unmodified, resolves it against the real catalog."""
    import app.opa_client as opa_client_module

    monkeypatch.setattr(opa_client_module, "OPA_URL", opa_url)
    respx.route(host="localhost").pass_through()  # let real testcontainer calls (OPA) through unmocked

    respx.get(
        "https://graph.microsoft.com/v1.0/teams/team1/channels/channel1/messages",
        params={"$top": "50"},
    ).mock(return_value=httpx.Response(200, json=GRAPH_MESSAGES_PAGE_RESPONSE))
    respx.get("https://t-mobile.atlassian.net/rest/api/3/search").mock(
        return_value=httpx.Response(200, json={"issues": [JIRA_ISSUE_RS_173234]})
    )

    redis = make_redis(redis_url)
    connection = await make_connection(rabbitmq_url)
    channel = await make_channel(connection)
    await declare_incidents_raw(channel)

    try:
        async with open_db(*real_catalog_db) as db, httpx.AsyncClient() as http_client:
            await poll_once(
                db,
                channel,
                redis,
                http_client,
                access_token="fake-token",
                team_id="team1",
                channel_id="channel1",
                since=datetime(2026, 8, 1, tzinfo=timezone.utc),
                jira_site="t-mobile.atlassian.net",
            )
    finally:
        await connection.close()
        await redis.aclose()

    [target] = find_docs(catalog_sync_db, Incident, {"jira_key": "RS-173234"})
    target_incident_id = target.id

    # incidents.raw is a plain queue shared across test modules in this
    # session -- other tests' unconsumed messages may precede ours in it.
    # Drain until this test's own incident is found, rather than assuming
    # the very next message is ours.
    classify_connection = await make_connection(rabbitmq_url)
    classify_channel = await make_channel(classify_connection)
    queue = await declare_incidents_raw(classify_channel)
    try:
        async with open_db(*real_catalog_db) as db, queue.iterator() as iterator:
            for _ in range(20):
                message = await consume_one(iterator, timeout=20.0)
                assert message is not None, "expected to find our incident on incidents.raw but ran out of messages"
                payload = decode(message)
                await process_message(db, payload)
                await message.ack()
                if payload["incident_id"] == target_incident_id:
                    break
    finally:
        await classify_connection.close()

    incident = get_doc(catalog_sync_db, Incident, target_incident_id)
    print(
        f"\n[OUTCOME] jira_key={incident.jira_key} classification_status={incident.classification_status} "
        f"source_system_id={incident.source_system_id} category={incident.category!r} "
        f"matched_rule_id={incident.matched_rule_id}"
    )
    assert incident.classification_status == "resolved"
    assert incident.source_system_id == "SYS_FIBER"
    assert incident.category == "FUNCTIONAL DEFECT (QA/UAT)"


class _FakeIncident:
    def __init__(self, id_: str, raw_text: str):
        self.id = id_
        self.raw_text = raw_text


async def test_poll_once_publishes_nothing_when_batch_fails_partway(monkeypatch):
    """upsert_incident_from_jira doesn't publish itself -- poll_once
    collects and publishes only after the whole batch's transaction commits. If
    a later issue in the batch raises, nothing should have been published
    for ANY issue in that batch, including ones upserted successfully before
    the failure -- the old per-issue-inside-the-loop publish would have
    published the first issue regardless of the second one's failure.

    No testcontainers needed: publish_incident_received and every
    Graph/Jira/DB touch point poll_once calls are monkeypatched directly, so
    this is a pure control-flow test of poll_once's collect-then-publish
    ordering."""
    calls = {"upsert": 0}

    async def fake_upsert(db, issue, mentions, session=None):
        calls["upsert"] += 1
        if calls["upsert"] == 1:
            return _FakeIncident("incident-1", "raw text one")
        raise RuntimeError("simulated failure on the second issue in this batch")

    publish_mock = AsyncMock()
    monkeypatch.setattr(poller_module, "get_channel_messages", AsyncMock(return_value=[]))
    monkeypatch.setattr(poller_module, "_collect_jira_mentions", lambda messages: {"AA-1": [], "BB-2": []})
    monkeypatch.setattr(poller_module, "search_issues", AsyncMock(return_value=[{"key": "AA-1"}, {"key": "BB-2"}]))
    monkeypatch.setattr(poller_module, "upsert_incident_from_jira", fake_upsert)
    monkeypatch.setattr(poller_module, "publish_incident_received", publish_mock)

    async def no_transaction(db, fn):
        return await fn(None)

    monkeypatch.setattr(poller_module, "in_transaction", no_transaction)

    with pytest.raises(RuntimeError, match="simulated failure"):
        await poll_once(
            object(),
            channel=object(),
            redis=object(),
            http_client=object(),
            access_token="fake-token",
            since=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    assert calls["upsert"] == 2
    publish_mock.assert_not_called()


async def test_a_failing_batch_writes_no_incidents(sync_db, mongo_url, mongo_db_name, monkeypatch):
    """The batch is one transaction, as it was one commit before: an issue
    that fails after another was already upserted rolls that one back too
    (so nothing is left stranded unpublished)."""
    real_upsert = poller_module.upsert_incident_from_jira
    calls = {"upsert": 0}

    async def failing_second(db, issue, mentions, session=None):
        calls["upsert"] += 1
        if calls["upsert"] == 2:
            raise RuntimeError("simulated failure on the second issue in this batch")
        return await real_upsert(db, issue, mentions, session=session)

    second_issue = {**JIRA_ISSUE_RS_173234, "key": "RS-173235"}
    monkeypatch.setattr(poller_module, "get_channel_messages", AsyncMock(return_value=[]))
    monkeypatch.setattr(poller_module, "_collect_jira_mentions", lambda messages: {"RS-173234": [], "RS-173235": []})
    monkeypatch.setattr(poller_module, "search_issues", AsyncMock(return_value=[JIRA_ISSUE_RS_173234, second_issue]))
    monkeypatch.setattr(poller_module, "upsert_incident_from_jira", failing_second)
    monkeypatch.setattr(poller_module, "publish_incident_received", AsyncMock())

    async with open_db(mongo_url, mongo_db_name) as db:
        with pytest.raises(RuntimeError, match="simulated failure"):
            await poll_once(
                db, channel=object(), redis=object(), http_client=object(), access_token="fake-token",
                since=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )

    assert find_docs(sync_db, Incident) == []
