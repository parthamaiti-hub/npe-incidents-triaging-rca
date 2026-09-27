"""End-to-end pipeline for a single Jira issue: fetch real issue detail ->
classify -> look up its RCA playbook -> execute (lightweight, stubbed
checks) -> synthesize an RCA -> optionally post the RCA back as a Jira
comment. Ties together the classification pipeline with the playbook
engine and RCA synthesizer -- no Teams/Graph involved, this takes a Jira
key directly.
"""

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import rca_status
from app.adf_report import build_rca_comment_adf
from app.classification import FULLY_CLASSIFIED_STATUSES, classify_raw_text
from app.config import JIRA_SITE
from app.jira_client import get_issue, parse_issue, post_comment
from app.models import Incident
from app.workflow_orchestrator import execute_and_record, get_active_workflow, record_unexecuted_attempt


async def run_e2e_for_jira_key(
    session: AsyncSession,
    http_client: httpx.AsyncClient,
    jira_key: str,
    jira_site: str = JIRA_SITE,
    post_rca_comment: bool = False,
) -> dict:
    issue = await get_issue(http_client, jira_key, site=jira_site)
    parsed = parse_issue(issue)
    raw_text = f"Subject: {parsed['summary']}\nBody:\n{parsed['description_text']}"

    classification = await classify_raw_text(session, raw_text)

    incident = (await session.scalars(select(Incident).where(Incident.jira_key == jira_key))).first()
    incident_id = incident.id if incident is not None else None

    result: dict = {
        "jira_key": jira_key,
        "summary": parsed["summary"],
        "status": parsed["status"],
        "priority": parsed["priority"],
        "classification": classification,
        "evidence": None,
        "rca": None,
        "execution_id": None,
        "comment_id": None,
    }

    if classification["status"] in FULLY_CLASSIFIED_STATUSES:
        version = await get_active_workflow(
            session, classification["source_system_id"], classification["category"]
        )

        if version is None:
            rca = {
                "matched_pattern": "no_playbook",
                "rca_status": rca_status.NEED_MANUAL_INTERVENTION,
                "root_cause_summary": (
                    f"No RCA playbook configured yet for "
                    f"({classification['source_system_id']}, {classification['category']})."
                ),
                "contributing_factors": [],
                "recommended_actions": ["Author an RCA_PLAYBOOK for this (source_system, category) pair"],
            }
            execution = await record_unexecuted_attempt(
                session, jira_key=jira_key, incident_id=incident_id, rca=rca, triggered_by="rca"
            )
            result["rca"] = rca
            result["execution_id"] = execution.id
        else:
            execution = await execute_and_record(
                session, version, jira_key=jira_key, incident_id=incident_id, triggered_by="rca"
            )
            result["evidence"] = execution.evidence
            result["rca"] = execution.rca
            result["execution_id"] = execution.id
    else:
        rca = {
            "matched_pattern": "not_classified",
            "rca_status": rca_status.NEED_MANUAL_INTERVENTION,
            "root_cause_summary": f"Could not classify this incident (status={classification['status']}).",
            "contributing_factors": [],
            "recommended_actions": ["Route for manual triage"],
        }
        execution = await record_unexecuted_attempt(
            session, jira_key=jira_key, incident_id=incident_id, rca=rca, triggered_by="rca"
        )
        result["rca"] = rca
        result["execution_id"] = execution.id

    if post_rca_comment:
        adf = build_rca_comment_adf(result)
        comment = await post_comment(http_client, jira_key, adf, site=jira_site)
        result["comment_id"] = comment.get("id")

    return result
