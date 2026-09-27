from pathlib import Path

from app.function_registry import FUNCTION_REGISTRY
from dataloadscripts.load_catalog import load_catalog_file

REAL_CATALOG_PATH = Path(__file__).resolve().parents[1] / "dataloadscripts" / "npe_real_source_systems.yaml"

FUNC_CHECK_TYPES = {
    "service_health",
    "error_logs",
    "apm_traces",
    "recent_deployments",
    "feature_flags",
    "dependent_services_health",
}


def test_all_func_check_types_are_registered():
    assert FUNC_CHECK_TYPES <= FUNCTION_REGISTRY.keys()


def test_registry_covers_all_20_check_types():
    assert len(FUNCTION_REGISTRY) == 20


def test_real_func_playbooks_satisfy_their_registered_required_params():
    catalog = load_catalog_file(REAL_CATALOG_PATH)
    func_playbooks = [p for p in catalog.rca_playbooks if p.category == "FUNCTIONAL DEFECT (QA/UAT)"]
    assert func_playbooks  # RCA_HSI_FUNC/RCA_FIBER_FUNC must still exist

    for playbook in func_playbooks:
        for step in playbook.steps:
            spec = FUNCTION_REGISTRY[step.call]
            missing = spec.required_param_names() - step.with_.keys()
            assert not missing, f"{playbook.id}/{step.call} missing required params: {missing}"


def test_every_registry_entry_resolves_a_real_check_type():
    from app.checks import STUB_RESULTS

    assert FUNCTION_REGISTRY.keys() == STUB_RESULTS.keys()
