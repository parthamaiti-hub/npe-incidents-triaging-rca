import asyncio
import re

import pytest
from fastapi.testclient import TestClient

from app.db import make_sync_mongo_client
from app.events import consume_one, decode, declare_incidents_raw, make_channel, make_connection
from app.idempotency import make_dedup_key, make_redis
from app.incident_parser import parse_template
from app.main import create_app
from app.models import Incident, WorkflowExecution
from app.worker import process_message
from dataloadscripts.test_fixtures import (
    DUPLICATE_TEST_TEXT,
    NO_FOOTPRINT_TEXT,
    WORKED_TRACE_TEXT,
    find_docs,
    get_doc,
    open_db,
    seeded_catalog_database,
)


@pytest.fixture(scope="module")
def loaded_catalog_db(mongo_url):
    """(mongo_url, database name) -- shared by the whole module."""
    with seeded_catalog_database(mongo_url) as name:
        yield mongo_url, name


@pytest.fixture(scope="module")
def catalog_sync_db(loaded_catalog_db):
    mongo_url, name = loaded_catalog_db
    client = make_sync_mongo_client(mongo_url)
    yield client[name]
    client.close()


@pytest.fixture()
def app(loaded_catalog_db, redis_url, rabbitmq_url, opa_url, vector_store, monkeypatch):
    import app.opa_client as opa_client_module

    monkeypatch.setattr(opa_client_module, "OPA_URL", opa_url)
    mongo_url, name = loaded_catalog_db
    return create_app(
        mongodb_url=mongo_url,
        mongodb_db=name,
        redis_url=redis_url,
        rabbitmq_url=rabbitmq_url,
        vector_store=vector_store,
    )


def _classify_next_message(loaded_catalog_db, rabbitmq_url) -> dict:
    """incidents.raw is a plain RabbitMQ queue, so a fresh connection each
    call still correctly gets the *next* unacked message in publish order
    (FIFO per queue) -- no shared consumer group required."""

    async def _run() -> dict:
        connection = await make_connection(rabbitmq_url)
        channel = await make_channel(connection)
        queue = await declare_incidents_raw(channel)
        try:
            async with open_db(*loaded_catalog_db) as db, queue.iterator() as iterator:
                message = await consume_one(iterator, timeout=20.0)
                assert message is not None, "expected a message on incidents.raw but none arrived"
                payload = decode(message)
                await process_message(db, payload)
                await message.ack()
                return payload
        finally:
            await connection.close()

    return asyncio.run(_run())


def _get_incident(catalog_sync_db, incident_id: str) -> Incident:
    return get_doc(catalog_sync_db, Incident, incident_id)


def _executions(catalog_sync_db, incident_id: str) -> list[WorkflowExecution]:
    return find_docs(catalog_sync_db, WorkflowExecution, {"incident_id": incident_id})


async def _process_again(loaded_catalog_db, payload: dict) -> None:
    async with open_db(*loaded_catalog_db) as db:
        await process_message(db, payload)


def test_worked_trace_resolves_dcd_data_end_to_end(app, loaded_catalog_db, catalog_sync_db, rabbitmq_url):
    with TestClient(app) as client:
        response = client.post("/webhooks/teams", json={"id": "msg-1", "text": WORKED_TRACE_TEXT})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "accepted"
        incident_id = body["incident_id"]

    message = _classify_next_message(loaded_catalog_db, rabbitmq_url)
    assert message["incident_id"] == incident_id

    incident = _get_incident(catalog_sync_db, incident_id)
    print(
        f"\n[OUTCOME] incident_id={incident.id} status={incident.classification_status} "
        f"source_system_id={incident.source_system_id} category={incident.category!r} "
        f"matched_rule_id={incident.matched_rule_id}"
    )
    assert incident.classification_status == "resolved"
    assert incident.source_system_id == "SYS_DCD"
    assert incident.category == "DATA QUALITY / TEST DATA"
    assert incident.jira_key is None  # Teams source, no confirmed Jira ticket at ingest time
    assert incident.matched_rule_id == "IMR_DCD_TABLE_DATA"
    assert re.match(r"^int_\d{14}_\d{5}$", incident.incident_key)  # generated, no Jira key

    # The worker ran the first RCA automatically: SYS_DCD has a playbook.
    (execution,) = _executions(catalog_sync_db, incident_id)
    assert execution.triggered_by == "auto"
    assert execution.incident_key == incident.incident_key
    assert execution.workflow_definition_version_id is not None
    assert execution.status == "completed"

    # A redelivered message re-classifies but doesn't run a second automatic RCA.
    asyncio.run(_process_again(loaded_catalog_db, message))
    assert len(_executions(catalog_sync_db, incident_id)) == 1


