"""Generic document reads/writes over app.models. Every write is explicit:
there is no unit of work and no dirty tracking, so a model mutated in
memory is only persisted by one of the write helpers below.

Every helper takes an optional `session` so it can join an
app.db.in_transaction block.
"""

from typing import Any, TypeVar

from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.asynchronous.database import AsyncDatabase

from app.models import Document

D = TypeVar("D", bound=Document)

Sort = list[tuple[str, int]]


async def get(db: AsyncDatabase, model: type[D], id: str, session: AsyncClientSession | None = None) -> D | None:
    doc = await db[model.COLLECTION].find_one({"_id": id}, session=session)
    return model.from_doc(doc) if doc is not None else None


async def find(
    db: AsyncDatabase,
    model: type[D],
    filter: dict | None = None,
    *,
    sort: Sort | None = None,
    skip: int = 0,
    limit: int = 0,
    session: AsyncClientSession | None = None,
) -> list[D]:
    cursor = db[model.COLLECTION].find(filter or {}, sort=sort, skip=skip, limit=limit, session=session)
    return [model.from_doc(doc) async for doc in cursor]


async def find_one(
    db: AsyncDatabase,
    model: type[D],
    filter: dict,
    *,
    sort: Sort | None = None,
    session: AsyncClientSession | None = None,
) -> D | None:
    doc = await db[model.COLLECTION].find_one(filter, sort=sort, session=session)
    return model.from_doc(doc) if doc is not None else None


async def exists(db: AsyncDatabase, model: type[Document], filter: dict, session: AsyncClientSession | None = None) -> bool:
    return await db[model.COLLECTION].find_one(filter, projection={"_id": 1}, session=session) is not None


async def insert(db: AsyncDatabase, obj: D, session: AsyncClientSession | None = None) -> D:
    await db[obj.COLLECTION].insert_one(obj.to_doc(), session=session)
    return obj


async def replace(db: AsyncDatabase, obj: D, *, upsert: bool = False, session: AsyncClientSession | None = None) -> D:
    doc = obj.to_doc()
    await db[obj.COLLECTION].replace_one({"_id": doc["_id"]}, doc, upsert=upsert, session=session)
    return obj


async def update_fields(
    db: AsyncDatabase, obj: Document, fields: dict[str, Any], session: AsyncClientSession | None = None
) -> None:
    """Sets `fields` on the in-memory model (validated) and $sets exactly
    those fields in the database -- nothing else, so a concurrent writer's
    change to another field is never clobbered by a stale in-memory copy."""
    for key, value in fields.items():
        setattr(obj, key, value)
    await db[obj.COLLECTION].update_one(
        {"_id": obj.id}, {"$set": {key: getattr(obj, key) for key in fields}}, session=session
    )


async def delete(db: AsyncDatabase, model: type[Document], id: str, session: AsyncClientSession | None = None) -> bool:
    result = await db[model.COLLECTION].delete_one({"_id": id}, session=session)
    return result.deleted_count == 1


async def max_version_number(
    db: AsyncDatabase, model: type[Document], definition_field: str, definition_id: str, session: AsyncClientSession | None = None
) -> int:
    """Highest version_number for one definition, 0 if it has none. A racing
    `max + 1` insert is caught by the (definition, version_number) unique
    index and surfaces as DuplicateKeyError."""
    doc = await db[model.COLLECTION].find_one(
        {definition_field: definition_id},
        projection={"version_number": 1},
        sort=[("version_number", -1)],
        session=session,
    )
    return doc["version_number"] if doc is not None else 0
