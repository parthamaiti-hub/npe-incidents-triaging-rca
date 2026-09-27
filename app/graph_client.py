"""Microsoft Graph client: app-only auth + polling a Teams channel for
messages, extracting referenced Jira keys.

No real Azure AD app is provisioned yet -- acquire_token()
will fail without real credentials. Tested via mocked HTTP responses built
from the real sample tickets' Teams discussion text (tests/test_graph_client.py).
"""

import re
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import msal

from app.config import AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID
from app.incident_parser import strip_html

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPES = ["https://graph.microsoft.com/.default"]

# Prefer matching the actual browse link over bare text mentions like "GT-333"
# in prose, which are false positives.
JIRA_LINK_RE = re.compile(r"atlassian\.net/browse/([A-Z][A-Z0-9]+-\d+)")


def parse_teams_link(url: str) -> tuple[str, str]:
    """Extracts (team_id, channel_id) from a Teams channel deep link."""
    parts = urlparse(url)
    team_id = parse_qs(parts.query)["groupId"][0]
    channel_id = unquote(parts.path.split("/channel/")[1].split("/")[0])
    return team_id, channel_id


def acquire_token(
    tenant_id: str = AZURE_TENANT_ID,
    client_id: str = AZURE_CLIENT_ID,
    client_secret: str = AZURE_CLIENT_SECRET,
) -> str:
    app = msal.ConfidentialClientApplication(
        client_id=client_id,
        client_credential=client_secret,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
    )
    result = app.acquire_token_for_client(scopes=GRAPH_SCOPES)
    if "access_token" not in result:
        raise RuntimeError(f"Failed to acquire Graph token: {result.get('error_description')}")
    return result["access_token"]


def extract_jira_keys(html_body: str) -> set[str]:
    return set(JIRA_LINK_RE.findall(html_body))


async def get_channel_messages(
    client: httpx.AsyncClient,
    team_id: str,
    channel_id: str,
    access_token: str,
    since: datetime,
) -> list[dict]:
    """Pages through GET /teams/{team_id}/channels/{channel_id}/messages via
    @odata.nextLink, stopping once messages are older than `since`."""
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"{GRAPH_BASE_URL}/teams/{team_id}/channels/{channel_id}/messages?$top=50"
    messages: list[dict] = []

    while url:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        payload = response.json()
        for message in payload.get("value", []):
            created = datetime.fromisoformat(message["createdDateTime"].replace("Z", "+00:00"))
            if created < since:
                return messages
            messages.append(message)
        url = payload.get("@odata.nextLink")

    return messages


def message_text(message: dict) -> str:
    return strip_html(message.get("body", {}).get("content", ""))


def message_author(message: dict) -> str | None:
    return ((message.get("from") or {}).get("user") or {}).get("displayName")
