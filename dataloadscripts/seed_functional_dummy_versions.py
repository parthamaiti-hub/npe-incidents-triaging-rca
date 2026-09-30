"""Publishes version 2 -- the "functional dummy" -- as the active version
for every function in the registry. Version 2's actual code lives in
app/check_implementations.py: real per-function async Python (not a
template-fill string like version 1), each doing real introspection (OS
user, calling function, timestamp), logging it, and embedding it in the
returned evidence. Running a workflow after this shows in both the logs and
the evidence exactly which functions were actually invoked -- for testing
"did every function in this workflow get called", not for producing
realistic monitoring data.

Idempotent: if a function already has an active version 2, leaves it alone.
Explicitly targets version_number=2 (not "whatever's next") so it always
lines up with app.check_implementations.CHECK_IMPLEMENTATIONS_V2's fixed
version-2 dispatch -- see app/checks.py::run_check.

Safe to run against a live catalog: existing WorkflowDefinitionVersion
documents keep executing whichever version they were built against
(the reproducibility guarantee) -- this only changes what a *new*
build/load pins to going forward.

Usage:
    uv run python -m dataloadscripts.seed_functional_dummy_versions
"""

from pymongo.database import Database

from app.check_implementations import CHECK_IMPLEMENTATIONS_V2
from app.db import ensure_indexes_sync, get_database, make_sync_mongo_client
from app.function_registry import DEFAULT_FUNCTION_REGISTRY
from app.models import FunctionDefinition, FunctionDefinitionVersion

DUMMY_CREATED_BY = "functional_dummy_seed"


def publish_functional_dummy_versions(db: Database) -> int:
    """Returns how many functions got a newly-published v2 (functions that
    already had an active v2 are skipped, not re-published)."""
    definitions = db[FunctionDefinition.COLLECTION]
    versions = db[FunctionDefinitionVersion.COLLECTION]

    def publish(session) -> int:
        published = 0
        for name, spec in DEFAULT_FUNCTION_REGISTRY.items():
            if name not in CHECK_IMPLEMENTATIONS_V2:
                continue  # no real v2 code for this check_type yet -- nothing to publish

            if definitions.find_one({"_id": name}, session=session) is None:
                definitions.insert_one(FunctionDefinition(id=name).to_doc(), session=session)

            existing_v2 = versions.find_one({"function_definition_id": name, "version_number": 2}, session=session)

            # Always supersede the current active version *before* activating
            # another one: one active version per function is index-enforced.
            if existing_v2 is not None:
                if existing_v2["status"] != "active":
                    versions.update_many(
                        {"function_definition_id": name, "status": "active"},
                        {"$set": {"status": "superseded"}},
                        session=session,
                    )
                    versions.update_one({"_id": existing_v2["_id"]}, {"$set": {"status": "active"}}, session=session)
                continue

            versions.update_many(
                {"function_definition_id": name, "status": "active"}, {"$set": {"status": "superseded"}}, session=session
            )
            version = FunctionDefinitionVersion(
                function_definition_id=name,
                version_number=2,
                description=(
                    f"[FUNCTIONAL DUMMY v2] {spec.description} Real callable "
                    f"(app.check_implementations.{name}_v2) that logs the OS user, "
                    f"calling function, and timestamp on every invocation."
                ),
                params=[p.model_dump() for p in spec.params],
                default_retry=spec.default_retry.model_dump(),
                status="active",
                created_by=DUMMY_CREATED_BY,
            )
            versions.insert_one(version.to_doc(), session=session)
            published += 1
        return published

    with db.client.start_session() as session:
        return session.with_transaction(publish)


def main() -> None:
    client = make_sync_mongo_client()
    try:
        db = get_database(client)
        ensure_indexes_sync(db)
        published = publish_functional_dummy_versions(db)
    finally:
        client.close()

    print(f"Published functional dummy v2 for {published} functions ({len(CHECK_IMPLEMENTATIONS_V2)} have v2 code)")


if __name__ == "__main__":
    main()
