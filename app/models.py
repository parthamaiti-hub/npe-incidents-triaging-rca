"""MongoDB document models -- one collection per model, `_id` = `id`.

Class and field names are exactly the old relational ones, so API schemas
built with `Out.model_validate(row, from_attributes=True)` keep working.
What used to be CHECK constraints are Literal/Field constraints here, so an
invalid value can't even be constructed (validate_assignment covers later
mutation too). Foreign keys don't exist in Mongo: insert-side checks live
where they always did (routers), delete-side checks in
app.repositories.refs.

Writes are always explicit (app.repositories.base), never implicit dirty
tracking -- mutating a model in memory changes nothing in the database.

The two RAG corpora (classification/feedback embeddings) are not here: they
live in ChromaDB, see app.vector_store.
"""

import datetime
import uuid
from typing import Any, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator


def normalize_datetime(value: datetime.datetime) -> datetime.datetime:
    """Naive UTC at millisecond precision -- exactly what a BSON date
    round-trips as, so an in-memory model always equals its stored form."""
    if value.tzinfo is not None:
        value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=value.microsecond // 1000 * 1000)


def utcnow() -> datetime.datetime:
    return normalize_datetime(datetime.datetime.now(datetime.timezone.utc))


def new_id() -> str:
    return str(uuid.uuid4())


class Document(BaseModel):
    COLLECTION: ClassVar[str]

    model_config = ConfigDict(validate_assignment=True, extra="ignore")

    id: str

    @field_validator("*", mode="after")
    @classmethod
    def _normalize_datetimes(cls, value: Any) -> Any:
        if isinstance(value, datetime.datetime):
            return normalize_datetime(value)
        return value

    def to_doc(self) -> dict:
        doc = self.model_dump()
        doc["_id"] = doc.pop("id")
        return doc

    @classmethod
    def from_doc(cls, doc: dict) -> Self:
        data = dict(doc)
        data["id"] = data.pop("_id")
        return cls.model_validate(data)


class FunctionDefinition(Document):
    """Stable identity for one callable check_type -- id is the
    check_type/function name itself, e.g. 'error_logs'. Never holds the
    contract itself; that lives in its versions (FunctionDefinitionVersion),
    mirroring WorkflowDefinition/WorkflowDefinitionVersion so
    editing a function's contract never mutates history in place."""

    COLLECTION: ClassVar[str] = "function_definition"


class FunctionDefinitionVersion(Document):
    """One immutable snapshot of a function's contract (description, typed
    params, default retry policy). At most one version per function has
    status='active' at a time (a partial unique index enforces it) --
    publishing a new version supersedes the prior one, same pattern as
    WorkflowDefinitionVersion. This is what a WorkflowDefinitionVersion's
    document tasks pin via function_version_id (a plain string reference
    inside the document, so a workflow can record exactly which function
    contract version it was built/validated against, even after that
    function's contract later changes)."""

    COLLECTION: ClassVar[str] = "function_definition_version"

    id: str = Field(default_factory=new_id)
    function_definition_id: str
    version_number: int
    description: str
    params: list[dict]
    default_retry: dict
    status: Literal["active", "superseded"]
    created_by: str
    created_at: datetime.datetime = Field(default_factory=utcnow)


class Team(Document):
    """Manual-triage assignment group (e.g. dpo-dice, dpo-omd).
    Reporting metadata only -- never an input to source-system classification."""

    COLLECTION: ClassVar[str] = "team"

    name: str
    teams_handle: str
    aliases: str | None = None


class Environment(Document):
    """Enterprise master data for non-prod environment names (QLAB01-06,
    PIT03, DEV2, STG02, PLAB01, ...). Matched via simple keyword/alias
    lookup, independent of the SOURCE_SYSTEM OPA-based matching pipeline."""

    COLLECTION: ClassVar[str] = "environment"

    code: str
    name: str
    aliases: str | None = None
    in_scope: bool = True


class SourceSystem(Document):
    COLLECTION: ClassVar[str] = "source_system"

    name: str
    code: str
    type: str
    description: str
    owning_team: str
    environment: str
    owning_team_id: str | None = None


class SystemFootprint(Document):
    COLLECTION: ClassVar[str] = "system_footprint"

    source_system_id: str
    footprint_type: str
    value: str
    notes: str | None = None


class IncidentMappingRule(Document):
    COLLECTION: ClassVar[str] = "incident_mapping_rule"

    source_system_id: str
    category: str
    signal_type: str
    signal_pattern: str
    priority: int
    action: str


