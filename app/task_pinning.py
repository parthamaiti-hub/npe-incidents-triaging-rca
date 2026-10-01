"""Resolving which check_type version each playbook task runs (Sol-104).

One resolver for every path that turns authored tasks into a stored,
executable document: UI/API build and edit (app.workflow_orchestrator),
the upgrade flow, and the catalog loader (dataloadscripts/load_catalog.py).
Pure -- reads only the in-memory function caches, never the database.

Each task's version is resolved once, at build time, in this order:

  1. an explicit `version:` on the task (author's choice);
  2. otherwise, when editing/reloading, the pin the *same* task already had
     in the base document ("same" = same call, params and retry; the task
     name is cosmetic) -- unless the call is listed in `upgrade_calls`;
  3. otherwise, the function's active version.

There is no floating "latest": once stored, a pin never moves until a new
playbook version is built. The task is then validated against the contract
of the version it resolved to (not the active one), stamped with
function_version_id/function_version_number, and -- if it has no explicit
retry -- given that version's default retry as `resolved_retry`, so the
stored document is self-contained.
"""

import json

from pydantic import ValidationError

from app.checks import has_implementation
from app.function_registry import (
    FunctionSpec,
    active_version_number,
    get_function_spec,
    is_runnable,
)
from app.workflow_spec import TaskSpec

# Keys a stored task carries that only the server sets. Ignored when they
# come back in as input (the UI's graph editor round-trips stored tasks).
SERVER_FIELDS = ("function_version_id", "function_version_number", "resolved_retry")

ALL_CALLS = "*"


class WorkflowValidationError(ValueError):
    """`errors` lists every failing task (an editor highlights all
    broken steps at once, not just the first) as {task_index, path, message};
    str(exc) stays the joined human-readable form existing callers use."""

    def __init__(self, message: str, errors: list[dict] | None = None):
        super().__init__(message)
        self.errors = errors or []


