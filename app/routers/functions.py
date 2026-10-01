"""Full CRUD REST API for the versioned function (check_type) registry --
mirrors app/routers/workflows.py's WorkflowDefinition/WorkflowDefinitionVersion
pattern: FunctionDefinition is the stable identity, FunctionDefinitionVersion
rows are immutable contracts.

Lifecycle (Sol-104): draft -> active -> deprecated -> retired. Exactly one
version per function is `active` -- the default new playbook tasks pin to.
Publishing a version as active (the default) demotes the previous one to
`deprecated`, which keeps running for every playbook pinned to it. A
version can be published as `draft` (contract first, try it out via an
explicit pin + dry-run) and activated later. A version is retired only once
no playbook pins it.

Every write updates app.function_registry's in-memory caches
(FUNCTION_REGISTRY / FUNCTION_VERSIONS) directly, live, no restart needed.
"""

import inspect
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.checks import get_implementation, implementation_kind
from app.checks import has_implementation as check_has_implementation
from app.db import get_db, in_transaction
from app.function_lifecycle import check_function_integrity, function_version_usage
from app.function_registry import FunctionSpec, cache_function_version, forget_function, spec_from_row
from app.models import FunctionDefinition, FunctionDefinitionVersion
from app.repositories.base import find, find_one, get, insert, max_version_number
from app.repositories.refs import references_to

router = APIRouter(prefix="/functions", tags=["functions"])

# The implementation of (name, N) is discovered from the file
# app/check_types/<name>_v<N>.py, so a name must be usable as one.
_FUNCTION_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_VERSION_SUFFIX_RE = re.compile(r"_v\d+$")

# Statuses an existing version may be (re)activated from.
_ACTIVATABLE = ("draft", "deprecated", "superseded")


def _row_to_spec(row: FunctionDefinitionVersion) -> FunctionSpec:
    spec = spec_from_row(row)
    spec.has_implementation = check_has_implementation(row.function_definition_id, row.version_number)
    return spec


def _cache(row: FunctionDefinitionVersion) -> FunctionSpec:
    spec = _row_to_spec(row)
    cache_function_version(spec)
    return spec


class PublishFunctionIn(BaseModel):
    """Request body for both creating a brand-new function (POST /functions)
    and publishing a new version of an existing one
    (POST /functions/{id}/versions). version_id/version_number are always
    server-assigned. `status` applies to new versions only: `active` (the
    default) makes it the default for new pins right away; `draft` publishes
    the contract without changing what anything pins to. A brand-new
    function's first version is always active."""

    name: str
    description: str
    params: list[dict]
    default_retry: dict = {}
    created_by: str
    status: Literal["active", "draft"] = "active"

    @field_validator("name")
    @classmethod
    def name_is_a_check_type_identifier(cls, v: str) -> str:
        if not _FUNCTION_NAME_RE.match(v) or _VERSION_SUFFIX_RE.search(v):
            raise ValueError(
                "must be lower_snake_case and not end in _v<N> -- its code lives in "
                "app/check_types/<name>_v<N>.py"
            )
        return v


class StatusChangeIn(BaseModel):
    changed_by: str


class FunctionSourceOut(BaseModel):
    check_type: str
    version_number: int
    has_implementation: bool
    language: str | None
    code: str | None


async def _current_version(db: AsyncDatabase, function_id: str) -> FunctionDefinitionVersion | None:
    return await find_one(db, FunctionDefinitionVersion, {"function_definition_id": function_id, "status": "active"})


async def _version_or_404(db: AsyncDatabase, function_id: str, version_number: int) -> FunctionDefinitionVersion:
    row = await find_one(
        db, FunctionDefinitionVersion, {"function_definition_id": function_id, "version_number": version_number}
    )
    if row is None:
        raise HTTPException(404, f"No version {version_number} for function {function_id!r}")
    return row


@router.get("", response_model=list[FunctionSpec])
async def list_functions(db: AsyncDatabase = Depends(get_db)):
    """Current (active) version of every function."""
    rows = await find(db, FunctionDefinitionVersion, {"status": "active"})
    return [_row_to_spec(r) for r in rows]


@router.get("/integrity")
async def function_integrity(db: AsyncDatabase = Depends(get_db)):
    """Pinned check versions that couldn't run (errors) and active versions
    with no code yet (warnings). `ok` is true when there are no errors."""
    problems = await check_function_integrity(db)
    return {"ok": not any(p["severity"] == "error" for p in problems), "problems": problems}


