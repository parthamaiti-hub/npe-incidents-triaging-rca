"""Sol-104 -- check_type versioning: new check_types, new versions, pins
per playbook version, lifecycle (draft/active/deprecated/retired), upgrade,
dry-run, integrity, and a catalog loader that never re-pins on its own."""

import pytest
from fastapi.testclient import TestClient

from app.check_implementations import CHECK_IMPLEMENTATIONS, CHECK_IMPLEMENTATIONS_V2
from app.checks import has_implementation, implemented_versions, run_check
from app.function_registry import (
    FUNCTION_REGISTRY,
    FUNCTION_VERSIONS,
    DEFAULT_FUNCTION_REGISTRY,
    RetryPolicy,
)
from app.main import create_app
from app.models import FunctionDefinitionVersion, SourceSystem, WorkflowDefinitionVersion, WorkflowExecution
from app.playbook_engine import execute_workflow
from dataloadscripts.load_catalog import DEFAULT_CATALOG_PATH, load_catalog_file, upsert_catalog
from dataloadscripts.load_function_registry import upsert_function_registry
from dataloadscripts.seed_functional_dummy_versions import publish_functional_dummy_versions
from dataloadscripts.test_fixtures import find_docs, insert_docs

CATEGORY = "FUNCTIONAL DEFECT (QA/UAT)"
ERROR_LOGS = {"call": "error_logs", "with": {"env": "NPE", "app": "TESTAPP", "lookback_minutes": 240}}
DEPLOYS = {"call": "recent_deployments", "with": {"env": "NPE", "app": "TESTAPP", "lookback_minutes": 1440}}

ERROR_LOGS_PARAMS = [  # error_logs' real v1/v2 contract
    {"name": "env", "type": "string", "required": True},
    {"name": "app", "type": "string", "required": True},
    {"name": "lookback_minutes", "type": "int", "required": True},
    {"name": "severity_levels", "type": "list[string]", "required": False},
]


async def _v3_run(params: dict) -> dict:
    return {"status": "WARN", "details": f"[v3] index={params.get('index')}"}


# --------------------------------------------------------------------------
# Implementation registry (no containers)
# --------------------------------------------------------------------------


def test_modules_are_discovered_by_filename():
    assert ("error_logs", 2) in CHECK_IMPLEMENTATIONS
    assert len(CHECK_IMPLEMENTATIONS_V2) == 20
    assert all(version >= 1 for _, version in CHECK_IMPLEMENTATIONS)


async def test_a_new_version_runs_without_any_dispatcher_change(monkeypatch):
    assert has_implementation("error_logs", 3) is False
    monkeypatch.setitem(CHECK_IMPLEMENTATIONS, ("error_logs", 3), _v3_run)

    assert has_implementation("error_logs", 3) is True
    assert implemented_versions("error_logs") == [1, 2, 3]
    result = await run_check("error_logs", {"index": "app-logs"}, version_number=3)
    assert result == {"status": "WARN", "details": "[v3] index=app-logs"}
    # v1/v2 are untouched
    assert "FUNCTIONAL DUMMY v2" in (await run_check("error_logs", {}, version_number=2))["details"]


async def test_a_brand_new_check_type_needs_only_its_module(monkeypatch):
    async def run(params):
        return {"status": "OK", "details": "queue depth fine"}

    with pytest.raises(ValueError, match="Unknown check_type"):
        await run_check("queue_depth", {}, version_number=1)
    monkeypatch.setitem(CHECK_IMPLEMENTATIONS, ("queue_depth", 1), run)
    assert (await run_check("queue_depth", {}, version_number=1))["details"] == "queue depth fine"


async def test_engine_retries_per_the_pinned_contract_not_the_active_one():
    """Sol-104 V5: publishing a new version (with a different retry
    policy) must not change how an already-pinned task behaves."""
    v1 = DEFAULT_FUNCTION_REGISTRY["error_logs"].model_copy(
        update={"version_number": 1, "status": "deprecated", "default_retry": RetryPolicy(max_attempts=2, delay_seconds=0)}
    )
    v5 = v1.model_copy(update={"version_number": 5, "status": "active", "default_retry": RetryPolicy(max_attempts=1, delay_seconds=0)})
    FUNCTION_VERSIONS[("error_logs", 1)] = v1
    FUNCTION_VERSIONS[("error_logs", 5)] = v5
    FUNCTION_REGISTRY["error_logs"] = v5

    attempts = {"n": 0}

    async def always_fails(call, params, version_number):
        attempts["n"] += 1
        raise RuntimeError("down")

    evidence = await execute_workflow([{**ERROR_LOGS, "function_version_number": 1}], dispatch=always_fails)
    assert attempts["n"] == 2  # v1's policy, not v5's single attempt
    assert evidence[0]["version"] == 1

    # A retry snapshot stored in the task wins over any contract lookup.
    attempts["n"] = 0
    snapshot = {"max_attempts": 3, "delay_seconds": 0, "exponential_backoff": False}
    await execute_workflow([{**ERROR_LOGS, "function_version_number": 1, "resolved_retry": snapshot}], dispatch=always_fails)
    assert attempts["n"] == 3


