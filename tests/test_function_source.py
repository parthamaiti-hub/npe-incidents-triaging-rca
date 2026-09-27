import pytest
from fastapi.testclient import TestClient

from app.db import Base, make_engine, make_session_factory
from app.function_registry import FUNCTION_REGISTRY, reset_function_registry
from app.main import create_app


@pytest.fixture()
def app(postgres_url, redis_url, rabbitmq_url):
    engine = make_engine(postgres_url)
    Base.metadata.create_all(engine)
    yield create_app(database_url=postgres_url, redis_url=redis_url, rabbitmq_url=rabbitmq_url)
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture(autouse=True)
def restore_function_registry():
    snapshot = dict(FUNCTION_REGISTRY)
    yield
    reset_function_registry(snapshot)


def _seed_registry_with_v2(postgres_url):
    from dataloadscripts.load_function_registry import upsert_function_registry
    from dataloadscripts.seed_functional_dummy_versions import publish_functional_dummy_versions

    engine = make_engine(postgres_url)
    with make_session_factory(engine)() as session:
        upsert_function_registry(session)
        publish_functional_dummy_versions(session)
    engine.dispose()


def test_source_for_v2_functional_dummy_returns_real_python(app, postgres_url):
    _seed_registry_with_v2(postgres_url)
    with TestClient(app) as client:
        response = client.get("/functions/error_logs/versions/2/source")

    assert response.status_code == 200
    body = response.json()
    assert body["has_implementation"] is True
    assert body["language"] == "python"
    # 2026-09-23: each check_type's v2 lives in its own module
    # (app/check_types/error_logs_v2.py) with a same-named `run()` function
    # across every module -- the module's docstring is what identifies it
    # now, not a uniquely-named function.
    assert "async def run(params: dict) -> dict:" in body["code"]
    assert "error_logs" in body["code"]


def test_source_for_v1_stub_returns_template(app, postgres_url):
    _seed_registry_with_v2(postgres_url)
    with TestClient(app) as client:
        response = client.get("/functions/error_logs/versions/1/source")

    assert response.status_code == 200
    body = response.json()
    assert body["has_implementation"] is True
    assert body["language"] == "template"
    assert "STUBBED" in body["code"]


def test_source_for_unimplemented_version_reports_gracefully(app, postgres_url):
    # error_logs has code for v1 (STUB_RESULTS) and v2 (CHECK_IMPLEMENTATIONS_V2)
    # -- publishing a v3 contract with neither is exactly the "contract
    # published, no implementation yet" gap this endpoint must degrade
    # gracefully for (show the contract, not an error).
    _seed_registry_with_v2(postgres_url)
    with TestClient(app) as client:
        published = client.post(
            "/functions/error_logs/versions",
            json={
                "name": "error_logs",
                "description": "v3 contract only, no code yet",
                "params": [{"name": "env", "type": "string", "required": True}],
                "default_retry": {},
                "created_by": "test",
            },
        )
        assert published.status_code == 201, published.text
        assert published.json()["version_number"] == 3

        response = client.get("/functions/error_logs/versions/3/source")

    assert response.status_code == 200
    body = response.json()
    assert body["has_implementation"] is False
    assert body["language"] is None
    assert body["code"] is None


def test_source_for_nonexistent_version_404s(app, postgres_url):
    _seed_registry_with_v2(postgres_url)
    with TestClient(app) as client:
        response = client.get("/functions/error_logs/versions/99/source")
    assert response.status_code == 404
