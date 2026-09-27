# NPE Incident Triaging RCA Solution

Automated triage for non-production (NPE) incidents reported in the Triage-NPE Teams channel or filed as
Jira/ServiceNow tickets. For each incident the service:

1. **Ingests** it (webhooks, or a Teams channel poller that follows Jira-key mentions).
2. **Classifies** it against a source-system catalog — deterministic OPA rules first, optional LLM fallback.
3. **Correlates** it with other incidents on the same application within a rolling 24-hour window.
4. **Executes** the approved diagnostic playbook for its `(source_system, category)`.
5. **Synthesizes** a root-cause assessment with recommended actions.
6. **Reports** back — optionally as a Jira comment — and exposes everything in an operator UI for feedback and retries.

The tool **diagnoses, it never claims a fix**. Target scale: 3,000 incidents / 24h across 300 applications, each
deployed in 6 non-prod environments.

---

## Architecture

```
Teams / Jira / ServiceNow webhooks ─┐
Teams channel poller (Graph + Jira) ┴─► idempotency (Valkey) ─► RabbitMQ `incidents.raw` ─► classification worker
                                                                                              │
          signal extraction + OPA policy (opa/policies/mapping.rego) vs INCIDENT_MAPPING_RULE ◄┘
                                                                                              │
                              classified `incident` row ─► correlation (Postgres, 24h window per application)
                                                                                              │
                active approved playbook for (source_system, category) ─► playbook engine ─► evidence
                                                                                              │
                                   RCA synthesis (deterministic, or LLM/RAG via Redis Stream + rca_worker)
```

| Component | Module | Role |
|---|---|---|
| API | `app/main.py`, `app/routers/*`, `app/webhooks.py` | FastAPI on `:8420`: webhooks, catalog/function/workflow/incident APIs, `/health` |
| Classification worker | `app/worker.py`, `app/classification.py` | Consumes `incidents.raw` (with dead-letter queue), classifies via OPA |
| Poller | `app/poller.py` | Polls Teams via Microsoft Graph for Jira keys, fetches issues from Jira Cloud |
| Sweeper | `app/sweeper.py` | Re-publishes incidents stuck in `pending` (crash between DB commit and publish) |
| RCA worker | `app/rca_worker.py` | Consumes `stream:rca-pending` for LLM-based RCA synthesis; idles when that feature is off |
| Playbook engine | `app/workflow_orchestrator.py`, `app/playbook_engine.py` | Builds, versions, approves and executes playbooks |
| Operator UI | `frontend/` | Next.js app on `:3000` |

Infrastructure (via `docker-compose.yml`): Postgres 17 with pgvector, Valkey (Redis), RabbitMQ, OPA.

---

## Getting started (Windows)

