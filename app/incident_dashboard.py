"""Shared "latest RCA attempt per incident" join, used by both the
dashboard endpoint (app.routers.incidents) and the stats endpoint
(app.routers.stats).

Implemented as two plain queries plus an in-Python group-by rather than a
correlated-subquery/window-function join: this is a dev-scale table (the
NPE-triage volume this tool targets is nowhere near needing that), and a
straightforward version is far less risky to get right than hand-written
window-function SQL with no existing precedent elsewhere in this codebase.
Revisit with a real SQL join if/when incident volume actually makes this a
bottleneck.
"""

import datetime
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Incident, WorkflowExecution


@dataclass
class IncidentWithLatestExecution:
    incident: Incident
    latest_execution: WorkflowExecution | None


async def load_incidents_with_latest_execution(
    session: AsyncSession,
    *,
    q: str | None = None,
    classification_status: str | None = None,
    source_system_id: str | None = None,
    category: str | None = None,
    date_from: datetime.datetime | None = None,
    date_to: datetime.datetime | None = None,
) -> list[IncidentWithLatestExecution]:
    """Every Incident matching the given filters, each paired with its most
    recent WorkflowExecution (by started_at), if any. Executions are matched
    by incident_id when present, falling back to jira_key for rows that
    only recorded jira_key."""
    stmt = select(Incident)
    if classification_status is not None:
        stmt = stmt.where(Incident.classification_status == classification_status)
    if source_system_id is not None:
        stmt = stmt.where(Incident.source_system_id == source_system_id)
    if category is not None:
        stmt = stmt.where(Incident.category == category)
    if date_from is not None:
        stmt = stmt.where(Incident.received_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(Incident.received_at <= date_to)
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(or_(Incident.jira_key.ilike(pattern), Incident.subject.ilike(pattern)))

    incidents = (await session.scalars(stmt)).all()
    if not incidents:
        return []

    incident_ids = [i.id for i in incidents]
    jira_keys = [i.jira_key for i in incidents if i.jira_key is not None]

    exec_filters = [WorkflowExecution.incident_id.in_(incident_ids)]
    if jira_keys:
        exec_filters.append(WorkflowExecution.jira_key.in_(jira_keys))
    exec_stmt = (
        select(WorkflowExecution)
        .where(or_(*exec_filters))
        .order_by(WorkflowExecution.started_at.desc())
    )
    executions = (await session.scalars(exec_stmt)).all()

    latest_by_incident_id: dict[str, WorkflowExecution] = {}
    latest_by_jira_key: dict[str, WorkflowExecution] = {}
    for execution in executions:  # already newest-first, so first-seen wins
        if execution.incident_id is not None:
            latest_by_incident_id.setdefault(execution.incident_id, execution)
        if execution.jira_key is not None:
            latest_by_jira_key.setdefault(execution.jira_key, execution)

    results = []
    for incident in incidents:
        latest = latest_by_incident_id.get(incident.id)
        if latest is None and incident.jira_key is not None:
            latest = latest_by_jira_key.get(incident.jira_key)
        results.append(IncidentWithLatestExecution(incident=incident, latest_execution=latest))
    return results
