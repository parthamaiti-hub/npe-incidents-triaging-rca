"""Dynamic workflow build CLI -- the command-line equivalent of the UI's
"build a workflow" screen. Takes a use case (source system +
category) and an ordered list of {call, with} function requests, compiles
and validates them against app.function_registry.FUNCTION_REGISTRY, renders
the resulting document, and -- only with --approve -- makes it the active
workflow for that (source_system, category).

Usage:
    uv run python -m scripts.build_workflow \\
        --source-system SYS_HSI --category "FUNCTIONAL DEFECT (QA/UAT)" \\
        --functions functions.json --requested-by you@example.com

    uv run python -m scripts.build_workflow ... --approve

functions.json is a JSON array of {"call": "<check_type>", "with": {...}}.
"""

import argparse
import asyncio
import json
from pathlib import Path

from app.db import make_async_engine, make_async_session_factory
from app.workflow_orchestrator import WorkflowValidationError, approve_build_request, build_workflow_from_request


async def run(
    source_system_id: str,
    category: str,
    functions_path: Path,
    requested_by: str,
    use_case_description: str | None,
    do_approve: bool,
) -> None:
    requested_functions = json.loads(functions_path.read_text())

    engine = make_async_engine()
    session_factory = make_async_session_factory(engine)
    try:
        async with session_factory() as session:
            request = await build_workflow_from_request(
                session,
                source_system_id,
                category,
                requested_functions,
                requested_by,
                use_case_description,
            )
            print(f"Build request {request.id} rendered for ({source_system_id}, {category}):")
            print(json.dumps(request.generated_document, indent=2))

            if do_approve:
                version = await approve_build_request(session, request.id, approved_by=requested_by)
                print(
                    f"\nApproved as WorkflowDefinitionVersion {version.id} "
                    f"(version_number={version.version_number}) -- now the active workflow "
                    f"for ({source_system_id}, {category})."
                )
            else:
                print(f"\nNot approved yet. Approve with: --approve (build_request_id={request.id})")
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-system", required=True, help="e.g. SYS_HSI")
    parser.add_argument("--category", required=True, help='e.g. "FUNCTIONAL DEFECT (QA/UAT)"')
    parser.add_argument("--functions", required=True, type=Path, help="JSON file: [{call, with}, ...]")
    parser.add_argument("--requested-by", required=True, help="Requester identity, e.g. an email address")
    parser.add_argument("--use-case-description", default=None)
    parser.add_argument("--approve", action="store_true", help="Immediately approve the rendered document.")
    args = parser.parse_args()

    try:
        asyncio.run(
            run(
                args.source_system,
                args.category,
                args.functions,
                args.requested_by,
                args.use_case_description,
                args.approve,
            )
        )
    except WorkflowValidationError as exc:
        raise SystemExit(f"Rejected: {exc}")


if __name__ == "__main__":
    main()
