"""Editing an existing playbook -- YAML endpoints, edit ->
approve -> version N+1, pin preservation, stale-base conflicts."""

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import Base, make_engine, make_session_factory
from app.function_registry import FUNCTION_REGISTRY, reset_function_registry
from app.main import create_app
from app.models import SourceSystem, WorkflowBuildRequest, WorkflowExecution

CATEGORY = "FUNCTIONAL DEFECT (QA/UAT)"
ERROR_LOGS = {"call": "error_logs", "with": {"env": "NPE", "app": "TESTAPP", "lookback_minutes": 240}}
DEPLOYS = {"call": "recent_deployments", "with": {"env": "NPE", "app": "TESTAPP", "lookback_minutes": 1440}}


@pytest.fixture()
def app(postgres_url, redis_url, rabbitmq_url):
    engine = make_engine(postgres_url)
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        session.add(
            SourceSystem(
                id="SYS_TESTAPP",
                name="Test App",
                code="TESTAPP",
                type="Application",
                description="x",
                owning_team="x",
                environment="NPE",
            )
        )
        session.commit()
    yield create_app(database_url=postgres_url, redis_url=redis_url, rabbitmq_url=rabbitmq_url)
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture(autouse=True)
def restore_function_registry():
    snapshot = dict(FUNCTION_REGISTRY)
    yield
    reset_function_registry(snapshot)


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c


def _publish(client, functions) -> dict:
    request = client.post(
        "/workflows/build-requests",
        json={"source_system_id": "SYS_TESTAPP", "category": CATEGORY, "requested_functions": functions, "requested_by": "u1"},
    )
    assert request.status_code == 201, request.text
    version = client.post(f"/workflows/build-requests/{request.json()['id']}/approve", json={"approved_by": "u1"})
    assert version.status_code == 200, version.text
    return version.json()


def _yaml_for(tasks) -> str:
    do = [{t.get("name") or t["call"]: {"call": t["call"], "with": t["with"]}} for t in tasks]
    return yaml.safe_dump({"document": {"dsl": "1.0.0"}, "do": do}, sort_keys=False)


def _edit(client, version, **body):
    return client.post(
        f"/workflows/definitions/{version['workflow_definition_id']}/edits",
        json={"base_version_id": version["id"], "edited_by": "editor", **body},
    )


def _versions(client, definition_id):
    return client.get(f"/workflows/definitions/{definition_id}/versions").json()


def test_version_yaml_renders_cncf_document(client):
    v1 = _publish(client, [ERROR_LOGS, DEPLOYS])
    response = client.get(f"/workflows/versions/{v1['id']}/yaml")
    assert response.status_code == 200
    doc = yaml.safe_load(response.json()["yaml"])
    assert doc["document"]["name"] == "SYS_TESTAPP-functional-defect-qa-uat"
    assert doc["document"]["version"] == "1"
    assert [list(t)[0] for t in doc["do"]] == ["error_logs", "recent_deployments"]
    assert client.get("/workflows/versions/nope/yaml").status_code == 404


def test_render_yaml_converts_draft_tasks(client):
    response = client.post("/workflows/render-yaml", json={"tasks": [ERROR_LOGS], "name": "n", "version": "draft"})
    assert response.status_code == 200
    assert yaml.safe_load(response.json()["yaml"])["do"][0]["error_logs"]["call"] == "error_logs"
    assert client.post("/workflows/render-yaml", json={"tasks": [{"with": {}}]}).status_code == 422


def test_validate_yaml_is_a_dry_run(client, postgres_url):
    response = client.post("/workflows/validate-yaml", json={"yaml": _yaml_for([ERROR_LOGS])})
    assert response.status_code == 200, response.text
    assert response.json()["tasks"][0]["function_version_number"] == 1

    engine = make_engine(postgres_url)
    with make_session_factory(engine)() as session:
        assert session.scalars(select(WorkflowBuildRequest)).all() == []
    engine.dispose()


