import pytest
from sqlalchemy import select

from app.db import Base, make_engine, make_session_factory
from app.models import RcaPatternType
from dataloadscripts.seed_rca_pattern_types import DEFAULT_PATTERNS, seed_default_patterns


@pytest.fixture()
def session_factory(postgres_url):
    engine = make_engine(postgres_url)
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_seeds_all_default_patterns(session_factory):
    with session_factory() as session:
        inserted = seed_default_patterns(session)
        assert inserted == len(DEFAULT_PATTERNS)

        rows = session.scalars(select(RcaPatternType)).all()
        assert {r.id for r in rows} == {p["id"] for p in DEFAULT_PATTERNS}
        assert all(r.status == "active" for r in rows)
        assert all(r.created_by == "rca_pattern_type_seed" for r in rows)


def test_is_idempotent_and_never_overwrites_a_raised_ceiling(session_factory):
    with session_factory() as session:
        seed_default_patterns(session)

        # Simulate an admin having deliberately raised a pattern's ceiling.
        pattern = session.get(RcaPatternType, "environment_config")
        pattern.max_rca_status = "Identified"
        session.commit()

        second = seed_default_patterns(session)
        assert second == 0

        refetched = session.get(RcaPatternType, "environment_config")
        assert refetched.max_rca_status == "Identified"  # not reset back to Probable
