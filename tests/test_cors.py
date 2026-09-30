import pytest
from fastapi.testclient import TestClient

import app.main as main_module


@pytest.fixture()
def make_app(mongo_url, mongo_db_name, redis_url, rabbitmq_url, vector_store):
    def make():
        return main_module.create_app(
            mongodb_url=mongo_url, mongodb_db=mongo_db_name, redis_url=redis_url, rabbitmq_url=rabbitmq_url,
            vector_store=vector_store,
        )

    return make


def test_cors_disabled_by_default(make_app, monkeypatch):
    monkeypatch.setattr(main_module, "CORS_ALLOWED_ORIGINS", [])
    app = make_app()
    with TestClient(app) as client:
        response = client.get("/health", headers={"Origin": "http://localhost:3000"})

    assert "access-control-allow-origin" not in response.headers


def test_cors_allows_configured_origin(make_app, monkeypatch):
    monkeypatch.setattr(main_module, "CORS_ALLOWED_ORIGINS", ["http://localhost:3000"])
    app = make_app()
    with TestClient(app) as client:
        allowed = client.get("/health", headers={"Origin": "http://localhost:3000"})
        rejected = client.get("/health", headers={"Origin": "http://evil.example.com"})

    assert allowed.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "access-control-allow-origin" not in rejected.headers
