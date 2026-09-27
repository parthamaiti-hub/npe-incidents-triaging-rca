"""Generic CNCF Serverless Workflow-style interpreter -- the playbook
engine. Runs in-process: this workload is read-only diagnostic checks, at
most 8 steps per playbook, and doesn't need a durable-execution server.
Reads a workflow document's `do:`-style task list and dispatches each
task's `call` (at its pinned `function_version_number`) to
app.checks.run_check. No per-playbook or per-category code, just
data-driven dispatch -- a single generic engine.
"""

import asyncio

from app.checks import run_check
from app.function_registry import FUNCTION_REGISTRY, RetryPolicy


async def _run_with_retry(dispatch, call: str, params: dict, version_number: int, retry: RetryPolicy) -> dict:
    last_error: Exception | None = None
    for attempt in range(1, retry.max_attempts + 1):
        try:
            result = await dispatch(call, params, version_number)
            return {"check": call, "params": params, **result}
        except Exception as exc:  # noqa: BLE001 -- deliberately broad: any failing check becomes an ERROR entry
            last_error = exc
            if attempt < retry.max_attempts:
                delay = retry.delay_seconds * (2 ** (attempt - 1) if retry.exponential_backoff else 1)
                if delay:
                    await asyncio.sleep(delay)
    return {
        "check": call,
        "params": params,
        "status": "ERROR",
        "details": f"Exhausted {retry.max_attempts} attempt(s): {last_error}",
    }


async def execute_workflow(tasks: list[dict], dispatch=run_check) -> list[dict]:
    """tasks: ordered [{"name"?, "call", "with", "function_version_number"?, "retry"?}, ...] --
    WorkflowDefinitionVersion.document / WorkflowBuildRequest.generated_document.

    Returns the evidence list: each task's result appended with its
    check/params, in task list order (no separate step_order field --
    order is the list position, matching the spec's sequential `do:`
    semantics). One task retrying/failing doesn't abort the rest -- per the
    spec's `catch...then: continue`, a task that exhausts its retries
    becomes an ERROR evidence entry rather than raising. A task with no
    `function_version_number` (e.g. an old document from before versioning
    existed) dispatches at version 1, matching app.checks.run_check's own
    default.

    `dispatch` is injectable for tests -- must accept (call, params,
    version_number); defaults to the real check implementations in
    app.checks.
    """
    evidence = []
    for task in tasks:
        call = task["call"]
        params = task.get("with", {})
        version_number = task.get("function_version_number") or 1
        spec = FUNCTION_REGISTRY.get(call)
        if spec is None:
            raise ValueError(f"Unknown function/check_type: {call!r} -- not in FUNCTION_REGISTRY")

        retry = task.get("retry") or spec.default_retry
        if isinstance(retry, dict):
            retry = RetryPolicy(**retry)

        evidence.append(await _run_with_retry(dispatch, call, params, version_number, retry))
    return evidence
