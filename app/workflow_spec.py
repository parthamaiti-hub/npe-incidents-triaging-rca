"""Pydantic model for a single workflow task -- the restricted CNCF
Serverless Workflow DSL subset this project implements: sequential `call`
tasks with `with` params and an optional `retry` override. No `fork`,
`switch`, `emit`/`listen`, or external `http`/`openapi`/`grpc` call types --
none are needed for evidence-gathering RCA playbooks.

Used both to validate statically-authored playbook steps at catalog-load
time (dataloadscripts/load_catalog.py) and to validate a dynamically-built
workflow's requested functions at build time (app/task_pinning.py) -- one
set of rules for both paths.
"""

from pydantic import BaseModel, Field, field_validator, model_validator

from app.function_registry import FUNCTION_REGISTRY, RetryPolicy, get_function_spec, is_known_function


class TaskSpec(BaseModel):
    name: str | None = None
    call: str
    with_: dict = Field(default_factory=dict, alias="with")
    retry: RetryPolicy | None = None
    # Requested check version (Sol-104). Set by an author to pin a task to a
    # specific version; app.task_pinning also sets it to the resolved pin
    # before validating, so required params are checked against the
    # contract of the version the task will actually run -- not the active
    # one. Never stored in a document: function_version_number is.
    version: int | None = Field(default=None, ge=1)
    # Pins exactly which FunctionDefinitionVersion this task was validated
    # against -- stamped server-side by app.task_pinning, not something a
    # client sets. None if that function has no DB-backed version yet
    # (still only in the built-in DEFAULT_FUNCTION_REGISTRY).
    function_version_id: str | None = None
    # The same version, as the int app.checks.run_check dispatches on.
    # Two versions of the same function are two different pieces of code:
    # a version with no implementation is rejected at build time.
    function_version_number: int | None = None
    # The pinned version's default retry policy, copied in at build time
    # when the task has no explicit `retry` -- so the stored document is
    # self-contained and a later contract change can't alter how an
    # already-approved playbook retries (Sol-104 V5). Not part of the
    # authored task (never rendered to YAML, ignored by pin matching).
    resolved_retry: RetryPolicy | None = None

    model_config = {"populate_by_name": True}

    @field_validator("call")
    @classmethod
    def call_must_be_registered(cls, v: str) -> str:
        if not is_known_function(v):
            raise ValueError(f"Unknown function {v!r} -- not in FUNCTION_REGISTRY")
        return v

    @model_validator(mode="after")
    def with_must_satisfy_required_params(self) -> "TaskSpec":
        if self.version is not None:
            spec = get_function_spec(self.call, self.version)
            if spec is None:
                raise ValueError(f"{self.call} has no version {self.version}")
        else:
            spec = FUNCTION_REGISTRY.get(self.call)
            if spec is None:
                raise ValueError(f"{self.call} has no active version -- pin one explicitly with `version:`")
        missing = spec.required_param_names() - self.with_.keys()
        if missing:
            raise ValueError(f"{self.call} missing required params: {sorted(missing)}")
        return self

    def task_name(self) -> str:
        return self.name or self.call
