"""Processing-statistics sticker for the UI's top nav: Total / Success / Failed / Unidentified(NotInScope) / average
process time, over incidents that have had at least one RCA attempt in the
given period. Reuses app.incident_dashboard's latest-execution-per-incident
join so the definition of "latest outcome" is identical to the dashboard
list -- an incident's stats bucket always matches what its
dashboard row shows.
"""

import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app import rca_status
from app.classification import MANUAL_TRIAGE
from app.db import get_async_session
from app.incident_dashboard import load_incidents_with_latest_execution

router = APIRouter(prefix="/stats", tags=["stats"])

PERIOD_TO_TIMEDELTA = {
    "24h": datetime.timedelta(hours=24),
    "7d": datetime.timedelta(days=7),
    "30d": datetime.timedelta(days=30),
}


class IncidentStatsOut(BaseModel):
    total: int
    succeeded: int
    failed: int
    unidentified: int
    avg_process_seconds: float | None


@router.get("/incidents", response_model=IncidentStatsOut)
async def incident_stats(period: str = "7d", session: AsyncSession = Depends(get_async_session)):
    """`period`: 24h | 7d | 30d | all (default 7d). Filters by the latest
    RCA attempt's started_at -- an incident with no RCA attempt yet
    contributes nothing (these stats are about processing outcomes, not raw
    ingestion volume)."""
    date_from = None
    if period != "all":
        delta = PERIOD_TO_TIMEDELTA.get(period)
        if delta is None:
            delta = PERIOD_TO_TIMEDELTA["7d"]
        # naive UTC, matching the naive TIMESTAMP columns started_at/completed_at
        # are stored as (server_default=func.now()) throughout this codebase.
        date_from = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - delta

    rows = await load_incidents_with_latest_execution(session)
    processed = [
        r for r in rows if r.latest_execution is not None and (date_from is None or r.latest_execution.started_at >= date_from)
    ]

    total = len(processed)
    unidentified = sum(1 for r in processed if r.incident.classification_status == MANUAL_TRIAGE)
    failed = sum(
        1
        for r in processed
        if r.incident.classification_status != MANUAL_TRIAGE
        and (
            r.latest_execution.status == "failed"
            or r.latest_execution.rca_status == rca_status.ISSUE_COULD_NOT_BE_TRACED
        )
    )
    succeeded = total - unidentified - failed

    durations = [
        (r.latest_execution.completed_at - r.latest_execution.started_at).total_seconds()
        for r in processed
        if r.latest_execution.status == "completed" and r.latest_execution.completed_at is not None
    ]
    avg_process_seconds = sum(durations) / len(durations) if durations else None

    return IncidentStatsOut(
        total=total,
        succeeded=succeeded,
        failed=failed,
        unidentified=unidentified,
        avg_process_seconds=avg_process_seconds,
    )
