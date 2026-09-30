"""Scheduled poller: Teams channel -> Jira key extraction -> Jira issue
fetch -> Incident upsert -> RabbitMQ publish.

A simple interval loop, independent of the playbook engine, which stays
scoped to executing RCA playbooks. Runs alongside (not instead of) the
ingestion webhooks.

Run standalone:
    uv run python -m app.poller
"""

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone

import httpx
from aio_pika.abc import AbstractChannel
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.asynchronous.database import AsyncDatabase
from redis.asyncio import Redis

from app.config import (
    JIRA_SITE,
    MAX_POLL_BACKOFF_SECONDS,
    POLL_INTERVAL_SECONDS,
    POLL_LOOKBACK_MINUTES,
    TEAMS_CHANNEL_ID,
    TEAMS_TEAM_ID,
)
from app.db import ensure_indexes, get_database, in_transaction, make_mongo_client
from app.events import declare_incidents_raw, make_channel, make_connection, publish_incident_received
from app.graph_client import acquire_token, extract_jira_keys, get_channel_messages, message_author, message_text
from app.idempotency import make_redis
from app.incident_parser import extract_application_id_hint, extract_error_system_hint
from app.jira_client import parse_issue, search_issues
from app.models import Incident
from app.reference_data import resolve_addressed_team, resolve_environment
from app.repositories.base import find_one, update_fields
from app.repositories.incidents import insert_incident

logger = logging.getLogger(__name__)

CHECKPOINT_KEY = "npe:graph-poll:last-checkpoint"


async def _get_checkpoint(redis: Redis) -> datetime:
    value = await redis.get(CHECKPOINT_KEY)
    if value:
        return datetime.fromisoformat(value)
    return datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=POLL_LOOKBACK_MINUTES)


async def _set_checkpoint(redis: Redis, when: datetime) -> None:
    await redis.set(CHECKPOINT_KEY, when.isoformat())


def _collect_jira_mentions(messages: list[dict]) -> dict[str, list[dict]]:
    """Groups Teams messages by the Jira key(s) they reference."""
    mentions: dict[str, list[dict]] = {}
    for message in messages:
        html = message.get("body", {}).get("content", "")
        keys = extract_jira_keys(html)
        if not keys:
            continue
        mention = {
            "author": message_author(message),
            "timestamp": message.get("createdDateTime"),
            "text": message_text(message),
        }
        for key in keys:
            mentions.setdefault(key, []).append(mention)
    return mentions


async def upsert_incident_from_jira(
    db: AsyncDatabase, issue: dict, mentions: list[dict], session: AsyncClientSession | None = None
) -> Incident:
    """Creates the incident for this Jira issue, or refreshes the Jira-owned
    fields of the existing one. Only those fields are written -- never a
    whole-document replace -- so a concurrent classification write by the
    worker can't be clobbered by this refresh."""
    parsed = parse_issue(issue)
    raw_text = f"Subject: {parsed['summary']}\nBody:\n{parsed['description_text']}"

    incident = await find_one(db, Incident, {"jira_key": parsed["jira_key"]}, session=session)

    existing_mentions = list(incident.teams_mentions or []) if incident is not None else []
    for mention in mentions:
        if mention not in existing_mentions:
            existing_mentions.append(mention)

    # addressed_team lives in the Teams discussion ("Hi dpo-dice, ..."), not
    # in the Jira issue's own summary/description -- resolve it from the
    # mention text specifically, not raw_text.
    mentions_text = "\n".join(m.get("text", "") for m in existing_mentions)

    environment = await resolve_environment(db, raw_text)
    addressed_team = await resolve_addressed_team(db, mentions_text or raw_text)

    fields = {
        "raw_text": raw_text,
        "subject": parsed["summary"],
        "status": parsed["status"],
        "priority": parsed["priority"],
        "issue_type": parsed["issue_type"],
        "reporter": parsed["reporter"],
        "assignee": parsed["assignee"],
        "labels": parsed["labels"],
        "resolution": parsed["resolution"],
        "created": parsed["created"],
        "updated": parsed["updated"],
        "teams_mentions": existing_mentions,
        "related_jira_keys": [
            k for m in existing_mentions for k in extract_jira_keys(m.get("text", "")) if k != parsed["jira_key"]
        ]
        or None,
        "environment_raw": environment.code if environment else None,
        "environment_id": environment.id if environment else None,
        "application_id_hint": extract_application_id_hint(raw_text),
        "error_system_hint": extract_error_system_hint(raw_text),
        "addressed_team_raw": addressed_team.teams_handle if addressed_team else None,
        "addressed_team_id": addressed_team.id if addressed_team else None,
    }

    if incident is None:
        incident = Incident(source="jira", external_id=parsed["jira_key"], jira_key=parsed["jira_key"], **fields)
        return await insert_incident(db, incident, session=session)

    await update_fields(db, incident, fields, session=session)
    return incident