def test_duplicate_incident_is_deduped(app, loaded_catalog_db, catalog_sync_db, rabbitmq_url):
    with TestClient(app) as client:
        first = client.post("/webhooks/teams", json={"id": "msg-2", "text": DUPLICATE_TEST_TEXT})
        assert first.json()["status"] == "accepted"
        first_incident_id = first.json()["incident_id"]

        duplicate = client.post("/webhooks/teams", json={"id": "msg-3", "text": DUPLICATE_TEST_TEXT})
        print(f"\n[OUTCOME] first_post={first.json()} duplicate_post={duplicate.json()}")
        assert duplicate.json() == {"incident_id": None, "status": "duplicate"}

    # Drain the one message this test actually published, so the queue
    # stays in sync for the next test.
    message = _classify_next_message(loaded_catalog_db, rabbitmq_url)
    assert message["incident_id"] == first_incident_id


def test_no_footprint_incident_routes_to_manual_triage(app, loaded_catalog_db, catalog_sync_db, rabbitmq_url):
    with TestClient(app) as client:
        response = client.post("/webhooks/teams", json={"id": "msg-4", "text": NO_FOOTPRINT_TEXT})
        incident_id = response.json()["incident_id"]

    message = _classify_next_message(loaded_catalog_db, rabbitmq_url)
    assert message["incident_id"] == incident_id

    incident = _get_incident(catalog_sync_db, incident_id)
    print(
        f"\n[OUTCOME] incident_id={incident.id} status={incident.classification_status} "
        f"source_system_id={incident.source_system_id} category={incident.category}"
    )
    assert incident.classification_status == "manual_triage"
    assert incident.source_system_id is None
    assert incident.category is None

    # Nothing to run, but the automatic RCA attempt is still recorded.
    (execution,) = _executions(catalog_sync_db, incident_id)
    assert execution.triggered_by == "auto"
    assert execution.workflow_definition_version_id is None
    assert execution.rca["matched_pattern"] == "not_classified"


def test_jira_webhook_ingests_and_classifies(app, loaded_catalog_db, catalog_sync_db, rabbitmq_url):
    with TestClient(app) as client:
        response = client.post(
            "/webhooks/jira",
            json={
                "issue": {
                    "key": "FDM-9999",
                    "fields": {
                        "summary": "DATA QUALITY / TEST DATA - Billing UAT failing",
                        "description": "Nulls in DSNADEV.dcd_billing_summary contract_value column.",
                    },
                }
            },
        )
        assert response.status_code == 200
        incident_id = response.json()["incident_id"]

    message = _classify_next_message(loaded_catalog_db, rabbitmq_url)
    assert message["incident_id"] == incident_id

    incident = _get_incident(catalog_sync_db, incident_id)
    print(
        f"\n[OUTCOME] source={incident.source} external_id={incident.external_id} "
        f"status={incident.classification_status} source_system_id={incident.source_system_id} "
        f"category={incident.category!r}"
    )
    assert incident.source == "jira"
    assert incident.external_id == "FDM-9999"
    assert incident.jira_key == "FDM-9999"
    assert incident.classification_status == "resolved"
    assert incident.source_system_id == "SYS_DCD"


# Distinct subject/start_time so its dedup fingerprint doesn't collide with
# any other test's (the Redis container is shared across the whole session).
IDEMPOTENCY_RELEASE_TEXT = """\
**Subject:** DATA QUALITY / TEST DATA – DATA – IDEMPOTENCY RELEASE TEST
**Environment:** NPE
**Impact:** Nulls in contract_value found in DSNADEV.dcd_billing_summary; QA blocked.
**Start Time:** 2026-08-20 13:45 ET
"""


