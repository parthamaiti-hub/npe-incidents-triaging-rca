"""Creates/updates every source system's DEFAULT RCA playbook from a
template (dataloadscripts/default_rca_playbook.yaml).

The DEFAULT playbook is a system's fallback: when an incident's source
system is known but its category couldn't be mapped by a mapping rule, or
the mapped (system, category) has no playbook, the worker/retry runs the
system's DEFAULT playbook and marks the run triage_mode="DefaultRCA". The
operator fixes the mapping rule (or adds the missing playbook) and Retries;
retry re-classifies, so the specific playbook then runs.

For each source system in the database (or only --system ...), this writes a
playbook `RCA_<CODE>_DEFAULT` with category DEFAULT, through the same path
as a catalog load (load_catalog.upsert_catalog): catalog check first,
check-version pinning, a new playbook version only when the steps changed,
idempotent.

A system whose DEFAULT playbook was customized -- its latest version was
written by anyone but this loader (a UI edit, a catalog YAML) -- is left
alone and reported as skipped; --force overwrites it with the template.

Run after the catalogs are loaded (scripts/start.ps1 does), and again
whenever a source system is added:

    uv run python -m dataloadscripts.load_default_playbooks
    uv run python -m dataloadscripts.load_default_playbooks --system SYS_HSI SYS_TPAS
    uv run python -m dataloadscripts.load_default_playbooks --check-only
    uv run python -m dataloadscripts.load_default_playbooks --template my_template.yaml --force
"""

import argparse
import re
import sys
from pathlib import Path

import yaml
from pymongo.database import Database

from app.categories import DEFAULT_CATEGORY
from app.db import ensure_indexes_sync, get_database, make_sync_mongo_client
from app.models import SourceSystem, WorkflowDefinition, WorkflowDefinitionVersion
from app.schemas import CatalogIn, RcaPlaybookIn
from app.task_pinning import ALL_CALLS
from dataloadscripts.check_catalog import CatalogCheckError, Finding, print_report
from dataloadscripts.load_catalog import check_catalog_report, upsert_catalog

DEFAULT_TEMPLATE_PATH = Path(__file__).resolve().parent / "default_rca_playbook.yaml"
DEFAULT_LOADER = "default_playbook_loader"  # created_by on the versions this script writes


def default_playbook_id(system: SourceSystem) -> str:
    code = re.sub(r"[^A-Z0-9]+", "_", system.code.upper()).strip("_") or system.id
    return f"RCA_{code}_DEFAULT"


def load_template(path: Path = DEFAULT_TEMPLATE_PATH) -> list[dict]:
    data = yaml.safe_load(path.read_text()) or {}
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError(f"{path}: needs a non-empty 'steps' list")
    return steps


def _fill(value, placeholders: dict[str, str]):
    """Replaces {code}/{system_id}/{name} in every string, recursively.
    Plain replacement, not str.format, so other braces stay literal."""
    if isinstance(value, str):
        for key, replacement in placeholders.items():
            value = value.replace("{" + key + "}", replacement)
        return value
    if isinstance(value, list):
        return [_fill(v, placeholders) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, placeholders) for k, v in value.items()}
    return value


def plan_default_playbooks(
    db: Database, template_steps: list[dict], system_ids: list[str] | None = None, force: bool = False
) -> tuple[list[RcaPlaybookIn], list[tuple[str, str]]]:
    """(playbooks to upsert, [(system_id, reason skipped)])."""
    filter = {"_id": {"$in": system_ids}} if system_ids else {}
    systems = [SourceSystem.from_doc(d) for d in db[SourceSystem.COLLECTION].find(filter).sort("_id", 1)]
    if system_ids:
        unknown = sorted(set(system_ids) - {s.id for s in systems})
        if unknown:
            raise ValueError(f"unknown source system(s): {unknown}")

    playbooks: list[RcaPlaybookIn] = []
    skipped: list[tuple[str, str]] = []
    for system in systems:
        playbook_id = default_playbook_id(system)
        existing = db[WorkflowDefinition.COLLECTION].find_one({"source_system_id": system.id, "category": DEFAULT_CATEGORY})
        if existing is not None:
            latest = db[WorkflowDefinitionVersion.COLLECTION].find_one(
                {"workflow_definition_id": existing["_id"]}, sort=[("version_number", -1)]
            )
            owner = latest["created_by"] if latest is not None else None
            if owner not in (None, DEFAULT_LOADER) and not force:
                skipped.append((system.id, f"DEFAULT playbook {existing['_id']} was customized (latest version by {owner}) -- use --force to overwrite"))
                continue
            playbook_id = existing["_id"]  # keep whatever id the pair already has

        placeholders = {"code": system.code, "system_id": system.id, "name": system.name}
        playbooks.append(
            RcaPlaybookIn.model_validate(
                {
                    "id": playbook_id,
                    "source_system_id": system.id,
                    "category": DEFAULT_CATEGORY,
                    "steps": _fill(template_steps, placeholders),
                }
            )
        )
    return playbooks, skipped


