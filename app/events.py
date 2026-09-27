"""RabbitMQ access layer.

One durable queue, `incidents.raw`, with a fanout dead-letter exchange
(`incidents.raw.dlx`) bound to `incidents.raw.dlq` -- the only queue this
system needs a message broker for.

A poison message doesn't need the application to construct and publish its
own DLQ message: nack'ing with `requeue=False` routes it to the DLX
automatically. See app.worker.process_with_retry.
"""

import asyncio
import json

import aio_pika
from aio_pika.abc import (
    AbstractChannel,
    AbstractIncomingMessage,
    AbstractQueue,
    AbstractQueueIterator,
    AbstractRobustConnection,
)

from app.config import RABBITMQ_URL

INCIDENTS_RAW_QUEUE = "incidents.raw"
INCIDENTS_RAW_DLX = "incidents.raw.dlx"
INCIDENTS_RAW_DLQ = "incidents.raw.dlq"


async def make_connection(url: str = RABBITMQ_URL) -> AbstractRobustConnection:
    return await aio_pika.connect_robust(url)


async def make_channel(connection: AbstractRobustConnection) -> AbstractChannel:
    channel = await connection.channel()
    # One unacked message in flight per consumer -- simple, predictable
    # backpressure, matching how this codebase's workers already process
    # one message at a time with retry before moving to the next, rather
    # than a burst of concurrent in-flight messages.
    await channel.set_qos(prefetch_count=1)
    return channel


async def declare_incidents_raw(channel: AbstractChannel) -> AbstractQueue:
    """Idempotent -- safe to call on every producer/consumer startup, same
    as Base.metadata.create_all for the DB schema. Declares the DLX first
    (fanout: routes every dead-lettered message to the one DLQ, no
    routing-key matching to get wrong) then the main queue pointing at it."""
    dlx = await channel.declare_exchange(INCIDENTS_RAW_DLX, aio_pika.ExchangeType.FANOUT, durable=True)
    dlq = await channel.declare_queue(INCIDENTS_RAW_DLQ, durable=True)
    await dlq.bind(dlx)

    return await channel.declare_queue(
        INCIDENTS_RAW_QUEUE,
        durable=True,
        arguments={"x-dead-letter-exchange": INCIDENTS_RAW_DLX},
    )


async def publish_incident_received(channel: AbstractChannel, incident_id: str, raw_text: str, source: str) -> None:
    payload = {"incident_id": incident_id, "raw_text": raw_text, "source": source}
    message = aio_pika.Message(
        body=json.dumps(payload).encode("utf-8"),
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        content_type="application/json",
    )
    await channel.default_exchange.publish(message, routing_key=INCIDENTS_RAW_QUEUE)


def decode(message: AbstractIncomingMessage) -> dict:
    return json.loads(message.body.decode("utf-8"))


async def consume_one(iterator: AbstractQueueIterator, timeout: float | None = 15.0) -> AbstractIncomingMessage | None:
    """Fetches and returns exactly one message (still unacked -- the caller
    acks/nacks it), or None on timeout. Same bounded-poll interface the
    Redis Streams consumer uses, so app.worker's loop and the test suite
    share one calling convention; RabbitMQ ties acknowledgment to the
    message itself.

    timeout=None blocks indefinitely -- safe here (unlike the Redis
    Streams gotcha found in app.rca_worker) because aio-pika's iterator is
    a real client-side asyncio.Queue.get(), not a per-call AMQP RPC with
    its own socket timeout."""
    try:
        if timeout is None:
            return await iterator.__anext__()
        return await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
    except (TimeoutError, StopAsyncIteration):
        return None
