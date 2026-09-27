"""Full CRUD REST API for the versioned function registry -- mirrors
app/routers/workflows.py's WorkflowDefinition/WorkflowDefinitionVersion
pattern: FunctionDefinition is the stable identity,
FunctionDefinitionVersion rows are immutable once created (at most one
'active' per function at a time -- publishing a new version supersedes the
prior one). Every write updates app.function_registry.FUNCTION_REGISTRY (the
in-memory cache validation/dispatch actually reads) directly, live, no
restart needed.
"""

import inspect
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.check_implementations import CHECK_IMPLEMENTATIONS_V2
from app.checks import STUB_RESULTS
from app.checks import has_implementation as check_has_implementation
from app.db import get_async_session
from app.function_registry import FUNCTION_REGISTRY, FunctionSpec
from app.models import FunctionDefinition, FunctionDefinitionVersion

router = APIRouter(prefix="/functions", tags=["functions"])


def _row_to_spec(row: FunctionDefinitionVersion) -> FunctionSpec:
    return FunctionSpec(
        name=row.function_definition_id,
        description=row.description,
        params=row.params,
        default_retry=row.default_retry,
        version_id=row.id,
        version_number=row.version_number,
        has_implementation=check_has_implementation(row.function_definition_id, row.version_number),
    )


class PublishFunctionIn(BaseModel):
    """Request body for both creating a brand-new function (POST /functions)
    and publishing a new version of an existing one
    (POST /functions/{id}/versions) -- name/description/params/default_retry
    only; version_id/version_number are always server-assigned."""

    name: str
    description: str
    params: list[dict]
    default_retry: dict = {}
    created_by: str


async def _current_version(session: AsyncSession, function_id: str) -> FunctionDefinitionVersion | None:
    return (
        await session.scalars(
            select(FunctionDefinitionVersion).where(
                FunctionDefinitionVersion.function_definition_id == function_id,
                FunctionDefinitionVersion.status == "active",
            )
        )
    ).first()


@router.get("", response_model=list[FunctionSpec])
async def list_functions(session: AsyncSession = Depends(get_async_session)):
    """Current (active) version of every function."""
    rows = (await session.scalars(select(FunctionDefinitionVersion).where(FunctionDefinitionVersion.status == "active"))).all()
    return [_row_to_spec(r) for r in rows]


@router.get("/{function_id}", response_model=FunctionSpec)
async def get_function(function_id: str, session: AsyncSession = Depends(get_async_session)):
    row = await _current_version(session, function_id)
    if row is None:
        raise HTTPException(404, f"Function {function_id!r} not found (or has no active version)")
    return _row_to_spec(row)


class FunctionSourceOut(BaseModel):
    check_type: str
    version_number: int
    has_implementation: bool
    language: str | None
    code: str | None


@router.get("/{function_id}/versions/{version_number}/source", response_model=FunctionSourceOut)
async def get_function_source(function_id: str, version_number: int, session: AsyncSession = Depends(get_async_session)):
    """Read-only source for the UI's playbook-tab function panel. A published contract doesn't imply an implementation exists
    (app.checks.has_implementation) -- that's not an error here, just
    reported as has_implementation=false with no code, since the version
    itself is real."""
    exists = (
        await session.scalars(
            select(FunctionDefinitionVersion).where(
                FunctionDefinitionVersion.function_definition_id == function_id,
                FunctionDefinitionVersion.version_number == version_number,
            )
        )
    ).first()
    if exists is None:
        raise HTTPException(404, f"No version {version_number} for function {function_id!r}")

    if version_number == 2 and function_id in CHECK_IMPLEMENTATIONS_V2:
        # 2026-09-23: each check_type's v2 now lives in its own module
        # (app/check_types/<function_id>_v2.py) rather than as a
        # distinctly-named function inline in one file -- every module's
        # run() has the same name, so the function's *own* source is no
        # longer self-identifying. The module's source (docstring + run())
        # is: the docstring still names the check_type explicitly, and this
        # is exactly the file someone would open to add a real integration.
        return FunctionSourceOut(
            check_type=function_id,
            version_number=version_number,
            has_implementation=True,
            language="python",
            code=inspect.getsource(inspect.getmodule(CHECK_IMPLEMENTATIONS_V2[function_id])),
        )

    stub = STUB_RESULTS.get(function_id, {}).get(version_number)
    if stub is not None:
        status, template = stub
        return FunctionSourceOut(
            check_type=function_id,
            version_number=version_number,
            has_implementation=True,
            language="template",
            code=f"status={status!r}, details_template={template!r}",
        )

    return FunctionSourceOut(
        check_type=function_id, version_number=version_number, has_implementation=False, language=None, code=None
    )