def test_ingest_failure_releases_idempotency_claim_for_retry(app, loaded_catalog_db, redis_url, rabbitmq_url, monkeypatch):
    """A DB failure after the Redis dedup claim used to block a legitimate
    retry of the same event for up to IDEMPOTENCY_TTL_SECONDS (24h default)
    with no Incident ever created. webhooks._ingest now releases the claim
    on ingest failure, so an identical retry is accepted, not "duplicate"."""
    import app.webhooks as webhooks_module

    real_create = webhooks_module.create_incident_record
    calls = {"n": 0}

    async def flaky_create(db, source, external_id, raw_text):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated DB failure after the idempotency claim")
        return await real_create(db, source, external_id, raw_text)

    monkeypatch.setattr(webhooks_module, "create_incident_record", flaky_create)

    parsed = parse_template(IDEMPOTENCY_RELEASE_TEXT)
    dedup_key = make_dedup_key(parsed["subject"], parsed["environment"], parsed["start_time"])

    async def _key_exists() -> bool:
        redis = make_redis(redis_url)
        try:
            return bool(await redis.exists(dedup_key))
        finally:
            await redis.aclose()

    with TestClient(app) as client:
        with pytest.raises(RuntimeError, match="simulated DB failure"):
            client.post("/webhooks/teams", json={"id": "msg-idem-1", "text": IDEMPOTENCY_RELEASE_TEXT})

        assert asyncio.run(_key_exists()) is False, "idempotency claim should have been released after ingest failure"

        retry = client.post("/webhooks/teams", json={"id": "msg-idem-2", "text": IDEMPOTENCY_RELEASE_TEXT})
        assert retry.status_code == 200
        body = retry.json()
        print(f"\n[OUTCOME] retry_status={body['status']} calls={calls['n']}")
        assert body["status"] == "accepted", "retry after a released claim must not read as duplicate"
        incident_id = body["incident_id"]

    message = _classify_next_message(loaded_catalog_db, rabbitmq_url)  # drain so the shared queue stays in sync
    assert message["incident_id"] == incident_id


def test_two_webhook_incidents_without_jira_keys_coexist(app, loaded_catalog_db, catalog_sync_db, rabbitmq_url):
    """jira_key's unique index must be partial: most webhook incidents have
    no Jira key, and a plain unique index would treat those nulls as equal
    and refuse the second one."""
    texts = [
        NO_FOOTPRINT_TEXT.replace("10:00 ET", "10:01 ET").replace("unrelated incident", "unrelated incident A"),
        NO_FOOTPRINT_TEXT.replace("10:00 ET", "10:02 ET").replace("unrelated incident", "unrelated incident B"),
    ]
    with TestClient(app) as client:
        responses = [client.post("/webhooks/teams", json={"id": f"msg-null-{i}", "text": t}) for i, t in enumerate(texts)]

    assert [r.status_code for r in responses] == [200, 200]
    ids = [r.json()["incident_id"] for r in responses]
    incidents = [_get_incident(catalog_sync_db, i) for i in ids]
    assert [i.jira_key for i in incidents] == [None, None]
    assert incidents[0].incident_key != incidents[1].incident_key

    for incident_id in ids:  # drain so the shared queue stays in sync
        assert _classify_next_message(loaded_catalog_db, rabbitmq_url)["incident_id"] == incident_id


def test_same_source_and_external_id_twice_is_409(app, loaded_catalog_db, catalog_sync_db, rabbitmq_url):
    """(source, external_id) is unique; a re-sent event that slips past the
    content fingerprint (different text) is a conflict, not a 500."""
    first_text = DUPLICATE_TEST_TEXT.replace("11:15 ET", "11:16 ET").replace("DEDUP TEST", "SOURCE ID TEST 1")
    second_text = DUPLICATE_TEST_TEXT.replace("11:15 ET", "11:17 ET").replace("DEDUP TEST", "SOURCE ID TEST 2")
    with TestClient(app) as client:
        first = client.post("/webhooks/teams", json={"id": "msg-same-id", "text": first_text})
        second = client.post("/webhooks/teams", json={"id": "msg-same-id", "text": second_text})

    assert first.status_code == 200
    assert second.status_code == 409
    assert _classify_next_message(loaded_catalog_db, rabbitmq_url)["incident_id"] == first.json()["incident_id"]
