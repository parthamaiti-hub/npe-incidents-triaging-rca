import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.db import Base, make_engine


@pytest.fixture()
def db(postgres_url):
    engine = make_engine(postgres_url)
    Base.metadata.create_all(engine)
    yield postgres_url
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_cors_disabled_by_default(db, redis_url, rabbitmq_url, monkeypatch):
    monkeypatch.setattr(main_module, "CORS_ALLOWED_ORIGINS", [])
    app = main_module.create_app(database_url=db, redis_url=redis_url, rabbitmq_url=rabbitmq_url)
    with TestClient(app) as client:
        response = client.get("/health", headers={"Origin": "http://localhost:3000"})

    assert "access-control-allow-origin" not in response.headers


def test_cors_allows_configured_origin(db, redis_url, rabbitmq_url, monkeypatch):
    monkeypatch.setattr(main_module, "CORS_ALLOWED_ORIGINS", ["http://localhost:3000"])
    app = main_module.create_app(database_url=db, redis_url=redis_url, rabbitmq_url=rabbitmq_url)
    with TestClient(app) as client:
        allowed = client.get("/health", headers={"Origin": "http://localhost:3000"})
        rejected = client.get("/health", headers={"Origin": "http://evil.example.com"})

    assert allowed.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "access-control-allow-origin" not in rejected.headers
