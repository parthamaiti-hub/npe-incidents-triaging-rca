"""Cross-incident correlation for incidents against the same application:
given a just-classified incident, find or create the CorrelationGroup for
other incidents against the same (source_system_id, category) within a
sliding time window.

Pure Postgres, by design -- the computation doesn't need a
streaming/windowed-aggregation engine at this incident volume. Called
synchronously, inline in app.workflow_orchestrator. Being
broker-independent also keeps this directly reusable from
scripts/recorrelate.py without duplicating the logic.
"""

import dataclasses
import datetime
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import CORRELATION_THRESHOLD, CORRELATION_WINDOW_MINUTES
from app.models import CorrelationGroup, Incident


@dataclasses.dataclass
class CorrelationContext:
    """What app.rca_synthesizer.synthesize_rca needs to know about an
    incident's cluster -- deliberately just plain data, no DB session, so
    synthesize_rca stays a pure function."""

    group_id: str
    source_system_id: str
    incident_count: int
    window_minutes: int
    sibling_jira_keys: list[str]
    representative_jira_key: str | None


async def correlate_incident(session: AsyncSession, incident: Incident) -> CorrelationGroup | None:
    """Finds or creates the CorrelationGroup for one classified incident.

    Returns None if the incident isn't classified yet (no source_system_id/
    category to correlate on), or if fewer than CORRELATION_THRESHOLD
    incidents (including this one) exist for the same (source_system_id,
    category) within the window -- most incidents, most of the time.

    Idempotent per (incident, group): re-calling this for an incident that
    already belongs to the group its matches resolve to is a no-op, not a
    double-count. This matters for real callers, not just hygiene -- a
    retried execution would otherwise inflate incident_count, and
    scripts/recorrelate.py's batch replay calls this once per incident in a
    window where *all* siblings already exist as committed rows (unlike the
    live path, where a sibling usually doesn't exist yet) -- the first call
    in a cluster mass-assigns every match to a new group, so later calls for
    those same already-assigned incidents must recognize that and stop.

    Known limitation, not fixed here: if two *already-grouped* incidents
    with different group ids both match (possible under concurrent/
    out-of-order processing), this joins the incident to whichever group is
    found first rather than merging the groups. Rare at NPE volume; an
    accepted judgment call.
    """
    if incident.source_system_id is None or incident.category is None:
        return None

    window = datetime.timedelta(minutes=CORRELATION_WINDOW_MINUTES)
    base_filter = (
        Incident.id != incident.id,
        Incident.source_system_id == incident.source_system_id,
        Incident.category == incident.category,
        Incident.received_at >= incident.received_at - window,
        Incident.received_at <= incident.received_at + window,
    )

    # With a 24h correlation window at 3,000
    # incidents/24h across 300 applications, a single application's burst
    # can plausibly grow a group into the hundreds of members. Checking
    # "does an existing group already apply" first, as its own cheap
    # existence query, means every arrival after a group has formed costs
    # O(1) regardless of the group's size -- the full-row fetch below only
    # ever runs for the (normally 0-1, live path) incidents that don't yet
    # match an existing group, not once per arrival against an ever-larger
    # already-formed group.
    existing_group_id = (
        await session.scalars(
            select(Incident.correlation_group_id).where(*base_filter, Incident.correlation_group_id.is_not(None)).limit(1)
        )
    ).first()
    if existing_group_id is not None:
        group = await session.get(CorrelationGroup, existing_group_id)
        if incident.correlation_group_id != group.id:
            incident.correlation_group_id = group.id
            group.incident_count += 1
            group.last_seen_at = max(group.last_seen_at, incident.received_at)
            await session.commit()
        return group

    # No existing group among matches -- fetch full rows only now, needed
    # to decide whether this arrival reaches CORRELATION_THRESHOLD and, if
    # so, to assign every member at once (a one-time cost per newly-forming
    # cluster, not a per-arrival one).
    matches = (await session.scalars(select(Incident).where(*base_filter))).all()

    members = [*matches, incident]
    if len(members) < CORRELATION_THRESHOLD:
        return None

    earliest = min(members, key=lambda m: m.received_at)
    group = CorrelationGroup(
        id=str(uuid.uuid4()),
        source_system_id=incident.source_system_id,
        category=incident.category,
        opened_at=earliest.received_at,
        last_seen_at=max(m.received_at for m in members),
        incident_count=len(members),
        representative_incident_id=earliest.id,
        status="open",
    )
    session.add(group)
    await session.flush()  # group.id must exist in the DB before the FK assignments below
    for member in members:
        member.correlation_group_id = group.id
    await session.commit()
    return group


async def build_correlation_context(
    session: AsyncSession, group: CorrelationGroup, exclude_incident_id: str
) -> CorrelationContext:
    """Sibling jira_keys for the RCA text -- everything in the group except
    the incident whose RCA is currently being synthesized."""
    siblings = (
        await session.scalars(
            select(Incident).where(
                Incident.correlation_group_id == group.id,
                Incident.id != exclude_incident_id,
            )
        )
    ).all()
    representative = (
        await session.get(Incident, group.representative_incident_id) if group.representative_incident_id else None
    )
    return CorrelationContext(
        group_id=group.id,
        source_system_id=group.source_system_id,
        incident_count=group.incident_count,
        window_minutes=CORRELATION_WINDOW_MINUTES,
        sibling_jira_keys=[s.jira_key for s in siblings if s.jira_key],
        representative_jira_key=representative.jira_key if representative else None,
    )
