from sqlalchemy.ext.asyncio import AsyncSession

from app.incident_parser import (
    extract_application_id_hint,
    extract_error_system_hint,
    parse_template,
)
from app.models import Incident
from app.reference_data import resolve_addressed_team, resolve_environment


async def create_incident_record(
    session: AsyncSession, source: str, external_id: str, raw_text: str
) -> Incident:
    subject = parse_template(raw_text)["subject"]

    environment = await resolve_environment(session, raw_text)
    addressed_team = await resolve_addressed_team(session, raw_text)

    incident = Incident(
        source=source,
        external_id=external_id,
        # A Jira-sourced incident's external_id *is* its real issue key --
        # previously left unset here, so POST /incidents/{jira_key}/retry
        # and GET /workflows/executions?jira_key= could never find an
        # incident that arrived via POST /webhooks/jira directly (only ones
        # that came through app.poller's Teams->Jira correlation, which
        # sets jira_key itself). Teams/ServiceNow incidents still have no
        # confirmed Jira ticket at ingest time, so jira_key stays None for
        # them, same as before.
        jira_key=external_id if source == "jira" else None,
        raw_text=raw_text,
        subject=subject,
        environment_raw=environment.code if environment else None,
        environment_id=environment.id if environment else None,
        application_id_hint=extract_application_id_hint(raw_text),
        error_system_hint=extract_error_system_hint(raw_text),
        addressed_team_raw=addressed_team.teams_handle if addressed_team else None,
        addressed_team_id=addressed_team.id if addressed_team else None,
    )
    session.add(incident)
    await session.flush()
    return incident
