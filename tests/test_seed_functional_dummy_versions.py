from app.check_implementations import CHECK_IMPLEMENTATIONS_V2
from app.models import FunctionDefinitionVersion
from dataloadscripts.load_function_registry import upsert_function_registry
from dataloadscripts.seed_functional_dummy_versions import publish_functional_dummy_versions
from dataloadscripts.test_fixtures import find_docs


def test_publishes_an_active_v2_for_every_function_with_v2_code(sync_db):
    upsert_function_registry(sync_db)  # seeds v1 first, like a normal setup would
    published = publish_functional_dummy_versions(sync_db)

    assert published == len(CHECK_IMPLEMENTATIONS_V2) == 20

    rows = find_docs(sync_db, FunctionDefinitionVersion)
    by_function: dict[str, list[FunctionDefinitionVersion]] = {}
    for row in rows:
        by_function.setdefault(row.function_definition_id, []).append(row)

    for name, versions in by_function.items():
        v1 = next(v for v in versions if v.version_number == 1)
        v2 = next(v for v in versions if v.version_number == 2)
        assert v1.status == "superseded"
        assert v2.status == "active"
        assert v2.created_by == "functional_dummy_seed"


def test_is_idempotent(sync_db):
    upsert_function_registry(sync_db)
    first = publish_functional_dummy_versions(sync_db)
    second = publish_functional_dummy_versions(sync_db)

    assert first == 20
    assert second == 0  # already published, nothing new to do

    rows = find_docs(sync_db, FunctionDefinitionVersion)
    assert len(rows) == 40  # v1 + v2 per function, no duplicates from the second run
