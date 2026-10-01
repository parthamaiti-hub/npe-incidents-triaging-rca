"""DEFAULT RCA playbooks: the per-system fallback when an incident's
category can't be mapped (or has no playbook), marked triage_mode=DefaultRCA,
and fixed by changing the mapping rule + Retry."""

import pydantic
import pytest
import yaml
from fastapi.testclient import TestClient

from app.categories import DEFAULT_CATEGORY
from app.main import create_app
from app.models import Incident, WorkflowDefinition, WorkflowDefinitionVersion
from app.schemas import IncidentMappingRuleIn
from dataloadscripts.check_catalog import check_catalog
from dataloadscripts.load_catalog import DEFAULT_CATALOG_PATH, load_catalog_file, upsert_catalog
from dataloadscripts.load_default_playbooks import DEFAULT_LOADER, load_default_playbooks
from dataloadscripts.test_fixtures import find_docs, insert_docs

DATA = "DATA QUALITY / TEST DATA"


@pytest.fixture()
def demo_db(sync_db):
    upsert_catalog(sync_db, load_catalog_file(DEFAULT_CATALOG_PATH))
    return sync_db


def _definition(db, system_id):
    return db[WorkflowDefinition.COLLECTION].find_one({"source_system_id": system_id, "category": DEFAULT_CATEGORY})


def _versions(db, playbook_id):
    return find_docs(db, WorkflowDefinitionVersion, {"workflow_definition_id": playbook_id}, sort=[("version_number", 1)])


# -- the upload script -------------------------------------------------------


def test_loader_creates_one_default_playbook_per_system_like_the_func_playbook(demo_db):
    report = load_default_playbooks(demo_db)
    assert sorted(report["created"]) == ["RCA_DAS_DEFAULT", "RCA_DCD_DEFAULT", "RCA_DO_DEFAULT", "RCA_DPS_DEFAULT", "RCA_SAMSON_DEFAULT"]
    assert report["skipped"] == [] and report["findings"] == []

    assert _definition(demo_db, "SYS_DCD")["_id"] == "RCA_DCD_DEFAULT"
    [version] = _versions(demo_db, "RCA_DCD_DEFAULT")
    assert version.status == "approved" and version.created_by == DEFAULT_LOADER
    assert [t["call"] for t in version.document] == ["error_logs", "recent_deployments", "apm_traces", "dependent_services_health"]
    assert {t["with"]["app"] for t in version.document} == {"DCD"}  # {code} filled per system


def test_loader_is_idempotent_and_reports_template_changes(demo_db, tmp_path):
    load_default_playbooks(demo_db)
    again = load_default_playbooks(demo_db)
    assert again["created"] == [] and again["updated"] == [] and len(again["unchanged"]) == 5

    template = tmp_path / "template.yaml"
    template.write_text(yaml.safe_dump({"steps": [{"call": "error_logs", "with": {"env": "NPE", "app": "{code}", "lookback_minutes": 60}}]}))
    changed = load_default_playbooks(demo_db, template_path=template, system_ids=["SYS_DCD"])
    assert changed["updated"] == ["RCA_DCD_DEFAULT"]
    assert [t["with"]["lookback_minutes"] for t in _versions(demo_db, "RCA_DCD_DEFAULT")[-1].document] == [60]


def test_loader_leaves_a_customized_default_playbook_alone_unless_forced(demo_db):
    load_default_playbooks(demo_db)
    # An operator edited SYS_DCD's DEFAULT playbook in the UI (a newer version, by someone else).
    demo_db[WorkflowDefinitionVersion.COLLECTION].update_many(
        {"workflow_definition_id": "RCA_DCD_DEFAULT"}, {"$set": {"status": "superseded"}}
    )
    insert_docs(
        demo_db,
        WorkflowDefinitionVersion(
            id="RCA_DCD_DEFAULT_v2", workflow_definition_id="RCA_DCD_DEFAULT", version_number=2,
            document=[{"call": "error_logs", "with": {"env": "NPE", "app": "DCD", "lookback_minutes": 5}, "function_version_number": 1}],
            status="approved", source="dynamic_generated", created_by="operator@x",
        ),
    )
    report = load_default_playbooks(demo_db)
    assert [s for s, _ in report["skipped"]] == ["SYS_DCD"]
    assert len(_versions(demo_db, "RCA_DCD_DEFAULT")) == 2

    forced = load_default_playbooks(demo_db, system_ids=["SYS_DCD"], force=True)
    assert forced["updated"] == ["RCA_DCD_DEFAULT"]
    assert _versions(demo_db, "RCA_DCD_DEFAULT")[-1].created_by == DEFAULT_LOADER


def test_loader_rejects_unknown_systems(demo_db):
    with pytest.raises(ValueError, match="SYS_NOPE"):
        load_default_playbooks(demo_db, system_ids=["SYS_NOPE"])


# -- reserved category -------------------------------------------------------


def test_a_mapping_rule_cannot_target_default():
    with pytest.raises(pydantic.ValidationError, match="DEFAULT"):
        IncidentMappingRuleIn(
            id="IMR_X", source_system_id="SYS_DCD", category=DEFAULT_CATEGORY, signal_type="keyword",
            signal_pattern="x", priority=1, action="x",
        )


def test_catalog_check_understands_default_playbooks(demo_db):
    load_default_playbooks(demo_db)
    real = load_catalog_file(DEFAULT_CATALOG_PATH.parent / "npe_real_source_systems.yaml")
    findings = check_catalog(real, demo_db)
    assert not any(f.code == "unreachable_playbook" for f in findings)  # DEFAULT playbooks aren't "unreachable"

    demo = load_catalog_file(DEFAULT_CATALOG_PATH)
    demo.rca_playbooks.append(demo.rca_playbooks[0].model_copy(update={"id": "RCA_DCD_TYPO", "category": "Default"}))
    assert any(f.code == "category_spelling" and "reserved 'DEFAULT'" in f.message for f in check_catalog(demo))