@router.get("/{function_id}", response_model=FunctionSpec)
async def get_function(function_id: str, db: AsyncDatabase = Depends(get_db)):
    row = await _current_version(db, function_id)
    if row is None:
        raise HTTPException(404, f"Function {function_id!r} not found (or has no active version)")
    return _row_to_spec(row)


@router.get("/{function_id}/versions/{version_number}/source", response_model=FunctionSourceOut)
async def get_function_source(function_id: str, version_number: int, db: AsyncDatabase = Depends(get_db)):
    """Read-only source for the UI's playbook-tab function panel: the
    module source for a Python implementation (app/check_types/<id>_v<N>.py),
    the template for a v1 stub. A published contract doesn't imply an
    implementation exists -- that's reported as has_implementation=false
    with no code, not an error, since the version itself is real."""
    await _version_or_404(db, function_id, version_number)

    kind = implementation_kind(function_id, version_number)
    if kind == "python":
        impl = get_implementation(function_id, version_number)
        code = inspect.getsource(inspect.getmodule(impl))
    elif kind == "template":
        from app.checks import STUB_RESULTS

        status, template = STUB_RESULTS[function_id][version_number]
        code = f"status={status!r}, details_template={template!r}"
    else:
        code = None
    return FunctionSourceOut(
        check_type=function_id,
        version_number=version_number,
        has_implementation=kind is not None,
        language=kind,
        code=code,
    )


@router.get("/{function_id}/versions/{version_number}/usage")
async def get_function_version_usage(function_id: str, version_number: int, db: AsyncDatabase = Depends(get_db)):
    """Which playbook versions (approved/superseded) and pending build
    requests pin this check version -- what a retire would affect."""
    await _version_or_404(db, function_id, version_number)
    return await function_version_usage(db, function_id, version_number)


@router.get("/{function_id}/versions", response_model=list[FunctionSpec])
async def list_function_versions(function_id: str, db: AsyncDatabase = Depends(get_db)):
    if await get(db, FunctionDefinition, function_id) is None:
        raise HTTPException(404, f"Function {function_id!r} not found")
    rows = await find(
        db, FunctionDefinitionVersion, {"function_definition_id": function_id}, sort=[("version_number", 1)]
    )
    return [_row_to_spec(r) for r in rows]


@router.post("", response_model=FunctionSpec, status_code=201)
async def create_function(payload: PublishFunctionIn, db: AsyncDatabase = Depends(get_db)):
    """A brand-new check_type, created with its version 1 contract (active).
    It becomes usable in playbooks once app/check_types/<name>_v1.py exists;
    until then builds referencing it are refused with a clear message."""
    conflict = HTTPException(
        409, f"Function {payload.name!r} already exists -- use POST .../versions to publish a new version"
    )
    if await get(db, FunctionDefinition, payload.name) is not None:
        raise conflict

    version = FunctionDefinitionVersion(
        function_definition_id=payload.name,
        version_number=1,
        description=payload.description,
        params=payload.params,
        default_retry=payload.default_retry,
        status="active",
        created_by=payload.created_by,
    )

    async def create(session):
        await insert(db, FunctionDefinition(id=payload.name), session=session)
        await insert(db, version, session=session)

    try:
        await in_transaction(db, create)
    except DuplicateKeyError as exc:  # lost a race with a concurrent create
        raise conflict from exc

    return _cache(version)


@router.post("/{function_id}/versions", response_model=FunctionSpec, status_code=201)
async def publish_function_version(function_id: str, payload: PublishFunctionIn, db: AsyncDatabase = Depends(get_db)):
    if payload.name != function_id:
        raise HTTPException(422, "Body name must match path function_id")
    if await get(db, FunctionDefinition, function_id) is None:
        raise HTTPException(404, f"Function {function_id!r} not found -- use POST /functions to create it first")

    async def publish(session) -> tuple[FunctionDefinitionVersion, list[FunctionDefinitionVersion]]:
        demoted = []
        if payload.status == "active":
            # Demote first: the one-active-per-function index is checked
            # per write, not at commit.
            demoted = await find(
                db, FunctionDefinitionVersion, {"function_definition_id": function_id, "status": "active"}, session=session
            )
            await db[FunctionDefinitionVersion.COLLECTION].update_many(
                {"function_definition_id": function_id, "status": "active"}, {"$set": {"status": "deprecated"}}, session=session
            )
        version = FunctionDefinitionVersion(
            function_definition_id=function_id,
            version_number=await max_version_number(
                db, FunctionDefinitionVersion, "function_definition_id", function_id, session=session
            )
            + 1,
            description=payload.description,
            params=payload.params,
            default_retry=payload.default_retry,
            status=payload.status,
            created_by=payload.created_by,
        )
        return await insert(db, version, session=session), demoted

    try:
        version, demoted = await in_transaction(db, publish)
    except DuplicateKeyError as exc:  # a concurrent publish took this version number
        raise HTTPException(409, f"Function {function_id!r} was published concurrently -- retry") from exc

    for row in demoted:
        _cache(row.model_copy(update={"status": "deprecated"}))
    return _cache(version)