async def poll_once(
    db: AsyncDatabase,
    channel: AbstractChannel,
    redis: Redis,
    http_client: httpx.AsyncClient,
    access_token: str,
    team_id: str = TEAMS_TEAM_ID,
    channel_id: str = TEAMS_CHANNEL_ID,
    since: datetime | None = None,
    jira_site: str = JIRA_SITE,
) -> int:
    """Runs one poll cycle. Returns the number of incidents upserted.

    `since` overrides the Redis-stored checkpoint, and `jira_site` the
    configured Jira Cloud site -- both used by tests to control behavior
    explicitly rather than relying on wall-clock time or monkeypatching
    frozen default-argument values."""
    since = since if since is not None else await _get_checkpoint(redis)
    messages = await get_channel_messages(http_client, team_id, channel_id, access_token, since)
    mentions_by_key = _collect_jira_mentions(messages)

    if not mentions_by_key:
        await _set_checkpoint(redis, datetime.now(timezone.utc))
        return 0

    issues = await search_issues(http_client, list(mentions_by_key.keys()), site=jira_site)

    # A1: publish only after the transaction commits -- upsert_incident_from_jira
    # no longer publishes itself. Publishing per-issue *inside* this loop (the
    # old behavior) meant a worker could consume a message for an Incident
    # that didn't exist yet, or -- worse -- never would, if a later issue in
    # this same batch failed and rolled the whole transaction back. Collecting
    # here and publishing after commit closes that race outright.
    async def upsert_batch(session) -> list[tuple[str, str, str]]:
        # Built inside the callback: with_transaction may re-run it.
        batch = []
        for issue in issues:
            incident = await upsert_incident_from_jira(db, issue, mentions_by_key.get(issue["key"], []), session=session)
            batch.append((incident.id, incident.raw_text, "jira"))
        return batch

    to_publish = await in_transaction(db, upsert_batch)

    for incident_id, raw_text, source in to_publish:
        await publish_incident_received(channel, incident_id, raw_text, source)

    await _set_checkpoint(redis, datetime.now(timezone.utc))
    return len(to_publish)


async def run_poller() -> None:
    mongo = make_mongo_client()
    db = get_database(mongo)
    await ensure_indexes(db)
    redis = make_redis()
    connection = await make_connection()
    channel = await make_channel(connection)
    await declare_incidents_raw(channel)  # idempotent, same as Base.metadata.create_all

    # A5: this is the sole Teams ingestion path -- a scheduled component needs
    # retry/backoff more than a request-driven one does, since nothing else
    # retries a missed cycle. Any Graph/Jira/DB blip must not kill the process.
    consecutive_failures = 0
    try:
        async with httpx.AsyncClient(timeout=10.0) as http_client:
            while True:
                try:
                    # acquire_token() is a synchronous MSAL call (blocking network
                    # I/O) -- offloaded to a thread so it doesn't stall the event
                    # loop the way calling it inline would.
                    access_token = await asyncio.to_thread(acquire_token)
                    count = await poll_once(db, channel, redis, http_client, access_token)
                    logger.info("Poll cycle upserted %d incidents", count)
                    consecutive_failures = 0
                    await asyncio.sleep(POLL_INTERVAL_SECONDS * random.uniform(0.9, 1.1))
                except Exception:
                    consecutive_failures += 1
                    backoff = min(POLL_INTERVAL_SECONDS * (2**consecutive_failures), MAX_POLL_BACKOFF_SECONDS)
                    backoff *= random.uniform(0.9, 1.1)
                    logger.exception(
                        "Poll cycle failed (%d consecutive failure(s)) -- backing off %.0fs",
                        consecutive_failures,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
    finally:
        await connection.close()
        await redis.aclose()
        await mongo.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_poller())
