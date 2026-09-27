"""CNCF YAML <-> stored task list. Pure unit tests, no containers."""

import pytest
import yaml

from app.workflow_yaml import MAX_YAML_BYTES, WorkflowYamlError, cncf_yaml_to_tasks, tasks_to_cncf_yaml

STORED = [
    {
        "call": "error_logs",
        "with": {"env": "NPE", "app": "TESTAPP", "lookback_minutes": 240},
        "function_version_id": "fv-1",
        "function_version_number": 1,
    },
    {
        "name": "deploys",
        "call": "recent_deployments",
        "with": {"env": "NPE", "app": "TESTAPP", "lookback_minutes": 1440},
        "retry": {"max_attempts": 5, "delay_seconds": 1, "exponential_backoff": False},
        "function_version_number": 2,
    },
]


def _strip_pins(tasks):
    return [{k: v for k, v in t.items() if not k.startswith("function_version")} for t in tasks]


def test_render_is_cncf_shaped_and_hides_pins():
    text = tasks_to_cncf_yaml(STORED, name="SYS_X-func", version=3)
    doc = yaml.safe_load(text)
    assert doc["document"] == {"dsl": "1.0.0", "namespace": "npe-rca", "name": "SYS_X-func", "version": "3"}
    assert list(doc["do"][0]) == ["error_logs"]
    assert list(doc["do"][1]) == ["deploys"]
    assert doc["do"][1]["deploys"]["retry"]["max_attempts"] == 5
    assert "function_version" not in text


def test_round_trip_is_lossless_apart_from_pins():
    parsed = cncf_yaml_to_tasks(tasks_to_cncf_yaml(STORED))
    assert parsed.tasks == _strip_pins(STORED)
    assert len(parsed.task_lines) == 2 and parsed.task_lines[0] < parsed.task_lines[1]


def test_duplicate_unnamed_calls_get_distinct_keys_on_render():
    tasks = [{"call": "error_logs", "with": {}}, {"call": "error_logs", "with": {}}]
    parsed = cncf_yaml_to_tasks(tasks_to_cncf_yaml(tasks))
    assert [t.get("name") for t in parsed.tasks] == [None, "error_logs_2"]


def test_blank_with_becomes_empty_params():
    parsed = cncf_yaml_to_tasks("do:\n  - a:\n      call: error_logs\n      with:\n")
    assert parsed.tasks == [{"call": "error_logs", "with": {}, "name": "a"}]


def test_syntax_error_reports_line_and_column():
    with pytest.raises(WorkflowYamlError) as exc_info:
        cncf_yaml_to_tasks("do:\n  - a:\n      call: error_logs\n     with: [unclosed\n")
    exc = exc_info.value
    assert exc.kind == "syntax"
    assert exc.errors[0].line is not None and exc.errors[0].column is not None


@pytest.mark.parametrize(
    "text, path_fragment, message_fragment",
    [
        ("document: {}\n", "do", "must be a list"),
        ("do: {a: 1}\n", "do", "must be a list"),
        ("use: {}\ndo:\n  - a: {call: error_logs}\n", "use", "not supported"),
        ("do:\n  - a: {call: error_logs}\n    b: {call: error_logs}\n", "do[0]", "single-key mapping"),
        ("do:\n  - a:\n      switch: []\n", "do[0].a.switch", "'switch' is not supported"),
        ("do:\n  - a:\n      fork: {}\n", "do[0].a.fork", "'fork' is not supported"),
        ("do:\n  - a:\n      with: {}\n", "do[0].a.call", "'call' is required"),
        ("do:\n  - a:\n      call: error_logs\n      with: [1]\n", "do[0].a.with", "must be a mapping"),
        ("do:\n  - a:\n      call: error_logs\n      then: end\n", "do[0].a.then", "unknown key"),
        ("do:\n  - a:\n      call: error_logs\n      retry: {limit: 3}\n", "do[0].a.retry", "unknown retry keys"),
        ("do: []\n", "do", "at least one task"),
        ("- just\n- a list\n", "", "top level must be a mapping"),
    ],
)
def test_unsupported_shapes_are_schema_errors_with_paths(text, path_fragment, message_fragment):
    with pytest.raises(WorkflowYamlError) as exc_info:
        cncf_yaml_to_tasks(text)
    exc = exc_info.value
    assert exc.kind == "schema"
    assert any(path_fragment in e.path and message_fragment in e.message for e in exc.errors), exc.errors
    assert all(e.line is not None for e in exc.errors)


def test_duplicate_task_names_rejected_and_all_errors_collected():
    text = "do:\n  - a: {call: error_logs}\n  - a: {call: error_logs}\n  - b:\n      switch: []\n"
    with pytest.raises(WorkflowYamlError) as exc_info:
        cncf_yaml_to_tasks(text)
    errors = exc_info.value.errors
    assert {e.task_index for e in errors} == {1, 2}
    dup = next(e for e in errors if "duplicate" in e.message)
    assert dup.line == 3


def test_oversize_input_rejected():
    with pytest.raises(WorkflowYamlError, match="limit"):
        cncf_yaml_to_tasks("#" * (MAX_YAML_BYTES + 1))


def test_unsafe_tags_are_not_executed():
    with pytest.raises(WorkflowYamlError) as exc_info:
        cncf_yaml_to_tasks("do: !!python/object/apply:os.system ['echo hi']\n")
    assert exc_info.value.kind == "syntax"
