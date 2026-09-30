import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import create_app
from app.vector_store import VectorStore

# Nothing listens on port 1; short timeouts keep these tests fast instead of
# waiting on the drivers' default server-selection/connect timeouts.
UNREACHABLE_MONGO = "mongodb://localhost:1/?directConnection=true&serverSelectionTimeoutMS=1000"


def _unreachable_vector_store() -> VectorStore:
    return VectorStore(host="localhost", port=1)


@pytest.fixture()
def app(mongo_url, mongo_db_name, redis_url, rabbitmq_url, vector_store):
    return create_app(
        mongodb_url=mongo_url,
        mongodb_db=mongo_db_name,
        redis_url=redis_url,
        rabbitmq_url=rabbitmq_url,
        vector_store=vector_store,
    )


def test_health_reports_ok_when_all_dependencies_are_reachable(app):
    with TestClient(app) as client:
        response = client.get("/health")

    print(f"\n[OUTCOME] {response.json()}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"] == {"database": "ok", "redis": "ok", "rabbitmq": "ok", "vector_store": "ok"}


def test_health_reports_unhealthy_when_database_is_unreachable(redis_url, rabbitmq_url, vector_store):
    app = create_app(
        mongodb_url=UNREACHABLE_MONGO,
        redis_url=redis_url,
        rabbitmq_url=rabbitmq_url,
        vector_store=vector_store,
    )
    with TestClient(app) as client:
        response = client.get("/health")

    print(f"\n[OUTCOME] {response.json()}")
    assert response.status_code == 503
    assert response.json()["status"] == "unhealthy"
    assert "error" in response.json()["checks"]["database"]


def test_health_stays_ok_without_vector_store_when_llm_features_are_off(
    mongo_url, mongo_db_name, redis_url, rabbitmq_url
):
    """Chroma only backs the LLM/RAG paths; the deterministic pipeline must
    not report unhealthy because it's down."""
    app = create_app(
        mongodb_url=mongo_url,
        mongodb_db=mongo_db_name,
        redis_url=redis_url,
        rabbitmq_url=rabbitmq_url,
        vector_store=_unreachable_vector_store(),
    )
    with TestClient(app) as client:
        response = client.get("/health")

    print(f"\n[OUTCOME] {response.json()}")
    assert response.status_code == 200
    assert response.json()["checks"]["vector_store"].startswith("unavailable")


def test_health_unhealthy_without_vector_store_when_an_llm_feature_is_on(
    mongo_url, mongo_db_name, redis_url, rabbitmq_url, monkeypatch
):
    monkeypatch.setattr(main_module, "RCA_SYNTHESIS_LLM_ENABLED", True)
    app = create_app(
        mongodb_url=mongo_url,
        mongodb_db=mongo_db_name,
        redis_url=redis_url,
        rabbitmq_url=rabbitmq_url,
        vector_store=_unreachable_vector_store(),
    )
    with TestClient(app) as client:
        response = client.get("/health")

    print(f"\n[OUTCOME] {response.json()}")
    assert response.status_code == 503
    assert "error" in response.json()["checks"]["vector_store"]
