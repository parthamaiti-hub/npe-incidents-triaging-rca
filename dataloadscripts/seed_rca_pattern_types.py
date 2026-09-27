"""Seeds the RCA_PATTERN_TYPE catalog with today's
five named outcomes from app.rca_synthesizer's hardcoded PATTERN_RCA_STATUS
dict -- the starting catalog LLM/RAG-primary synthesis (app.rca_worker)
classifies evidence against. Growing this catalog beyond these five, or
raising a pattern's max_rca_status ceiling, is a human-curated action from
here on (the same approval discipline already used for playbook versions),
not something this script does again.

Idempotent: an existing row (matched by id) is left alone, never
overwritten -- if a pattern's ceiling has been deliberately raised in
production, re-running this script must not silently reset it.

Usage:
    uv run python -m dataloadscripts.seed_rca_pattern_types
"""

from sqlalchemy.orm import Session

from app import rca_status
from app.db import Base, make_engine, make_session_factory
from app.models import RcaPatternType

SEED_CREATED_BY = "rca_pattern_type_seed"

DEFAULT_PATTERNS: list[dict] = [
    {
        "id": "data_quality_pipeline_change",
        "description": "A data contract violation coincides with a recent upstream pipeline change.",
        "max_rca_status": rca_status.PROBABLE,
    },
    {
        "id": "environment_config",
        "description": "Service health or configuration drift evidence in this environment.",
        "max_rca_status": rca_status.PROBABLE,
    },
    {
        "id": "functional_defect_recent_release",
        "description": "Error logs coincide with a recent deployment to the affected component.",
        "max_rca_status": rca_status.PROBABLE,
    },
    {
        "id": "generic_issues_in_checks",
        "description": "One or more checks reported WARN/ERROR that don't fit a more specific pattern.",
        "max_rca_status": rca_status.INCONCLUSIVE,
    },
    {
        "id": "inconclusive",
        "description": "No check reported anything actionable.",
        "max_rca_status": rca_status.INCONCLUSIVE,
    },
]


def seed_default_patterns(session: Session) -> int:
    """Returns how many new RcaPatternType rows were inserted (existing
    rows are left untouched, not re-seeded)."""
    inserted = 0
    for pattern in DEFAULT_PATTERNS:
        if session.get(RcaPatternType, pattern["id"]) is not None:
            continue
        session.add(RcaPatternType(created_by=SEED_CREATED_BY, status="active", **pattern))
        inserted += 1
    session.commit()
    return inserted


def main() -> None:
    engine = make_engine()
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)

    with session_factory() as session:
        inserted = seed_default_patterns(session)

    print(f"Seeded {inserted} new RCA_PATTERN_TYPE rows ({len(DEFAULT_PATTERNS)} in the default set)")


if __name__ == "__main__":
    main()
