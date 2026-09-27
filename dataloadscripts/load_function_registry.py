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

import uuid

from sqlalchemy.orm import Session

from app.db import Base, make_engine, make_session_factory
from app.function_registry import DEFAULT_FUNCTION_REGISTRY, FunctionSpec
from app.models import FunctionDefinition, FunctionDefinitionVersion


def upsert_function_registry(session: Session, entries: dict[str, FunctionSpec] | None = None) -> int:
    """Returns how many functions were newly created."""
    entries = entries if entries is not None else DEFAULT_FUNCTION_REGISTRY
    created = 0
    for name, spec in entries.items():
        if session.get(FunctionDefinition, name) is not None:
            continue
        session.add(FunctionDefinition(id=name))
        session.flush()
        session.add(
            FunctionDefinitionVersion(
                id=str(uuid.uuid4()),
                function_definition_id=name,
                version_number=1,
                description=spec.description,
                params=[p.model_dump() for p in spec.params],
                default_retry=spec.default_retry.model_dump(),
                status="active",
                created_by="seed_script",
            )
        )
        created += 1
    session.commit()
    return created


def main() -> None:
    engine = make_engine()
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)

    with session_factory() as session:
        created = upsert_function_registry(session)

    print(f"Created {created} new function definitions ({len(DEFAULT_FUNCTION_REGISTRY) - created} already existed)")


if __name__ == "__main__":
    main()
