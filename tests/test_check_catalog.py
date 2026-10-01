"""Catalog check (dataloadscripts/check_catalog.py): every check fires on a
crafted catalog, both shipped catalogs pass with no errors, and
load_catalog refuses to write when the check finds errors."""

from pathlib import Path

import pytest
import yaml

from app.models import IncidentMappingRule, SourceSystem, WorkflowDefinitionVersion
from app.schemas import CatalogIn, IncidentMappingRuleIn, RcaPlaybookIn
from dataloadscripts.check_catalog import ERROR, WARNING, CatalogCheckError, check_catalog, re2_problems, rule_examples
from dataloadscripts.load_catalog import DEFAULT_CATALOG_PATH, check_only, load_catalog_file, upsert_catalog

REAL_CATALOG_PATH = Path(DEFAULT_CATALOG_PATH).parent / "npe_real_source_systems.yaml"
FUNC = "FUNCTIONAL DEFECT (QA/UAT)"


def _codes(findings, severity=None):
    return sorted(f.code for f in findings if severity is None or f.severity == severity)


def _rule(id, system, category, signal_type, pattern, priority=3):
    return IncidentMappingRuleIn(
        id=id, source_system_id=system, category=category, signal_type=signal_type,
        signal_pattern=pattern, priority=priority, action="x",
    )


@pytest.fixture()
def real():
    return load_catalog_file(REAL_CATALOG_PATH)


@pytest.fixture()
def demo():
    return load_catalog_file(DEFAULT_CATALOG_PATH)


# -- the shipped catalogs ---------------------------------------------------


def test_demo_catalog_has_no_errors_and_flags_its_two_unreachable_playbooks(demo):
    findings = check_catalog(demo)
    assert _codes(findings, ERROR) == []
    unreachable = sorted(f.ids[0] for f in findings if f.code == "unreachable_playbook")
    assert unreachable == ["RCA_DO_ENV", "RCA_DPS_ENV"]
    assert "ANY rule + LLM fallback" in next(f for f in findings if f.code == "unreachable_playbook").message


def test_real_catalog_has_no_errors_and_flags_systems_without_a_playbook(real):
    findings = check_catalog(real)
    assert _codes(findings, ERROR) == []
    assert _codes(findings) == ["route_without_playbook"] * 20


def test_cross_catalog_tie_is_found_when_checked_against_the_database(sync_db, demo, real):
    """The two catalogs share one database: the real IMR_DOTOOL_KEYWORD and
    the demo IMR_DO_KEY_FALLBACK (\\bDO\\b) both match 'DO tool' at P3."""
    upsert_catalog(sync_db, demo)
    ties = [f for f in check_catalog(real, sync_db) if f.code == "priority_tie"]
    assert len(ties) == 1
    assert set(ties[0].ids) == {"IMR_DOTOOL_KEYWORD", "IMR_DO_KEY_FALLBACK"}
    assert "'DO tool'" in ties[0].message


# -- each error -------------------------------------------------------------


def test_duplicate_id(demo):
    demo.incident_mapping_rules.append(demo.incident_mapping_rules[0].model_copy(update={"signal_pattern": "other"}))
    assert "duplicate_id" in _codes(check_catalog(demo), ERROR)


def test_playbook_pair_conflict(demo):
    first = demo.rca_playbooks[0]
    demo.rca_playbooks.append(first.model_copy(update={"id": "RCA_SECOND"}))
    finding = next(f for f in check_catalog(demo) if f.code == "playbook_pair_conflict")
    assert set(finding.ids) == {first.id, "RCA_SECOND"}


def test_unknown_signal_type(demo):
    demo.incident_mapping_rules.append(_rule("IMR_BAD_TYPE", "SYS_DCD", "ANY", "hostnme", "dcd"))
    finding = next(f for f in check_catalog(demo) if f.code == "unknown_signal_type")
    assert finding.ids == ("IMR_BAD_TYPE",)


@pytest.mark.parametrize(
    "pattern, problem",
    [
        (r"samson(?!-prod)", "negative lookahead"),
        (r"(?<=host=)dcd", "lookbehind"),
        (r"(a)\1", "backreference"),
        (r"dcd\Z", r"\Z"),
        (r"a++", "possessive"),
    ],
)
def test_regex_not_supported_by_re2(pattern, problem):
    assert any(problem in p for p in re2_problems(pattern))


@pytest.mark.parametrize("pattern", [r"\bDCD\b", r"[(?=]x", r"digital-qlab\d+", r"\(?!literal", r"(?P<g>x)", r"a\z"])
def test_re2_compatible_patterns_pass(pattern):
    assert re2_problems(pattern) == []


def test_regex_not_re2_is_an_error_in_the_check(demo):
    demo.incident_mapping_rules.append(_rule("IMR_LOOKAHEAD", "SYS_DCD", "ANY", "keyword", r"\bDCD\b(?! test)"))
    assert "regex_not_re2" in _codes(check_catalog(demo), ERROR)


