from datetime import datetime, timezone

import httpx
import respx

from app.graph_client import (
    extract_jira_keys,
    get_channel_messages,
    message_author,
    message_text,
    parse_teams_link,
)
from dataloadscripts.test_fixtures import GRAPH_MESSAGE_RS_173234, GRAPH_MESSAGES_PAGE_RESPONSE


def test_parse_teams_link_extracts_team_and_channel_id():
    url = (
        "https://teams.microsoft.com/l/channel/19%3A282bd3da...%40thread.tacv2/"
        "DSG-Triage-NPE?groupId=607c0654-99cd-406c-8de7-211c6d339ea6&tenantId=abc"
    )
    team_id, channel_id = parse_teams_link(url)
    assert team_id == "607c0654-99cd-406c-8de7-211c6d339ea6"
    assert channel_id == "19:282bd3da...@thread.tacv2"


def test_extract_jira_keys_finds_browse_link_not_prose_mentions():
    # "GT-333" mentioned in prose should not be picked up -- only the actual
    # browse link.
    html = 'See <a href="https://t-mobile.atlassian.net/browse/RS-173234">RS-173234</a>, related to GT-333.'
    assert extract_jira_keys(html) == {"RS-173234"}


def test_message_text_strips_html():
    text = message_text(GRAPH_MESSAGE_RS_173234)
    assert "Hi dpo-dice" in text
    assert "<a href=" not in text


def test_message_author():
    assert message_author(GRAPH_MESSAGE_RS_173234) == "Dongale, Kapil"


@respx.mock
async def test_get_channel_messages_single_page():
    respx.get(
        "https://graph.microsoft.com/v1.0/teams/team1/channels/channel1/messages",
        params={"$top": "50"},
    ).mock(return_value=httpx.Response(200, json=GRAPH_MESSAGES_PAGE_RESPONSE))

    async with httpx.AsyncClient() as client:
        since = datetime(2026, 8, 1, tzinfo=timezone.utc)
        messages = await get_channel_messages(client, "team1", "channel1", "fake-token", since)

    assert len(messages) == 1
    assert messages[0]["id"] == GRAPH_MESSAGE_RS_173234["id"]


@respx.mock
async def test_get_channel_messages_stops_at_cutoff():
    respx.get(
        "https://graph.microsoft.com/v1.0/teams/team1/channels/channel1/messages",
        params={"$top": "50"},
    ).mock(return_value=httpx.Response(200, json=GRAPH_MESSAGES_PAGE_RESPONSE))

    async with httpx.AsyncClient() as client:
        # cutoff after the message's createdDateTime -> should be excluded
        since = datetime(2026, 8, 22, tzinfo=timezone.utc)
        messages = await get_channel_messages(client, "team1", "channel1", "fake-token", since)

    assert messages == []


@respx.mock
async def test_get_channel_messages_follows_next_link():
    page1 = {
        "value": [GRAPH_MESSAGE_RS_173234],
        "@odata.nextLink": "https://graph.microsoft.com/v1.0/teams/team1/channels/channel1/messages?$skiptoken=abc",
    }
    page2 = {"value": []}

    respx.get(
        "https://graph.microsoft.com/v1.0/teams/team1/channels/channel1/messages",
        params={"$top": "50"},
    ).mock(return_value=httpx.Response(200, json=page1))
    respx.get(
        "https://graph.microsoft.com/v1.0/teams/team1/channels/channel1/messages",
        params={"$skiptoken": "abc"},
    ).mock(return_value=httpx.Response(200, json=page2))

    async with httpx.AsyncClient() as client:
        since = datetime(2026, 8, 1, tzinfo=timezone.utc)
        messages = await get_channel_messages(client, "team1", "channel1", "fake-token", since)

    assert len(messages) == 1
