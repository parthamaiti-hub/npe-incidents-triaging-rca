from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.db import get_db
from app.events import publish_incident_received
from app.idempotency import claim_incident, release_claim
from app.incident_parser import parse_template
from app.ingestion import create_incident_record

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class TeamsWebhookPayload(BaseModel):
    id: str
    text: str


class JiraIssueFields(BaseModel):
    summary: str
    description: str = ""


class JiraIssue(BaseModel):
    key: str
    fields: JiraIssueFields


class JiraWebhookPayload(BaseModel):
    issue: JiraIssue


class ServiceNowWebhookPayload(BaseModel):
    sys_id: str
    short_description: str
    description: str = ""


class IngestResult(BaseModel):
    incident_id: str | None
    status: str  # "accepted" | "duplicate"


async def _ingest(
    request: Request, db: AsyncDatabase, source: str, external_id: str, raw_text: str
) -> IngestResult:
    parsed = parse_template(raw_text)
    claimed = await claim_incident(
        request.app.state.redis, parsed["subject"], parsed["environment"], parsed["start_time"]
    )
    if not claimed:
        return IngestResult(incident_id=None, status="duplicate")

    # Release the claim on any ingest failure so a legitimate retry
    # of the same event isn't read as "duplicate" for up to 24h with no
    # Incident ever created.
    try:
        incident = await create_incident_record(db, source, external_id, raw_text)
    except Exception as exc:
        await release_claim(request.app.state.redis, parsed["subject"], parsed["environment"], parsed["start_time"])
        if isinstance(exc, DuplicateKeyError):
            # Same (source, external_id) -- or Jira key -- already ingested.
            raise HTTPException(409, f"Incident {source}:{external_id} already exists") from exc
        raise

    await publish_incident_received(request.app.state.rabbitmq_channel, incident.id, raw_text, source)

    return IngestResult(incident_id=incident.id, status="accepted")


@router.post("/teams", response_model=IngestResult)
async def teams_webhook(
    payload: TeamsWebhookPayload,
    request: Request,
    db: AsyncDatabase = Depends(get_db),
) -> IngestResult:
    return await _ingest(request, db, source="teams", external_id=payload.id, raw_text=payload.text)


@router.post("/jira", response_model=IngestResult)
async def jira_webhook(
    payload: JiraWebhookPayload,
    request: Request,
    db: AsyncDatabase = Depends(get_db),
) -> IngestResult:
    raw_text = f"Subject: {payload.issue.fields.summary}\nBody:\n{payload.issue.fields.description}"
    return await _ingest(request, db, source="jira", external_id=payload.issue.key, raw_text=raw_text)


@router.post("/servicenow", response_model=IngestResult)
async def servicenow_webhook(
    payload: ServiceNowWebhookPayload,
    request: Request,
    db: AsyncDatabase = Depends(get_db),
) -> IngestResult:
    raw_text = f"Subject: {payload.short_description}\nBody:\n{payload.description}"
    return await _ingest(request, db, source="servicenow", external_id=payload.sys_id, raw_text=raw_text)
