import pytest
from fastapi.testclient import TestClient

from app.db import Base, make_engine
from app.main import create_app


@pytest.fixture()
def app(postgres_url, redis_url, rabbitmq_url):
    engine = make_engine(postgres_url)
    Base.metadata.create_all(engine)
    yield create_app(
        database_url=postgres_url,
        redis_url=redis_url,
        rabbitmq_url=rabbitmq_url,
    )
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_health_reports_ok_when_all_dependencies_are_reachable(app):
    with TestClient(app) as client:
        response = client.get("/health")

    print(f"\n[OUTCOME] {response.json()}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"] == {"database": "ok", "redis": "ok", "rabbitmq": "ok"}


def test_health_reports_unhealthy_when_database_is_unreachable(redis_url, rabbitmq_url):
    app = create_app(
        # nothing listens on port 1; connect_timeout keeps this test fast
        # instead of waiting on the OS's full TCP connect timeout.
        database_url="postgresql+psycopg://npe:npe@localhost:1/npe_triage?connect_timeout=2",
        redis_url=redis_url,
        rabbitmq_url=rabbitmq_url,
    )
    with TestClient(app) as client:
        response = client.get("/health")

    print(f"\n[OUTCOME] {response.json()}")
    assert response.status_code == 503
    assert response.json()["status"] == "unhealthy"
    assert "error" in response.json()["checks"]["database"]
