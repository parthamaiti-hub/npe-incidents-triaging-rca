"""Idempotently seeds function_definition + an initial active
function_definition_version (version 1) from
app.function_registry.DEFAULT_FUNCTION_REGISTRY -- the DB-backed source of
truth app/routers/functions.py's CRUD and app.function_registry.FUNCTION_REGISTRY
(the in-memory cache every validator/dispatcher reads) build on. The
defaults live in Python (not a YAML file) since they're also the fallback
FUNCTION_REGISTRY starts with before any DB call -- one source of truth,
not two.

Only creates a function that doesn't already exist -- re-running this after
a function has been published to a later version (via the API) leaves it
alone rather than resetting it back to the v1 defaults.

Usage:
    uv run python -m dataloadscripts.load_function_registry
"""

from pymongo.database import Database

from app.db import ensure_indexes_sync, get_database, make_sync_mongo_client
from app.function_registry import DEFAULT_FUNCTION_REGISTRY, FunctionSpec
from app.models import FunctionDefinition, FunctionDefinitionVersion


def upsert_function_registry(db: Database, entries: dict[str, FunctionSpec] | None = None) -> int:
    """Returns how many functions were newly created."""
    entries = entries if entries is not None else DEFAULT_FUNCTION_REGISTRY

    def seed(session) -> int:
        count = 0
        for name, spec in entries.items():
            if db[FunctionDefinition.COLLECTION].find_one({"_id": name}, session=session) is not None:
                continue
            db[FunctionDefinition.COLLECTION].insert_one(FunctionDefinition(id=name).to_doc(), session=session)
            version = FunctionDefinitionVersion(
                function_definition_id=name,
                version_number=1,
                description=spec.description,
                params=[p.model_dump() for p in spec.params],
                default_retry=spec.default_retry.model_dump(),
                status="active",
                created_by="seed_script",
            )
            db[FunctionDefinitionVersion.COLLECTION].insert_one(version.to_doc(), session=session)
            count += 1
        return count

    with db.client.start_session() as session:
        return session.with_transaction(seed)


def main() -> None:
    client = make_sync_mongo_client()
    try:
        db = get_database(client)
        ensure_indexes_sync(db)
        created = upsert_function_registry(db)
    finally:
        client.close()

    print(f"Created {created} new function definitions ({len(DEFAULT_FUNCTION_REGISTRY) - created} already existed)")


if __name__ == "__main__":
    main()
