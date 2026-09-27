import asyncio
import sys
from pathlib import Path

import pytest

if sys.platform == "win32":
    # psycopg's async driver cannot run under Windows' default
    # ProactorEventLoop; it requires a SelectorEventLoop.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.rabbitmq import RabbitMqContainer
from testcontainers.community.redis import RedisContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

POLICIES_DIR = Path(__file__).resolve().parents[1] / "opa" / "policies"


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
    import app.routers.workflows as workflows_router_module
    import app.worker as worker_module
    import app.workflow_orchestrator as workflow_orchestrator_module

    monkeypatch.setattr(classification_module, "LLM_FALLBACK_ENABLED", False)
    monkeypatch.setattr(worker_module, "LLM_FALLBACK_ENABLED", False)
    monkeypatch.setattr(workflow_orchestrator_module, "RCA_SYNTHESIS_LLM_ENABLED", False)
    monkeypatch.setattr(workflows_router_module, "RCA_SYNTHESIS_LLM_ENABLED", False)


@pytest.fixture(scope="session")
def postgres_url():
    # pgvector-enabled image (drop-in over postgres:17-alpine, adds
    # the `vector` extension) -- needed for classification/feedback RAG
    # embedding tests.
    with PostgresContainer("pgvector/pgvector:pg17", driver="psycopg") as pg:
        yield pg.get_connection_url()


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
