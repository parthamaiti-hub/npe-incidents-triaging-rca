"""REST API for viewing ingested incidents (app.poller/app.webhooks ->
Incident table) and triggering an RCA run on demand -- the same
app.e2e_pipeline.run_e2e_for_jira_key scripts/e2e_rca.py drives from the
CLI, now callable so a frontend button can trigger it instead of a
terminal.
"""

import datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from pymongo.asynchronous.database import AsyncDatabase

from app.classification import apply_classification, classify_raw_text
from app.config import JIRA_SITE
from app.db import get_db
from app.e2e_pipeline import run_e2e_for_jira_key
from app.incident_dashboard import load_incidents_with_latest_execution
from app.models import Incident
from app.repositories.base import find, find_one, get
from app.routers.workflows import WorkflowExecutionOut
from app.workflow_orchestrator import WorkflowValidationError, get_version_for_retry, run_rca_for_incident

router = APIRouter(tags=["incidents"])


class IncidentOut(BaseModel):
    id: str
    incident_key: str
    source: str
    external_id: str
    subject: str | None
    jira_key: str | None
    status: str | None
    priority: str | None
    issue_type: str | None
    reporter: str | None
    assignee: str | None
    labels: list[str] | None
    environment_raw: str | None
    environment_id: str | None
    addressed_team_raw: str | None
    addressed_team_id: str | None
    classification_status: str
    matched_rule_id: str | None
    source_system_id: str | None
    category: str | None
    classification_method: str | None
    llm_confidence: float | None
    received_at: datetime.datetime
    model_config = {"from_attributes": True}


class RcaResultOut(BaseModel):
    jira_key: str
    incident_key: str
    summary: str
    status: str
    priority: str
    classification: dict
    evidence: list[dict] | None
    rca: dict | None
    execution_id: str | None
    comment_id: str | None


class IncidentDashboardOut(IncidentOut):
    """IncidentOut plus its most recent RCA attempt -- "if
    processed more than once, show the latest processed version" means the list is one row per incident, not one per execution."""

    latest_execution_id: str | None
    latest_rca_status: str | None
    latest_matched_pattern: str | None
    latest_execution_status: str | None
    latest_executed_at: datetime.datetime | None


class IncidentDashboardPage(BaseModel):
    total: int
    items: list[IncidentDashboardOut]


class RetryRequestIn(BaseModel):
    workflow_definition_version_id: str | None = None
    requested_by: str
    # Optional operator-supplied hint for RCA synthesis --
    # persisted regardless, only actually consumed by the LLM prompt once
    # RCA_SYNTHESIS_LLM_ENABLED.
    operator_context: str | None = None


@router.get("/incidents", response_model=list[IncidentOut])
async def list_incidents(
    limit: int = 50,
    offset: int = 0,
    classification_status: str | None = None,
    source_system_id: str | None = None,
    db: AsyncDatabase = Depends(get_db),
):
    filter = {}
    if classification_status is not None:
        filter["classification_status"] = classification_status
    if source_system_id is not None:
        filter["source_system_id"] = source_system_id
    rows = await find(db, Incident, filter, sort=[("received_at", -1)], skip=offset, limit=limit)
    return [IncidentOut.model_validate(r, from_attributes=True) for r in rows]


