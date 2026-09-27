import re

import pytest
from sqlalchemy import func, select

from app.db import Base, make_engine, make_session_factory
from app.models import (
    IncidentMappingRule,
    SourceSystem,
    SystemFootprint,
    WorkflowDefinition,
    WorkflowDefinitionVersion,
)
from dataloadscripts.load_catalog import DEFAULT_CATALOG_PATH, load_catalog_file, upsert_catalog


@pytest.fixture()
def session_factory(postgres_url):
    engine = make_engine(postgres_url)
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    Base.metadata.drop_all(engine)
    engine.dispose()


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


def test_load_persists_expected_row_counts(session_factory, catalog):
    with session_factory() as session:
        upsert_catalog(session, catalog)

        assert session.scalar(select(func.count()).select_from(SourceSystem)) == 5
        assert session.scalar(select(func.count()).select_from(SystemFootprint)) == 17
        assert session.scalar(select(func.count()).select_from(IncidentMappingRule)) == 14
        assert session.scalar(select(func.count()).select_from(WorkflowDefinition)) == 10
        assert session.scalar(select(func.count()).select_from(WorkflowDefinitionVersion)) == 10


def test_load_is_idempotent(session_factory, catalog):
    with session_factory() as session:
        upsert_catalog(session, catalog)
        upsert_catalog(session, catalog)

        assert session.scalar(select(func.count()).select_from(SourceSystem)) == 5
        assert session.scalar(select(func.count()).select_from(WorkflowDefinition)) == 10
        # unchanged YAML -> no new version created on re-load
        assert session.scalar(select(func.count()).select_from(WorkflowDefinitionVersion)) == 10


def test_golden_path_worked_trace_resolves_dcd_data(session_factory, catalog):
    """DSNADEV.dcd_billing_summary must resolve to
    SYS_DCD / DATA QUALITY / TEST DATA via IMR_DCD_TABLE_DATA."""
    with session_factory() as session:
        upsert_catalog(session, catalog)

        table_name = "DSNADEV.dcd_billing_summary"
        rules = session.scalars(
            select(IncidentMappingRule)
            .where(IncidentMappingRule.signal_type == "table_name")
            .order_by(IncidentMappingRule.priority)
        ).all()

        matched = next(r for r in rules if re.search(r.signal_pattern, table_name))

        print(
            f"\n[OUTCOME] table_name={table_name!r} -> "
            f"matched_rule_id={matched.id}, source_system_id={matched.source_system_id}, "
            f"category={matched.category!r}"
        )
        assert matched.id == "IMR_DCD_TABLE_DATA"
        assert matched.source_system_id == "SYS_DCD"
        assert matched.category == "DATA QUALITY / TEST DATA"


def test_rca_playbook_steps_loaded_as_approved_version_document(session_factory, catalog):
    with session_factory() as session:
        upsert_catalog(session, catalog)

        version = session.scalars(
            select(WorkflowDefinitionVersion).where(
                WorkflowDefinitionVersion.workflow_definition_id == "RCA_DCD_DATA"
            )
        ).one()
        assert [task["call"] for task in version.document] == [
            "data_contract_violations",
            "schema_mismatch",
            "null_density",
            "recent_pipeline_changes",
        ]
        assert version.status == "approved"
        assert version.source == "static_authored"
        assert version.version_number == 1


def test_reloading_edited_playbook_creates_a_new_version_and_supersedes_the_old(session_factory, catalog):
    with session_factory() as session:
        upsert_catalog(session, catalog)

        edited = catalog.model_copy(deep=True)
        dcd_data = next(p for p in edited.rca_playbooks if p.id == "RCA_DCD_DATA")
        dcd_data.steps[0].with_["severity_threshold"] = "ERROR"

        upsert_catalog(session, edited)

        versions = session.scalars(
            select(WorkflowDefinitionVersion)
            .where(WorkflowDefinitionVersion.workflow_definition_id == "RCA_DCD_DATA")
            .order_by(WorkflowDefinitionVersion.version_number)
        ).all()
        assert [v.version_number for v in versions] == [1, 2]
        assert versions[0].status == "superseded"
        assert versions[1].status == "approved"
        assert versions[1].document[0]["with"]["severity_threshold"] == "ERROR"


def test_rca_playbook_category_cannot_be_any(session_factory):
    with session_factory() as session:
        session.add(
            SourceSystem(
                id="SYS_TEST",
                name="Test",
                code="TST",
                type="Application",
                description="x",
                owning_team="x",
                environment="NPE",
            )
        )
        session.flush()
        session.add(WorkflowDefinition(id="WFD_TEST_ANY", source_system_id="SYS_TEST", category="ANY"))
        with pytest.raises(Exception):
            session.commit()
