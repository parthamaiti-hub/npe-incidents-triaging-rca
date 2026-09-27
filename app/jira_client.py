"""Jira Cloud REST API v3 client: API-token Basic Auth, fetch issue detail
by key or batch via JQL search.

No real Jira API token is provisioned yet. Tested via mocked
HTTP responses built from the real sample tickets (tests/test_jira_client.py).
"""

import base64

import httpx

from app.config import JIRA_API_TOKEN, JIRA_EMAIL, JIRA_SITE
from app.incident_parser import strip_html

ISSUE_FIELDS = "summary,status,priority,issuetype,assignee,reporter,created,updated,resolution,labels,description"
# renderedFields returns description as HTML instead of raw ADF JSON --
# much simpler to flatten to plain text than parsing the ADF document tree.
EXPAND = "renderedFields"

# Jira Cloud caps JQL "in (...)" batches around 40-50 keys per call.
MAX_BATCH_SIZE = 40


def _auth_header(email: str = JIRA_EMAIL, api_token: str = JIRA_API_TOKEN) -> dict[str, str]:
    token = base64.b64encode(f"{email}:{api_token}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Accept": "application/json"}


async def get_issue(client: httpx.AsyncClient, key: str, site: str = JIRA_SITE) -> dict:
    url = f"https://{site}/rest/api/3/issue/{key}"
    response = await client.get(url, headers=_auth_header(), params={"fields": ISSUE_FIELDS, "expand": EXPAND})
    response.raise_for_status()
    return response.json()


async def search_issues(client: httpx.AsyncClient, keys: list[str], site: str = JIRA_SITE) -> list[dict]:
    """Batches lookups via JQL instead of one call per key."""
    if not keys:
        return []

    issues: list[dict] = []
    for i in range(0, len(keys), MAX_BATCH_SIZE):
        batch = keys[i : i + MAX_BATCH_SIZE]
        jql = "key in (" + ",".join(batch) + ")"
        url = f"https://{site}/rest/api/3/search"
        response = await client.get(
            url, headers=_auth_header(), params={"jql": jql, "fields": ISSUE_FIELDS, "expand": EXPAND}
        )
        response.raise_for_status()
        issues.extend(response.json().get("issues", []))

    return issues


async def post_comment(client: httpx.AsyncClient, key: str, adf_body: dict, site: str = JIRA_SITE) -> dict:
    """Adds a comment to an issue. adf_body is an Atlassian Document Format
    document (see app/adf_report.py), not plain text -- Jira Cloud REST v3
    comments require ADF."""
    url = f"https://{site}/rest/api/3/issue/{key}/comment"
    response = await client.post(url, headers=_auth_header(), json={"body": adf_body})
    response.raise_for_status()
    return response.json()


def parse_issue(issue: dict) -> dict:
    """Normalizes a Jira issue API response into the fields Incident needs."""
    fields = issue.get("fields", {})
    rendered = issue.get("renderedFields", {})
    description_html = rendered.get("description") or ""

    return {
        "jira_key": issue["key"],
        "summary": fields.get("summary"),
        "status": (fields.get("status") or {}).get("name"),
        "priority": (fields.get("priority") or {}).get("name"),
        "issue_type": (fields.get("issuetype") or {}).get("name"),
        "reporter": (fields.get("reporter") or {}).get("displayName"),
        "assignee": (fields.get("assignee") or {}).get("displayName"),
        "labels": fields.get("labels") or [],
        "resolution": (fields.get("resolution") or {}).get("name"),
        "created": fields.get("created"),
        "updated": fields.get("updated"),
        "description_text": strip_html(description_html),
    }
