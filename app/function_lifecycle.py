"""Which playbooks use which check_type version, and whether everything
still pinned can actually run (Sol-104).

Pins live inside WorkflowDefinitionVersion.document as plain values, so
"who uses error_logs v2?" is a query over documents, not a foreign key.
Approved and superseded playbook versions both count as users: superseded
versions stay runnable through Retry's version override.
"""

from pymongo.asynchronous.database import AsyncDatabase

from app.checks import has_implementation
from app.function_registry import DEFAULT_FUNCTION_REGISTRY
from app.models import (
    FunctionDefinitionVersion,
    WorkflowBuildRequest,
    WorkflowDefinition,
    WorkflowDefinitionVersion,
)
from app.repositories.base import find

PINNING_PLAYBOOK_STATUSES = ("approved", "superseded")


def _task_pins(check_type: str, version_number: int) -> dict:
    match: dict = {"call": check_type, "function_version_number": version_number}
    if version_number == 1:
        # Documents from before versioning carry no number -- they run v1.
        match = {"call": check_type, "$or": [{"function_version_number": 1}, {"function_version_number": {"$exists": False}}]}
    return {"$elemMatch": match}


async def function_version_usage(db: AsyncDatabase, check_type: str, version_number: int) -> dict:
    """Playbook versions (approved/superseded) and pending build requests
    whose documents pin (check_type, version_number)."""
    versions = await find(
        db,
        WorkflowDefinitionVersion,
        {"document": _task_pins(check_type, version_number), "status": {"$in": list(PINNING_PLAYBOOK_STATUSES)}},
        sort=[("workflow_definition_id", 1), ("version_number", 1)],
    )
    definitions = {d.id: d for d in await find(db, WorkflowDefinition, {"_id": {"$in": sorted({v.workflow_definition_id for v in versions})}})}
    pending = await find(
        db, WorkflowBuildRequest, {"generated_document": _task_pins(check_type, version_number), "status": "rendered"}
    )
    return {
        "check_type": check_type,
        "version_number": version_number,
        "playbook_versions": [
            {
                "workflow_definition_id": v.workflow_definition_id,
                "workflow_version_id": v.id,
                "version_number": v.version_number,
                "status": v.status,
                "source_system_id": definitions[v.workflow_definition_id].source_system_id
                if v.workflow_definition_id in definitions
                else None,
                "category": definitions[v.workflow_definition_id].category if v.workflow_definition_id in definitions else None,
            }
            for v in versions
        ],
        "pending_build_requests": [r.id for r in pending],
    }


async def check_function_integrity(db: AsyncDatabase) -> list[dict]:
    """Problems that would make a check fail when it runs, found before it
    does. Errors: an approved/superseded playbook pins a version that is
    missing, retired or has no code. Warnings: an active version has no code
    (new builds will be refused until it does). Empty list = healthy.
    Run at API startup (logged) and by GET /functions/integrity."""
    rows = await find(db, FunctionDefinitionVersion)
    by_key = {(r.function_definition_id, r.version_number): r for r in rows}
    functions_in_db = {r.function_definition_id for r in rows}

    problems: list[dict] = []
    for row in rows:
        if row.status == "active" and not has_implementation(row.function_definition_id, row.version_number):
            problems.append(
                {
                    "severity": "warning",
                    "code": "active_without_implementation",
                    "check_type": row.function_definition_id,
                    "version_number": row.version_number,
                    "message": f"{row.function_definition_id} v{row.version_number} is active but has no implementation "
                    f"-- add app/check_types/{row.function_definition_id}_v{row.version_number}.py",
                    "workflow_version_ids": [],
                }
            )

    pinned: dict[tuple[str, int], list[str]] = {}
    for version in await find(db, WorkflowDefinitionVersion, {"status": {"$in": list(PINNING_PLAYBOOK_STATUSES)}}):
        for task in version.document:
            key = (task.get("call"), task.get("function_version_number") or 1)
            pinned.setdefault(key, []).append(version.id)

    for (check_type, version_number), workflow_version_ids in sorted(pinned.items()):
        row = by_key.get((check_type, version_number))
        known_default = row is None and check_type not in functions_in_db and version_number == 1 and check_type in DEFAULT_FUNCTION_REGISTRY
        if row is None and not known_default:
            code, message = "pinned_version_missing", f"{check_type} v{version_number} does not exist"
        elif row is not None and row.status == "retired":
            code, message = "pinned_version_retired", f"{check_type} v{version_number} is retired"
        elif not has_implementation(check_type, version_number):
            code, message = "pinned_version_no_implementation", f"{check_type} v{version_number} has no implementation"
        else:
            continue
        problems.append(
            {
                "severity": "error",
                "code": code,
                "check_type": check_type,
                "version_number": version_number,
                "message": f"{message} but {len(workflow_version_ids)} playbook version(s) pin it",
                "workflow_version_ids": sorted(set(workflow_version_ids)),
            }
        )
    return problems
