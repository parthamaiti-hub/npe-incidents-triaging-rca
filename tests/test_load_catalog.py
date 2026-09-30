import re

import pydantic
import pytest

from app.models import (
    IncidentMappingRule,
    SourceSystem,
    SystemFootprint,
    WorkflowDefinition,
    WorkflowDefinitionVersion,
)
from dataloadscripts.load_catalog import DEFAULT_CATALOG_PATH, load_catalog_file, upsert_catalog
from dataloadscripts.test_fixtures import find_docs, insert_docs


def _count(db, model) -> int:
    return db[model.COLLECTION].count_documents({})


@pytest.fixture()
def catalog():
    return load_catalog_file(DEFAULT_CATALOG_PATH)


def test_row_counts_match_yaml(catalog):
    assert len(catalog.source_systems) == 5
    assert len(catalog.system_footprints) == 17
    assert len(catalog.incident_mapping_rules) == 14
    assert len(catalog.rca_playbooks) == 10


def test_all_signal_patterns_and_footprint_values_compile(catalog):
    for footprint in catalog.system_footprints:
        re.compile(footprint.value)
    for rule in catalog.incident_mapping_rules:
        re.compile(rule.signal_pattern)


def test_load_persists_expected_row_counts(sync_db, catalog):
    upsert_catalog(sync_db, catalog)

    assert _count(sync_db, SourceSystem) == 5
    assert _count(sync_db, SystemFootprint) == 17
    assert _count(sync_db, IncidentMappingRule) == 14
    assert _count(sync_db, WorkflowDefinition) == 10
    assert _count(sync_db, WorkflowDefinitionVersion) == 10


def test_load_is_idempotent(sync_db, catalog):
    upsert_catalog(sync_db, catalog)
    upsert_catalog(sync_db, catalog)

    assert _count(sync_db, SourceSystem) == 5
    assert _count(sync_db, WorkflowDefinition) == 10
    # unchanged YAML -> no new version created on re-load
    assert _count(sync_db, WorkflowDefinitionVersion) == 10


def test_golden_path_worked_trace_resolves_dcd_data(sync_db, catalog):
    """DSNADEV.dcd_billing_summary must resolve to
    SYS_DCD / DATA QUALITY / TEST DATA via IMR_DCD_TABLE_DATA."""
    upsert_catalog(sync_db, catalog)

    table_name = "DSNADEV.dcd_billing_summary"
    rules = find_docs(sync_db, IncidentMappingRule, {"signal_type": "table_name"}, sort=[("priority", 1)])

    matched = next(r for r in rules if re.search(r.signal_pattern, table_name))

    print(
        f"\n[OUTCOME] table_name={table_name!r} -> "
        f"matched_rule_id={matched.id}, source_system_id={matched.source_system_id}, "
        f"category={matched.category!r}"
    )
    assert matched.id == "IMR_DCD_TABLE_DATA"
    assert matched.source_system_id == "SYS_DCD"
    assert matched.category == "DATA QUALITY / TEST DATA"


def test_rca_playbook_steps_loaded_as_approved_version_document(sync_db, catalog):
    upsert_catalog(sync_db, catalog)

    [version] = find_docs(sync_db, WorkflowDefinitionVersion, {"workflow_definition_id": "RCA_DCD_DATA"})
    assert [task["call"] for task in version.document] == [
        "data_contract_violations",
        "schema_mismatch",
        "null_density",
        "recent_pipeline_changes",
    ]
    assert version.status == "approved"
    assert version.source == "static_authored"
    assert version.version_number == 1


def test_reloading_edited_playbook_creates_a_new_version_and_supersedes_the_old(sync_db, catalog):
    upsert_catalog(sync_db, catalog)

    edited = catalog.model_copy(deep=True)
    dcd_data = next(p for p in edited.rca_playbooks if p.id == "RCA_DCD_DATA")
    dcd_data.steps[0].with_["severity_threshold"] = "ERROR"

    upsert_catalog(sync_db, edited)

    versions = find_docs(
        sync_db, WorkflowDefinitionVersion, {"workflow_definition_id": "RCA_DCD_DATA"}, sort=[("version_number", 1)]
    )
    assert [v.version_number for v in versions] == [1, 2]
    assert versions[0].status == "superseded"
    assert versions[1].status == "approved"
    assert versions[1].document[0]["with"]["severity_threshold"] == "ERROR"


def test_rca_playbook_category_cannot_be_any(sync_db):
    insert_docs(
        sync_db,
        SourceSystem(
            id="SYS_TEST",
            name="Test",
            code="TST",
            type="Application",
            description="x",
            owning_team="x",
            environment="NPE",
        ),
    )
    with pytest.raises(pydantic.ValidationError):
        WorkflowDefinition(id="WFD_TEST_ANY", source_system_id="SYS_TEST", category="ANY")


def test_a_failing_load_writes_nothing(sync_db, catalog):
    """The whole load is one transaction, as the single commit used to be:
    a playbook conflicting with an existing (source_system, category)
    definition rolls back every other upsert too."""
    insert_docs(sync_db, WorkflowDefinition(id="WFD_SQUATTER", source_system_id="SYS_DCD", category="DATA QUALITY / TEST DATA"))

    with pytest.raises(Exception):
        upsert_catalog(sync_db, catalog)

    assert _count(sync_db, SourceSystem) == 0
    assert _count(sync_db, WorkflowDefinitionVersion) == 0
