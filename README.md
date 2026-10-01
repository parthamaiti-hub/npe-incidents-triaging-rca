# NPE Incident Triaging RCA Solution

Automated triage for non-production (NPE) incidents reported in the Triage-NPE Teams channel or filed as
Jira/ServiceNow tickets. For each incident the service:

1. **Ingests** it (webhooks, or a Teams channel poller that follows Jira-key mentions).
2. **Classifies** it against a source-system catalog — deterministic OPA rules first, optional LLM fallback.
3. **Correlates** it with other incidents on the same application within a rolling 24-hour window.
4. **Executes** the approved diagnostic playbook for its `(source_system, category)` — automatically, right after classification.
5. **Synthesizes** a root-cause assessment with recommended actions.
6. **Reports** back — optionally as a Jira comment — and exposes everything in an operator UI for feedback and retries.

The tool **diagnoses, it never claims a fix**. Target scale: 3,000 incidents / 24h across 300 applications, each
deployed in 6 non-prod environments.

---

## Architecture

```
Teams / Jira / ServiceNow webhooks ─┐
Teams channel poller (Graph + Jira) ┴─► idempotency (Valkey) ─► `incident` row + incident ID ─► RabbitMQ `incidents.raw`
                                                                                                      │
          classification worker: signal extraction + OPA policy (opa/policies/mapping.rego) ◄─────────┘
                                 vs INCIDENT_MAPPING_RULE (optional LLM fallback)
                                                          │
                                   automatic first RCA ◄──┘      Retry tab / POST /incidents/{incident_key}/retry
                                            │                    (re-classify with current rules) ──┐
                                            ▼                                                       │
     active approved playbook for (source_system, category) ◄────────────────────────────────────────┘
       ─► playbook engine ─► evidence ─► correlation (MongoDB, 24h window per application)
       ─► RCA synthesis (deterministic, or LLM/RAG via Redis Stream + rca_worker) ─► execution record
```

| Component | Module | Role |
|---|---|---|
| API | `app/main.py`, `app/routers/*`, `app/webhooks.py` | FastAPI on `:8421`: webhooks, catalog/function/workflow/incident APIs, `/health` |
| Classification worker | `app/worker.py`, `app/classification.py` | Consumes `incidents.raw` (with dead-letter queue), classifies via OPA, then runs the incident's first RCA |
| Poller | `app/poller.py` | Polls Teams via Microsoft Graph for Jira keys, fetches issues from Jira Cloud |
| Sweeper | `app/sweeper.py` | Re-publishes incidents stuck in `pending` (crash between DB commit and publish) |
| RCA worker | `app/rca_worker.py` | Consumes `stream:rca-pending` for LLM-based RCA synthesis; idles when that feature is off |
| Playbook engine | `app/workflow_orchestrator.py`, `app/playbook_engine.py` | Builds, versions, approves and executes playbooks |
| Operator UI | `frontend/` | Next.js app on `:3001` |

Infrastructure (via `docker-compose.yml`): MongoDB 8 as a single-node replica set (database `npe_triage_rca`, the
source of truth), ChromaDB 1.5.9 (RAG vectors -- derived data, rebuildable from Mongo), Valkey (Redis), RabbitMQ, OPA.
Host ports are offset so this stack can run alongside another on the defaults: MongoDB `27018`, Chroma `8011`, Valkey `6380`,
RabbitMQ `5673` (management `15673`), OPA `8182`, API `8421`, UI `3001`.

---

## Getting started (Windows)

