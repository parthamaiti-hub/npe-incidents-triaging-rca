import datetime
import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import OPENAI_EMBEDDING_DIMENSIONS
from app.db import Base


class FunctionDefinition(Base):
    """Stable identity for one callable check_type -- id is the
    check_type/function name itself, e.g. 'error_logs'. Never holds the
    contract itself; that lives in its versions (FunctionDefinitionVersion),
    mirroring WorkflowDefinition/WorkflowDefinitionVersion so
    editing a function's contract never mutates history in place."""

    __tablename__ = "function_definition"

    id: Mapped[str] = mapped_column(primary_key=True)

    versions: Mapped[list["FunctionDefinitionVersion"]] = relationship(back_populates="function_definition")


class FunctionDefinitionVersion(Base):
    """One immutable snapshot of a function's contract (description, typed
    params, default retry policy). At most one version per function has
    status='active' at a time -- publishing a new version supersedes the
    prior one, same pattern as WorkflowDefinitionVersion. This is what a
    WorkflowDefinitionVersion's document tasks pin via function_version_id
    (a plain string reference inside the JSONB document, not a DB-level FK
    array -- so a workflow can record exactly which function contract
    version it was built/validated against, even after that function's
    contract later changes)."""

    __tablename__ = "function_definition_version"
    __table_args__ = (
        UniqueConstraint("function_definition_id", "version_number", name="uq_fdv_definition_version_number"),
        CheckConstraint("status in ('active', 'superseded')", name="ck_fdv_status"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    function_definition_id: Mapped[str] = mapped_column(ForeignKey("function_definition.id"))
    version_number: Mapped[int]
    description: Mapped[str]
    params: Mapped[list[dict]] = mapped_column(JSONB)
    default_retry: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str]
    created_by: Mapped[str]
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=func.now())

    function_definition: Mapped["FunctionDefinition"] = relationship(back_populates="versions")


class Team(Base):
    """Manual-triage assignment group (e.g. dpo-dice, dpo-omd).
    Reporting metadata only -- never an input to source-system classification."""

    __tablename__ = "team"

    id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]
    teams_handle: Mapped[str]
    aliases: Mapped[str | None]


class Environment(Base):
    """Enterprise master data for non-prod environment names (QLAB01-06,
    PIT03, DEV2, STG02, PLAB01, ...). Matched via simple keyword/alias
    lookup, independent of the SOURCE_SYSTEM OPA-based matching pipeline."""

    __tablename__ = "environment"

    id: Mapped[str] = mapped_column(primary_key=True)
    code: Mapped[str]
    name: Mapped[str]
    aliases: Mapped[str | None]
    in_scope: Mapped[bool] = mapped_column(default=True)


class SourceSystem(Base):
    __tablename__ = "source_system"

    id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]
    code: Mapped[str]
    type: Mapped[str]
    description: Mapped[str]
    owning_team: Mapped[str]
    environment: Mapped[str]
    owning_team_id: Mapped[str | None] = mapped_column(ForeignKey("team.id"))

    footprints: Mapped[list["SystemFootprint"]] = relationship(back_populates="source_system")
    mapping_rules: Mapped[list["IncidentMappingRule"]] = relationship(back_populates="source_system")
    playbooks: Mapped[list["WorkflowDefinition"]] = relationship(back_populates="source_system")


class SystemFootprint(Base):
    __tablename__ = "system_footprint"

    id: Mapped[str] = mapped_column(primary_key=True)
    source_system_id: Mapped[str] = mapped_column(ForeignKey("source_system.id"))
    footprint_type: Mapped[str]
    value: Mapped[str]
    notes: Mapped[str | None]

    source_system: Mapped["SourceSystem"] = relationship(back_populates="footprints")


class IncidentMappingRule(Base):
    __tablename__ = "incident_mapping_rule"

    id: Mapped[str] = mapped_column(primary_key=True)
    source_system_id: Mapped[str] = mapped_column(ForeignKey("source_system.id"))
    category: Mapped[str]
    signal_type: Mapped[str]
    signal_pattern: Mapped[str]
    priority: Mapped[int]
    action: Mapped[str]

    source_system: Mapped["SourceSystem"] = relationship(back_populates="mapping_rules")


