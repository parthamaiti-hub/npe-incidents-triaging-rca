import hashlib

from redis.asyncio import Redis

from app.config import IDEMPOTENCY_TTL_SECONDS, REDIS_URL


def make_redis(url: str = REDIS_URL) -> Redis:
    return Redis.from_url(url, decode_responses=True)


def make_dedup_key(subject: str | None, environment: str | None, start_time: str | None) -> str:
    raw = f"{subject or ''}|{environment or ''}|{start_time or ''}"
    return "npe:incident-dedup:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def claim_incident(
    redis: Redis,
    subject: str | None,
    environment: str | None,
    start_time: str | None,
) -> bool:
    """Atomically claims the (subject, environment, start_time) fingerprint.
    Returns True for a new incident, False for a duplicate. The TTL slides
    forward on every duplicate hit so a burst of repeats keeps it deduped."""
    key = make_dedup_key(subject, environment, start_time)
    claimed = await redis.set(key, "1", nx=True, ex=IDEMPOTENCY_TTL_SECONDS)
    if not claimed:
        await redis.expire(key, IDEMPOTENCY_TTL_SECONDS)
    return bool(claimed)


async def release_claim(
    redis: Redis,
    subject: str | None,
    environment: str | None,
    start_time: str | None,
) -> None:
    """claim_incident's key is set before the Incident row is durably
    committed. If the commit fails after a successful claim, the key stays
    claimed for up to IDEMPOTENCY_TTL_SECONDS (24h by default) with no
    Incident ever created -- a legitimate retry of the same event reads as
    "duplicate" for that whole window. Callers must release the claim on
    ingest failure so a retry can go through."""
    await redis.delete(make_dedup_key(subject, environment, start_time))
