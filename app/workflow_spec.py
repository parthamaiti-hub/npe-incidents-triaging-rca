"""Pydantic model for a single workflow task -- the restricted CNCF
Serverless Workflow DSL subset this project implements: sequential `call`
tasks with `with` params and an optional `retry` override. No `fork`,
`switch`, `emit`/`listen`, or external `http`/`openapi`/`grpc` call types --
none are needed for evidence-gathering RCA playbooks.

Used both to validate statically-authored playbook steps at catalog-load
time (dataloadscripts/load_catalog.py) and to validate a dynamically-built
workflow's requested functions at build time (app/workflow_orchestrator.py)
-- one set of rules for both paths.
"""

from pydantic import BaseModel, Field, field_validator, model_validator

from app.function_registry import FUNCTION_REGISTRY, RetryPolicy


class TaskSpec(BaseModel):
    name: str | None = None
    call: str
    with_: dict = Field(default_factory=dict, alias="with")
    retry: RetryPolicy | None = None
    # Pins exactly which FunctionDefinitionVersion this task was validated
    # against -- stamped by app.workflow_orchestrator.build_workflow_from_request
    # (the function's *current* active version at build time), not
    # something a client sets. None if that function has no DB-backed
    # version yet (still only in the built-in DEFAULT_FUNCTION_REGISTRY).
    function_version_id: str | None = None
    # The same version, as the int app.checks.run_check actually dispatches
    # on -- version_id is an opaque DB row reference (for audit/lookup),
    # this is what selects which STUB_RESULTS[call][N] entry executes.
    # Two versions of the same function are two different pieces of code:
    # a version with no matching STUB_RESULTS entry fails at run time
    # (app.checks.run_check) rather than silently reusing another
    # version's behavior.
    function_version_number: int | None = None

    model_config = {"populate_by_name": True}

    @field_validator("call")
    @classmethod
    def call_must_be_registered(cls, v: str) -> str:
        if v not in FUNCTION_REGISTRY:
            raise ValueError(f"Unknown function {v!r} -- not in FUNCTION_REGISTRY")
        return v

    @model_validator(mode="after")
    def with_must_satisfy_required_params(self) -> "TaskSpec":
        spec = FUNCTION_REGISTRY[self.call]
        missing = spec.required_param_names() - self.with_.keys()
        if missing:
            raise ValueError(f"{self.call} missing required params: {sorted(missing)}")
        return self

    def task_name(self) -> str:
        return self.name or self.call