def test_duplicate_rule(demo):
    original = next(r for r in demo.incident_mapping_rules if r.id == "IMR_DCD_TABLE_DATA")
    demo.incident_mapping_rules.append(original.model_copy(update={"id": "IMR_DCD_TABLE_COPY", "priority": 2}))
    finding = next(f for f in check_catalog(demo) if f.code == "duplicate_rule")
    assert set(finding.ids) == {"IMR_DCD_TABLE_DATA", "IMR_DCD_TABLE_COPY"}


def test_category_spelling_drift(demo):
    demo.incident_mapping_rules.append(_rule("IMR_DCD_DRIFT", "SYS_DCD", "Environment/Config", "keyword", "dcd config"))
    finding = next(f for f in check_catalog(demo) if f.code == "category_spelling")
    assert set(finding.ids) == {"ENVIRONMENT / CONFIG", "Environment/Config"}


# -- warnings from probe texts ---------------------------------------------


def test_rule_examples_are_literal_strings_the_rule_matches():
    assert rule_examples(r"\bHSI Gateway\b|HSI orders") == ["HSI Gateway", "HSI orders"]
    assert rule_examples(r"digital-qlab\d+") == ["digital-qlab1"]
    assert rule_examples(r"\.fiber\.t-mobile\.com") == [".fiber.t-mobile.com"]
    assert rule_examples(r"(?i)hsi") == []  # groups/flags: no probe rather than a wrong one


def test_shadowed_rule(real):
    """A broad P1 keyword for another system swallows HSI's own text."""
    real.incident_mapping_rules.append(_rule("IMR_SAP_GATEWAY_ENV", "SYS_SAP", "ENVIRONMENT / CONFIG", "keyword", "Gateway", priority=1))
    finding = next(f for f in check_catalog(real) if f.code == "shadowed_rule")
    assert set(finding.ids) == {"IMR_HSI_KEYWORD", "IMR_SAP_GATEWAY_ENV"}
    assert "'HSI Gateway'" in finding.message


def test_priority_tie(real):
    real.incident_mapping_rules.append(_rule("IMR_AAA_GATEWAY", "SYS_SAP", FUNC, "keyword", "Gateway", priority=3))
    finding = next(f for f in check_catalog(real) if f.code == "priority_tie")
    assert set(finding.ids) == {"IMR_HSI_KEYWORD", "IMR_AAA_GATEWAY"}
    assert "IMR_AAA_GATEWAY wins only because its id sorts first" in finding.message


# -- enforcement in load_catalog -------------------------------------------


def test_load_is_refused_on_errors_and_writes_nothing(sync_db, demo):
    demo.rca_playbooks.append(demo.rca_playbooks[0].model_copy(update={"id": "RCA_SECOND"}))
    with pytest.raises(CatalogCheckError) as exc:
        upsert_catalog(sync_db, demo)
    assert _codes(exc.value.findings) == ["playbook_pair_conflict"]
    assert sync_db[SourceSystem.COLLECTION].count_documents({}) == 0
    assert sync_db[WorkflowDefinitionVersion.COLLECTION].count_documents({}) == 0


def test_warnings_are_returned_and_do_not_block_unless_strict(sync_db, demo):
    warnings = upsert_catalog(sync_db, demo)
    assert _codes(warnings) == ["unreachable_playbook", "unreachable_playbook"]
    assert sync_db[IncidentMappingRule.COLLECTION].count_documents({}) == 14

    edited = demo.model_copy(deep=True)
    edited.rca_playbooks.append(
        RcaPlaybookIn.model_validate(
            {"id": "RCA_DAS_FUNC", "source_system_id": "SYS_DAS", "category": FUNC,
             "steps": [{"call": "error_logs", "with": {"env": "NPE", "app": "DAS", "lookback_minutes": 60}}]}
        )
    )
    with pytest.raises(CatalogCheckError):
        upsert_catalog(sync_db, edited, strict=True)
    assert sync_db["workflow_definition"].find_one({"_id": "RCA_DAS_FUNC"}) is None


def test_check_only_reports_and_returns_exit_code(tmp_path, capsys, demo):
    good = tmp_path / "good.yaml"
    good.write_text(yaml.safe_dump(demo.model_dump(by_alias=True, exclude_none=True)))
    assert check_only(good, use_db=False) == 0
    assert "OK      " in capsys.readouterr().out
    assert check_only(good, use_db=False, strict=True) == 1  # the 2 warnings now block
    out = capsys.readouterr().out
    assert "FAILED" in out and "0 error(s), 2 warning(s)" in out

    data = demo.model_dump(by_alias=True, exclude_none=True)
    data["incident_mapping_rules"][0]["signal_type"] = "hostnme"
    data["rca_playbooks"][0]["steps"][0]["call"] = "not_a_check"
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(data))
    assert check_only(bad, use_db=False) == 1
    out = capsys.readouterr().out
    assert "unknown_signal_type" in out and "playbook_invalid" in out and "FAILED" in out
