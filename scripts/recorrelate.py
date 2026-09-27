"""Replay/backfill tool for cross-incident correlation.

Recomputes CorrelationGroup assignments for a historical window -- the
concrete payoff being: tune CORRELATION_WINDOW_MINUTES/CORRELATION_THRESHOLD,
then re-derive groups for past incidents without re-running classification
or touching anything else.

Queries Postgres directly: the data this tool needs (source_system_id,
category, received_at) is durable on the Incident row itself, and each
Incident row already is the single current state, so no broker, message
envelope or dedupe step is involved.

**Reuses the real code path.** Calls app.correlation.correlate_incident()
directly -- the same function app.workflow_orchestrator calls inline --
rather than reimplementing grouping logic here.

Usage:
    uv run python -m scripts.recorrelate --since 2026-09-01 --dry-run
    uv run python -m scripts.recorrelate --since 2026-09-01 --until 2026-09-02
"""

import argparse
import asyncio
import datetime
import logging

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.classification import FULLY_CLASSIFIED_STATUSES
from app.correlation import correlate_incident
from app.db import make_async_engine, make_async_session_factory
from app.models import CorrelationGroup, Incident

logger = logging.getLogger(__name__)


def _parse_bound(value: str) -> datetime.datetime:
    """CLI timestamps may be given with or without a timezone;
    Incident.received_at is compared as a naive datetime throughout this
    codebase (see app/correlation.py), so normalize to that."""
    dt = datetime.datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return dt


async def _find_candidates(
    session: AsyncSession, since: datetime.datetime, until: datetime.datetime | None
) -> list[str]:
    """Incident rows in [since, until) that reached a fully-classified
    status (RESOLVED or LLM_RESOLVED -- both have source_system_id and
    category set), ordered by received_at so
    correlate_incident() replays them in the order they actually arrived --
    correctness-critical, since a later arrival's grouping decision depends
    on which earlier incidents already exist and are (un)grouped."""
    stmt = (
        select(Incident.id)
        .where(
            Incident.received_at >= since,
            Incident.classification_status.in_(FULLY_CLASSIFIED_STATUSES),
        )
        .order_by(Incident.received_at)
    )
    if until is not None:
        stmt = stmt.where(Incident.received_at < until)
    return list((await session.scalars(stmt)).all())


async def recorrelate(
    session_factory: async_sessionmaker,
    since: datetime.datetime,
    until: datetime.datetime | None = None,
    dry_run: bool = False,
) -> dict:
    """Core, importable entry point -- main() below is a thin CLI wrapper
    around this, the same split scripts/e2e_rca.py uses."""
    async with session_factory() as session:
        incident_ids = await _find_candidates(session, since, until)

    if dry_run:
        return {"dry_run": True, "candidates": len(incident_ids), "incident_ids": incident_ids}

    async with session_factory() as session:
        # Reset this window's incidents back to unclustered before
        # replaying, so a changed CORRELATION_WINDOW_MINUTES/THRESHOLD
        # actually takes effect rather than just extending whatever
        # grouping the old settings already produced.
        #
        # Known limitation: a CorrelationGroup that also has members
        # *outside* [since, until) is only partially reset -- its
        # incident_count/last_seen_at can end up stale relative to the
        # members left untouched. For a clean recompute, pick --since
        # comfortably before any window boundary you care about rather than
        # a narrow slice that splits an existing group.
        reset_count = 0
        for incident_id in incident_ids:
            incident = await session.get(Incident, incident_id)
            if incident is not None and incident.correlation_group_id is not None:
                incident.correlation_group_id = None
                reset_count += 1
        await session.commit()

        # Orphan cleanup: a group can end up with zero members after the
        # reset above (every incident that pointed to it was in this
        # window). Delete those rather than leaving dead rows behind --
        # scoped to the whole table, not just this window, since an orphan
        # is an orphan regardless of which run caused it.
        orphaned = (
            await session.scalars(
                select(CorrelationGroup).where(
                    CorrelationGroup.id.not_in(
                        select(Incident.correlation_group_id).where(Incident.correlation_group_id.is_not(None))
                    )
                )
            )
        ).all()
        orphaned_ids = [g.id for g in orphaned]
        if orphaned_ids:
            await session.execute(delete(CorrelationGroup).where(CorrelationGroup.id.in_(orphaned_ids)))
            await session.commit()

        groups_touched: set[str] = set()
        incidents_grouped = 0
        for incident_id in incident_ids:
            incident = await session.get(Incident, incident_id)
            if incident is None:
                logger.warning("Incident %s not found, skipping", incident_id)
                continue
            group = await correlate_incident(session, incident)
            if group is not None:
                incidents_grouped += 1
                groups_touched.add(group.id)

    return {
        "dry_run": False,
        "candidates": len(incident_ids),
        "reset": reset_count,
        "orphaned_groups_deleted": len(orphaned_ids),
        "groups_touched": len(groups_touched),
        "incidents_grouped": incidents_grouped,
    }


def print_report(result: dict) -> None:
    sep = "=" * 78
    print(sep)
    print("Correlation replay")
    print(sep)
    if result["dry_run"]:
        print(f"[dry-run] {result['candidates']} candidate incident(s) in range, no changes made:")
        for incident_id in result["incident_ids"]:
            print(f"  - {incident_id}")
    else:
        print(f"Candidates in range:      {result['candidates']}")
        print(f"Incidents reset:          {result['reset']}")
        print(f"Orphaned groups deleted:  {result['orphaned_groups_deleted']}")
        print(f"Groups touched:           {result['groups_touched']}")
        print(f"Incidents grouped:        {result['incidents_grouped']}")
    print(sep)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", required=True, type=_parse_bound, help="ISO date/datetime, inclusive lower bound")
    parser.add_argument("--until", type=_parse_bound, default=None, help="ISO date/datetime, exclusive upper bound")
    parser.add_argument("--dry-run", action="store_true", help="List candidate incidents without changing anything")
    args = parser.parse_args()

    engine = make_async_engine()
    session_factory = make_async_session_factory(engine)

    async def _run() -> dict:
        try:
            return await recorrelate(session_factory, args.since, args.until, dry_run=args.dry_run)
        finally:
            await engine.dispose()

    result = asyncio.run(_run())
    print_report(result)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
