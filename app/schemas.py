import re

from pydantic import BaseModel, Field, field_validator

from app.categories import DEFAULT_CATEGORY


class TeamIn(BaseModel):
    id: str
    name: str
    teams_handle: str
    aliases: str | None = None


class EnvironmentIn(BaseModel):
    id: str
    code: str
    name: str
    aliases: str | None = None
    in_scope: bool = True


class SourceSystemIn(BaseModel):
    id: str
    name: str
    code: str
    type: str
    description: str
    owning_team: str
    environment: str
    owning_team_id: str | None = None


class SystemFootprintIn(BaseModel):
    id: str
    source_system_id: str
    footprint_type: str
    value: str
    notes: str | None = None

    @field_validator("value")
    @classmethod
    def value_must_compile_as_regex(cls, v: str) -> str:
        # re.error subclasses Exception, not ValueError -- Pydantic v2 only
        # converts ValueError/TypeError/AssertionError raised in a validator
        # into a proper 422; anything else propagates as an unhandled 500.
        # Covered by the catalog write-path tests, which exercise an invalid
        # regex through this path.
        try:
            re.compile(v)
        except re.error as exc:
            raise ValueError(f"not a valid regular expression: {exc}") from exc
        return v


class IncidentMappingRuleIn(BaseModel):
    id: str
    source_system_id: str
    category: str
    signal_type: str
    signal_pattern: str
    priority: int
    action: str

    @field_validator("category")
    @classmethod
    def category_not_default(cls, v: str) -> str:
        if v == DEFAULT_CATEGORY:
            raise ValueError(
                f"INCIDENT_MAPPING_RULE.category cannot be {DEFAULT_CATEGORY!r} -- the default playbook runs when no "
                f"specific category/playbook is found; use 'ANY' for a rule that identifies only the system"
            )
        return v

    @field_validator("signal_pattern")
    @classmethod
    def pattern_must_compile(cls, v: str) -> str:
        try:
            re.compile(v)
        except re.error as exc:
            raise ValueError(f"not a valid regular expression: {exc}") from exc
        return v


class TaskIn(BaseModel):
    """One CNCF Serverless Workflow-style task: `call` references a
    app.function_registry.FUNCTION_REGISTRY entry, `with` is its params.
    Validated for real (unknown call / missing required param) by
    app.workflow_spec.TaskSpec once FUNCTION_REGISTRY is available --
    kept as a permissive dict-shaped model here to avoid a schemas.py ->
    function_registry.py import at YAML-parse time."""

    name: str | None = None
    call: str
    # Optional explicit check_type version pin (Sol-104). Omitted: the step
    # keeps the pin it had in the previously loaded version of this
    # playbook if unchanged, else gets the function's active version.
    version: int | None = Field(default=None, ge=1)
    with_: dict = Field(default_factory=dict, alias="with")
    retry: dict | None = None

    model_config = {"populate_by_name": True}


class RcaPlaybookIn(BaseModel):
    id: str
    source_system_id: str
    category: str
    steps: list[TaskIn]

    @field_validator("category")
    @classmethod
    def category_not_any(cls, v: str) -> str:
        if v == "ANY":
            raise ValueError("RCA_PLAYBOOK.category cannot be 'ANY'")
        return v


class CatalogIn(BaseModel):
    teams: list[TeamIn] = []
    environments: list[EnvironmentIn] = []
    source_systems: list[SourceSystemIn]
    system_footprints: list[SystemFootprintIn]
    incident_mapping_rules: list[IncidentMappingRuleIn]
    rca_playbooks: list[RcaPlaybookIn]
