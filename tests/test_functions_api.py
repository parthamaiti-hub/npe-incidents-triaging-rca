import pytest
from fastapi.testclient import TestClient

from app.function_registry import FUNCTION_REGISTRY, reset_function_registry
from app.main import create_app


@pytest.fixture()
def app(mongo_url, mongo_db_name, redis_url, rabbitmq_url, vector_store):
    return create_app(
        mongodb_url=mongo_url, mongodb_db=mongo_db_name, redis_url=redis_url, rabbitmq_url=rabbitmq_url,
        vector_store=vector_store,
    )


@pytest.fixture(autouse=True)
def restore_function_registry():
    # FUNCTION_REGISTRY is a process-wide, mutated-in-place singleton --
    # tests here create/publish/delete entries through the API, which
    # mutates it directly. Restore it so other test modules (and other
    # tests in this module) never see a polluted registry.
    snapshot = dict(FUNCTION_REGISTRY)
    yield
    reset_function_registry(snapshot)


NEW_FUNCTION = {
    "name": "test_only_check",
    "description": "A function that only exists for this test.",
    "params": [{"name": "env", "type": "string", "required": True}],
    "default_retry": {"max_attempts": 2, "delay_seconds": 1, "exponential_backoff": False},
    "created_by": "neelburu@outlook.com",
}


def test_list_functions_matches_the_seeded_defaults(app, sync_db):
    from app.function_registry import DEFAULT_FUNCTION_REGISTRY
    from dataloadscripts.load_function_registry import upsert_function_registry

    upsert_function_registry(sync_db)

    with TestClient(app) as client:
        response = client.get("/functions")

    assert response.status_code == 200
    body = response.json()
    assert {f["name"] for f in body} == set(DEFAULT_FUNCTION_REGISTRY.keys())
    assert all(f["version_number"] == 1 for f in body)
    assert all(f["version_id"] for f in body)


def test_create_publish_version_and_delete_function(app):
    with TestClient(app) as client:
        # create (version 1)
        created = client.post("/functions", json=NEW_FUNCTION)
        assert created.status_code == 201, created.text
        assert created.json()["version_number"] == 1
        v1_id = created.json()["version_id"]
        assert FUNCTION_REGISTRY["test_only_check"].version_number == 1

        # duplicate create rejected
        dup = client.post("/functions", json=NEW_FUNCTION)
        assert dup.status_code == 409

        # list/get reflect the current (v1) version
        listed = client.get("/functions")
        assert any(f["name"] == "test_only_check" for f in listed.json())
        got = client.get("/functions/test_only_check")
        assert got.status_code == 200
        assert got.json()["version_id"] == v1_id

        # publish version 2 -- supersedes v1, becomes current
        v2_body = dict(NEW_FUNCTION)
        v2_body["description"] = "Updated for v2."
        v2_body["params"] = [{"name": "env", "type": "string", "required": True}, {"name": "app", "type": "string", "required": True}]
        published = client.post("/functions/test_only_check/versions", json=v2_body)
        assert published.status_code == 201, published.text
        assert published.json()["version_number"] == 2
        v2_id = published.json()["version_id"]
        assert v2_id != v1_id
        assert FUNCTION_REGISTRY["test_only_check"].version_number == 2
        assert FUNCTION_REGISTRY["test_only_check"].version_id == v2_id

        # current view now shows v2
        current = client.get("/functions/test_only_check")
        assert current.json()["version_id"] == v2_id
        assert current.json()["description"] == "Updated for v2."

        # full history shows both, v1 no longer "current" but still queryable
        history = client.get("/functions/test_only_check/versions")
        assert history.status_code == 200
        assert [v["version_number"] for v in history.json()] == [1, 2]

        # delete removes the function and all its versions
        deleted = client.delete("/functions/test_only_check")
        assert deleted.status_code == 204
        assert "test_only_check" not in FUNCTION_REGISTRY
        assert client.get("/functions/test_only_check").status_code == 404
        assert client.get("/functions/test_only_check/versions").status_code == 404


def test_get_unknown_function_404s(app):
    with TestClient(app) as client:
        response = client.get("/functions/not_a_real_function")
    assert response.status_code == 404


def test_publish_version_for_unknown_function_404s(app):
    body = dict(NEW_FUNCTION)
    body["name"] = "not_a_real_function"  # must match the path param to reach the not-found check
    with TestClient(app) as client:
        response = client.post("/functions/not_a_real_function/versions", json=body)
    assert response.status_code == 404


def test_publish_version_rejects_body_name_mismatch(app):
    with TestClient(app) as client:
        client.post("/functions", json=NEW_FUNCTION)
        mismatched = dict(NEW_FUNCTION)
        mismatched["name"] = "a_different_name"
        response = client.post("/functions/test_only_check/versions", json=mismatched)
    assert response.status_code == 422


def test_at_most_one_active_version_is_enforced_by_the_database(sync_db):
    """Stronger than the old app-logic-only invariant: a second 'active'
    version for the same function can't be written at all."""
    import pymongo.errors

    from app.models import FunctionDefinition, FunctionDefinitionVersion
    from dataloadscripts.test_fixtures import insert_docs

    def version(n):
        return FunctionDefinitionVersion(
            function_definition_id="f", version_number=n, description="d", params=[], default_retry={},
            status="active", created_by="t",
        )

    insert_docs(sync_db, FunctionDefinition(id="f"), version(1))
    with pytest.raises(pymongo.errors.DuplicateKeyError):
        insert_docs(sync_db, version(2))
