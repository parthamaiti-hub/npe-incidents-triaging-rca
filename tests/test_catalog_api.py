import pytest
from fastapi.testclient import TestClient

from app.db import Base, make_engine, make_session_factory
from app.main import create_app
from app.models import IncidentMappingRule, SourceSystem, SystemFootprint


@pytest.fixture()
def app(postgres_url, redis_url, rabbitmq_url):
    engine = make_engine(postgres_url)
    Base.metadata.create_all(engine)
    yield create_app(database_url=postgres_url, redis_url=redis_url, rabbitmq_url=rabbitmq_url)
    Base.metadata.drop_all(engine)
    engine.dispose()


def _seed(postgres_url):
    engine = make_engine(postgres_url)
    with make_session_factory(engine)() as session:
        session.add_all(
            [
                SourceSystem(
                    id="SYS_A", name="A", code="A", type="Application", description="a", owning_team="t", environment="NPE"
                ),
                SourceSystem(
                    id="SYS_B", name="B", code="B", type="Application", description="b", owning_team="t", environment="NPE"
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                SystemFootprint(id="FP_A1", source_system_id="SYS_A", footprint_type="hostname", value="a-host"),
                SystemFootprint(id="FP_B1", source_system_id="SYS_B", footprint_type="hostname", value="b-host"),
                IncidentMappingRule(
                    id="IMR_A",
                    source_system_id="SYS_A",
                    category="FUNCTIONAL DEFECT (QA/UAT)",
                    signal_type="hostname",
                    signal_pattern="a-host",
                    priority=1,
                    action="assign_to_A",
                ),
                IncidentMappingRule(
                    id="IMR_B",
                    source_system_id="SYS_B",
                    category="FUNCTIONAL DEFECT (QA/UAT)",
                    signal_type="hostname",
                    signal_pattern="b-host",
                    priority=1,
                    action="assign_to_B",
                ),
            ]
        )
        session.commit()
    engine.dispose()


def test_mapping_rules_filtered_by_source_system_id(app, postgres_url):
    _seed(postgres_url)
    with TestClient(app) as client:
        response = client.get("/catalog/mapping-rules", params={"source_system_id": "SYS_A"})

    assert response.status_code == 200
    rows = response.json()
    assert [r["id"] for r in rows] == ["IMR_A"]


def test_footprints_filtered_by_source_system_id(app, postgres_url):
    _seed(postgres_url)
    with TestClient(app) as client:
        response = client.get("/catalog/footprints", params={"source_system_id": "SYS_B"})

    assert response.status_code == 200
    rows = response.json()
    assert [r["id"] for r in rows] == ["FP_B1"]


def test_mapping_rules_unfiltered_returns_all(app, postgres_url):
    _seed(postgres_url)
    with TestClient(app) as client:
        response = client.get("/catalog/mapping-rules")

    assert {r["id"] for r in response.json()} == {"IMR_A", "IMR_B"}


# --- Write-path coverage for source-systems / footprints ------------------
# (the System Mapping Data UI is the first thing to exercise these paths
# for real -- previously only the read/filter paths above were tested.)

SOURCE_SYSTEM_PAYLOAD = {
    "id": "SYS_NEW",
    "name": "New System",
    "code": "NEW",
    "type": "Application",
    "description": "created via API",
    "owning_team": "team-x",
    "environment": "NPE",
}


def test_create_source_system_succeeds(app):
    with TestClient(app) as client:
        response = client.post("/catalog/source-systems", json=SOURCE_SYSTEM_PAYLOAD)

    assert response.status_code == 201
    assert response.json()["id"] == "SYS_NEW"

    with TestClient(app) as client:
        fetched = client.get("/catalog/source-systems/SYS_NEW")
    assert fetched.status_code == 200
    assert fetched.json() == SOURCE_SYSTEM_PAYLOAD | {"owning_team_id": None}


def test_create_source_system_duplicate_id_is_409(app, postgres_url):
    _seed(postgres_url)
    with TestClient(app) as client:
        response = client.post(
            "/catalog/source-systems",
            json=SOURCE_SYSTEM_PAYLOAD | {"id": "SYS_A"},
        )

    assert response.status_code == 409


def test_create_source_system_unknown_owning_team_id_is_422(app):
    with TestClient(app) as client:
        response = client.post(
            "/catalog/source-systems",
            json=SOURCE_SYSTEM_PAYLOAD | {"owning_team_id": "TEAM_DOES_NOT_EXIST"},
        )

    assert response.status_code == 422


def test_update_source_system_succeeds(app, postgres_url):
    _seed(postgres_url)
    with TestClient(app) as client:
        response = client.put(
            "/catalog/source-systems/SYS_A",
            json=SOURCE_SYSTEM_PAYLOAD | {"id": "SYS_A", "name": "Renamed"},
        )

    assert response.status_code == 200
    assert response.json()["name"] == "Renamed"


def test_update_source_system_id_mismatch_is_422(app, postgres_url):
    _seed(postgres_url)
    with TestClient(app) as client:
        response = client.put(
            "/catalog/source-systems/SYS_A",
            json=SOURCE_SYSTEM_PAYLOAD | {"id": "SYS_B"},
        )

    assert response.status_code == 422


def test_update_source_system_not_found_is_404(app):
    with TestClient(app) as client:
        response = client.put(
            "/catalog/source-systems/DOES_NOT_EXIST",
            json=SOURCE_SYSTEM_PAYLOAD | {"id": "DOES_NOT_EXIST"},
        )

    assert response.status_code == 404


def test_delete_source_system_succeeds(app, postgres_url):
    _seed(postgres_url)
    with TestClient(app) as client:
        response = client.delete("/catalog/source-systems/SYS_A")

    # SYS_A is referenced by FP_A1/IMR_A in the seed data -- deleting a
    # genuinely unreferenced system is the real success case.
    assert response.status_code == 409

    with TestClient(app) as client:
        create = client.post("/catalog/source-systems", json=SOURCE_SYSTEM_PAYLOAD)
        assert create.status_code == 201
        delete = client.delete("/catalog/source-systems/SYS_NEW")

    assert delete.status_code == 204
    with TestClient(app) as client:
        assert client.get("/catalog/source-systems/SYS_NEW").status_code == 404


def test_delete_source_system_referenced_by_footprint_is_409(app, postgres_url):
    _seed(postgres_url)
    with TestClient(app) as client:
        response = client.delete("/catalog/source-systems/SYS_A")

    assert response.status_code == 409
    assert "SYS_A" in response.json()["detail"]


FOOTPRINT_PAYLOAD = {
    "id": "FP_NEW",
    "source_system_id": "SYS_A",
    "footprint_type": "hostname",
    "value": "new-host",
    "notes": None,
}


def test_create_footprint_succeeds(app, postgres_url):
    _seed(postgres_url)
    with TestClient(app) as client:
        response = client.post("/catalog/footprints", json=FOOTPRINT_PAYLOAD)

    assert response.status_code == 201
    assert response.json()["id"] == "FP_NEW"


def test_create_footprint_unknown_source_system_is_422(app):
    with TestClient(app) as client:
        response = client.post(
            "/catalog/footprints",
            json=FOOTPRINT_PAYLOAD | {"source_system_id": "SYS_DOES_NOT_EXIST"},
        )

    assert response.status_code == 422


def test_create_footprint_invalid_regex_value_is_422(app, postgres_url):
    _seed(postgres_url)
    with TestClient(app) as client:
        response = client.post(
            "/catalog/footprints",
            json=FOOTPRINT_PAYLOAD | {"value": "unclosed-paren("},
        )

    assert response.status_code == 422


def test_update_footprint_succeeds(app, postgres_url):
    _seed(postgres_url)
    with TestClient(app) as client:
        response = client.put(
            "/catalog/footprints/FP_A1",
            json=FOOTPRINT_PAYLOAD | {"id": "FP_A1", "value": "updated-host"},
        )

    assert response.status_code == 200
    assert response.json()["value"] == "updated-host"


def test_delete_footprint_succeeds(app, postgres_url):
    _seed(postgres_url)
    with TestClient(app) as client:
        response = client.delete("/catalog/footprints/FP_A1")

    assert response.status_code == 204
    with TestClient(app) as client:
        assert client.get("/catalog/footprints/FP_A1").status_code == 404