@router.get("/{function_id}/versions", response_model=list[FunctionSpec])
async def list_function_versions(function_id: str, session: AsyncSession = Depends(get_async_session)):
    if await session.get(FunctionDefinition, function_id) is None:
        raise HTTPException(404, f"Function {function_id!r} not found")
    rows = (
        await session.scalars(
            select(FunctionDefinitionVersion)
            .where(FunctionDefinitionVersion.function_definition_id == function_id)
            .order_by(FunctionDefinitionVersion.version_number)
        )
    ).all()
    return [_row_to_spec(r) for r in rows]


@router.post("", response_model=FunctionSpec, status_code=201)
async def create_function(payload: PublishFunctionIn, session: AsyncSession = Depends(get_async_session)):
    if await session.get(FunctionDefinition, payload.name) is not None:
        raise HTTPException(409, f"Function {payload.name!r} already exists -- use POST .../versions to publish a new version")

    definition = FunctionDefinition(id=payload.name)
    session.add(definition)
    await session.flush()

    version = FunctionDefinitionVersion(
        id=str(uuid.uuid4()),
        function_definition_id=payload.name,
        version_number=1,
        description=payload.description,
        params=payload.params,
        default_retry=payload.default_retry,
        status="active",
        created_by=payload.created_by,
    )
    session.add(version)
    await session.commit()

    spec = _row_to_spec(version)
    FUNCTION_REGISTRY[payload.name] = spec
    return spec


@router.post("/{function_id}/versions", response_model=FunctionSpec, status_code=201)
async def publish_function_version(
    function_id: str, payload: PublishFunctionIn, session: AsyncSession = Depends(get_async_session)
):
    if payload.name != function_id:
        raise HTTPException(422, "Body name must match path function_id")
    if await session.get(FunctionDefinition, function_id) is None:
        raise HTTPException(404, f"Function {function_id!r} not found -- use POST /functions to create it first")

    prior_active = await _current_version(session, function_id)
    if prior_active is not None:
        prior_active.status = "superseded"

    max_version = (
        await session.scalars(
            select(FunctionDefinitionVersion.version_number)
            .where(FunctionDefinitionVersion.function_definition_id == function_id)
            .order_by(FunctionDefinitionVersion.version_number.desc())
        )
    ).first()

    version = FunctionDefinitionVersion(
        id=str(uuid.uuid4()),
        function_definition_id=function_id,
        version_number=(max_version or 0) + 1,
        description=payload.description,
        params=payload.params,
        default_retry=payload.default_retry,
        status="active",
        created_by=payload.created_by,
    )
    session.add(version)
    await session.commit()

    spec = _row_to_spec(version)
    FUNCTION_REGISTRY[function_id] = spec
    return spec


@router.delete("/{function_id}", status_code=204)
async def delete_function(function_id: str, session: AsyncSession = Depends(get_async_session)):
    """Deletes the function and its entire version history. Doesn't check
    whether any WorkflowDefinitionVersion.document task references it (by
    `call` name or a pinned `function_version_id`) -- JSONB documents have
    no FK to enforce that. Deleting only affects future validation; already-
    approved workflow versions keep their document/evidence unaffected,
    since they're never re-validated after approval."""
    definition = await session.get(FunctionDefinition, function_id)
    if definition is None:
        raise HTTPException(404, f"Function {function_id!r} not found")
    versions = (
        await session.scalars(
            select(FunctionDefinitionVersion).where(FunctionDefinitionVersion.function_definition_id == function_id)
        )
    ).all()
    for version in versions:
        await session.delete(version)
    await session.delete(definition)
    await session.commit()
    FUNCTION_REGISTRY.pop(function_id, None)
