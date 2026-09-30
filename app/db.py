"""MongoDB access: client factories, the FastAPI dependency, a transaction
helper, and the index definitions that replace the old relational schema.

There are no migrations. ensure_indexes() is idempotent and runs on app
startup, in every load script and in every test fixture -- the same role
Base.metadata.create_all used to play. Adding a field needs no DB change at
all; changing an index definition needs the old index dropped first.

Multi-document transactions (in_transaction) need a replica set, which is
why even the local Docker Mongo runs as a single-node one.
"""

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from fastapi import Request
from pymongo import ASCENDING, DESCENDING, AsyncMongoClient, IndexModel, MongoClient
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.database import Database

from app.config import MONGODB_DB, MONGODB_URL

T = TypeVar("T")


def make_mongo_client(url: str = MONGODB_URL) -> AsyncMongoClient:
    # tz_aware=False: reads return naive UTC datetimes, which is what every
    # comparison in this codebase (sweeper cutoffs, stats periods,
    # correlation windows) is written against.
    return AsyncMongoClient(url, tz_aware=False)


def make_sync_mongo_client(url: str = MONGODB_URL) -> MongoClient:
    return MongoClient(url, tz_aware=False)


def get_database(client: AsyncMongoClient, name: str = MONGODB_DB) -> AsyncDatabase:
    return client[name]


async def get_db(request: Request) -> AsyncDatabase:
    return request.app.state.db


async def in_transaction(db: AsyncDatabase, fn: Callable[[AsyncClientSession], Awaitable[T]]) -> T:
    """Runs fn(session) in one multi-document transaction. with_transaction
    retries transient errors and unknown commit results, so fn may run more
    than once: build everything it writes inside fn, not before it."""
    async with db.client.start_session() as session:
        return await session.with_transaction(fn)


# (collection, keys, options). Unique constraints that used to be
# relational constraints are marked; the two partial unique indexes on
# version status make "at most one approved/active version" a DB-enforced
# invariant, not just app logic.
INDEXES: list[tuple[str, list[tuple[str, int]], dict[str, Any]]] = [
    ("function_definition_version", [("function_definition_id", ASCENDING), ("version_number", ASCENDING)], {"unique": True}),
    (
        "function_definition_version",
        [("function_definition_id", ASCENDING)],
        {"unique": True, "partialFilterExpression": {"status": "active"}, "name": "uq_fdv_one_active"},
    ),
    ("function_definition_version", [("status", ASCENDING)], {}),
    ("environment", [("in_scope", ASCENDING)], {}),
    ("source_system", [("owning_team_id", ASCENDING)], {}),
    ("system_footprint", [("source_system_id", ASCENDING)], {}),
    ("incident_mapping_rule", [("source_system_id", ASCENDING), ("category", ASCENDING)], {}),
    ("workflow_definition", [("source_system_id", ASCENDING), ("category", ASCENDING)], {"unique": True}),
    ("workflow_definition_version", [("workflow_definition_id", ASCENDING), ("version_number", ASCENDING)], {"unique": True}),
    (
        "workflow_definition_version",
        [("workflow_definition_id", ASCENDING)],
        {"unique": True, "partialFilterExpression": {"status": "approved"}, "name": "uq_wdv_one_approved"},
    ),
    ("workflow_build_request", [("created_at", DESCENDING)], {}),
    ("workflow_build_request", [("status", ASCENDING)], {}),
    ("workflow_execution", [("incident_id", ASCENDING), ("started_at", DESCENDING)], {}),
    ("workflow_execution", [("jira_key", ASCENDING), ("started_at", DESCENDING)], {}),
    ("workflow_execution", [("incident_key", ASCENDING), ("started_at", DESCENDING)], {}),
    ("workflow_execution", [("started_at", DESCENDING)], {}),
    ("workflow_execution", [("incident_id", ASCENDING), ("triggered_by", ASCENDING)], {}),
    ("rca_feedback", [("workflow_execution_id", ASCENDING), ("created_at", ASCENDING)], {}),
    ("rca_feedback", [("corrected_pattern_id", ASCENDING)], {}),
    ("rca_pattern_type", [("status", ASCENDING)], {}),
    ("correlation_group", [("source_system_id", ASCENDING), ("category", ASCENDING), ("status", ASCENDING)], {}),
    ("incident", [("incident_key", ASCENDING)], {"unique": True}),
    ("incident", [("source", ASCENDING), ("external_id", ASCENDING)], {"unique": True}),
    # Partial, not plain unique: most webhook incidents have no Jira key,
    # and a plain unique index would treat every null as the same value.
    (
        "incident",
        [("jira_key", ASCENDING)],
        {"unique": True, "partialFilterExpression": {"jira_key": {"$type": "string"}}, "name": "uq_incident_jira_key"},
    ),
    ("incident", [("classification_status", ASCENDING), ("received_at", ASCENDING)], {}),
    ("incident", [("source_system_id", ASCENDING), ("category", ASCENDING), ("received_at", ASCENDING)], {}),
    ("incident", [("correlation_group_id", ASCENDING)], {}),
    ("incident", [("received_at", DESCENDING)], {}),
    # Reference checks before catalog deletes (app.repositories.refs).
    ("incident", [("addressed_team_id", ASCENDING)], {}),
    ("incident", [("environment_id", ASCENDING)], {}),
    ("incident", [("matched_rule_id", ASCENDING)], {}),
]


def _index_models() -> dict[str, list[IndexModel]]:
    by_collection: dict[str, list[IndexModel]] = {}
    for collection, keys, options in INDEXES:
        by_collection.setdefault(collection, []).append(IndexModel(keys, **options))
    return by_collection


async def ensure_indexes(db: AsyncDatabase) -> None:
    for collection, models in _index_models().items():
        await db[collection].create_indexes(models)


def ensure_indexes_sync(db: Database) -> None:
    for collection, models in _index_models().items():
        db[collection].create_indexes(models)
