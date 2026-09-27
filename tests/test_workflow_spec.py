import pytest
from pydantic import ValidationError

from app.workflow_spec import TaskSpec


def test_valid_task_parses():
    task = TaskSpec.model_validate(
        {"call": "error_logs", "with": {"env": "NPE", "app": "HSI", "lookback_minutes": 240}}
    )
    assert task.call == "error_logs"
    assert task.with_["app"] == "HSI"
    assert task.task_name() == "error_logs"


def test_explicit_name_overrides_default_task_name():
    task = TaskSpec.model_validate({"name": "check-errors", "call": "error_logs", "with": {"env": "NPE", "app": "HSI", "lookback_minutes": 240}})
    assert task.task_name() == "check-errors"


def test_unknown_call_rejected():
    with pytest.raises(ValidationError, match="Unknown function"):
        TaskSpec.model_validate({"call": "not_a_real_function", "with": {}})


def test_missing_required_param_rejected():
    with pytest.raises(ValidationError, match="missing required params"):
        TaskSpec.model_validate({"call": "error_logs", "with": {"env": "NPE"}})


def test_optional_param_may_be_omitted():
    task = TaskSpec.model_validate(
        {"call": "apm_traces", "with": {"env": "NPE", "app": "HSI", "lookback_minutes": 240}}
    )
    assert "latency_threshold_ms" not in task.with_


def test_retry_override_parses_as_retry_policy():
    task = TaskSpec.model_validate(
        {
            "call": "error_logs",
            "with": {"env": "NPE", "app": "HSI", "lookback_minutes": 240},
            "retry": {"max_attempts": 5, "delay_seconds": 1},
        }
    )
    assert task.retry.max_attempts == 5
    assert task.retry.delay_seconds == 1