class WorkflowDefinition(Base):
    """Stable identity for a (source_system, category)'s workflow -- replaces
    the old single-row-per-playbook RcaPlaybook. Never holds the task list
    itself; that lives in its versions (WorkflowDefinitionVersion), so
    editing a workflow never mutates history in place."""

    __tablename__ = "workflow_definition"
    __table_args__ = (
        UniqueConstraint("source_system_id", "category", name="uq_workflow_definition_system_category"),
        CheckConstraint("category <> 'ANY'", name="ck_workflow_definition_category_not_any"),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    source_system_id: Mapped[str] = mapped_column(ForeignKey("source_system.id"))
    category: Mapped[str]

    source_system: Mapped["SourceSystem"] = relationship(back_populates="playbooks")
    versions: Mapped[list["WorkflowDefinitionVersion"]] = relationship(back_populates="workflow_definition")


class WorkflowDefinitionVersion(Base):
    """One immutable snapshot of a workflow's task list -- never edited in
    place, a new version is created instead. At most one version per
    definition has status='approved' at a time (approving a new one
    supersedes the prior approved version); execution only ever looks up
    the 'approved' one, per app.workflow_orchestrator.get_active_workflow."""

    __tablename__ = "workflow_definition_version"
    __table_args__ = (
        UniqueConstraint("workflow_definition_id", "version_number", name="uq_wdv_definition_version_number"),
        CheckConstraint(
            "status in ('draft','pending_approval','approved','rejected','superseded')",
            name="ck_wdv_status",
        ),
        CheckConstraint("source in ('static_authored','dynamic_generated')", name="ck_wdv_source"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    workflow_definition_id: Mapped[str] = mapped_column(ForeignKey("workflow_definition.id"))
    version_number: Mapped[int]
    document: Mapped[list[dict]] = mapped_column(JSONB)
    status: Mapped[str]
    source: Mapped[str]
    build_request_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_build_request.id"))
    created_by: Mapped[str]
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=func.now())
    approved_by: Mapped[str | None]
    approved_at: Mapped[datetime.datetime | None]

    workflow_definition: Mapped["WorkflowDefinition"] = relationship(
        back_populates="versions", foreign_keys=[workflow_definition_id]
    )


class WorkflowBuildRequest(Base):
    """The raw ask behind a dynamically-generated workflow version -- kept
    separate from the compiled/validated document (generated_document) so
    both "what was asked for" and "what was actually generated" are
    auditable even if compilation adjusts or rejects something."""

    __tablename__ = "workflow_build_request"
    __table_args__ = (CheckConstraint("status in ('pending','rendered','approved','rejected')", name="ck_wbr_status"),)

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    source_system_id: Mapped[str] = mapped_column(ForeignKey("source_system.id"))
    category: Mapped[str]
    requested_by: Mapped[str]
    use_case_description: Mapped[str | None]
    requested_functions: Mapped[list[dict]] = mapped_column(JSONB)
    generated_document: Mapped[list[dict] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(default="pending")
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=func.now())
    # Set when this request edits an existing version -- approval
    # is refused if that version is no longer the approved one (a stale
    # edit must not silently supersede a newer publish). Plain column, no
    # FK: workflow_definition_version already FKs back to this table.
    base_version_id: Mapped[str | None]


class WorkflowExecution(Base):
    """One recorded RCA attempt -- document_snapshot is a defensive copy (not
    just a join through the version FK) so a run stays reproducible even if
    a version row were ever mutated.

    This is the single log of every RCA attempt, not just
    ones that reached an executable workflow -- workflow_definition_version_id
    is nullable so a no_playbook/not_classified outcome (previously never
    recorded anywhere) still gets a row, with document_snapshot/evidence
    empty and rca/rca_status set to the synthetic outcome. This is what the
    UI's dashboard/stats/retry-history endpoints read latest-per-incident
    from."""

    __tablename__ = "workflow_execution"
    __table_args__ = (
        CheckConstraint("status in ('running','completed','failed')", name="ck_we_status"),
        CheckConstraint("triggered_by in ('rca','manual_execute','retry')", name="ck_we_triggered_by"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    jira_key: Mapped[str | None]
    incident_id: Mapped[str | None] = mapped_column(ForeignKey("incident.id"))
    workflow_definition_version_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_definition_version.id"))
    document_snapshot: Mapped[list[dict]] = mapped_column(JSONB)
    evidence: Mapped[list[dict] | None] = mapped_column(JSONB)
    rca: Mapped[dict | None] = mapped_column(JSONB)
    rca_status: Mapped[str | None]
    triggered_by: Mapped[str] = mapped_column(default="manual_execute")
    requested_by: Mapped[str | None]
    mapping_overridden: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(default="running")
    started_at: Mapped[datetime.datetime] = mapped_column(server_default=func.now())
    completed_at: Mapped[datetime.datetime | None]

    # Operator-supplied free text, set on trigger/retry
    # (Retry tab), included as its own labeled tier in the RCA-synthesis
    # LLM prompt -- distinct from automated evidence and RAG-retrieved
    # history. Persisted for audit, not just transient input.
    operator_context: Mapped[str | None]

    # LLM-derived RCA relaxes byte-for-byte reproducibility
    # (a retry can produce a different result -- model updates, retrieval
    # corpus growth), so *why* a given RCA came out the way it did needs
    # its own audit trail: {model, prompt_version, retrieved_context_ids,
    # generated_at}. Null for deterministic-path executions.
    rca_meta: Mapped[dict | None] = mapped_column(JSONB)


class RcaFeedback(Base):
    """Operator feedback on one RCA attempt -- comment + confidence score.
    Multiple entries per execution are allowed (a feedback
    thread, not a single overwritable field)."""

    __tablename__ = "rca_feedback"
    __table_args__ = (CheckConstraint("confidence_score between 1 and 5", name="ck_feedback_confidence_score"),)

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    workflow_execution_id: Mapped[str] = mapped_column(ForeignKey("workflow_execution.id"))
    comment: Mapped[str | None]
    confidence_score: Mapped[int]
    given_by: Mapped[str]
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=func.now())

    # Optional structured correction -- much cleaner RAG
    # grounding than free-text comment alone ("this past case's
    # environment_config classification was corrected to
    # functional_defect_recent_release", unambiguous, vs. inferring that
    # from prose). Both nullable; a plain comment+score is still valid.
    corrected_pattern_id: Mapped[str | None] = mapped_column(ForeignKey("rca_pattern_type.id"))
    corrected_rca_status: Mapped[str | None]


class RcaPatternType(Base):
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

    __tablename__ = "rca_pattern_type"
    __table_args__ = (
        CheckConstraint("status in ('active', 'retired')", name="ck_rca_pattern_type_status"),
        CheckConstraint(
            "max_rca_status in ('Identified','Probable','Inconclusive')",
            name="ck_rca_pattern_type_max_rca_status",
        ),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    description: Mapped[str]
    max_rca_status: Mapped[str]
    status: Mapped[str] = mapped_column(default="active")
    created_by: Mapped[str]
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=func.now())


class ClassificationEmbedding(Base):
    """RAG retrieval corpus for LLM fallback classification -- embedded
    SystemFootprint rows and resolved Incident rows, so the LLM classifies
    against retrieved real candidates rather than an open-ended guess.
    pgvector-backed (Postgres extension), not a separate vector-DB service."""

    __tablename__ = "classification_embedding"
    __table_args__ = (
        CheckConstraint("kind in ('footprint', 'incident')", name="ck_classification_embedding_kind"),
        # Belt-and-suspenders against duplicate embeddings on message
        # redelivery -- the primary defense is app.worker's existence-check
        # before embedding, this is the DB-level backstop. Partial (kind
        # rows other than 'incident' have no incident_id to be unique on).
        Index(
            "uq_classification_embedding_incident",
            "incident_id",
            unique=True,
            postgresql_where=text("kind = 'incident'"),
        ),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    kind: Mapped[str]
    source_system_id: Mapped[str] = mapped_column(ForeignKey("source_system.id"))
    category: Mapped[str | None]
    text: Mapped[str]
    embedding: Mapped[list[float]] = mapped_column(Vector(OPENAI_EMBEDDING_DIMENSIONS))
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=func.now())

    # Added so a redelivered message can check "have I already
    # embedded this incident?" before calling OpenAI again. Nullable --
    # kind="footprint" rows aren't tied to any one incident.
    incident_id: Mapped[str | None] = mapped_column(ForeignKey("incident.id"))


class FeedbackEmbedding(Base):
    """The feedback-grounded RAG loop -- one row per
    embedded RcaFeedback, composite text of the triggering execution's
    diagnosis plus the feedback itself, so retrieval finds "similar
    evidence AND how a human judged it," not just similar text.
    confidence_score is carried alongside the embedding so retrieval can
    label a low-confidence match as a correction to avoid repeating,
    never as a validated example (see the prompt-injection guardrail in
    app.rca_worker -- this text is untrusted operator input, embedded and
    retrieved as reference data, never as instructions)."""

    __tablename__ = "feedback_embedding"

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    rca_feedback_id: Mapped[str] = mapped_column(ForeignKey("rca_feedback.id"), unique=True)
    workflow_execution_id: Mapped[str] = mapped_column(ForeignKey("workflow_execution.id"))
    text: Mapped[str]
    confidence_score: Mapped[int]
    embedding: Mapped[list[float]] = mapped_column(Vector(OPENAI_EMBEDDING_DIMENSIONS))
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=func.now())


class CorrelationGroup(Base):
    """A cluster of incidents against the same application
    (source_system_id) and category, arriving within a sliding time window
    of each other -- e.g. 5 tickets filed against SYS_HSI in 12 minutes are
    almost certainly one outage, not 5 independent root causes. Has its own
    identity (not just a nullable FK on Incident) because a cluster needs a
    lifecycle and a representative root cause independent of any single
    member incident."""

    __tablename__ = "correlation_group"
    __table_args__ = (CheckConstraint("status in ('open', 'closed')", name="ck_correlation_group_status"),)

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    source_system_id: Mapped[str] = mapped_column(ForeignKey("source_system.id"))
    category: Mapped[str]
    opened_at: Mapped[datetime.datetime]
    last_seen_at: Mapped[datetime.datetime]
    incident_count: Mapped[int] = mapped_column(default=1)
    representative_incident_id: Mapped[str | None] = mapped_column(
        ForeignKey("incident.id", use_alter=True, name="fk_correlation_group_representative_incident_id")
    )
    status: Mapped[str] = mapped_column(default="open")


class Incident(Base):
    """An ingested incident and its classification outcome. Fields mirror
    real Jira issue data plus the signals extracted from its text."""

    __tablename__ = "incident"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_incident_source_external_id"),)

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    source: Mapped[str]
    external_id: Mapped[str]
    raw_text: Mapped[str]
    subject: Mapped[str | None]

    jira_key: Mapped[str | None] = mapped_column(unique=True)
    status: Mapped[str | None]
    priority: Mapped[str | None]
    issue_type: Mapped[str | None]
    reporter: Mapped[str | None]
    assignee: Mapped[str | None]
    labels: Mapped[list[str] | None] = mapped_column(JSONB)
    resolution: Mapped[str | None]
    created: Mapped[datetime.datetime | None]
    updated: Mapped[datetime.datetime | None]

    environment_raw: Mapped[str | None]
    environment_id: Mapped[str | None] = mapped_column(ForeignKey("environment.id"))
    application_id_hint: Mapped[str | None]
    error_system_hint: Mapped[str | None]
    addressed_team_raw: Mapped[str | None]
    addressed_team_id: Mapped[str | None] = mapped_column(ForeignKey("team.id"))
    teams_mentions: Mapped[list[dict] | None] = mapped_column(JSONB)
    related_jira_keys: Mapped[list[str] | None] = mapped_column(JSONB)

    classification_status: Mapped[str] = mapped_column(default="pending")
    matched_rule_id: Mapped[str | None] = mapped_column(ForeignKey("incident_mapping_rule.id"))
    source_system_id: Mapped[str | None] = mapped_column(ForeignKey("source_system.id"))
    category: Mapped[str | None]

    # Set together, always -- "rule" + None for a
    # deterministic match, "llm" + a confidence score for an
    # LLM_RESOLVED one. Lets an operator (and the dashboard) always tell
    # an LLM-driven classification from a rule-driven one at a glance,
    # never presented as equal-confidence.
    llm_confidence: Mapped[float | None]
    classification_method: Mapped[str | None]

    # Set by app.correlation.correlate_incident once this
    # incident has been clustered with CORRELATION_THRESHOLD+ others against
    # the same (source_system_id, category) within CORRELATION_WINDOW_MINUTES.
    correlation_group_id: Mapped[str | None] = mapped_column(
        ForeignKey("correlation_group.id", use_alter=True, name="fk_incident_correlation_group_id")
    )

    received_at: Mapped[datetime.datetime] = mapped_column(server_default=func.now())