class WorkflowDefinition(Document):
    """Stable identity for a (source_system, category)'s workflow -- replaces
    the old single-row-per-playbook RcaPlaybook. Never holds the task list
    itself; that lives in its versions (WorkflowDefinitionVersion), so
    editing a workflow never mutates history in place."""

    COLLECTION: ClassVar[str] = "workflow_definition"

    source_system_id: str
    category: str

    @field_validator("category")
    @classmethod
    def _category_not_any(cls, value: str) -> str:
        if value == "ANY":
            raise ValueError("WorkflowDefinition.category cannot be 'ANY'")
        return value


class WorkflowDefinitionVersion(Document):
    """One immutable snapshot of a workflow's task list -- never edited in
    place, a new version is created instead. At most one version per
    definition has status='approved' at a time (approving a new one
    supersedes the prior approved version; a partial unique index enforces
    it); execution only ever looks up the 'approved' one, per
    app.workflow_orchestrator.get_active_workflow."""

    COLLECTION: ClassVar[str] = "workflow_definition_version"

    id: str = Field(default_factory=new_id)
    workflow_definition_id: str
    version_number: int
    document: list[dict]
    status: Literal["draft", "pending_approval", "approved", "rejected", "superseded"]
    source: Literal["static_authored", "dynamic_generated"]
    build_request_id: str | None = None
    created_by: str
    created_at: datetime.datetime = Field(default_factory=utcnow)
    approved_by: str | None = None
    approved_at: datetime.datetime | None = None


class WorkflowBuildRequest(Document):
    """The raw ask behind a dynamically-generated workflow version -- kept
    separate from the compiled/validated document (generated_document) so
    both "what was asked for" and "what was actually generated" are
    auditable even if compilation adjusts or rejects something."""

    COLLECTION: ClassVar[str] = "workflow_build_request"

    id: str = Field(default_factory=new_id)
    source_system_id: str
    category: str
    requested_by: str
    use_case_description: str | None = None
    requested_functions: list[dict]
    generated_document: list[dict] | None = None
    status: Literal["pending", "rendered", "approved", "rejected"] = "pending"
    created_at: datetime.datetime = Field(default_factory=utcnow)
    # Set when this request edits an existing version -- approval
    # is refused if that version is no longer the approved one (a stale
    # edit must not silently supersede a newer publish).
    base_version_id: str | None = None


class WorkflowExecution(Document):
    """One recorded RCA attempt -- document_snapshot is a defensive copy (not
    just a reference to the version) so a run stays reproducible even if a
    version document were ever mutated.

    This is the single log of every RCA attempt, not just
    ones that reached an executable workflow -- workflow_definition_version_id
    is nullable so a no_playbook/not_classified outcome still gets a
    document, with document_snapshot/evidence empty and rca/rca_status set
    to the synthetic outcome. This is what the UI's dashboard/stats/
    retry-history endpoints read latest-per-incident from."""

    COLLECTION: ClassVar[str] = "workflow_execution"

    id: str = Field(default_factory=new_id)
    jira_key: str | None = None
    incident_id: str | None = None
    # The incident's operator-facing key at the time of the run (see
    # Incident.incident_key); what execution history is filtered by.
    incident_key: str | None = None
    workflow_definition_version_id: str | None = None
    document_snapshot: list[dict]
    evidence: list[dict] | None = None
    rca: dict | None = None
    rca_status: str | None = None
    # auto: the worker's RCA right after classification.
    triggered_by: Literal["rca", "manual_execute", "retry", "auto"] = "manual_execute"
    requested_by: str | None = None
    mapping_overridden: bool = False
    status: Literal["running", "completed", "failed"] = "running"
    started_at: datetime.datetime = Field(default_factory=utcnow)
    completed_at: datetime.datetime | None = None

    # Operator-supplied free text, set on trigger/retry
    # (Retry tab), included as its own labeled tier in the RCA-synthesis
    # LLM prompt -- distinct from automated evidence and RAG-retrieved
    # history. Persisted for audit, not just transient input.
    operator_context: str | None = None

    # LLM-derived RCA relaxes byte-for-byte reproducibility
    # (a retry can produce a different result -- model updates, retrieval
    # corpus growth), so *why* a given RCA came out the way it did needs
    # its own audit trail: {model, prompt_version, retrieved_context_ids,
    # generated_at}. Null for deterministic-path executions.
    rca_meta: dict | None = None


