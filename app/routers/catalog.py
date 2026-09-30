"""Full CRUD REST API for the catalog entities that dataloadscripts/load_catalog.py
bulk-loads from YAML -- teams, environments, source systems, footprints,
mapping rules. Reuses app.schemas' *In models (already validated: footprint
value / mapping rule pattern must compile as regex) as both the request body
and the response shape, since they mirror each document model's fields
1:1. Every resource gets the same 5 operations via make_crud_router, so
adding a 6th catalog entity later is a 3-line addition, not a new endpoint
set to hand-write.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.db import get_db
from app.embeddings import delete_footprint_vector
from app.models import Document, Environment, IncidentMappingRule, SourceSystem, SystemFootprint, Team
from app.repositories.base import delete as delete_doc
from app.repositories.base import find, get, insert, replace
from app.repositories.refs import references_to
from app.schemas import EnvironmentIn, IncidentMappingRuleIn, SourceSystemIn, SystemFootprintIn, TeamIn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/catalog", tags=["catalog"])


async def _drop_footprint_vector(request: Request, footprint_id: str) -> None:
    """The footprint's RAG vector is derived data; a failure here leaves a
    stale candidate that scripts/rebuild_vector_index.py cleans up, and
    classification validates LLM picks against the live catalog anyway."""
    try:
        await delete_footprint_vector(request.app.state.vector_store, footprint_id)
    except Exception as exc:  # noqa: BLE001 -- best-effort, see docstring
        logger.warning("Could not delete vector for footprint %s: %s", footprint_id, exc)


def make_crud_router(
    *,
    prefix: str,
    model: type[Document],
    in_schema: type[BaseModel],
    fk_checks: tuple[tuple[str, type[Document]], ...] = (),
    filterable_by_source_system: bool = False,
) -> APIRouter:
    sub = APIRouter(prefix=prefix)

    async def _check_fks(db: AsyncDatabase, data: dict) -> None:
        for field, fk_model in fk_checks:
            value = data.get(field)
            if value is not None and await get(db, fk_model, value) is None:
                raise HTTPException(422, f"{field}={value!r} references unknown {fk_model.__name__}")

    @sub.get("", response_model=list[in_schema])
    async def list_all(source_system_id: str | None = None, db: AsyncDatabase = Depends(get_db)):
        # Footprints/mapping-rules are the two entities the
        # playbook tab needs scoped to one system (its "matched mapping
        # rule" header) -- source_system_id is simply ignored for entities
        # that don't declare filterable_by_source_system (teams,
        # environments, source-systems themselves).
        filter = {}
        if filterable_by_source_system and source_system_id is not None:
            filter["source_system_id"] = source_system_id
        rows = await find(db, model, filter)
        return [in_schema.model_validate(r, from_attributes=True) for r in rows]

    @sub.get("/{item_id}", response_model=in_schema)
    async def get_one(item_id: str, db: AsyncDatabase = Depends(get_db)):
        row = await get(db, model, item_id)
        if row is None:
            raise HTTPException(404, f"{model.__name__} {item_id!r} not found")
        return in_schema.model_validate(row, from_attributes=True)

    @sub.post("", response_model=in_schema, status_code=201)
    async def create(payload: in_schema, db: AsyncDatabase = Depends(get_db)):
        data = payload.model_dump()
        await _check_fks(db, data)
        if await get(db, model, data["id"]) is not None:
            raise HTTPException(409, f"{model.__name__} {data['id']!r} already exists")
        row = model(**data)
        try:
            await insert(db, row)
        except DuplicateKeyError as exc:  # lost a race with a concurrent create
            raise HTTPException(409, f"{model.__name__} {data['id']!r} already exists") from exc
        return in_schema.model_validate(row, from_attributes=True)

    @sub.put("/{item_id}", response_model=in_schema)
    async def update(item_id: str, payload: in_schema, db: AsyncDatabase = Depends(get_db)):
        if payload.id != item_id:
            raise HTTPException(422, "Body id must match path item_id")
        data = payload.model_dump()
        await _check_fks(db, data)
        if await get(db, model, item_id) is None:
            raise HTTPException(404, f"{model.__name__} {item_id!r} not found")
        row = await replace(db, model(**data))
        return in_schema.model_validate(row, from_attributes=True)

    @sub.delete("/{item_id}", status_code=204)
    async def delete(item_id: str, request: Request, db: AsyncDatabase = Depends(get_db)):
        if await get(db, model, item_id) is None:
            raise HTTPException(404, f"{model.__name__} {item_id!r} not found")
        # No foreign keys in Mongo: what the FK used to refuse is checked
        # explicitly (app.repositories.refs).
        referenced_by = await references_to(db, model.COLLECTION, item_id)
        if referenced_by:
            raise HTTPException(
                409, f"Cannot delete {model.__name__} {item_id!r}: referenced by other rows ({', '.join(referenced_by)})"
            )
        await delete_doc(db, model, item_id)
        if model is SystemFootprint:
            await _drop_footprint_vector(request, item_id)

    return sub


router.include_router(make_crud_router(prefix="/teams", model=Team, in_schema=TeamIn))
router.include_router(make_crud_router(prefix="/environments", model=Environment, in_schema=EnvironmentIn))
router.include_router(
    make_crud_router(
        prefix="/source-systems",
        model=SourceSystem,
        in_schema=SourceSystemIn,
        fk_checks=(("owning_team_id", Team),),
    )
)
router.include_router(
    make_crud_router(
        prefix="/footprints",
        model=SystemFootprint,
        in_schema=SystemFootprintIn,
        fk_checks=(("source_system_id", SourceSystem),),
        filterable_by_source_system=True,
    )
)
router.include_router(
    make_crud_router(
        prefix="/mapping-rules",
        model=IncidentMappingRule,
        in_schema=IncidentMappingRuleIn,
        fk_checks=(("source_system_id", SourceSystem),),
        filterable_by_source_system=True,
    )
)
