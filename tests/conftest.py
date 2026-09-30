import time
import uuid
from pathlib import Path

import chromadb
import pytest
from testcontainers.community.rabbitmq import RabbitMqContainer
from testcontainers.community.redis import RedisContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from app.db import ensure_indexes_sync, make_sync_mongo_client
from app.vector_store import VectorStore

POLICIES_DIR = Path(__file__).resolve().parents[1] / "opa" / "policies"

# Pinned together with chromadb-client in pyproject.toml.
CHROMA_IMAGE = "chromadb/chroma:1.5.9"


@pytest.fixture(autouse=True)
def _llm_feature_flags_off_by_default(monkeypatch):
    """app/config.py's load_dotenv() means a developer's local .env --
    e.g. RCA_SYNTHESIS_LLM_ENABLED=true, set for manual testing against a
    real OpenAI key -- otherwise leaks straight into the test suite, since
    every module below binds its own copy of these flags at import time
    (`from app.config import X`) rather than reading app.config.X live.
    Force both off for every test by default regardless of the local .env;
    a test that specifically needs one on still monkeypatches it within its
    own body, same as today, and that override is unaffected by this (it
    runs after this fixture's setup, and monkeypatch unwinds both in the
    correct order at teardown)."""
    import app.classification as classification_module
    import app.main as main_module
    import app.routers.workflows as workflows_router_module
    import app.worker as worker_module
    import app.workflow_orchestrator as workflow_orchestrator_module

    monkeypatch.setattr(classification_module, "LLM_FALLBACK_ENABLED", False)
    monkeypatch.setattr(worker_module, "LLM_FALLBACK_ENABLED", False)
    monkeypatch.setattr(main_module, "LLM_FALLBACK_ENABLED", False)
    monkeypatch.setattr(workflow_orchestrator_module, "RCA_SYNTHESIS_LLM_ENABLED", False)
    monkeypatch.setattr(workflows_router_module, "RCA_SYNTHESIS_LLM_ENABLED", False)
    monkeypatch.setattr(main_module, "RCA_SYNTHESIS_LLM_ENABLED", False)


@pytest.fixture(scope="session")
def mongo_url():
    """A single-node replica set (not a plain mongod): the app's
    multi-document transactions need one, so tests must run on one too."""
    container = (
        DockerContainer("mongo:8.0")
        .with_command("--replSet rs0 --bind_ip_all")
        .with_exposed_ports(27017)
        .waiting_for(LogMessageWaitStrategy("Waiting for connections"))
    )
    with container as running:
        result = running.exec(
            ["mongosh", "--quiet", "--eval", "rs.initiate({_id: 'rs0', members: [{_id: 0, host: 'localhost:27017'}]}).ok"]
        )
        assert result.exit_code == 0, result.output
        host = running.get_container_host_ip()
        port = running.get_exposed_port(27017)
        url = f"mongodb://{host}:{port}/?directConnection=true"
        # rs.initiate returns before the member is primary; wait for it.
        client = make_sync_mongo_client(url)
        try:
            client.admin.command("ping")
            with client.start_session() as session:
                session.with_transaction(lambda s: client["warmup"]["warmup"].insert_one({}, session=s))
            client.drop_database("warmup")
        finally:
            client.close()
        yield url


@pytest.fixture()
def mongo_db_name(mongo_url):
    """A fresh database per test (dropped afterwards) -- the Mongo
    equivalent of create_all/drop_all around every test."""
    name = f"t_{uuid.uuid4().hex[:16]}"
    yield name
    client = make_sync_mongo_client(mongo_url)
    try:
        client.drop_database(name)
    finally:
        client.close()


@pytest.fixture()
def sync_db(mongo_url, mongo_db_name):
    """Synchronous handle on this test's database, with indexes created --
    for seeding and asserting from plain (non-async) test code."""
    client = make_sync_mongo_client(mongo_url)
    db = client[mongo_db_name]
    ensure_indexes_sync(db)
    yield db
    client.close()


@pytest.fixture(scope="session")
def chroma_endpoint():
    container = (
        DockerContainer(CHROMA_IMAGE)
        .with_exposed_ports(8000)
        .waiting_for(LogMessageWaitStrategy("Connect to Chroma at"))
    )
    with container as running:
        host, port = running.get_container_host_ip(), int(running.get_exposed_port(8000))
        deadline = time.monotonic() + 30
        while True:  # the banner is logged just before the server binds
            try:
                chromadb.HttpClient(host=host, port=port).heartbeat()
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        yield host, port


@pytest.fixture()
def vector_store(chroma_endpoint):
    """A VectorStore with a unique collection prefix, so every test has its
    own empty corpora; they're deleted afterwards."""
    host, port = chroma_endpoint
    prefix = f"t{uuid.uuid4().hex[:12]}_"
    vs = VectorStore(host=host, port=port, prefix=prefix)
    yield vs
    client = chromadb.HttpClient(host=host, port=port)
    for collection in client.list_collections():
        name = collection.name if hasattr(collection, "name") else collection
        if name.startswith(prefix):
            client.delete_collection(name)


@pytest.fixture(scope="session")
def redis_url():
    with RedisContainer("valkey/valkey:8-alpine") as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(container.port)
        yield f"redis://{host}:{port}/0"


@pytest.fixture(scope="session")
def rabbitmq_url():
    with RabbitMqContainer() as container:
        params = container.get_connection_params()
        yield f"amqp://{params.credentials.username}:{params.credentials.password}@{params.host}:{params.port}{params.virtual_host}"


@pytest.fixture(scope="session")
def opa_url():
    container = (
        DockerContainer("openpolicyagent/opa:latest")
        .with_command("run --server --addr 0.0.0.0:8181 /policies")
        .with_volume_mapping(str(POLICIES_DIR), "/policies", mode="ro")
        .with_exposed_ports(8181)
        .waiting_for(LogMessageWaitStrategy("Initializing server"))
    )
    with container as running:
        host = running.get_container_host_ip()
        port = running.get_exposed_port(8181)
        yield f"http://{host}:{port}"