class RcaFeedback(Document):
    """Operator feedback on one RCA attempt -- comment + confidence score.
    Multiple entries per execution are allowed (a feedback
    thread, not a single overwritable field)."""

    COLLECTION: ClassVar[str] = "rca_feedback"

    id: str = Field(default_factory=new_id)
    workflow_execution_id: str
    comment: str | None = None
    confidence_score: int = Field(ge=1, le=5)
    given_by: str
    created_at: datetime.datetime = Field(default_factory=utcnow)

    # Optional structured correction -- much cleaner RAG
    # grounding than free-text comment alone ("this past case's
    # environment_config classification was corrected to
    # functional_defect_recent_release", unambiguous, vs. inferring that
    # from prose). Both nullable; a plain comment+score is still valid.
    corrected_pattern_id: str | None = None
    corrected_rca_status: str | None = None


class RcaPatternType(Document):
    """The catalog that replaces app.rca_synthesizer's
    hardcoded if/elif pattern-matching -- a closed, but growable, set the
    LLM classifies evidence against (never free text, same anti-
    hallucination discipline as classification's closed-set constraint).
    max_rca_status is the honest-ceiling clamp: the LLM's own
    self-assessed status for one execution can only be downgraded to this
    pattern's registered ceiling, never upgraded past it. Growing this
    catalog (or raising a pattern's ceiling) is a human-curated action, the
    same approval discipline already used for playbook versions -- not an
    automatic consequence of an LLM call."""

    COLLECTION: ClassVar[str] = "rca_pattern_type"

    description: str
    max_rca_status: Literal["Identified", "Probable", "Inconclusive"]
    status: Literal["active", "retired"] = "active"
    created_by: str
    created_at: datetime.datetime = Field(default_factory=utcnow)


class CorrelationGroup(Document):
    """A cluster of incidents against the same application
    (source_system_id) and category, arriving within a sliding time window
    of each other -- e.g. 5 tickets filed against SYS_HSI in 12 minutes are
    almost certainly one outage, not 5 independent root causes. Has its own
    identity (not just a field on Incident) because a cluster needs a
    lifecycle and a representative root cause independent of any single
    member incident."""

    COLLECTION: ClassVar[str] = "correlation_group"

    id: str = Field(default_factory=new_id)
    source_system_id: str
    category: str
    opened_at: datetime.datetime
    last_seen_at: datetime.datetime
    incident_count: int = 1
    representative_incident_id: str | None = None
    status: Literal["open", "closed"] = "open"


class Incident(Document):
    """An ingested incident and its classification outcome. Fields mirror
    real Jira issue data plus the signals extracted from its text.

    incident_key is the one operator-facing ID every incident has -- the
    dashboard, detail page, Retry tab and execution history all use it. It
    is the Jira key when the incident arrived with one; otherwise
    app.repositories.incidents.insert_incident generates an internal
    int_<MMDDYYYYHHMMSS UTC>_<NNNNN> key, so webhook incidents without a Jira
    ticket can still be found and reprocessed. It never changes once
    assigned, and is always set once the incident has been inserted."""

    COLLECTION: ClassVar[str] = "incident"

    id: str = Field(default_factory=new_id)
    incident_key: str | None = None
    source: str
    external_id: str
    raw_text: str
    subject: str | None = None

    # Only ever a real Jira issue key -- anything that talks to Jira uses this,
    # never incident_key.
    jira_key: str | None = None
    status: str | None = None
    priority: str | None = None
    issue_type: str | None = None
    reporter: str | None = None
    assignee: str | None = None
    labels: list[str] | None = None
    resolution: str | None = None
    created: datetime.datetime | None = None
    updated: datetime.datetime | None = None

    environment_raw: str | None = None
    environment_id: str | None = None
    application_id_hint: str | None = None
    error_system_hint: str | None = None
    addressed_team_raw: str | None = None
    addressed_team_id: str | None = None
    teams_mentions: list[dict] | None = None
    related_jira_keys: list[str] | None = None

    classification_status: str = "pending"
    matched_rule_id: str | None = None
    source_system_id: str | None = None
    category: str | None = None

    # Set together, always -- "rule" + None for a
    # deterministic match, "llm" + a confidence score for an
    # LLM_RESOLVED one. Lets an operator (and the dashboard) always tell
    # an LLM-driven classification from a rule-driven one at a glance,
    # never presented as equal-confidence.
    llm_confidence: float | None = None
    classification_method: str | None = None

    # Set by app.correlation.correlate_incident once this
    # incident has been clustered with CORRELATION_THRESHOLD+ others against
    # the same (source_system_id, category) within CORRELATION_WINDOW_MINUTES.
    correlation_group_id: str | None = None

    received_at: datetime.datetime = Field(default_factory=utcnow)
