import asyncio
import json
import uuid

import aio_pika
import pytest

from app.events import consume_one, decode, make_channel, make_connection
from app.worker import handle_message, process_with_retry
from dataloadscripts.test_fixtures import open_db


def test_process_with_retry_returns_true_on_success(monkeypatch):
    calls = []

    async def ok(db, payload, openai_client=None):
        calls.append(payload)

    monkeypatch.setattr("app.worker.process_message", ok)

    result = asyncio.run(process_with_retry(db=None, payload={"incident_id": "x"}, max_attempts=3))

    assert result is True
    assert calls == [{"incident_id": "x"}]


def test_process_with_retry_returns_false_after_exhausting_attempts(monkeypatch):
    attempts = []

    async def always_fails(db, payload, openai_client=None):
        attempts.append(payload)
        raise RuntimeError("boom")

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("app.worker.process_message", always_fails)
    monkeypatch.setattr("app.worker.asyncio.sleep", no_sleep)

    result = asyncio.run(process_with_retry(db=None, payload={"incident_id": "x"}, max_attempts=3))

    assert result is False
    assert len(attempts) == 3


async def _declare_isolated_dead_letter_queue(channel):
    """A throwaway queue/DLX/DLQ under a unique name, wired the same way
    declare_incidents_raw wires incidents.raw -- but private to this test,
    since incidents.raw itself is a shared, session-scoped queue other test
    modules also publish/consume on (see test_poller.py's own comment on
    this). Consuming-and-nacking whatever happens to be at the head of the
    *shared* queue would risk dead-lettering another test's real message;
    an isolated queue makes that impossible by construction."""
    suffix = uuid.uuid4().hex[:8]
    dlx_name = f"test.worker.dlx.{suffix}"
    dlq_name = f"test.worker.dlq.{suffix}"
    queue_name = f"test.worker.queue.{suffix}"
    dlx = await channel.declare_exchange(dlx_name, aio_pika.ExchangeType.FANOUT, durable=True, auto_delete=True)
    dlq = await channel.declare_queue(dlq_name, durable=True, auto_delete=True)
    await dlq.bind(dlx)
    queue = await channel.declare_queue(
        queue_name, durable=True, auto_delete=True, arguments={"x-dead-letter-exchange": dlx_name}
    )
    return queue, dlq


def test_worker_dead_letters_message_after_exhausting_retries(mongo_url, mongo_db_name, rabbitmq_url):
    """Publishes a payload missing "incident_id" -- process_message raises
    KeyError on every attempt -- runs it through process_with_retry exactly
    as run_worker() does, nacks with requeue=False on exhaustion, and
    confirms RabbitMQ's dead-letter-exchange actually routes it to the DLQ."""

    async def _run():
        connection = await make_connection(rabbitmq_url)
        channel = await make_channel(connection)
        queue, dlq = await _declare_isolated_dead_letter_queue(channel)
        try:
            bad_payload = {"not_incident_id": str(uuid.uuid4())}
            await channel.default_exchange.publish(
                aio_pika.Message(body=json.dumps(bad_payload).encode("utf-8")),
                routing_key=queue.name,
            )

            async with open_db(mongo_url, mongo_db_name) as db, queue.iterator() as iterator:
                message = await consume_one(iterator, timeout=20.0)
                assert message is not None, "expected the published message on the isolated test queue"
                payload = decode(message)
                succeeded = await process_with_retry(db, payload, max_attempts=1)
                assert succeeded is False
                await message.nack(requeue=False)

            async with dlq.iterator() as dlq_iterator:
                dlq_message = await consume_one(dlq_iterator, timeout=20.0)
                assert dlq_message is not None, "expected the dead-lettered message on the isolated test DLQ"
                assert decode(dlq_message) == bad_payload
                await dlq_message.ack()
        finally:
            await connection.close()

    asyncio.run(_run())


def test_worker_dead_letters_message_referencing_missing_incident(mongo_url, mongo_db_name, rabbitmq_url):
    """A message whose incident_id doesn't exist in the DB used to be acked
    (silent permanent loss -- just a warning log). process_message now
    raises IncidentNotFoundError instead, so process_with_retry's existing
    retry-then-DLQ loop handles it the same as any other failure: bounded
    retries, then dead-letter -- not a fresh ack-on-missing-work default."""

    async def _run():
        connection = await make_connection(rabbitmq_url)
        channel = await make_channel(connection)
        queue, dlq = await _declare_isolated_dead_letter_queue(channel)
        try:
            missing_payload = {
                "incident_id": str(uuid.uuid4()),
                "raw_text": "does not matter, incident row was never created",
                "source": "teams",
            }
            await channel.default_exchange.publish(
                aio_pika.Message(body=json.dumps(missing_payload).encode("utf-8")),
                routing_key=queue.name,
            )

            async with open_db(mongo_url, mongo_db_name) as db, queue.iterator() as iterator:
                message = await consume_one(iterator, timeout=20.0)
                assert message is not None
                payload = decode(message)
                succeeded = await process_with_retry(db, payload, max_attempts=2)
                assert succeeded is False
                await message.nack(requeue=False)

            async with dlq.iterator() as dlq_iterator:
                dlq_message = await consume_one(dlq_iterator, timeout=20.0)
                assert dlq_message is not None, "expected the missing-incident message on the isolated test DLQ"
                assert decode(dlq_message) == missing_payload
                await dlq_message.ack()
        finally:
            await connection.close()

    asyncio.run(_run())


def test_handle_message_nacks_undecodable_body_without_raising(mongo_url, mongo_db_name, rabbitmq_url):
    """An unguarded decode exception would propagate out of run_worker's
    loop entirely, through the `finally`, killing the whole worker process
    over one poison message. handle_message wraps decode in its own
    try/except, dead-lettering
    just that message and returning normally so the caller's loop can keep
    consuming."""

    async def _run():
        connection = await make_connection(rabbitmq_url)
        channel = await make_channel(connection)
        queue, dlq = await _declare_isolated_dead_letter_queue(channel)
        try:
            await channel.default_exchange.publish(
                aio_pika.Message(body=b"this is not valid json {{{"),
                routing_key=queue.name,
            )

            async with open_db(mongo_url, mongo_db_name) as db, queue.iterator() as iterator:
                message = await consume_one(iterator, timeout=20.0)
                assert message is not None
                await handle_message(message, db)  # must not raise

            async with dlq.iterator() as dlq_iterator:
                dlq_message = await consume_one(dlq_iterator, timeout=20.0)
                assert dlq_message is not None, "expected the undecodable message on the isolated test DLQ"
                assert dlq_message.body == b"this is not valid json {{{"
                await dlq_message.ack()
        finally:
            await connection.close()

    asyncio.run(_run())
