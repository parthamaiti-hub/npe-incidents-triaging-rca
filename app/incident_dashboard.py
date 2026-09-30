"""Shared "latest RCA attempt per incident" join, used by both the
dashboard endpoint (app.routers.incidents) and the stats endpoint
(app.routers.stats).

Implemented as two plain queries plus an in-Python group-by rather than an
aggregation-pipeline $lookup: this is a dev-scale collection (the
NPE-triage volume this tool targets is nowhere near needing that), and a
straightforward version is far less risky to get right. Revisit with an
aggregation pipeline if/when incident volume actually makes this a
bottleneck.
"""

import datetime
import re
from dataclasses import dataclass

from pymongo.asynchronous.database import AsyncDatabase

from app.models import Incident, WorkflowExecution
from app.repositories.base import find


@dataclass
class IncidentWithLatestExecution:
    incident: Incident
    latest_execution: WorkflowExecution | None


async def load_incidents_with_latest_execution(
    db: AsyncDatabase,
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
    filter: dict = {}
    if classification_status is not None:
        filter["classification_status"] = classification_status
    if source_system_id is not None:
        filter["source_system_id"] = source_system_id
    if category is not None:
        filter["category"] = category
    received_at = {}
    if date_from is not None:
        received_at["$gte"] = date_from
    if date_to is not None:
        received_at["$lte"] = date_to
    if received_at:
        filter["received_at"] = received_at
    if q:
        # Escaped: user input is a literal substring (ILIKE '%q%'), never a
        # regex pattern -- unescaped it would be an injection/ReDoS vector.
        pattern = {"$regex": re.escape(q), "$options": "i"}
        filter["$or"] = [{"incident_key": pattern}, {"jira_key": pattern}, {"subject": pattern}]

    incidents = await find(db, Incident, filter)
    if not incidents:
        return []

    incident_ids = [i.id for i in incidents]
    jira_keys = [i.jira_key for i in incidents if i.jira_key is not None]

    exec_filters: list[dict] = [{"incident_id": {"$in": incident_ids}}]
    if jira_keys:
        exec_filters.append({"jira_key": {"$in": jira_keys}})
    executions = await find(db, WorkflowExecution, {"$or": exec_filters}, sort=[("started_at", -1)])

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