def _task_error_message(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(e["msg"].removeprefix("Value error, ") for e in exc.errors())
    return str(exc)


def pin_key(task: dict) -> tuple:
    """What makes a task 'unchanged' for pin preservation -- the name is
    cosmetic, so it's left out; the function, its params and retry aren't."""
    return (task.get("call"), json.dumps(task.get("with") or {}, sort_keys=True), json.dumps(task.get("retry"), sort_keys=True))


def _raise_if_errors(errors: list[dict]) -> None:
    if errors:
        raise WorkflowValidationError("; ".join(f"{e['path']}: {e['message']}" for e in errors), errors)


def resolve_tasks(
    requested_functions: list[dict],
    preserve_pins_from: list[dict] | None = None,
    upgrade_calls: set[str] | None = None,
) -> list[dict]:
    """Validates and pins every requested task; all failures are raised
    together as one WorkflowValidationError. Returns the stored-document
    task list.

    preserve_pins_from: the base document when editing or reloading.
    upgrade_calls: calls whose preserved pins are ignored (re-pinned to the
    active version); ALL_CALLS means every call."""
    upgrade_calls = upgrade_calls or set()
    existing_pins: dict[tuple, dict] = {}
    for old in preserve_pins_from or []:
        if old.get("function_version_number") is not None:
            existing_pins.setdefault(pin_key(old), old)

    tasks: list[dict] = []
    errors: list[dict] = []
    for i, raw in enumerate(requested_functions):
        path = f"requested_functions[{i}]"

        def fail(message: str) -> None:
            errors.append({"task_index": i, "path": path, "message": message})

        if not isinstance(raw, dict):
            fail("each task must be a mapping with 'call' and 'with'")
            continue
        candidate = {k: v for k, v in raw.items() if k not in SERVER_FIELDS}
        call = candidate.get("call")

        version_id: str | None = None
        if candidate.get("version") is None and isinstance(call, str):
            pinned = None
            if ALL_CALLS not in upgrade_calls and call not in upgrade_calls:
                pinned = existing_pins.get(pin_key(candidate))
            pinned_spec = get_function_spec(call, pinned["function_version_number"]) if pinned is not None else None
            if (
                pinned_spec is not None
                and is_runnable(pinned_spec)
                and has_implementation(call, pinned["function_version_number"])
            ):
                candidate["version"] = pinned["function_version_number"]
                version_id = pinned.get("function_version_id")
            else:
                candidate["version"] = active_version_number(call)

        try:
            task = TaskSpec.model_validate(candidate)
        except Exception as exc:
            fail(_task_error_message(exc))
            continue

        version_number = task.version
        spec: FunctionSpec = get_function_spec(task.call, version_number)  # TaskSpec guarantees it exists
        if not is_runnable(spec):
            fail(f"{task.call!r} version {version_number} is {spec.status} and can no longer be used")
            continue
        if not has_implementation(task.call, version_number):
            fail(
                f"{task.call!r} version {version_number} has a published contract but no matching implementation "
                f"(app.checks.STUB_RESULTS or app/check_types/{task.call}_v{version_number}.py) yet"
            )
            continue

        task.version = None
        task.function_version_id = version_id or spec.version_id
        task.function_version_number = version_number
        if task.retry is None:
            task.resolved_retry = spec.default_retry
        tasks.append(task.model_dump(by_alias=True, exclude_none=True))

    _raise_if_errors(errors)
    return tasks


def unapprovable_pins(document: list[dict]) -> list[dict]:
    """Tasks that must not be in an *approved* playbook version: pinned to
    a draft (activate it first), retired, unknown or unimplemented version.
    A build request may hold draft pins (to dry-run a new check version);
    approval is where they're refused."""
    errors = []
    for i, task in enumerate(document):
        call, version_number = task.get("call"), task.get("function_version_number") or 1
        spec = get_function_spec(call, version_number)
        if spec is None:
            message = f"{call!r} version {version_number} does not exist"
        elif spec.status == "draft":
            message = f"{call!r} version {version_number} is still a draft -- activate it before approving"
        elif not is_runnable(spec):
            message = f"{call!r} version {version_number} is {spec.status}"
        elif not has_implementation(call, version_number):
            message = f"{call!r} version {version_number} has no implementation"
        else:
            continue
        errors.append({"task_index": i, "path": f"document[{i}]", "message": message})
    return errors


def require_approvable(document: list[dict]) -> None:
    _raise_if_errors(unapprovable_pins(document))


def _param_diff(old: FunctionSpec, new: FunctionSpec) -> dict:
    old_params = {p.name: p for p in old.params}
    new_params = {p.name: p for p in new.params}
    return {
        "added": sorted(set(new_params) - set(old_params)),
        "removed": sorted(set(old_params) - set(new_params)),
        "newly_required": sorted(
            name for name, p in new_params.items() if p.required and (name not in old_params or not old_params[name].required)
        ),
    }


def describe_pins(document: list[dict]) -> list[dict]:
    """Per task: the pinned version against the function's active version,
    plus the contract difference an upgrade would bring. States:
    up_to_date | upgrade_available | ahead_of_active (pinned to a draft
    newer than active) | retired | missing | no_active_version."""
    rows = []
    for i, task in enumerate(document):
        call = task.get("call")
        pinned_number = task.get("function_version_number") or 1
        pinned = get_function_spec(call, pinned_number)
        active_number = active_version_number(call)
        active = get_function_spec(call, active_number) if active_number is not None else None

        if pinned is None:
            state = "missing"
        elif pinned.status == "retired":
            state = "retired"
        elif active_number is None:
            state = "no_active_version"
        elif pinned_number == active_number:
            state = "up_to_date"
        elif pinned_number < active_number:
            state = "upgrade_available"
        else:
            state = "ahead_of_active"

        rows.append(
            {
                "task_index": i,
                "name": task.get("name") or call,
                "call": call,
                "pinned_version": pinned_number,
                "pinned_status": (pinned.status or "active") if pinned is not None else None,
                "active_version": active_number,
                "state": state,
                "contract_diff": _param_diff(pinned, active)
                if pinned is not None and active is not None and pinned_number != active_number
                else None,
            }
        )
    return rows