Prerequisites: Docker, [uv](https://docs.astral.sh/uv/) (Python ≥ 3.12). Node 22 + pnpm only if you develop the
frontend locally or run its end-to-end tests (`corepack enable`, or prefix commands with `npx pnpm@10`).

```
uv sync
copy .env.example .env         # then fill in real values (see Configuration)
.\scripts\start.ps1            # compose up, load catalogs, start API + worker + poller + rca_worker + sweeper
.\scripts\stop.ps1             # stop app processes, docker compose stop (data preserved)
```

`start.ps1` waits for MongoDB and Chroma to be healthy, loads/seeds the catalogs, starts each process in the background, then
polls `/health`. PIDs and logs live under `.run\` (gitignored). Each process is skipped if it's already running, so
after changing backend code run `stop.ps1 -KeepDockerRunning` then `start.ps1 -SkipCatalogLoad` to pick it up.

`docker compose up -d` builds the UI image only the first time; after frontend changes rebuild it with
`docker compose up -d --build frontend`.

| Flag | Effect |
|---|---|
| `start.ps1 -SkipCatalogLoad` | Don't reload catalogs/seed data |
| `start.ps1 -NoWorker` / `-NoPoller` / `-NoRcaWorker` / `-NoSweeper` | Skip that process |
| `stop.ps1 -KeepDockerRunning` | Stop app processes only |
| `stop.ps1 -RemoveVolumes` | Also wipe the MongoDB and Chroma volumes (`docker compose down -v`) |

Check the stack is really up:

```
curl http://localhost:8421/health     # exercises database, Redis and RabbitMQ; 503 if any is down
```

- API docs: http://localhost:8421/docs
- Operator UI: http://localhost:3001
- RabbitMQ management: http://localhost:15673 (guest/guest)

### Running pieces manually

```
docker compose up -d
uv run python -m dataloadscripts.load_catalog --file dataloadscripts/source_systems.yaml
uv run python -m dataloadscripts.load_catalog --file dataloadscripts/npe_real_source_systems.yaml
uv run python -m dataloadscripts.load_default_playbooks   # one DEFAULT (fallback) playbook per source system
uv run python -m dataloadscripts.load_function_registry
uv run python -m dataloadscripts.seed_functional_dummy_versions
uv run python -m dataloadscripts.seed_rca_pattern_types

uv run python -m scripts.run_dev_server   # API on :8421
uv run python -m app.worker               # classification worker
uv run python -m app.poller               # Teams -> Jira poller
uv run python -m app.rca_worker           # LLM RCA synthesis worker
uv run python -m app.sweeper              # stuck-incident sweeper
```

All loaders are idempotent (rows are upserted by primary key).

Every `load_catalog` run first runs the **catalog check** (`dataloadscripts/check_catalog.py`). Errors (duplicate ids or rules, unknown signal types, regexes OPA's RE2 can't run, category spelling drift, two playbooks for one (system, category), invalid playbook steps or pins) abort the load with nothing written, and stop `scripts/start.ps1`. Warnings (unreachable playbooks, rules with no playbook, rules that tie or are shadowed by another rule) are printed. Check a file without loading it:

```
uv run python -m dataloadscripts.check_catalog --file dataloadscripts/npe_real_source_systems.yaml [--strict] [--no-db]
```

See `design/NPE_Incident_PlaybookMappingProcess.md` §9.2 for every check and its level.

Post a test incident (PowerShell). The worker classifies it and runs its first RCA; it appears on the dashboard
under a generated `int_...` ID:

```powershell
$body = @{ id = "msg-1"; text = "Subject: Billing UAT failing`nEnvironment: NPE`nImpact: ..." } | ConvertTo-Json
Invoke-RestMethod -Method Post http://localhost:8421/webhooks/teams -ContentType "application/json" -Body $body
```

### Frontend development

The Docker container serves the UI on `:3001`. For hot reload instead:

```
cd frontend
pnpm install
pnpm dev --port 3002     # Next's default :3000 may be taken; proxies /api/backend/* to BACKEND_URL (default http://localhost:8421)
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
| **Playbooks for RCA** | Browse playbook definitions and versions as a CNCF workflow graph, view each step's function source; create a new playbook, or edit the approved one with the graph and its CNCF YAML side by side |
| **Retry RCA** | Reprocess an incident by its ID: re-classify with the current mapping rules, then run the active playbook (or a chosen version), optionally with operator context |
| **System Mapping Data** | CRUD for source systems and their footprints (what classification matches against) |
| **Check Type Registry** | Browse check functions, their parameter contracts and version history |

There is no login yet: the UI asks for an operator name once and attaches it to every build, approval, retry and
feedback. Don't expose the stack beyond localhost or a trusted network.

---

## Playbooks and check functions

**Check functions** (`app/function_registry.py`) declare the contract — typed params and default retry policy — for
20 check types (`error_logs`, `recent_deployments`, `service_health`, …).

- Functions are versioned: a `FunctionDefinition` has many immutable `FunctionDefinitionVersion` rows. A change is
  always a new version. Lifecycle (see `design/Sol-104-CheckType_Versioning.md`):
  `draft` → `active` (exactly one; the default for new pins) → `deprecated` (still runs wherever pinned) → `retired`
  (never runs; only allowed once no playbook pins it).
- `FUNCTION_REGISTRY` caches the active versions and `FUNCTION_VERSIONS` every version; both are refreshed at startup
  (API and worker) and on every `/functions` write, and fall back to built-in defaults on an unseeded database.
- A version only runs if it has an implementation: a v1 template in `app/checks.py::STUB_RESULTS`, or a module
  `app/check_types/<check_type>_v<N>.py` exporting `async def run(params) -> dict`, discovered automatically by
  filename. **A new version or a brand-new check type is one new file** — no dispatcher change. Referencing a
  version without code is rejected when the playbook is built, not at run time; `GET /functions/integrity` (also
  logged at startup) reports pinned versions that couldn't run.
- **Check results are stubbed.** Every evidence line is marked `[STUBBED]` or `[FUNCTIONAL DUMMY v2]`. The v2
  modules log the caller and timestamp so you can prove which checks actually ran. To integrate a real system, add
  the next version's module (e.g. `error_logs_v3.py`) and publish its contract.

**Playbooks** are a subset of the [CNCF Serverless Workflow](https://serverlessworkflow.io) DSL, executed in-process
(no external orchestrator): a sequence of `call` tasks with `with` params and an optional `retry` override
(`app/workflow_spec.py`).

- Versioned like functions: `WorkflowDefinition` → immutable `WorkflowDefinitionVersion` rows. Approving a new
  version supersedes the previous one; only the `approved` version executes.
- Each task is pinned to a function version when the playbook is built, so later function changes don't alter a
  published playbook. The pin is: an explicit `version:` on the task, else (when editing or reloading) the pin the
  unchanged task already had, else the active version. The pinned version's retry policy is copied into the task,
  and params are validated against the pinned version's contract. Evidence records the `version` that ran.
- Moving a playbook to a newer check version is always a new, approved playbook version: edit the task's
  `version:`, or `POST /workflows/definitions/{id}/upgrade`. Publishing a check version never changes an existing
  playbook, and reloading the catalog never re-pins (unless `--upgrade-functions` is passed).
- Playbooks come from two sources: statically authored in the catalog YAML (`load_catalog.py`), or built from a
  request (`{call, with}` list) that is validated, rendered, then approved — via the UI, API, or CLI.
- Editing an existing version creates a new build request based on it; unchanged steps keep their function pins,
  and approval is refused (409) if another version was published in the meantime. Playbooks can be exchanged as
  CNCF YAML (`document:` + `do:`) through the API.
- A failed check doesn't abort the run: exhausted retries record an `ERROR` evidence entry and the playbook continues.
- Every run is stored as a `WorkflowExecution` with a snapshot of exactly what executed, its evidence and its RCA.

**DEFAULT playbooks / DefaultRCA.** Every source system can have a fallback playbook with the reserved category
`DEFAULT` (`RCA_<CODE>_DEFAULT`, same checks as the FUNC playbooks; template
`dataloadscripts/default_rca_playbook.yaml`, loaded by `dataloadscripts/load_default_playbooks.py`). When an incident's
system is known but its category couldn't be mapped (only an `ANY` rule matched), or the mapped (system, category) has
no playbook, the DEFAULT playbook runs and the execution is marked `triage_mode = "DefaultRCA"` with a `triage_note`
saying what to fix. Fix the mapping rule (or add the playbook) and Retry: retry re-classifies, so the specific playbook
runs (`triage_mode = "Mapped"`). `GET /incidents/dashboard?triage_mode=DefaultRCA` lists what still needs mapping;
`GET /stats/incidents` reports `default_rca`. Mapping rules can't use `DEFAULT`.

**Scope:** only the FUNC category (`FUNCTIONAL DEFECT (QA/UAT)`) has shipped playbooks, and only a few real source
systems have one; others report "no playbook configured" rather than a fabricated RCA. The engine is
category-agnostic — enabling a new category is catalog data, not code.

### Incident IDs and reprocessing

Every incident has an **incident ID** (`incident_key`), used by the dashboard, detail page, Retry tab and execution
history:

- An incident that arrives with a Jira key is known by that key (e.g. `RS-173234`).
- Any other incident (e.g. pushed by the Teams or ServiceNow webhook) gets a generated ID
  `int_MMDDYYYYHHMMSS_NNNNN`: the ingest time in UTC plus a running number from a database sequence, `00001`–`99999`,
  then wrapping. Example: `int_09272026192617_00001`. The source's own identifier stays in `external_id`.

Each incident gets its first RCA automatically after classification — the active playbook, or a recorded
*no playbook* / *not classified* outcome. After fixing a mapping rule or a playbook, **Retry** the incident by its ID:
retry re-classifies the stored text with the current rules, then runs the now-active playbook. Every attempt is kept
as its own execution (`triggered_by` = `auto`, `rca`, `retry`).

### Two statuses, never conflated

- **RCA status** (`app/rca_status.py`) — diagnostic confidence: `Identified`, `Probable`, `Inconclusive`,
  `Correlated`, `IssueCouldNotBeTraced`, `NeedManualIntervention`.
- **Classification status** (`app/classification_status.py`) — whether the source system/category were determined:
  `Success`, `Probable`, `Not determined`.

Neither ever says "resolved".

---

## CLI scripts

```
uv run python -m scripts.e2e_rca TT-1                   # fetch Jira issue, record/refresh the incident, classify, run playbook, print RCA
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

Served on `:8421`; interactive docs at `/docs`. No authentication.

| Area | Endpoints |
|---|---|
| Health | `GET /health` |
| Webhooks | `POST /webhooks/{teams,jira,servicenow}` |
| Catalog | `/catalog/{teams,environments,source-systems,footprints,mapping-rules}` — `GET`, `POST`, `GET/PUT/DELETE /{id}`. References are checked on write; deleting a referenced row returns `409`. `footprints` and `mapping-rules` accept `?source_system_id=` |
| Functions | `GET/POST /functions`, `GET/DELETE /functions/{id}` (delete `409` while any playbook uses it), `GET/POST /functions/{id}/versions` (body `status: active\|draft`), `GET /functions/{id}/versions/{n}/source`, `GET .../{n}/usage`, `POST .../{n}/activate`, `POST .../{n}/retire` (`409` while pinned or active), `GET /functions/integrity`. Responses include `has_implementation` and `status` |
| Playbook builds | `POST/GET /workflows/build-requests`, `GET /workflows/build-requests/{id}`, `POST .../{id}/dry-run` (runs it, persists nothing), `POST .../{id}/approve` (`422` if a task pins a draft/retired check version), `POST .../{id}/reject` |
| Check-version pins | `GET /workflows/versions/{id}/pins` (pinned vs active version per task, with contract diff), `POST /workflows/definitions/{id}/upgrade` (`{base_version_id, calls: [..]\|"all", to: "active"\|N}` → build request) |
| Playbook definitions | `GET /workflows/definitions`, `GET /workflows/definitions/{id}/versions`, `GET /workflows/active?source_system_id=&category=`, `POST /workflows/versions/{id}/execute` |
| Playbook editing | `GET /workflows/versions/{id}/yaml`, `POST /workflows/render-yaml`, `POST /workflows/validate-yaml` (dry run), `POST /workflows/definitions/{id}/edits`. Validation errors are `422` with `{kind: syntax\|schema\|registry, errors: [{message, path, line, column, task_index}]}` |
| Executions & feedback | `GET /workflows/executions[?incident_key=&jira_key=]`, `GET /workflows/executions/{id}`, `GET/POST /workflows/executions/{id}/feedback`, `GET /workflows/rca-patterns` |
| Incidents | `GET /incidents`, `GET /incidents/dashboard`, `GET /incidents/{id}`, `POST /incidents/{incident_key}/retry` (re-classifies, then runs), `POST /rca/{jira_key}[?post_comment=true]` (404 if Jira has no such ticket) |
| Stats | `GET /stats/incidents` |

---

## Testing

```
uv run pytest                                   # full suite
uv run pytest tests/test_checks.py              # one file
uv run pytest tests/test_checks.py::test_x      # one test
```

Tests start ephemeral MongoDB (replica set), Chroma, Valkey, RabbitMQ and OPA containers with testcontainers, so **Docker must be
running**. Poller/Graph/Jira logic is covered by mocked tests using real sample-ticket fixtures.
`tests/test_JIRA_Token.py` is a manual live-Jira connectivity check with a placeholder site; exclude it from routine
runs with `--deselect tests/test_JIRA_Token.py::test_read_jira_ticket`.

Frontend end-to-end tests (Playwright) run against the real running stack — `start.ps1` plus the UI on `:3001`
(override with `E2E_BASE_URL`):

```
cd frontend
pnpm exec playwright install chromium   # first time only
pnpm e2e
```

The dashboard and retry tests fetch the real ticket `TT-1` from Jira, and with `RCA_SYNTHESIS_LLM_ENABLED=true` each
playbook run makes an OpenAI call.

---

## Operational notes

- **MongoDB is a replica set** (single-node locally): multi-document transactions need one. Connection strings use
  `directConnection=true`.
- **RAG vectors are derived data.** ChromaDB only holds embeddings of Mongo documents (footprints, classified
  incidents, feedback). If it drifts (an embed failed, a volume was lost, the embedding model changed), run
  `uv run python -m scripts.rebuild_vector_index --missing` (`--dry-run` to only report, `--only feedback` etc.). The
  deterministic pipeline, and `/health`, don't need Chroma unless an LLM feature flag is on.
- **Logs:** Python `logging` writes to stderr, which `start.ps1` sends to `.run\logs\<process>.err.log`.
- **Schema changes:** there are no migrations. Collections and indexes are created by `app.db.ensure_indexes()` at
  startup and by the load scripts. Adding a field needs nothing; changing an index definition needs the old index
  dropped (or `.\scripts\stop.ps1 -RemoveVolumes`, then `start.ps1`).
- **No linter/formatter or CI** is configured.

## Project layout

```
app/                FastAPI app, workers, classification, playbook engine, RCA synthesis
app/routers/        REST endpoints
app/check_types/    v2 check implementations (one module per check type)
dataloadscripts/    catalog YAML (source systems, mapping rules, playbooks) and the loaders/seeders that load it
scripts/            start/stop, dev server, CLI tools
opa/policies/       OPA classification policy
frontend/           Next.js operator UI (frontend/e2e/: Playwright tests)
tests/              pytest suite
```