# --------------------------------------------------------------------------
# API fixtures
# --------------------------------------------------------------------------


@pytest.fixture()
def seeded(sync_db):
    """The normal startup state: every check_type with v1 (template) and an
    active v2 (functional dummy)."""
    upsert_function_registry(sync_db)
    publish_functional_dummy_versions(sync_db)
    insert_docs(
        sync_db,
        SourceSystem(
            id="SYS_TESTAPP", name="Test App", code="TESTAPP", type="Application", description="x",
            owning_team="x", environment="NPE",
        ),
    )
    return sync_db


@pytest.fixture()
def client(seeded, mongo_url, mongo_db_name, redis_url, rabbitmq_url, vector_store):
    app = create_app(
        mongodb_url=mongo_url, mongodb_db=mongo_db_name, redis_url=redis_url, rabbitmq_url=rabbitmq_url,
        vector_store=vector_store,
    )
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def error_logs_v3_code(monkeypatch):
    monkeypatch.setitem(CHECK_IMPLEMENTATIONS, ("error_logs", 3), _v3_run)


def _publish_function_version(client, name, params, status="active", default_retry=None) -> dict:
    response = client.post(
        f"/functions/{name}/versions",
        json={
            "name": name, "description": f"{name} next", "params": params,
            "default_retry": default_retry or {}, "created_by": "t", "status": status,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _build(client, functions):
    return client.post(
        "/workflows/build-requests",
        json={"source_system_id": "SYS_TESTAPP", "category": CATEGORY, "requested_functions": functions, "requested_by": "u"},
    )


def _approve(client, request_id):
    return client.post(f"/workflows/build-requests/{request_id}/approve", json={"approved_by": "a"})


def _publish_playbook(client, functions) -> dict:
    request = _build(client, functions)
    assert request.status_code == 201, request.text
    version = _approve(client, request.json()["id"])
    assert version.status_code == 200, version.text
    return version.json()


def _pins(document):
    return [(t["call"], t["function_version_number"]) for t in document]


# --------------------------------------------------------------------------
# U1: a new check_type
# --------------------------------------------------------------------------


def test_new_check_type_end_to_end(client, monkeypatch):
    created = client.post(
        "/functions",
        json={
            "name": "queue_depth", "description": "Depth of a queue",
            "params": [{"name": "env", "type": "string", "required": True}], "created_by": "t",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["has_implementation"] is False

    task = {"call": "queue_depth", "with": {"env": "NPE"}}
    refused = _build(client, [task])
    assert refused.status_code == 422
    assert "app/check_types/queue_depth_v1.py" in refused.json()["detail"]

    async def run(params):
        return {"status": "OK", "details": f"queue fine in {params['env']}"}

    monkeypatch.setitem(CHECK_IMPLEMENTATIONS, ("queue_depth", 1), run)
    playbook = _publish_playbook(client, [task])
    assert _pins(playbook["document"]) == [("queue_depth", 1)]

    run_result = client.post(f"/workflows/versions/{playbook['id']}/execute", json={}).json()
    assert run_result["evidence"][0]["version"] == 1
    assert run_result["evidence"][0]["details"] == "queue fine in NPE"


@pytest.mark.parametrize("bad_name", ["Bad-Name", "queue_depth_v2", "2fast"])
def test_new_check_type_name_must_map_to_a_module_file(client, bad_name):
    response = client.post(
        "/functions", json={"name": bad_name, "description": "x", "params": [], "created_by": "t"}
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------
# U2-U4: a new version; old playbooks stay, new playbooks get it
# --------------------------------------------------------------------------


def test_old_playbook_keeps_its_version_and_edits_validate_against_the_pinned_contract(client, error_logs_v3_code):
    v1 = _publish_playbook(client, [ERROR_LOGS, DEPLOYS])
    assert _pins(v1["document"]) == [("error_logs", 2), ("recent_deployments", 2)]

    # error_logs v3 adds a *required* param.
    _publish_function_version(client, "error_logs", [*ERROR_LOGS_PARAMS, {"name": "index", "type": "string", "required": True}])

    # Editing only recent_deployments must not trip over v3's new param:
    # the untouched error_logs task stays on v2 and is checked against v2.
    changed_deploys = {**DEPLOYS, "with": {**DEPLOYS["with"], "lookback_minutes": 60}}
    edit = client.post(
        f"/workflows/definitions/{v1['workflow_definition_id']}/edits",
        json={"base_version_id": v1["id"], "edited_by": "e", "tasks": [ERROR_LOGS, changed_deploys]},
    )
    assert edit.status_code == 201, edit.text
    assert _pins(edit.json()["generated_document"]) == [("error_logs", 2), ("recent_deployments", 2)]

    # A new playbook gets the active v3 -- and so needs its new param.
    missing = _build(client, [ERROR_LOGS])
    assert missing.status_code == 422 and "index" in missing.json()["detail"]
    with_index = _build(client, [{**ERROR_LOGS, "with": {**ERROR_LOGS["with"], "index": "app"}}])
    assert with_index.status_code == 201, with_index.text
    assert _pins(with_index.json()["generated_document"]) == [("error_logs", 3)]

    # ... and the approved v1 still runs v2 code.
    run_result = client.post(f"/workflows/versions/{v1['id']}/execute", json={}).json()
    assert run_result["evidence"][0]["version"] == 2
    assert "FUNCTIONAL DUMMY v2" in run_result["evidence"][0]["details"]


def test_explicit_version_pin_and_retry_snapshot(client, error_logs_v3_code):
    _publish_function_version(client, "error_logs", ERROR_LOGS_PARAMS, default_retry={"max_attempts": 1})
    playbook = _publish_playbook(client, [{**ERROR_LOGS, "version": 2}, DEPLOYS])
    assert _pins(playbook["document"]) == [("error_logs", 2), ("recent_deployments", 2)]
    assert "version" not in playbook["document"][0]  # authored field, never stored
    # v2's own default retry is snapshotted into the task
    assert playbook["document"][0]["resolved_retry"] == RetryPolicy().model_dump()

    yaml_text = client.get(f"/workflows/versions/{playbook['id']}/yaml").json()["yaml"]
    assert "version: 2" in yaml_text and "resolved_retry" not in yaml_text

    nonexistent = _build(client, [{**ERROR_LOGS, "version": 9}])
    assert nonexistent.status_code == 422 and "no version 9" in nonexistent.json()["detail"]


# --------------------------------------------------------------------------
# Lifecycle: draft -> dry-run -> activate; retire; delete guard
# --------------------------------------------------------------------------


def test_draft_version_can_be_dry_run_but_not_approved_until_activated(client, seeded, error_logs_v3_code):
    draft = _publish_function_version(client, "error_logs", ERROR_LOGS_PARAMS, status="draft")
    assert draft["status"] == "draft" and draft["version_number"] == 3
    assert client.get("/functions/error_logs").json()["version_number"] == 2  # active unchanged
    assert _pins(_build(client, [ERROR_LOGS]).json()["generated_document"]) == [("error_logs", 2)]

    request = _build(client, [{**ERROR_LOGS, "version": 3}])
    assert request.status_code == 201, request.text
    request_id = request.json()["id"]

    dry = client.post(f"/workflows/build-requests/{request_id}/dry-run")
    assert dry.status_code == 200, dry.text
    assert dry.json()["evidence"][0]["version"] == 3
    assert dry.json()["evidence"][0]["details"].startswith("[v3]")
    assert "rca_status" in dry.json()["rca"]
    assert find_docs(seeded, WorkflowExecution) == []  # nothing persisted

    refused = _approve(client, request_id)
    assert refused.status_code == 422
    assert "draft" in refused.json()["detail"]["message"]

    activated = client.post("/functions/error_logs/versions/3/activate", json={"changed_by": "t"})
    assert activated.status_code == 200 and activated.json()["status"] == "active"
    statuses = {v["version_number"]: v["status"] for v in client.get("/functions/error_logs/versions").json()}
    assert statuses == {1: "deprecated", 2: "deprecated", 3: "active"}

    assert _approve(client, request_id).status_code == 200


def test_retire_is_refused_while_pinned_and_blocks_new_pins_after(client, error_logs_v3_code):
    playbook = _publish_playbook(client, [ERROR_LOGS])  # pins error_logs v2
    _publish_function_version(client, "error_logs", ERROR_LOGS_PARAMS)  # v3 active, v2 deprecated

    assert client.post("/functions/error_logs/versions/3/retire", json={"changed_by": "t"}).status_code == 409  # active

    usage = client.get("/functions/error_logs/versions/2/usage").json()
    assert [u["workflow_version_id"] for u in usage["playbook_versions"]] == [playbook["id"]]
    assert usage["playbook_versions"][0]["source_system_id"] == "SYS_TESTAPP"
    pinned = client.post("/functions/error_logs/versions/2/retire", json={"changed_by": "t"})
    assert pinned.status_code == 409
    assert pinned.json()["detail"]["usage"]["playbook_versions"][0]["workflow_version_id"] == playbook["id"]

    # v1 is pinned by nothing -> can be retired, and then can't be pinned.
    retired = client.post("/functions/error_logs/versions/1/retire", json={"changed_by": "t"})
    assert retired.status_code == 200 and retired.json()["status"] == "retired"
    refused = _build(client, [{**ERROR_LOGS, "version": 1}])
    assert refused.status_code == 422 and "retired" in refused.json()["detail"]
    assert client.post("/functions/error_logs/versions/1/activate", json={"changed_by": "t"}).status_code == 409


def test_delete_is_refused_for_a_function_any_playbook_uses(client):
    _publish_playbook(client, [ERROR_LOGS])
    response = client.delete("/functions/error_logs")
    assert response.status_code == 409
    assert "retire" in response.json()["detail"]


# --------------------------------------------------------------------------
# U5: upgrading a playbook to a newer check version
# --------------------------------------------------------------------------


def test_pins_report_and_upgrade_of_selected_tasks(client, error_logs_v3_code):
    v1 = _publish_playbook(client, [ERROR_LOGS, DEPLOYS])
    _publish_function_version(client, "error_logs", [*ERROR_LOGS_PARAMS, {"name": "index", "type": "string", "required": False}])

    pins = client.get(f"/workflows/versions/{v1['id']}/pins").json()["tasks"]
    by_call = {p["call"]: p for p in pins}
    assert by_call["error_logs"]["state"] == "upgrade_available"
    assert by_call["error_logs"]["active_version"] == 3
    assert by_call["error_logs"]["contract_diff"] == {"added": ["index"], "removed": [], "newly_required": []}
    assert by_call["recent_deployments"]["state"] == "up_to_date"

    upgrade = client.post(
        f"/workflows/definitions/{v1['workflow_definition_id']}/upgrade",
        json={"base_version_id": v1["id"], "calls": ["error_logs"], "requested_by": "u"},
    )
    assert upgrade.status_code == 201, upgrade.text
    assert _pins(upgrade.json()["generated_document"]) == [("error_logs", 3), ("recent_deployments", 2)]
    assert upgrade.json()["base_version_id"] == v1["id"]

    v2 = _approve(client, upgrade.json()["id"]).json()
    assert v2["version_number"] == 2
    # v1 (now superseded) still pins v2 code and stays runnable via Retry.
    versions = client.get(f"/workflows/definitions/{v1['workflow_definition_id']}/versions").json()
    assert [(v["version_number"], v["status"], _pins(v["document"])[0]) for v in versions] == [
        (1, "superseded", ("error_logs", 2)),
        (2, "approved", ("error_logs", 3)),
    ]


def test_upgrade_is_refused_when_the_new_contract_needs_new_params(client, error_logs_v3_code):
    v1 = _publish_playbook(client, [ERROR_LOGS])
    _publish_function_version(client, "error_logs", [*ERROR_LOGS_PARAMS, {"name": "index", "type": "string", "required": True}])

    pins = client.get(f"/workflows/versions/{v1['id']}/pins").json()["tasks"]
    assert pins[0]["contract_diff"]["newly_required"] == ["index"]

    upgrade = client.post(
        f"/workflows/definitions/{v1['workflow_definition_id']}/upgrade",
        json={"base_version_id": v1["id"], "calls": "all", "requested_by": "u"},
    )
    assert upgrade.status_code == 422
    assert "index" in upgrade.json()["detail"]["message"]

    unknown = client.post(
        f"/workflows/definitions/{v1['workflow_definition_id']}/upgrade",
        json={"base_version_id": v1["id"], "calls": ["apm_traces"], "requested_by": "u"},
    )
    assert unknown.status_code == 422


# --------------------------------------------------------------------------
# Integrity
# --------------------------------------------------------------------------


def test_integrity_reports_pinned_versions_that_cannot_run(client, monkeypatch):
    _publish_playbook(client, [ERROR_LOGS])
    assert client.get("/functions/integrity").json() == {"ok": True, "problems": []}

    monkeypatch.delitem(CHECK_IMPLEMENTATIONS, ("error_logs", 2))  # e.g. the module was deleted
    report = client.get("/functions/integrity").json()
    assert report["ok"] is False
    codes = {(p["code"], p["check_type"], p["version_number"]) for p in report["problems"]}
    assert ("pinned_version_no_implementation", "error_logs", 2) in codes
    assert ("active_without_implementation", "error_logs", 2) in codes


# --------------------------------------------------------------------------
# Catalog loader
# --------------------------------------------------------------------------


def _activate_error_logs_v3(db, monkeypatch, status="active"):
    """What POST /functions/error_logs/versions does, for the sync loader."""
    monkeypatch.setitem(CHECK_IMPLEMENTATIONS, ("error_logs", 3), _v3_run)
    if status == "active":
        db[FunctionDefinitionVersion.COLLECTION].update_many(
            {"function_definition_id": "error_logs", "status": "active"}, {"$set": {"status": "deprecated"}}
        )
    insert_docs(
        db,
        FunctionDefinitionVersion(
            function_definition_id="error_logs", version_number=3, description="v3", params=ERROR_LOGS_PARAMS,
            default_retry={}, status=status, created_by="t",
        ),
    )


def _samson_func_versions(db):
    return find_docs(db, WorkflowDefinitionVersion, {"workflow_definition_id": "RCA_SAMSON_FUNC"}, sort=[("version_number", 1)])


def test_loader_pins_to_the_databases_active_version(seeded):
    upsert_catalog(seeded, load_catalog_file(DEFAULT_CATALOG_PATH))
    documents = [v.document for v in find_docs(seeded, WorkflowDefinitionVersion)]
    assert {t["function_version_number"] for doc in documents for t in doc} == {2}
    assert all(t["function_version_id"] for doc in documents for t in doc)


def test_loader_reload_after_a_new_check_version_creates_no_playbook_versions(seeded, monkeypatch):
    catalog = load_catalog_file(DEFAULT_CATALOG_PATH)
    upsert_catalog(seeded, catalog)
    _activate_error_logs_v3(seeded, monkeypatch)

    upsert_catalog(seeded, catalog)
    assert seeded[WorkflowDefinitionVersion.COLLECTION].count_documents({}) == 10
    assert ("error_logs", 2) in _pins(_samson_func_versions(seeded)[-1].document)

    # Explicit upgrade of one call re-pins only those steps.
    upsert_catalog(seeded, catalog, upgrade_calls={"error_logs"})
    versions = _samson_func_versions(seeded)
    assert [v.status for v in versions] == ["superseded", "approved"]
    assert _pins(versions[-1].document) == [
        ("error_logs", 3), ("apm_traces", 2), ("recent_deployments", 2), ("dependent_services_health", 2),
    ]


def test_loader_keeps_pre_existing_v1_pins_when_functions_are_seeded_later(sync_db):
    """The start.ps1 order on a fresh database: catalogs load before the
    function registry exists (pins = built-in v1), then v2 is published.
    A later reload must not silently move every playbook to v2."""
    catalog = load_catalog_file(DEFAULT_CATALOG_PATH)
    upsert_catalog(sync_db, catalog)
    upsert_function_registry(sync_db)
    publish_functional_dummy_versions(sync_db)

    upsert_catalog(sync_db, catalog)
    versions = find_docs(sync_db, WorkflowDefinitionVersion)
    assert len(versions) == 10
    assert {t["function_version_number"] for v in versions for t in v.document} == {1}


def test_loader_explicit_version_and_draft_refusal(seeded, monkeypatch):
    catalog = load_catalog_file(DEFAULT_CATALOG_PATH)
    _activate_error_logs_v3(seeded, monkeypatch, status="draft")

    pinned = catalog.model_copy(deep=True)
    samson = next(p for p in pinned.rca_playbooks if p.id == "RCA_SAMSON_FUNC")
    samson.steps[0].version = 1
    upsert_catalog(seeded, pinned)
    assert _pins(_samson_func_versions(seeded)[-1].document)[0] == ("error_logs", 1)

    samson.steps[0].version = 3  # a draft: a loaded playbook is approved directly
    with pytest.raises(ValueError, match="draft"):
        upsert_catalog(seeded, pinned)