def test_validate_yaml_syntax_error(client):
    response = client.post("/workflows/validate-yaml", json={"yaml": "do:\n  - a:\n      call: [oops\n"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["kind"] == "syntax"
    assert detail["errors"][0]["line"] is not None


def test_validate_yaml_schema_error(client):
    response = client.post("/workflows/validate-yaml", json={"yaml": "do:\n  - a:\n      switch: []\n"})
    assert response.status_code == 422
    assert response.json()["detail"]["kind"] == "schema"


def test_validate_yaml_reports_every_bad_task_with_its_line(client):
    text = _yaml_for(
        [ERROR_LOGS, {"call": "not_real", "with": {}}, {"name": "partial", "call": "error_logs", "with": {"env": "NPE"}}]
    )
    response = client.post("/workflows/validate-yaml", json={"yaml": text})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["kind"] == "registry"
    by_index = {e["task_index"]: e for e in detail["errors"]}
    assert set(by_index) == {1, 2}
    assert "Unknown function" in by_index[1]["message"]
    assert "missing required params" in by_index[2]["message"]
    assert by_index[1]["line"] < by_index[2]["line"]
    assert by_index[1]["path"] == "do[1]"


def test_validate_yaml_rejects_function_without_implementation(client):
    FUNCTION_REGISTRY["error_logs"] = FUNCTION_REGISTRY["error_logs"].model_copy(update={"version_number": 99})
    response = client.post("/workflows/validate-yaml", json={"yaml": _yaml_for([ERROR_LOGS])})
    assert response.status_code == 422
    assert "no matching implementation" in response.json()["detail"]["errors"][0]["message"]


def test_yaml_edit_then_approve_publishes_next_version(client):
    v1 = _publish(client, [ERROR_LOGS, DEPLOYS])
    execution = client.post(f"/workflows/versions/{v1['id']}/execute", json={}).json()

    edited = _edit(client, v1, yaml=_yaml_for([DEPLOYS]), change_note="drop error_logs")
    assert edited.status_code == 201, edited.text
    request = edited.json()
    assert request["status"] == "rendered"
    assert request["base_version_id"] == v1["id"]
    assert request["use_case_description"] == "drop error_logs"
    assert [t["call"] for t in request["generated_document"]] == ["recent_deployments"]

    # Saved but not published yet -- v1 is still the executable version.
    assert [v["status"] for v in _versions(client, v1["workflow_definition_id"])] == ["approved"]

    v2 = client.post(f"/workflows/build-requests/{request['id']}/approve", json={"approved_by": "approver"}).json()
    assert v2["version_number"] == 2
    assert [(v["version_number"], v["status"]) for v in _versions(client, v1["workflow_definition_id"])] == [
        (1, "superseded"),
        (2, "approved"),
    ]
    # Past executions still reference exactly what they ran.
    old = client.get(f"/workflows/executions/{execution['id']}").json()
    assert [t["call"] for t in old["document_snapshot"]] == ["error_logs", "recent_deployments"]


def test_graph_edit_with_tasks_payload(client):
    v1 = _publish(client, [ERROR_LOGS])
    # Graph drafts carry the base document's pin fields through; the server re-stamps them.
    tasks = [dict(v1["document"][0]), DEPLOYS]
    edited = _edit(client, v1, tasks=tasks)
    assert edited.status_code == 201, edited.text
    assert [t["call"] for t in edited.json()["generated_document"]] == ["error_logs", "recent_deployments"]


def test_edit_requires_exactly_one_source(client):
    v1 = _publish(client, [ERROR_LOGS])
    assert _edit(client, v1).status_code == 422
    assert _edit(client, v1, yaml=_yaml_for([ERROR_LOGS]), tasks=[ERROR_LOGS]).status_code == 422


def test_invalid_edit_saves_nothing(client, postgres_url):
    v1 = _publish(client, [ERROR_LOGS])
    bad_syntax = _edit(client, v1, yaml="do: [unclosed\n")
    assert bad_syntax.status_code == 422 and bad_syntax.json()["detail"]["kind"] == "syntax"
    bad_call = _edit(client, v1, yaml=_yaml_for([{"call": "not_real", "with": {}}]))
    assert bad_call.status_code == 422 and bad_call.json()["detail"]["kind"] == "registry"

    engine = make_engine(postgres_url)
    with make_session_factory(engine)() as session:
        assert len(session.scalars(select(WorkflowBuildRequest)).all()) == 1  # only v1's own build
    engine.dispose()


def test_edit_of_superseded_base_is_a_conflict(client):
    v1 = _publish(client, [ERROR_LOGS])
    _publish(client, [DEPLOYS])  # v2 published by someone else
    response = _edit(client, v1, yaml=_yaml_for([ERROR_LOGS]))
    assert response.status_code == 409
    assert "newer version" in response.json()["detail"]


def test_approving_a_stale_edit_is_a_conflict(client):
    v1 = _publish(client, [ERROR_LOGS])
    stale = _edit(client, v1, yaml=_yaml_for([DEPLOYS])).json()
    _publish(client, [ERROR_LOGS, DEPLOYS])  # v2 lands while the edit waits for approval

    response = client.post(f"/workflows/build-requests/{stale['id']}/approve", json={"approved_by": "approver"})
    assert response.status_code == 409
    versions = _versions(client, v1["workflow_definition_id"])
    assert [(v["version_number"], v["status"]) for v in versions] == [(1, "superseded"), (2, "approved")]


def test_edit_on_unknown_definition_is_404(client):
    v1 = _publish(client, [ERROR_LOGS])
    response = client.post(
        "/workflows/definitions/NOPE/edits",
        json={"base_version_id": v1["id"], "edited_by": "e", "yaml": _yaml_for([ERROR_LOGS])},
    )
    assert response.status_code == 404


def test_unchanged_tasks_keep_their_pin_changed_tasks_repin(client):
    v1 = _publish(client, [ERROR_LOGS, DEPLOYS])
    assert [t["function_version_number"] for t in v1["document"]] == [1, 1]

    # Every function moves to v2 (all have v2 implementations).
    for name in ("error_logs", "recent_deployments", "apm_traces"):
        FUNCTION_REGISTRY[name] = FUNCTION_REGISTRY[name].model_copy(update={"version_number": 2})

    changed_deploys = {**DEPLOYS, "with": {**DEPLOYS["with"], "lookback_minutes": 60}}
    new_task = {"call": "apm_traces", "with": {"env": "NPE", "app": "TESTAPP", "lookback_minutes": 30}}
    edited = _edit(client, v1, yaml=_yaml_for([ERROR_LOGS, changed_deploys, new_task]))
    assert edited.status_code == 201, edited.text
    pins = [(t["call"], t["function_version_number"]) for t in edited.json()["generated_document"]]
    assert pins == [("error_logs", 1), ("recent_deployments", 2), ("apm_traces", 2)]

    # The validate preview pins identically when given the same base.
    preview = client.post(
        "/workflows/validate-yaml",
        json={"yaml": _yaml_for([ERROR_LOGS, changed_deploys, new_task]), "base_version_id": v1["id"]},
    ).json()
    assert [t["function_version_number"] for t in preview["tasks"]] == [1, 2, 2]


def test_edited_version_executes(client, postgres_url):
    v1 = _publish(client, [ERROR_LOGS])
    request = _edit(client, v1, yaml=_yaml_for([ERROR_LOGS, DEPLOYS])).json()
    v2 = client.post(f"/workflows/build-requests/{request['id']}/approve", json={"approved_by": "a"}).json()
    run = client.post(f"/workflows/versions/{v2['id']}/execute", json={})
    assert run.status_code == 200, run.text
    assert [e["check"] for e in run.json()["evidence"]] == ["error_logs", "recent_deployments"]

    engine = make_engine(postgres_url)
    with make_session_factory(engine)() as session:
        assert session.scalars(select(WorkflowExecution)).first() is not None
    engine.dispose()