@router.post("/{function_id}/versions/{version_number}/activate", response_model=FunctionSpec)
async def activate_function_version(
    function_id: str, version_number: int, payload: StatusChangeIn, db: AsyncDatabase = Depends(get_db)
):
    """Makes this version the default for new pins; the previously active
    version becomes deprecated (still runs wherever it is pinned). Existing
    playbook versions are untouched -- moving them is an explicit upgrade."""
    row = await _version_or_404(db, function_id, version_number)
    if row.status == "active":
        return _row_to_spec(row)
    if row.status not in _ACTIVATABLE:
        raise HTTPException(409, f"{function_id} v{version_number} is {row.status} and can't be activated")

    async def activate(session) -> list[FunctionDefinitionVersion]:
        demoted = await find(
            db, FunctionDefinitionVersion, {"function_definition_id": function_id, "status": "active"}, session=session
        )
        await db[FunctionDefinitionVersion.COLLECTION].update_many(
            {"function_definition_id": function_id, "status": "active"}, {"$set": {"status": "deprecated"}}, session=session
        )
        await db[FunctionDefinitionVersion.COLLECTION].update_one(
            {"_id": row.id}, {"$set": {"status": "active"}}, session=session
        )
        return demoted

    try:
        demoted = await in_transaction(db, activate)
    except DuplicateKeyError as exc:
        raise HTTPException(409, f"Function {function_id!r} was activated concurrently -- retry") from exc

    for old in demoted:
        _cache(old.model_copy(update={"status": "deprecated"}))
    return _cache(row.model_copy(update={"status": "active"}))


@router.post("/{function_id}/versions/{version_number}/retire", response_model=FunctionSpec)
async def retire_function_version(
    function_id: str, version_number: int, payload: StatusChangeIn, db: AsyncDatabase = Depends(get_db)
):
    """Permanently stops a version from running or being pinned. Refused
    for the active version (activate another one first) and while any
    approved or superseded playbook version pins it (upgrade those first --
    see GET .../usage)."""
    row = await _version_or_404(db, function_id, version_number)
    if row.status == "retired":
        return _row_to_spec(row)
    if row.status == "active":
        raise HTTPException(409, f"{function_id} v{version_number} is the active version -- activate another version first")

    usage = await function_version_usage(db, function_id, version_number)
    if usage["playbook_versions"]:
        raise HTTPException(
            409,
            {
                "message": f"{function_id} v{version_number} is still pinned by "
                f"{len(usage['playbook_versions'])} playbook version(s) -- upgrade them first",
                "usage": usage,
            },
        )

    await db[FunctionDefinitionVersion.COLLECTION].update_one({"_id": row.id}, {"$set": {"status": "retired"}})
    return _cache(row.model_copy(update={"status": "retired"}))


@router.delete("/{function_id}", status_code=204)
async def delete_function(function_id: str, db: AsyncDatabase = Depends(get_db)):
    """Deletes the function and its entire version history, atomically (no
    orphaned versions) -- only while no stored playbook version (any status)
    calls it, since those must stay explainable and retry-able. A function
    in use is retired version by version instead."""
    if await get(db, FunctionDefinition, function_id) is None:
        raise HTTPException(404, f"Function {function_id!r} not found")
    referenced_by = await references_to(db, "function_definition", function_id)
    if referenced_by:
        raise HTTPException(409, f"Function {function_id!r} is still referenced by {referenced_by} -- retire its versions instead")

    async def delete(session):
        await db[FunctionDefinitionVersion.COLLECTION].delete_many({"function_definition_id": function_id}, session=session)
        await db[FunctionDefinition.COLLECTION].delete_one({"_id": function_id}, session=session)

    await in_transaction(db, delete)
    forget_function(function_id)