@router.get("/incidents/dashboard", response_model=IncidentDashboardPage)
async def incidents_dashboard(
    limit: int = 50,
    offset: int = 0,
    q: str | None = None,
    classification_status: str | None = None,
    rca_status: str | None = None,
    source_system_id: str | None = None,
    category: str | None = None,
    date_from: datetime.datetime | None = None,
    date_to: datetime.datetime | None = None,
    db: AsyncDatabase = Depends(get_db),
):
    """One row per incident, newest-processed-first, joined to its latest
    RCA attempt. Registered before /incidents/{incident_id}
    so "dashboard" isn't swallowed as a path parameter."""
    rows = await load_incidents_with_latest_execution(
        db,
        q=q,
        classification_status=classification_status,
        source_system_id=source_system_id,
        category=category,
        date_from=date_from,
        date_to=date_to,
    )
    if rca_status is not None:
        rows = [r for r in rows if r.latest_execution is not None and r.latest_execution.rca_status == rca_status]

    def sort_key(row):
        latest = row.latest_execution
        return latest.started_at if latest is not None else row.incident.received_at

    rows.sort(key=sort_key, reverse=True)
    total = len(rows)
    page = rows[offset : offset + limit]

    items = [
        IncidentDashboardOut(
            **IncidentOut.model_validate(row.incident, from_attributes=True).model_dump(),
            latest_execution_id=row.latest_execution.id if row.latest_execution else None,
            latest_rca_status=row.latest_execution.rca_status if row.latest_execution else None,
            latest_matched_pattern=(row.latest_execution.rca or {}).get("matched_pattern") if row.latest_execution else None,
            latest_execution_status=row.latest_execution.status if row.latest_execution else None,
            latest_executed_at=row.latest_execution.started_at if row.latest_execution else None,
        )
        for row in page
    ]
    return IncidentDashboardPage(total=total, items=items)


@router.get("/incidents/{incident_id}", response_model=IncidentOut)
async def get_incident(incident_id: str, db: AsyncDatabase = Depends(get_db)):
    row = await get(db, Incident, incident_id)
    if row is None:
        raise HTTPException(404, f"Incident {incident_id!r} not found")
    return IncidentOut.model_validate(row, from_attributes=True)


@router.post("/rca/{jira_key}", response_model=RcaResultOut)
async def trigger_rca(
    jira_key: str,
    request: Request,
    post_comment: bool = False,
    db: AsyncDatabase = Depends(get_db),
):
    """Fetches jira_key fresh from Jira Cloud, classifies it, executes its
    active workflow (if any), and returns the synthesized RCA -- the API
    equivalent of `uv run python -m scripts.e2e_rca <jira_key>`. Requires
    JIRA_SITE/JIRA_EMAIL/JIRA_API_TOKEN to be configured. A ticket Jira
    doesn't have is a 404; any other Jira failure is a 502."""
    # Nothing is written before the Jira fetch, so a Jira failure there
    # leaves no partial state. (A failed comment post happens after the
    # execution was recorded -- the same as before, when the RCA run had
    # already committed by that point too.)
    try:
        result = await run_e2e_for_jira_key(
            db, request.app.state.http_client, jira_key, jira_site=JIRA_SITE, post_rca_comment=post_comment
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(404, f"Jira issue {jira_key!r} not found") from exc
        raise HTTPException(502, f"Jira request failed ({exc.response.status_code}) for {jira_key!r}") from exc
    return result


@router.post("/incidents/{incident_key}/retry", response_model=WorkflowExecutionOut)
async def retry_incident(
    incident_key: str,
    payload: RetryRequestIn,
    db: AsyncDatabase = Depends(get_db),
):
    """Reprocesses an already-ingested incident, found by its incident_key
    (its Jira key, or its generated int_... key). No fresh Jira fetch --
    that's POST /rca/{jira_key}.

    The stored incident text is re-classified against the current mapping
    rules first, so a fixed rule takes effect; then the now-active playbook
    runs, or the explicit version override (any approved-or-superseded
    version). When there's nothing to run (no playbook / not classified)
    that outcome is recorded rather than rejected. Every retry is its own
    WorkflowExecution row: history is GET /workflows/executions?incident_key=."""
    incident = await find_one(db, Incident, {"incident_key": incident_key})
    if incident is None:
        raise HTTPException(404, f"No ingested incident with key {incident_key!r}")

    version = None
    if payload.workflow_definition_version_id is not None:
        try:
            version = await get_version_for_retry(db, payload.workflow_definition_version_id)
        except WorkflowValidationError as exc:
            raise HTTPException(422, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    classification = await classify_raw_text(db, incident.raw_text)
    await apply_classification(db, incident, classification)

    execution = await run_rca_for_incident(
        db,
        incident,
        triggered_by="retry",
        version=version,
        requested_by=payload.requested_by,
        operator_context=payload.operator_context,
    )
    return WorkflowExecutionOut.model_validate(execution, from_attributes=True)
