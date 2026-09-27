import re
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.db import Base, make_engine, make_session_factory
from app.incident_parser import extract_signals
from app.models import Environment, SourceSystem, SystemFootprint, Team
from app.opa_client import evaluate_mapping_rules
from dataloadscripts.load_catalog import load_catalog_file, upsert_catalog
from dataloadscripts.test_fixtures import REAL_TICKET_FIBER, REAL_TICKET_IDS

REAL_CATALOG_PATH = Path(__file__).resolve().parents[1] / "dataloadscripts" / "npe_real_source_systems.yaml"


@pytest.fixture()
def real_catalog():
    return load_catalog_file(REAL_CATALOG_PATH)


@pytest.fixture()
def session_factory(postgres_url):
    engine = make_engine(postgres_url)
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_real_catalog_row_counts(real_catalog):
    assert len(real_catalog.teams) == 4
    assert len(real_catalog.environments) == 10
    assert len(real_catalog.source_systems) == 23
    # RCA_HSI_FUNC/RCA_FIBER_FUNC/RCA_IDS_DATA, authored to exercise the
    # lightweight playbook executor + RCA synthesizer
    # (tests/test_e2e_pipeline.py); the remaining 20 real systems still have
    # no playbook.
    assert {p.id for p in real_catalog.rca_playbooks} == {"RCA_HSI_FUNC", "RCA_FIBER_FUNC", "RCA_IDS_DATA"}


def test_real_catalog_signal_patterns_compile(real_catalog):
    for footprint in real_catalog.system_footprints:
        re.compile(footprint.value)
    for rule in real_catalog.incident_mapping_rules:
        re.compile(rule.signal_pattern)


def test_real_catalog_no_playbook_category_is_any(real_catalog):
    # Not every system has an RCA_PLAYBOOK, but every
    # mapping rule's category must still be a real taxonomy value.
    for rule in real_catalog.incident_mapping_rules:
        assert rule.category != "ANY"


def test_real_catalog_loads_into_postgres(session_factory, real_catalog):
    with session_factory() as session:
        upsert_catalog(session, real_catalog)

        assert session.scalar(select(func.count()).select_from(Team)) == 4
        assert session.scalar(select(func.count()).select_from(Environment)) == 10
        assert session.scalar(select(func.count()).select_from(SourceSystem)) == 23
        assert session.scalar(select(func.count()).select_from(SystemFootprint)) > 0


def test_real_catalog_qlab03_environment_has_qla03_alias(session_factory, real_catalog):
    with session_factory() as session:
        upsert_catalog(session, real_catalog)
        env = session.get(Environment, "ENV_QLAB03")
        assert env.aliases == "QLA03"


async def test_real_fiber_ticket_resolves_to_fiber_system(opa_url, monkeypatch):
    import app.opa_client as opa_client_module

    monkeypatch.setattr(opa_client_module, "OPA_URL", opa_url)

    real_catalog = load_catalog_file(REAL_CATALOG_PATH)
    rule_dicts = [r.model_dump() for r in real_catalog.incident_mapping_rules]

    signals = extract_signals(REAL_TICKET_FIBER)
    matches = await evaluate_mapping_rules(signals, rule_dicts)

    assert any(m["id"] == "IMR_FIBER_HOST" for m in matches)
    winner = min(matches, key=lambda r: (r["priority"], r["id"]))
    print(
        f"\n[OUTCOME] real RS-173234 (Fiber) text -> matched_rule_id={winner['id']} "
        f"source_system_id={winner['source_system_id']} category={winner['category']!r}"
    )
    assert winner["source_system_id"] == "SYS_FIBER"
    assert winner["category"] == "FUNCTIONAL DEFECT (QA/UAT)"


async def test_real_ids_ticket_resolves_to_ids_system(opa_url, monkeypatch):
    import app.opa_client as opa_client_module

    monkeypatch.setattr(opa_client_module, "OPA_URL", opa_url)

    real_catalog = load_catalog_file(REAL_CATALOG_PATH)
    rule_dicts = [r.model_dump() for r in real_catalog.incident_mapping_rules]

    signals = extract_signals(REAL_TICKET_IDS)
    matches = await evaluate_mapping_rules(signals, rule_dicts)

    assert any(m["id"] == "IMR_IDS_TABLE" for m in matches)
    winner = min(matches, key=lambda r: (r["priority"], r["id"]))
    print(
        f"\n[OUTCOME] real RS-169040 (IDS) text -> matched_rule_id={winner['id']} "
        f"source_system_id={winner['source_system_id']} category={winner['category']!r}"
    )
    assert winner["source_system_id"] == "SYS_IDS"
    assert winner["category"] == "DATA QUALITY / TEST DATA"
