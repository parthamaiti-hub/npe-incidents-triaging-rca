from app.function_registry import RetryPolicy
from app.playbook_engine import execute_workflow


async def test_execute_workflow_runs_tasks_in_list_order_and_returns_evidence():
    tasks = [
        {"call": "config_diff", "with": {"env": "NPE", "baseline_env": "DEV", "app": "X"}},
        {"call": "service_health", "with": {"env": "NPE", "endpoint": "https://x/health"}},
    ]

    evidence = await execute_workflow(tasks)

    assert [e["check"] for e in evidence] == ["config_diff", "service_health"]
    assert all(e["status"] in ("OK", "WARN", "ERROR") for e in evidence)
    assert all("params" in e and "details" in e for e in evidence)


async def test_execute_workflow_unknown_call_raises_immediately():
    tasks = [{"call": "not_a_real_function", "with": {}}]

    try:
        await execute_workflow(tasks)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "not_a_real_function" in str(exc)


async def test_execute_workflow_retries_then_succeeds():
    calls = {"n": 0}

    async def flaky_dispatch(call: str, params: dict, version_number: int) -> dict:
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("transient failure")
        return {"status": "OK", "details": "recovered"}

    tasks = [
        {
            "call": "service_health",
            "with": {"env": "NPE", "endpoint": "https://x/health"},
            "retry": {"max_attempts": 3, "delay_seconds": 0},
        }
    ]

    evidence = await execute_workflow(tasks, dispatch=flaky_dispatch)

    assert calls["n"] == 2
    assert evidence[0]["status"] == "OK"
    assert evidence[0]["details"] == "recovered"


async def test_execute_workflow_continues_after_retries_exhausted():
    async def fails_only_service_health(call: str, params: dict, version_number: int) -> dict:
        if call == "service_health":
            raise RuntimeError("permanent failure")
        return {"status": "OK", "details": "fine"}

    tasks = [
        {
            "call": "service_health",
            "with": {"env": "NPE", "endpoint": "https://x/health"},
            "retry": {"max_attempts": 2, "delay_seconds": 0},
        },
        {"call": "config_diff", "with": {"env": "NPE", "baseline_env": "DEV", "app": "X"}},
    ]

    evidence = await execute_workflow(tasks, dispatch=fails_only_service_health)

    # one bad task doesn't blank out the rest -- it becomes an ERROR entry
    # and the workflow continues to the next task.
    assert evidence[0]["status"] == "ERROR"
    assert "permanent failure" in evidence[0]["details"]
    assert evidence[1]["check"] == "config_diff"
    assert evidence[1]["status"] == "OK"


def test_retry_policy_defaults_are_reasonable():
    default = RetryPolicy()
    assert default.max_attempts == 3
    assert default.delay_seconds == 2
    assert default.exponential_backoff is True


async def test_execute_workflow_dispatches_the_pinned_version_not_just_the_name():
    seen_versions = []

    async def recording_dispatch(call: str, params: dict, version_number: int) -> dict:
        seen_versions.append(version_number)
        return {"status": "OK", "details": f"ran v{version_number}"}

    tasks = [
        {"call": "error_logs", "with": {"env": "NPE", "app": "X", "lookback_minutes": 60}, "function_version_number": 2},
        {"call": "apm_traces", "with": {"env": "NPE", "app": "X", "lookback_minutes": 60}},  # no pin -> defaults to 1
    ]

    evidence = await execute_workflow(tasks, dispatch=recording_dispatch)

    assert seen_versions == [2, 1]
    assert evidence[0]["details"] == "ran v2"
    assert evidence[1]["details"] == "ran v1"


async def test_execute_workflow_real_dispatch_fails_cleanly_for_an_unimplemented_version():
    # error_logs has real code for v1 (app.checks.STUB_RESULTS) and v2
    # (app.check_implementations.error_logs_v2) but nothing beyond that --
    # a task pinned to a version with no matching code must not silently
    # reuse an existing version's behavior.
    tasks = [
        {
            "call": "error_logs",
            "with": {"env": "NPE", "app": "X", "lookback_minutes": 60},
            "function_version_number": 3,
            "retry": {"max_attempts": 1, "delay_seconds": 0},
        }
    ]

    evidence = await execute_workflow(tasks)  # real app.checks.run_check, not a fake

    assert evidence[0]["status"] == "ERROR"
    assert "version 3" in evidence[0]["details"]


async def test_execute_workflow_real_dispatch_uses_v2_functional_dummy_code():
    tasks = [
        {
            "call": "error_logs",
            "with": {"env": "NPE", "app": "X", "lookback_minutes": 60},
            "function_version_number": 2,
        }
    ]

    evidence = await execute_workflow(tasks)  # real app.checks.run_check, not a fake

    assert evidence[0]["status"] == "OK"
    assert "FUNCTIONAL DUMMY v2" in evidence[0]["details"]
