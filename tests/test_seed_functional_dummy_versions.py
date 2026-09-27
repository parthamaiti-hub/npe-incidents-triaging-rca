import pytest
from sqlalchemy import select

from app.check_implementations import CHECK_IMPLEMENTATIONS_V2
from app.db import Base, make_engine, make_session_factory
from app.models import FunctionDefinitionVersion
from dataloadscripts.load_function_registry import upsert_function_registry
from dataloadscripts.seed_functional_dummy_versions import publish_functional_dummy_versions


@pytest.fixture()
def session_factory(postgres_url):
    engine = make_engine(postgres_url)
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_publishes_an_active_v2_for_every_function_with_v2_code(session_factory):
    with session_factory() as session:
        upsert_function_registry(session)  # seeds v1 first, like a normal setup would
        published = publish_functional_dummy_versions(session)

        assert published == len(CHECK_IMPLEMENTATIONS_V2) == 20

        rows = session.scalars(select(FunctionDefinitionVersion)).all()
        by_function: dict[str, list[FunctionDefinitionVersion]] = {}
        for row in rows:
            by_function.setdefault(row.function_definition_id, []).append(row)

        for name, versions in by_function.items():
            v1 = next(v for v in versions if v.version_number == 1)
            v2 = next(v for v in versions if v.version_number == 2)
            assert v1.status == "superseded"
            assert v2.status == "active"
            assert v2.created_by == "functional_dummy_seed"


def test_is_idempotent(session_factory):
    with session_factory() as session:
        upsert_function_registry(session)
        first = publish_functional_dummy_versions(session)
        second = publish_functional_dummy_versions(session)

        assert first == 20
        assert second == 0  # already published, nothing new to do

        rows = session.scalars(select(FunctionDefinitionVersion)).all()
        assert len(rows) == 40  # v1 + v2 per function, no duplicates from the second run
