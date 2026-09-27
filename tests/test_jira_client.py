import httpx
import respx

from app.jira_client import ISSUE_FIELDS, get_issue, parse_issue, post_comment, search_issues
from dataloadscripts.test_fixtures import JIRA_ISSUE_RS_173234


def test_issue_fields_requests_every_field_parse_issue_reads():
    # Jira only returns fields you explicitly request -- respx mocks don't
    # enforce that (they return whatever JSON you hand them regardless of
    # the request), so a missing field here silently becomes None and only
    # a real API call catches it (this caught a real bug: issuetype was
    # missing, so issue_type always came back None against the live API).
    required = {
        "summary",
        "status",
        "priority",
        "issuetype",
        "assignee",
        "reporter",
        "created",
        "updated",
        "resolution",
        "labels",
        "description",
    }
    requested = set(ISSUE_FIELDS.split(","))
    assert required <= requested


def test_parse_issue_normalizes_real_fields():
    parsed = parse_issue(JIRA_ISSUE_RS_173234)

    assert parsed["jira_key"] == "RS-173234"
    assert parsed["status"] == "In Progress"
    assert parsed["priority"] == "Urgent/Blocker"
    assert parsed["issue_type"] == "THE Bug"
    assert parsed["reporter"] == "Indrani Akula"
    assert parsed["assignee"] == "Jvalant Dave"
    assert parsed["labels"] == ["FIBER_QE_AUG_QLAB03", "Fiber_Automation"]
    assert parsed["resolution"] is None
    assert "api.qlab03.fiber.t-mobile.com" in parsed["description_text"]
    assert "<p>" not in parsed["description_text"]


def test_parse_issue_handles_missing_optional_fields():
    minimal_issue = {"key": "RS-1", "fields": {"summary": "x"}, "renderedFields": {}}
    parsed = parse_issue(minimal_issue)
    assert parsed["status"] is None
    assert parsed["assignee"] is None
    assert parsed["labels"] == []
    assert parsed["description_text"] == ""


@respx.mock
async def test_get_issue():
    respx.get("https://t-mobile.atlassian.net/rest/api/3/issue/RS-173234").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_RS_173234)
    )

    async with httpx.AsyncClient() as client:
        issue = await get_issue(client, "RS-173234", site="t-mobile.atlassian.net")

    assert issue["key"] == "RS-173234"


@respx.mock
async def test_search_issues_batches_and_flattens_results():
    respx.get("https://t-mobile.atlassian.net/rest/api/3/search").mock(
        return_value=httpx.Response(200, json={"issues": [JIRA_ISSUE_RS_173234]})
    )

    async with httpx.AsyncClient() as client:
        issues = await search_issues(client, ["RS-173234"], site="t-mobile.atlassian.net")

    assert len(issues) == 1
    assert issues[0]["key"] == "RS-173234"


@respx.mock
async def test_search_issues_empty_keys_makes_no_request():
    async with httpx.AsyncClient() as client:
        issues = await search_issues(client, [], site="t-mobile.atlassian.net")
    assert issues == []


@respx.mock
async def test_post_comment_sends_adf_body_and_returns_response():
    adf = {"type": "doc", "version": 1, "content": []}
    route = respx.post("https://t-mobile.atlassian.net/rest/api/3/issue/RS-173234/comment").mock(
        return_value=httpx.Response(201, json={"id": "10042", "body": adf})
    )

    async with httpx.AsyncClient() as client:
        comment = await post_comment(client, "RS-173234", adf, site="t-mobile.atlassian.net")

    assert comment["id"] == "10042"
    sent_body = route.calls.last.request.content
    assert b'"type":"doc"' in sent_body or b'"type": "doc"' in sent_body
