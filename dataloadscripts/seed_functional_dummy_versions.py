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

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.check_implementations import CHECK_IMPLEMENTATIONS_V2
from app.db import Base, make_engine, make_session_factory
from app.function_registry import DEFAULT_FUNCTION_REGISTRY
from app.models import FunctionDefinition, FunctionDefinitionVersion

DUMMY_CREATED_BY = "functional_dummy_seed"


def publish_functional_dummy_versions(session: Session) -> int:
    """Returns how many functions got a newly-published v2 (functions that
    already had an active v2 are skipped, not re-published)."""
    published = 0
    for name, spec in DEFAULT_FUNCTION_REGISTRY.items():
        if name not in CHECK_IMPLEMENTATIONS_V2:
            continue  # no real v2 code for this check_type yet -- nothing to publish

        if session.get(FunctionDefinition, name) is None:
            session.add(FunctionDefinition(id=name))
            session.flush()

        existing_v2 = session.scalars(
            select(FunctionDefinitionVersion).where(
                FunctionDefinitionVersion.function_definition_id == name,
                FunctionDefinitionVersion.version_number == 2,
            )
        ).first()

        if existing_v2 is not None:
            if existing_v2.status != "active":
                for other in session.scalars(
                    select(FunctionDefinitionVersion).where(
                        FunctionDefinitionVersion.function_definition_id == name,
                        FunctionDefinitionVersion.status == "active",
                    )
                ).all():
                    other.status = "superseded"
                existing_v2.status = "active"
            continue

        for current_active in session.scalars(
            select(FunctionDefinitionVersion).where(
                FunctionDefinitionVersion.function_definition_id == name,
                FunctionDefinitionVersion.status == "active",
            )
        ).all():
            current_active.status = "superseded"

        session.add(
            FunctionDefinitionVersion(
                id=str(uuid.uuid4()),
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
        )
        published += 1

    session.commit()
    return published


def main() -> None:
    engine = make_engine()
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)

    with session_factory() as session:
        published = publish_functional_dummy_versions(session)

    print(f"Published functional dummy v2 for {published} functions ({len(CHECK_IMPLEMENTATIONS_V2)} have v2 code)")


if __name__ == "__main__":
    main()
