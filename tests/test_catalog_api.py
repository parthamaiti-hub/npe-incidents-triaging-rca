import pytest
from fastapi.testclient import TestClient

from app.embeddings import embed_footprint, footprint_vector_id
from app.main import create_app
from app.models import Environment, Incident, IncidentMappingRule, SourceSystem, SystemFootprint, Team
from dataloadscripts.test_fixtures import insert_docs
from tests.test_embeddings import make_mock_client


@pytest.fixture()
def app(mongo_url, mongo_db_name, redis_url, rabbitmq_url, vector_store):
    return create_app(
        mongodb_url=mongo_url, mongodb_db=mongo_db_name, redis_url=redis_url, rabbitmq_url=rabbitmq_url,
        vector_store=vector_store,
    )


def _seed(db):
    insert_docs(
        db,
        SourceSystem(
            id="SYS_A", name="A", code="A", type="Application", description="a", owning_team="t", environment="NPE"
        ),
        SourceSystem(
            id="SYS_B", name="B", code="B", type="Application", description="b", owning_team="t", environment="NPE"
        ),
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
    )


def test_mapping_rules_filtered_by_source_system_id(app, sync_db):
    _seed(sync_db)
    with TestClient(app) as client:
        response = client.get("/catalog/mapping-rules", params={"source_system_id": "SYS_A"})

    assert response.status_code == 200
    rows = response.json()
    assert [r["id"] for r in rows] == ["IMR_A"]


def test_footprints_filtered_by_source_system_id(app, sync_db):
    _seed(sync_db)
    with TestClient(app) as client:
        response = client.get("/catalog/footprints", params={"source_system_id": "SYS_B"})

    assert response.status_code == 200
    rows = response.json()
    assert [r["id"] for r in rows] == ["FP_B1"]


def test_mapping_rules_unfiltered_returns_all(app, sync_db):
    _seed(sync_db)
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


def test_create_source_system_duplicate_id_is_409(app, sync_db):
    _seed(sync_db)
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


def test_update_source_system_succeeds(app, sync_db):
    _seed(sync_db)
    with TestClient(app) as client:
        response = client.put(
            "/catalog/source-systems/SYS_A",
            json=SOURCE_SYSTEM_PAYLOAD | {"id": "SYS_A", "name": "Renamed"},
        )

    assert response.status_code == 200
    assert response.json()["name"] == "Renamed"


def test_update_source_system_id_mismatch_is_422(app, sync_db):
    _seed(sync_db)
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


def test_delete_source_system_succeeds(app, sync_db):
    _seed(sync_db)
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


def test_delete_source_system_referenced_by_footprint_is_409(app, sync_db):
    _seed(sync_db)
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


def test_create_footprint_succeeds(app, sync_db):
    _seed(sync_db)
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


def test_create_footprint_invalid_regex_value_is_422(app, sync_db):
    _seed(sync_db)
    with TestClient(app) as client:
        response = client.post(
            "/catalog/footprints",
            json=FOOTPRINT_PAYLOAD | {"value": "unclosed-paren("},
        )

    assert response.status_code == 422


def test_update_footprint_succeeds(app, sync_db):
    _seed(sync_db)
    with TestClient(app) as client:
        response = client.put(
            "/catalog/footprints/FP_A1",
            json=FOOTPRINT_PAYLOAD | {"id": "FP_A1", "value": "updated-host"},
        )

    assert response.status_code == 200
    assert response.json()["value"] == "updated-host"


def test_delete_footprint_succeeds(app, sync_db):
    _seed(sync_db)
    with TestClient(app) as client:
        response = client.delete("/catalog/footprints/FP_A1")

    assert response.status_code == 204
    with TestClient(app) as client:
        assert client.get("/catalog/footprints/FP_A1").status_code == 404


async def test_delete_footprint_also_removes_its_vector(app, sync_db, vector_store):
    _seed(sync_db)
    client = make_mock_client()
    for footprint_id, system_id in (("FP_A1", "SYS_A"), ("FP_B1", "SYS_B")):
        await embed_footprint(
            vector_store,
            client,
            SystemFootprint(id=footprint_id, source_system_id=system_id, footprint_type="hostname", value="h"),
        )

    with TestClient(app) as http:
        assert http.delete("/catalog/footprints/FP_A1").status_code == 204

    assert (await (await vector_store.classification()).get())["ids"] == [footprint_vector_id("FP_B1")]


# --- Reference checks: what foreign keys used to refuse --------------------
# One test per row of app.repositories.refs.REFERENCES the catalog API can
# delete (source_system is covered above).

TEAM = Team(id="TEAM_1", name="Team 1", teams_handle="team-1")
ENVIRONMENT = Environment(id="ENV_1", code="QLAB01", name="QLAB01")


def test_delete_team_referenced_by_source_system_is_409(app, sync_db):
    insert_docs(
        sync_db,
        TEAM,
        SourceSystem(
            id="SYS_T", name="T", code="T", type="Application", description="t", owning_team="t", environment="NPE",
            owning_team_id="TEAM_1",
        ),
    )
    with TestClient(app) as client:
        response = client.delete("/catalog/teams/TEAM_1")

    assert response.status_code == 409
    assert "source_system.owning_team_id" in response.json()["detail"]


def test_delete_team_referenced_by_incident_is_409(app, sync_db):
    insert_docs(sync_db, TEAM, Incident(source="teams", external_id="m1", raw_text="x", addressed_team_id="TEAM_1"))
    with TestClient(app) as client:
        response = client.delete("/catalog/teams/TEAM_1")

    assert response.status_code == 409
    assert "incident.addressed_team_id" in response.json()["detail"]


def test_delete_unreferenced_team_succeeds(app, sync_db):
    insert_docs(sync_db, TEAM)
    with TestClient(app) as client:
        assert client.delete("/catalog/teams/TEAM_1").status_code == 204
        assert client.get("/catalog/teams/TEAM_1").status_code == 404


def test_delete_environment_referenced_by_incident_is_409(app, sync_db):
    insert_docs(sync_db, ENVIRONMENT, Incident(source="teams", external_id="m1", raw_text="x", environment_id="ENV_1"))
    with TestClient(app) as client:
        response = client.delete("/catalog/environments/ENV_1")

    assert response.status_code == 409
    assert "incident.environment_id" in response.json()["detail"]


def test_delete_mapping_rule_referenced_by_incident_is_409(app, sync_db):
    _seed(sync_db)
    insert_docs(sync_db, Incident(source="teams", external_id="m1", raw_text="x", matched_rule_id="IMR_B"))
    with TestClient(app) as client:
        referenced = client.delete("/catalog/mapping-rules/IMR_B")
        unreferenced = client.delete("/catalog/mapping-rules/IMR_A")

    assert referenced.status_code == 409
    assert "incident.matched_rule_id" in referenced.json()["detail"]
    assert unreferenced.status_code == 204


def test_delete_source_system_referenced_only_by_an_incident_is_409(app, sync_db):
    insert_docs(
        sync_db,
        SourceSystem(
            id="SYS_I", name="I", code="I", type="Application", description="i", owning_team="t", environment="NPE"
        ),
        Incident(source="teams", external_id="m1", raw_text="x", source_system_id="SYS_I"),
    )
    with TestClient(app) as client:
        response = client.delete("/catalog/source-systems/SYS_I")

    assert response.status_code == 409
    assert "incident.source_system_id" in response.json()["detail"]