def test_route_without_playbook_names_the_fallback(demo_db):
    load_default_playbooks(demo_db)
    demo = load_catalog_file(DEFAULT_CATALOG_PATH)
    demo.incident_mapping_rules.append(
        IncidentMappingRuleIn(
            id="IMR_DCD_PERF", source_system_id="SYS_DCD", category="PERFORMANCE / TIMEOUT", signal_type="keyword",
            signal_pattern="dcd timed out", priority=1, action="x",
        )
    )
    [finding] = [f for f in check_catalog(demo, demo_db) if f.code == "route_without_playbook"]
    assert "RCA_DCD_DEFAULT (DefaultRCA)" in finding.message


# -- the RCA path ------------------------------------------------------------


@pytest.fixture()
def client(demo_db, mongo_url, mongo_db_name, redis_url, rabbitmq_url, vector_store, opa_url, monkeypatch):
    import app.opa_client as opa_client_module

    monkeypatch.setattr(opa_client_module, "OPA_URL", opa_url)  # real rule classification on retry
    app = create_app(
        mongodb_url=mongo_url, mongodb_db=mongo_db_name, redis_url=redis_url, rabbitmq_url=rabbitmq_url,
        vector_store=vector_store,
    )
    with TestClient(app) as c:
        yield c


def _incident(db, key, text):
    insert_docs(db, Incident(id=f"INC-{key}", source="jira", external_id=key, jira_key=key, incident_key=key, raw_text=text))


def _retry(client, key, **body):
    response = client.post(f"/incidents/{key}/retry", json={"requested_by": "u", **body})
    assert response.status_code == 200, response.text
    return response.json()


def test_unmapped_category_runs_default_then_fixed_rule_and_retry_runs_the_mapped_playbook(client, demo_db):
    load_default_playbooks(demo_db)
    _incident(demo_db, "TT-1", "DCD dashboard shows wrong totals")  # only IMR_DCD_KEY_FALLBACK (ANY) matches

    first = _retry(client, "TT-1")
    assert first["triage_mode"] == "DefaultRCA"
    assert first["workflow_definition_version_id"] == _versions(demo_db, "RCA_DCD_DEFAULT")[-1].id
    assert "category couldn't be mapped" in first["triage_note"] and "IMR_DCD_KEY_FALLBACK" in first["triage_note"]
    assert [e["check"] for e in first["evidence"]] == ["error_logs", "recent_deployments", "apm_traces", "dependent_services_health"]
    assert first["rca_status"] is not None

    dashboard = client.get("/incidents/dashboard?triage_mode=DefaultRCA").json()
    assert [row["incident_key"] for row in dashboard["items"]] == ["TT-1"]
    assert client.get("/stats/incidents?period=all").json()["default_rca"] == 1

    # The operator adds a mapping rule for this kind of incident, then retries.
    rule = client.post(
        "/catalog/mapping-rules",
        json={"id": "IMR_DCD_WRONG_TOTALS", "source_system_id": "SYS_DCD", "category": DATA, "signal_type": "keyword",
              "signal_pattern": "wrong totals", "priority": 1, "action": "assign_to_DCD"},
    )
    assert rule.status_code == 201, rule.text

    second = _retry(client, "TT-1")
    assert second["triage_mode"] == "Mapped" and second["triage_note"] is None
    assert second["workflow_definition_version_id"] == _versions(demo_db, "RCA_DCD_DATA")[-1].id
    assert client.get("/stats/incidents?period=all").json()["default_rca"] == 0


def test_mapped_category_without_a_playbook_falls_back_to_default(client, demo_db):
    load_default_playbooks(demo_db)
    client.post(
        "/catalog/mapping-rules",
        json={"id": "IMR_DCD_PERF", "source_system_id": "SYS_DCD", "category": "PERFORMANCE / TIMEOUT",
              "signal_type": "keyword", "signal_pattern": "timed out", "priority": 1, "action": "assign_to_DCD"},
    )
    _incident(demo_db, "TT-2", "DCD export timed out")

    run = _retry(client, "TT-2")
    assert run["triage_mode"] == "DefaultRCA"
    assert "No playbook for (SYS_DCD, PERFORMANCE / TIMEOUT)" in run["triage_note"]


def test_without_a_default_playbook_the_old_outcomes_are_unchanged(client, demo_db):
    load_default_playbooks(demo_db, system_ids=["SYS_DCD"])  # SYS_DAS gets none
    _incident(demo_db, "TT-3", "DAS report is blank")  # IMR_DAS_KEY_FALLBACK (ANY)
    _incident(demo_db, "TT-4", "something broke, please check")  # no rule at all

    das = _retry(client, "TT-3")
    assert das["triage_mode"] is None and das["rca"]["matched_pattern"] == "not_classified"
    unknown = _retry(client, "TT-4")  # system unknown: no DEFAULT can apply
    assert unknown["triage_mode"] is None and unknown["rca"]["matched_pattern"] == "not_classified"


def test_mapped_and_override_modes(client, demo_db):
    load_default_playbooks(demo_db)
    _incident(demo_db, "TT-5", "table DSNADEV.dcd_billing_summary has nulls")  # IMR_DCD_TABLE_DATA

    mapped = _retry(client, "TT-5")
    assert mapped["triage_mode"] == "Mapped" and mapped["mapping_overridden"] is False

    default_version = _versions(demo_db, "RCA_DCD_DEFAULT")[-1].id
    override = _retry(client, "TT-5", workflow_definition_version_id=default_version)
    assert override["triage_mode"] == "Override" and override["mapping_overridden"] is True