def _catalog(playbooks: list[RcaPlaybookIn]) -> CatalogIn:
    return CatalogIn(source_systems=[], system_footprints=[], incident_mapping_rules=[], rca_playbooks=playbooks)


def _version_counts(db: Database, ids: list[str]) -> dict[str, int]:
    return {i: db[WorkflowDefinitionVersion.COLLECTION].count_documents({"workflow_definition_id": i}) for i in ids}


def load_default_playbooks(
    db: Database,
    template_path: Path = DEFAULT_TEMPLATE_PATH,
    system_ids: list[str] | None = None,
    force: bool = False,
    upgrade_calls: set[str] | None = None,
    strict: bool = False,
) -> dict:
    """Upserts the DEFAULT playbooks. Returns {created, updated, unchanged,
    skipped, findings}. Raises CatalogCheckError (nothing written) if the
    catalog check finds errors."""
    playbooks, skipped = plan_default_playbooks(db, load_template(template_path), system_ids, force)
    ids = [p.id for p in playbooks]
    before = _version_counts(db, ids)
    findings: list[Finding] = []
    if playbooks:
        findings = upsert_catalog(db, _catalog(playbooks), upgrade_calls, strict, created_by=DEFAULT_LOADER)
    after = _version_counts(db, ids)
    return {
        "created": [i for i in ids if before[i] == 0 and after[i] > 0],
        "updated": [i for i in ids if 0 < before[i] < after[i]],
        "unchanged": [i for i in ids if before[i] == after[i] > 0],
        "skipped": skipped,
        "findings": findings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE_PATH)
    parser.add_argument("--system", nargs="+", metavar="SYSTEM_ID", help="only these source systems (default: all)")
    parser.add_argument("--force", action="store_true", help="overwrite customized DEFAULT playbooks with the template")
    parser.add_argument("--check-only", action="store_true", help="run the catalog check on the planned playbooks; write nothing")
    parser.add_argument("--strict", action="store_true", help="treat catalog-check warnings as errors")
    parser.add_argument("--upgrade-functions", nargs="*", metavar="CALL", default=None,
                        help="re-pin steps to the active check version (all steps, or only these calls)")
    args = parser.parse_args()
    upgrade_calls = None
    if args.upgrade_functions is not None:
        upgrade_calls = set(args.upgrade_functions) or {ALL_CALLS}

    client = make_sync_mongo_client()
    try:
        db = get_database(client)
        ensure_indexes_sync(db)
        if args.check_only:
            playbooks, skipped = plan_default_playbooks(db, load_template(args.template), args.system, args.force)
            for system_id, reason in skipped:
                print(f"SKIP    {system_id}: {reason}")
            print(f"Planned {len(playbooks)} DEFAULT playbook(s)")
            sys.exit(check_catalog_report(_catalog(playbooks), "DEFAULT playbooks", strict=args.strict, upgrade_calls=upgrade_calls))
        try:
            report = load_default_playbooks(db, args.template, args.system, args.force, upgrade_calls, args.strict)
        except CatalogCheckError as exc:
            print_report(exc.findings, "DEFAULT playbooks", args.strict)
            print("Nothing loaded -- fix the errors above.")
            sys.exit(1)
    finally:
        client.close()

    if report["findings"]:
        print_report(report["findings"], "DEFAULT playbooks")
    for system_id, reason in report["skipped"]:
        print(f"SKIP    {system_id}: {reason}")
    print(
        f"DEFAULT playbooks: {len(report['created'])} created, {len(report['updated'])} updated, "
        f"{len(report['unchanged'])} unchanged, {len(report['skipped'])} skipped"
    )


if __name__ == "__main__":
    main()