Prerequisites: Docker, [uv](https://docs.astral.sh/uv/) (Python ≥ 3.12). Node 22 + pnpm only if you develop the frontend locally.

```
uv sync
copy .env.example .env         # then fill in real values (see Configuration)
.\scripts\start.ps1            # compose up, load catalogs, start API + worker + poller + rca_worker + sweeper
.\scripts\stop.ps1             # stop app processes, docker compose stop (data preserved)
```

`start.ps1` waits for Postgres to be healthy, loads/seeds the catalogs, starts each process in the background, then
polls `/health`. PIDs and logs live under `.run\` (gitignored). Each process is skipped if it's already running.

| Flag | Effect |
|---|---|
| `start.ps1 -SkipCatalogLoad` | Don't reload catalogs/seed data |
| `start.ps1 -NoWorker` / `-NoPoller` / `-NoRcaWorker` / `-NoSweeper` | Skip that process |
| `stop.ps1 -KeepDockerRunning` | Stop app processes only |
| `stop.ps1 -RemoveVolumes` | Also wipe the Postgres volume (`docker compose down -v`) |

Check the stack is really up:

```
curl http://localhost:8420/health     # exercises database, Redis and RabbitMQ; 503 if any is down
```

- API docs: http://localhost:8420/docs
- Operator UI: http://localhost:3000
- RabbitMQ management: http://localhost:15672 (guest/guest)

### Running pieces manually

```
docker compose up -d
uv run python -m dataloadscripts.load_catalog --file dataloadscripts/source_systems.yaml
uv run python -m dataloadscripts.load_catalog --file dataloadscripts/npe_real_source_systems.yaml
uv run python -m dataloadscripts.load_function_registry
uv run python -m dataloadscripts.seed_functional_dummy_versions
uv run python -m dataloadscripts.seed_rca_pattern_types

uv run python -m scripts.run_dev_server   # API on :8420
uv run python -m app.worker               # classification worker
uv run python -m app.poller               # Teams -> Jira poller
uv run python -m app.rca_worker           # LLM RCA synthesis worker
uv run python -m app.sweeper              # stuck-incident sweeper
```

All loaders are idempotent (rows are upserted by primary key).

Post a test incident:

```
curl -X POST http://localhost:8420/webhooks/teams -H "Content-Type: application/json" \
  -d '{"id": "msg-1", "text": "Subject: ...\nEnvironment: NPE\n..."}'
```

### Frontend development

`docker compose up -d` builds and serves the UI on `:3000`. For hot reload instead:

```
cd frontend
pnpm install
pnpm dev                 # proxies /api/backend/* to BACKEND_URL (default http://localhost:8420)
pnpm gen:api             # regenerate lib/types.ts from the running API's OpenAPI schema
```

The browser only calls the Next.js app; it forwards API calls server-side, so the backend needs no CORS.

---

## Configuration

Settings are environment variables, loaded from `.env` by `app/config.py`. Infrastructure defaults match
`docker-compose.yml`, so a local stack needs no infra overrides.

| Variables | Purpose |
|---|---|
| `DATABASE_URL`, `REDIS_URL`, `RABBITMQ_URL`, `OPA_URL` | Infrastructure endpoints |
| `JIRA_SITE`, `JIRA_EMAIL`, `JIRA_API_TOKEN` | Jira Cloud (hostname only for `JIRA_SITE`) |
| `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `TEAMS_TEAM_ID`, `TEAMS_CHANNEL_ID` | Microsoft Graph / Teams — needs an Azure AD app registration with admin-consented application permissions `ChannelMessage.Read.All`, `Channel.ReadBasic.All`, `Team.ReadBasic.All` |
| `POLL_INTERVAL_SECONDS`, `POLL_LOOKBACK_MINUTES`, `MAX_POLL_BACKOFF_SECONDS` | Poller cadence and backoff cap |
| `SWEEP_INTERVAL_SECONDS`, `SWEEP_THRESHOLD_MINUTES` | Sweeper cadence and "stuck" threshold |
| `CORRELATION_WINDOW_MINUTES` (1440), `CORRELATION_THRESHOLD` (2) | Rolling correlation window and group size |
| `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_EMBEDDING_MODEL`, `OPENAI_EMBEDDING_DIMENSIONS` | LLM + embeddings |
| `LLM_FALLBACK_ENABLED` (false), `CLASSIFICATION_CONFIDENCE_THRESHOLD` (0.7) | LLM classification fallback — only used when no deterministic rule matches |
| `RCA_SYNTHESIS_LLM_ENABLED` (false), `RAG_EMBED_FEEDBACK_ONLY`, `RAG_RETENTION_MONTHS` | LLM/RAG RCA synthesis grounded on operator feedback |
| `CORS_ALLOWED_ORIGINS` | Only for a browser calling the API directly from another origin |

The poller needs both Jira and Graph credentials; the Jira client alone works with just the Jira variables. Keep
`RCA_SYNTHESIS_LLM_ENABLED` off while check results are stubbed — there is no real evidence for an LLM to reason over.

---

## Operator UI

| Tab | What it does |
|---|---|
| **RCA of Incidents** | Dashboard of incidents with classification and RCA status; drill into an incident and each execution's evidence graph, give feedback |
| **Playbooks for RCA** | Browse playbook definitions and versions as a CNCF workflow graph, view each step's function source; create a new playbook |
| **Retry RCA** | Re-run a diagnosis, optionally with operator context or a different playbook version |
| **System Mapping Data** | CRUD for source systems and their footprints (what classification matches against) |
| **Check Type Registry** | Browse check functions, their parameter contracts and version history |

There is no login yet: the UI asks for an operator name once and attaches it to every build, approval, retry and
feedback. Don't expose the stack beyond localhost or a trusted network.

---

## Playbooks and check functions

**Check functions** (`app/function_registry.py`) declare the contract — typed params and default retry policy — for
20 check types (`error_logs`, `recent_deployments`, `service_health`, …).

- Functions are versioned: a `FunctionDefinition` has many immutable `FunctionDefinitionVersion` rows, one `active`
  at a time. A change is always a new version.
- `FUNCTION_REGISTRY` is an in-memory cache of the active versions, refreshed at startup and on every `/functions`
  write; it falls back to built-in defaults on an unseeded database.
- A version only runs if it has an implementation: `app/checks.py::STUB_RESULTS` (v1 template stubs) or
  `app/check_implementations.py` (v2, one async function per check type). Referencing a version without one is
  rejected when the playbook is built, not at run time.
- **Check results are stubbed.** Every evidence line is marked `[STUBBED]` or `[FUNCTIONAL DUMMY v2]`. The v2
  functions log the caller and timestamp so you can prove which checks actually ran. To integrate a real system,
  replace one function body in `app/check_implementations.py`.

**Playbooks** are a subset of the [CNCF Serverless Workflow](https://serverlessworkflow.io) DSL, executed in-process
(no external orchestrator): a sequence of `call` tasks with `with` params and an optional `retry` override
(`app/workflow_spec.py`).

- Versioned like functions: `WorkflowDefinition` → immutable `WorkflowDefinitionVersion` rows. Approving a new
  version supersedes the previous one; only the `approved` version executes.
- Each task is pinned to a function version when the playbook is built, so later function changes don't alter a
  published playbook.
- Playbooks come from two sources: statically authored in the catalog YAML (`load_catalog.py`), or built from a
  request (`{call, with}` list) that is validated, rendered, then approved — via the UI, API, or CLI.
- Editing an existing version creates a new build request based on it; unchanged steps keep their function pins,
  and approval is refused (409) if another version was published in the meantime. Playbooks can be exchanged as
  CNCF YAML (`document:` + `do:`) through the API.
- A failed check doesn't abort the run: exhausted retries record an `ERROR` evidence entry and the playbook continues.
- Every run is stored as a `WorkflowExecution` with a snapshot of exactly what executed, its evidence and its RCA.

**Scope:** only the FUNC category (`FUNCTIONAL DEFECT (QA/UAT)`) has shipped playbooks, and only a few real source
systems have one; others report "no playbook configured" rather than a fabricated RCA. The engine is
category-agnostic — enabling a new category is catalog data, not code.

### Two statuses, never conflated

- **RCA status** (`app/rca_status.py`) — diagnostic confidence: `Identified`, `Probable`, `Inconclusive`,
  `Correlated`, `IssueCouldNotBeTraced`, `NeedManualIntervention`.
- **Classification status** (`app/classification_status.py`) — whether the source system/category were determined:
  `Success`, `Probable`, `Not determined`.

Neither ever says "resolved".

---

## CLI scripts

```
uv run python -m scripts.e2e_rca TT-1                   # fetch Jira issue, classify, run playbook, print RCA
uv run python -m scripts.e2e_rca TT-1 --post-comment    # ...and post the RCA to the Jira issue

uv run python -m scripts.build_workflow --source-system SYS_HSI \
  --category "FUNCTIONAL DEFECT (QA/UAT)" --functions functions.json \
  --requested-by you@example.com [--approve]            # functions.json: [{"call": "...", "with": {...}}]

uv run python -m scripts.recorrelate --since 2026-09-01 [--until ...] [--dry-run]   # recompute correlation groups
```

Posting to Jira is always opt-in (`--post-comment`, or `?post_comment=true` on the API) — never automatic. The comment
is Atlassian Document Format with panels color-coded by RCA status (`app/adf_report.py`).

---

## REST API

Served on `:8420`; interactive docs at `/docs`. No authentication.

| Area | Endpoints |
|---|---|
| Health | `GET /health` |
| Webhooks | `POST /webhooks/{teams,jira,servicenow}` |
| Catalog | `/catalog/{teams,environments,source-systems,footprints,mapping-rules}` — `GET`, `POST`, `GET/PUT/DELETE /{id}`. References are checked on write; deleting a referenced row returns `409`. `footprints` and `mapping-rules` accept `?source_system_id=` |
| Functions | `GET/POST /functions`, `GET/DELETE /functions/{id}`, `GET/POST /functions/{id}/versions`, `GET /functions/{id}/versions/{n}/source`. Responses include `has_implementation` |
| Playbook builds | `POST/GET /workflows/build-requests`, `GET /workflows/build-requests/{id}`, `POST .../{id}/approve`, `POST .../{id}/reject` |
| Playbook definitions | `GET /workflows/definitions`, `GET /workflows/definitions/{id}/versions`, `GET /workflows/active?source_system_id=&category=`, `POST /workflows/versions/{id}/execute` |
| Playbook editing | `GET /workflows/versions/{id}/yaml`, `POST /workflows/render-yaml`, `POST /workflows/validate-yaml` (dry run), `POST /workflows/definitions/{id}/edits`. Validation errors are `422` with `{kind: syntax\|schema\|registry, errors: [{message, path, line, column, task_index}]}` |
| Executions & feedback | `GET /workflows/executions[?jira_key=]`, `GET /workflows/executions/{id}`, `GET/POST /workflows/executions/{id}/feedback`, `GET /workflows/rca-patterns` |
| Incidents | `GET /incidents`, `GET /incidents/dashboard`, `GET /incidents/{id}`, `POST /incidents/{jira_key}/retry`, `POST /rca/{jira_key}[?post_comment=true]` |
| Stats | `GET /stats/incidents` |

---

## Testing

```
uv run pytest                                   # full suite
uv run pytest tests/test_checks.py              # one file
uv run pytest tests/test_checks.py::test_x      # one test
```

Tests start ephemeral Postgres, Valkey, RabbitMQ and OPA containers with testcontainers, so **Docker must be
running**. Poller/Graph/Jira logic is covered by mocked tests using real sample-ticket fixtures.

Frontend end-to-end tests run against a real running stack:

```
cd frontend
pnpm e2e
```

---

## Operational notes

- **Windows event loop:** psycopg's async driver can't run on the default `ProactorEventLoop`. Start the API with
  `scripts/run_dev_server.py`, not `uvicorn app.main:app` (fine on Linux/Docker).
- **Logs:** Python `logging` writes to stderr, which `start.ps1` sends to `.run\logs\<process>.err.log`.
- **Schema changes:** there are no migrations; tables are created with `Base.metadata.create_all`. After a model
  change, recreate the database (`.\scripts\stop.ps1 -RemoveVolumes`, then `start.ps1`).
- **No linter/formatter or CI** is configured.

## Project layout

```
app/                FastAPI app, workers, classification, playbook engine, RCA synthesis
app/routers/        REST endpoints
app/check_types/    v2 check implementations (one module per check type)
dataloadscripts/    catalog YAML (source systems, mapping rules, playbooks) and the loaders/seeders that load it
scripts/            start/stop, dev server, CLI tools
opa/policies/       OPA classification policy
frontend/           Next.js operator UI
tests/              pytest suite
```
