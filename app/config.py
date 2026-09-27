import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://npe:npe@localhost:5433/npe_triage_rca",
)
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6380/0")
OPA_URL = os.environ.get("OPA_URL", "http://localhost:8182")

IDEMPOTENCY_TTL_SECONDS = int(os.environ.get("IDEMPOTENCY_TTL_SECONDS", str(24 * 60 * 60)))

# RabbitMQ carries a single queue, incidents.raw (plus its dead-letter
# queue). On-demand re-diagnosis goes through POST /incidents/{jira_key}/retry,
# which is DB-backed and needs no broker.
RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@localhost:5673/")

# Microsoft Graph (Teams) + Jira Cloud. These default to empty strings,
# which is fine for mocked tests but will fail acquire_token()/API calls
# until an Azure AD app registration and Jira API token are configured.
AZURE_TENANT_ID = os.environ.get("AZURE_TENANT_ID", "")
AZURE_CLIENT_ID = os.environ.get("AZURE_CLIENT_ID", "")
AZURE_CLIENT_SECRET = os.environ.get("AZURE_CLIENT_SECRET", "")
TEAMS_TEAM_ID = os.environ.get("TEAMS_TEAM_ID", "")
TEAMS_CHANNEL_ID = os.environ.get("TEAMS_CHANNEL_ID", "")

JIRA_SITE = os.environ.get("JIRA_SITE", "")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))
POLL_LOOKBACK_MINUTES = int(os.environ.get("POLL_LOOKBACK_MINUTES", "30"))

# Cap on the poller's exponential backoff after a failed cycle, so a
# prolonged Graph/Jira outage doesn't stretch the retry interval unboundedly.
MAX_POLL_BACKOFF_SECONDS = int(os.environ.get("MAX_POLL_BACKOFF_SECONDS", "300"))

# The sweeper re-publishes Incident rows still "pending" after this many
# minutes -- the safety net for a crash/failure between the DB commit and
# the RabbitMQ publish on either ingestion path (webhook or poller), since
# nothing else re-triggers classification for a stranded row.
SWEEP_INTERVAL_SECONDS = int(os.environ.get("SWEEP_INTERVAL_SECONDS", "60"))
SWEEP_THRESHOLD_MINUTES = int(os.environ.get("SWEEP_THRESHOLD_MINUTES", "5"))

# Two incidents against the same (source_system_id, category) within this
# many minutes of each other are treated as one systemic issue rather than
# two independent ones. Threshold is the number of such incidents
# (inclusive) needed before a CorrelationGroup forms -- 2 is the minimum
# meaningful value (a single incident is never "correlated" with itself).
#
# At the target volume (3,000 incidents/24h across 300 applications),
# "correlation per application per day" is a firm requirement, hence 24h.
# This is a rolling window (measured from each new incident's own arrival,
# not a fixed span from the group's first member): a chain of incidents
# with no single gap over 24h can keep one group open well past 24h in
# total -- an accepted property of this choice, not a bug.
CORRELATION_WINDOW_MINUTES = int(os.environ.get("CORRELATION_WINDOW_MINUTES", "1440"))
CORRELATION_THRESHOLD = int(os.environ.get("CORRELATION_THRESHOLD", "2"))

# Empty by default -- the Next.js UI is expected to proxy API
# calls server-side (same-origin from the browser's perspective), which
# needs no CORS at all. Only set this for the dev-only case of a browser
# calling this API directly from a different origin.
CORS_ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()]

# OpenAI is used for classification fallback and, once activated, RCA
# synthesis + embeddings for RAG retrieval.
# No real key is provisioned yet -- empty string is fine for mocked tests
# (app.llm_client is never called with LLM_FALLBACK_ENABLED/
# RCA_SYNTHESIS_LLM_ENABLED both false) but will fail real API calls until
# a real key is set.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_EMBEDDING_MODEL = os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
OPENAI_EMBEDDING_DIMENSIONS = int(os.environ.get("OPENAI_EMBEDDING_DIMENSIONS", "1536"))

# Classification fallback -- opt-in, off by default (same
# convention as --post-comment for Jira writes). A winning deterministic
# rule is never second-guessed; this only fires for MANUAL_TRIAGE (no rule
# matched) and ANY_CATEGORY_PENDING_LLM (source system known, category
# still pending). Below CLASSIFICATION_CONFIDENCE_THRESHOLD, the incident
# stays at its pre-LLM status -- never a silent, unconfident classification.
LLM_FALLBACK_ENABLED = os.environ.get("LLM_FALLBACK_ENABLED", "false").lower() == "true"
CLASSIFICATION_CONFIDENCE_THRESHOLD = float(os.environ.get("CLASSIFICATION_CONFIDENCE_THRESHOLD", "0.7"))

# RCA synthesis LLM/RAG-primary path -- opt-in, off by
# default. Do not enable against today's [STUBBED] check evidence
# (app/check_implementations.py) -- there is no real signal yet for an LLM
# to reason over. Flip on once real check integrations land.
RCA_SYNTHESIS_LLM_ENABLED = os.environ.get("RCA_SYNTHESIS_LLM_ENABLED", "false").lower() == "true"

# Feedback-grounded RAG corpus retention/curation. Default is
# conservative -- embed only feedback-confirmed executions (not every
# resolved run) and age out anything older than the retention window, so
# the corpus doesn't grow unmanaged at the 3,000-incidents/24h target
# scale. Both are config, not structural -- loosen later if curation turns
# out to be too aggressive.
RAG_EMBED_FEEDBACK_ONLY = os.environ.get("RAG_EMBED_FEEDBACK_ONLY", "true").lower() == "true"
RAG_RETENTION_MONTHS = int(os.environ.get("RAG_RETENTION_MONTHS", "6"))

# Redis Stream (Valkey, already deployed for idempotency). Decouples RCA
# synthesis's LLM latency from execute_and_record's request path.
RCA_PENDING_STREAM = "stream:rca-pending"
RCA_PENDING_STREAM_DLQ = "stream:rca-pending-dlq"
RCA_WORKER_CONSUMER_GROUP = "npe-rca-worker"
