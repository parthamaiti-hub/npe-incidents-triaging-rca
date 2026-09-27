"""Full CRUD REST API for the catalog entities that dataloadscripts/load_catalog.py
bulk-loads from YAML -- teams, environments, source systems, footprints,
mapping rules. Reuses app.schemas' *In models (already validated: footprint
value / mapping rule pattern must compile as regex) as both the request body
and the response shape, since they mirror each SQLAlchemy model's columns
1:1. Every resource gets the same 5 operations via make_crud_router, so
adding a 6th catalog entity later is a 3-line addition, not a new endpoint
set to hand-write.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_async_session
from app.models import Environment, IncidentMappingRule, SourceSystem, SystemFootprint, Team
from app.schemas import EnvironmentIn, IncidentMappingRuleIn, SourceSystemIn, SystemFootprintIn, TeamIn

router = APIRouter(prefix="/catalog", tags=["catalog"])


def make_crud_router(
    *,
    prefix: str,
    model,
    in_schema: type[BaseModel],
    fk_checks: tuple[tuple[str, type], ...] = (),
    filterable_by_source_system: bool = False,
) -> APIRouter:
    sub = APIRouter(prefix=prefix)

    async def _check_fks(session: AsyncSession, data: dict) -> None:
        for field, fk_model in fk_checks:
            value = data.get(field)
            if value is not None and await session.get(fk_model, value) is None:
                raise HTTPException(422, f"{field}={value!r} references unknown {fk_model.__name__}")

    @sub.get("", response_model=list[in_schema])
    async def list_all(source_system_id: str | None = None, session: AsyncSession = Depends(get_async_session)):
        stmt = select(model)
        # Footprints/mapping-rules are the two entities the
        # playbook tab needs scoped to one system (its "matched mapping
        # rule" header) -- source_system_id is simply ignored for entities
        # that don't declare filterable_by_source_system (teams,
        # environments, source-systems themselves).
        if filterable_by_source_system and source_system_id is not None:
            stmt = stmt.where(model.source_system_id == source_system_id)
        rows = (await session.scalars(stmt)).all()
        return [in_schema.model_validate(r, from_attributes=True) for r in rows]

    @sub.get("/{item_id}", response_model=in_schema)
    async def get_one(item_id: str, session: AsyncSession = Depends(get_async_session)):
        row = await session.get(model, item_id)
        if row is None:
            raise HTTPException(404, f"{model.__name__} {item_id!r} not found")
        return in_schema.model_validate(row, from_attributes=True)

    @sub.post("", response_model=in_schema, status_code=201)
    async def create(payload: in_schema, session: AsyncSession = Depends(get_async_session)):
        data = payload.model_dump()
        await _check_fks(session, data)
        if await session.get(model, data["id"]) is not None:
            raise HTTPException(409, f"{model.__name__} {data['id']!r} already exists")
        row = model(**data)
        session.add(row)
        await session.commit()
        return in_schema.model_validate(row, from_attributes=True)

    @sub.put("/{item_id}", response_model=in_schema)
    async def update(item_id: str, payload: in_schema, session: AsyncSession = Depends(get_async_session)):
        if payload.id != item_id:
            raise HTTPException(422, "Body id must match path item_id")
        data = payload.model_dump()
        await _check_fks(session, data)
        row = await session.get(model, item_id)
        if row is None:
            raise HTTPException(404, f"{model.__name__} {item_id!r} not found")
        for key, value in data.items():
            setattr(row, key, value)
        await session.commit()
        return in_schema.model_validate(row, from_attributes=True)

    @sub.delete("/{item_id}", status_code=204)
    async def delete(item_id: str, session: AsyncSession = Depends(get_async_session)):
        row = await session.get(model, item_id)
        if row is None:
            raise HTTPException(404, f"{model.__name__} {item_id!r} not found")
        await session.delete(row)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise HTTPException(409, f"Cannot delete {model.__name__} {item_id!r}: referenced by other rows")

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
