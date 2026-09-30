"""Replay/backfill tool for cross-incident correlation.

Recomputes CorrelationGroup assignments for a historical window -- the
concrete payoff being: tune CORRELATION_WINDOW_MINUTES/CORRELATION_THRESHOLD,
then re-derive groups for past incidents without re-running classification
or touching anything else.

Queries MongoDB directly: the data this tool needs (source_system_id,
category, received_at) is durable on the Incident document itself, and each
Incident document already is the single current state, so no broker, message
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

from pymongo.asynchronous.database import AsyncDatabase

from app.classification import FULLY_CLASSIFIED_STATUSES
from app.correlation import correlate_incident
from app.db import get_database, make_mongo_client
from app.models import CorrelationGroup, Incident
from app.repositories.base import get

logger = logging.getLogger(__name__)


def _parse_bound(value: str) -> datetime.datetime:
    """CLI timestamps may be given with or without a timezone;
    Incident.received_at is compared as a naive datetime throughout this
    codebase (see app/correlation.py), so normalize to that."""
    dt = datetime.datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return dt


async def _find_candidates(db: AsyncDatabase, since: datetime.datetime, until: datetime.datetime | None) -> list[str]:
    """Incident rows in [since, until) that reached a fully-classified
    status (RESOLVED or LLM_RESOLVED -- both have source_system_id and
    category set), ordered by received_at so
    correlate_incident() replays them in the order they actually arrived --
    correctness-critical, since a later arrival's grouping decision depends
    on which earlier incidents already exist and are (un)grouped."""
    received_at = {"$gte": since}
    if until is not None:
        received_at["$lt"] = until
    cursor = db[Incident.COLLECTION].find(
        {"received_at": received_at, "classification_status": {"$in": list(FULLY_CLASSIFIED_STATUSES)}},
        projection={"_id": 1},
        sort=[("received_at", 1)],
    )
    return [doc["_id"] async for doc in cursor]


async def recorrelate(
    db: AsyncDatabase,
    since: datetime.datetime,
    until: datetime.datetime | None = None,
    dry_run: bool = False,
) -> dict:
    """Core, importable entry point -- main() below is a thin CLI wrapper
    around this, the same split scripts/e2e_rca.py uses."""
    incident_ids = await _find_candidates(db, since, until)

    if dry_run:
        return {"dry_run": True, "candidates": len(incident_ids), "incident_ids": incident_ids}

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
    reset = await db[Incident.COLLECTION].update_many(
        {"_id": {"$in": incident_ids}, "correlation_group_id": {"$ne": None}},
        {"$set": {"correlation_group_id": None}},
    )
    reset_count = reset.modified_count

    # Orphan cleanup: a group can end up with zero members after the
    # reset above (every incident that pointed to it was in this
    # window). Delete those rather than leaving dead documents behind --
    # scoped to the whole collection, not just this window, since an
    # orphan is an orphan regardless of which run caused it.
    referenced = await db[Incident.COLLECTION].distinct("correlation_group_id", {"correlation_group_id": {"$ne": None}})
    orphaned = await db[CorrelationGroup.COLLECTION].delete_many({"_id": {"$nin": referenced}})
    orphaned_count = orphaned.deleted_count

    groups_touched: set[str] = set()
    incidents_grouped = 0
    for incident_id in incident_ids:
        incident = await get(db, Incident, incident_id)
        if incident is None:
            logger.warning("Incident %s not found, skipping", incident_id)
            continue
        group = await correlate_incident(db, incident)
        if group is not None:
            incidents_grouped += 1
            groups_touched.add(group.id)

    return {
        "dry_run": False,
        "candidates": len(incident_ids),
        "reset": reset_count,
        "orphaned_groups_deleted": orphaned_count,
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

    async def _run() -> dict:
        mongo = make_mongo_client()
        try:
            return await recorrelate(get_database(mongo), args.since, args.until, dry_run=args.dry_run)
        finally:
            await mongo.close()

    result = asyncio.run(_run())
    print_report(result)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
