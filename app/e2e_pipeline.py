"""End-to-end pipeline for a single Jira issue: fetch real issue detail ->
classify -> look up its RCA playbook -> execute (lightweight, stubbed
checks) -> synthesize an RCA -> optionally post the RCA back as a Jira
comment. Ties together the classification pipeline with the playbook
engine and RCA synthesizer -- no Teams/Graph involved, this takes a Jira
key directly.
"""

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.adf_report import build_rca_comment_adf
from app.classification import apply_classification, classify_raw_text
from app.config import JIRA_SITE
from app.jira_client import get_issue, parse_issue, post_comment
from app.poller import upsert_incident_from_jira
from app.workflow_orchestrator import run_rca_for_incident


async def run_e2e_for_jira_key(
    session: AsyncSession,
    http_client: httpx.AsyncClient,
    jira_key: str,
    jira_site: str = JIRA_SITE,
    post_rca_comment: bool = False,
) -> dict:
    issue = await get_issue(http_client, jira_key, site=jira_site)
    # Keyed on the requested key, so the Incident row and every execution
    # recorded below share one identity (the key the operator asked for).
    issue = {**issue, "key": jira_key}
    parsed = parse_issue(issue)

    # Every RCA attempt gets an Incident row (created, or refreshed from Jira)
    # -- the dashboard and Retry tab are built from Incident rows, so an
    # execution without one would be invisible there.
    incident = await upsert_incident_from_jira(session, issue, mentions=[])

    classification = await classify_raw_text(session, incident.raw_text)
    apply_classification(incident, classification)
    await session.flush()

    execution = await run_rca_for_incident(session, incident, triggered_by="rca")
    result: dict = {
        "jira_key": jira_key,
        "incident_key": incident.incident_key,
        "summary": parsed["summary"],
        "status": parsed["status"],
        "priority": parsed["priority"],
        "classification": classification,
        # None when nothing ran (no playbook / not classified), not an empty list.
        "evidence": execution.evidence if execution.workflow_definition_version_id else None,
        "rca": execution.rca,
        "execution_id": execution.id,
        "comment_id": None,
    }

    if post_rca_comment:
        adf = build_rca_comment_adf(result)
        comment = await post_comment(http_client, jira_key, adf, site=jira_site)
        result["comment_id"] = comment.get("id")

    return result
