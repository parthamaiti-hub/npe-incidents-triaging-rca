"""Idempotently load the NPE triage catalog (teams, environments, source
systems, footprints, mapping rules, RCA playbooks) from a YAML file into
MongoDB.

With LLM_FALLBACK_ENABLED, also embeds any footprint that has no vector yet
(scripts/rebuild_vector_index.py --only footprints --missing), the corpus
LLM fallback classification retrieves from.

Playbook check versions (Sol-104): a step may pin `version: N`. A step
without one keeps the pin it had in the previously loaded version of the
playbook when the step is unchanged, else gets the function's active
version. Publishing a new check version therefore never creates new
playbook versions on reload; moving playbooks to it is explicit -- edit the
step (or add `version:`), or pass --upgrade-functions.

Catalog check: before anything is written, the catalog is checked
(dataloadscripts/check_catalog.py) against itself and what is already in
the database. Errors abort the load with nothing written; warnings are
printed and the load continues (--strict: warnings abort too).

Usage:
    uv run python -m dataloadscripts.load_catalog --file dataloadscripts/source_systems.yaml
    uv run python -m dataloadscripts.load_catalog --file ... --check-only                      # report, write nothing
    uv run python -m dataloadscripts.load_catalog --file ... --strict                          # warnings block too
    uv run python -m dataloadscripts.load_catalog --file ... --upgrade-functions               # every step -> active
    uv run python -m dataloadscripts.load_catalog --file ... --upgrade-functions apm_traces    # only these calls
"""

import argparse
import sys
from pathlib import Path

import pydantic
import yaml
from pymongo.database import Database

from app.config import LLM_FALLBACK_ENABLED
from app.db import ensure_indexes_sync, get_database, make_sync_mongo_client
from app.function_registry import refresh_function_registry_from_db_sync
from app.models import (
    Environment,
    IncidentMappingRule,
    SourceSystem,
    SystemFootprint,
    Team,
    WorkflowDefinition,
    WorkflowDefinitionVersion,
)
from app.schemas import CatalogIn
from app.task_pinning import ALL_CALLS, WorkflowValidationError, require_approvable, resolve_tasks
from dataloadscripts.check_catalog import ERROR, CatalogCheckError, Finding, blocking, check_catalog, print_report

DEFAULT_CATALOG_PATH = Path(__file__).resolve().parent / "source_systems.yaml"

CATALOG_LOADER = "catalog_loader"  # created_by on playbook versions this script writes


def load_catalog_file(path: Path) -> CatalogIn:
    raw = yaml.safe_load(path.read_text())
    return CatalogIn.model_validate(raw)


def _validate_fk_integrity(catalog: CatalogIn, db: Database | None = None) -> None:
    """References must resolve to rows in this catalog or, given `db`, rows
    already loaded -- several catalog files share one database, and a
    playbooks-only load (load_default_playbooks) names systems loaded by
    another file."""
    system_ids = {s.id for s in catalog.source_systems}
    team_ids = {t.id for t in catalog.teams}
    if db is not None:
        system_ids |= set(db[SourceSystem.COLLECTION].distinct("_id"))
        team_ids |= set(db[Team.COLLECTION].distinct("_id"))

    for system in catalog.source_systems:
        if system.owning_team_id is not None and system.owning_team_id not in team_ids:
            raise ValueError(
                f"SOURCE_SYSTEM {system.id!r} references unknown owning_team_id {system.owning_team_id!r}"
            )
    for footprint in catalog.system_footprints:
        if footprint.source_system_id not in system_ids:
            raise ValueError(
                f"SYSTEM_FOOTPRINT {footprint.id!r} references unknown "
                f"source_system_id {footprint.source_system_id!r}"
            )
    for rule in catalog.incident_mapping_rules:
        if rule.source_system_id not in system_ids:
            raise ValueError(
                f"INCIDENT_MAPPING_RULE {rule.id!r} references unknown "
                f"source_system_id {rule.source_system_id!r}"
            )
    for playbook in catalog.rca_playbooks:
        if playbook.source_system_id not in system_ids:
            raise ValueError(
                f"RCA_PLAYBOOK {playbook.id!r} references unknown "
                f"source_system_id {playbook.source_system_id!r}"
            )


def _merge(db: Database, obj, session) -> None:
    """Upsert by primary key -- what session.merge() used to do."""
    doc = obj.to_doc()
    db[obj.COLLECTION].replace_one({"_id": doc["_id"]}, doc, upsert=True, session=session)


def _latest_documents(db: Database, catalog: CatalogIn) -> dict[str, list[dict]]:
    latest = {}
    for item in catalog.rca_playbooks:
        doc = db[WorkflowDefinitionVersion.COLLECTION].find_one(
            {"workflow_definition_id": item.id}, sort=[("version_number", -1)]
        )
        if doc is not None:
            latest[item.id] = doc["document"]
    return latest


def _compile_playbooks(
    catalog: CatalogIn,
    previous_documents: dict[str, list[dict]] | None = None,
    upgrade_calls: set[str] | None = None,
) -> list[tuple]:
    """Validates and pins every playbook's steps up front (same resolver
    and rules as UI-built playbooks, app.task_pinning), so a bad playbook
    fails the load before anything is written. A loaded version is approved
    directly, so it also must not pin a draft/retired check version."""
    previous_documents = previous_documents or {}
    compiled = []
    for item in catalog.rca_playbooks:
        steps = [step.model_dump(by_alias=True, exclude_none=True) for step in item.steps]
        try:
            tasks = resolve_tasks(steps, preserve_pins_from=previous_documents.get(item.id), upgrade_calls=upgrade_calls)
            require_approvable(tasks)
        except WorkflowValidationError as exc:
            details = "; ".join(f"step {e['task_index']} ({steps[e['task_index']].get('call')!r}): {e['message']}" for e in exc.errors)
            raise ValueError(f"RCA_PLAYBOOK {item.id!r}: {details or exc}") from exc
        compiled.append((item, tasks))
    return compiled


def _semantic(document: list[dict]) -> list[dict]:
    """A document as far as behaviour goes: function_version_id and the
    resolved_retry snapshot are bookkeeping, so a reload that only adds them
    to an otherwise identical document is not a new playbook version."""
    return [{k: v for k, v in task.items() if k not in ("function_version_id", "resolved_retry")} for task in document]


def upsert_catalog(
    db: Database,
    catalog: CatalogIn,
    upgrade_calls: set[str] | None = None,
    strict: bool = False,
    created_by: str = CATALOG_LOADER,
) -> list[Finding]:
    """Upsert every document by primary key, all in one transaction. Safe
    to call repeatedly. upgrade_calls: re-pin steps of these calls (or
    ALL_CALLS) to the active check version instead of keeping their pins.
    created_by: recorded on new playbook versions (who owns them).

    Runs the catalog check first: raises CatalogCheckError (nothing written)
    on any error -- or any finding at all with strict=True. Returns the
    non-blocking findings (warnings) for the caller to report."""
    _validate_fk_integrity(catalog, db)
    findings = check_catalog(catalog, db)
    if blocking(findings, strict):
        raise CatalogCheckError(blocking(findings, strict))
    # This script runs in its own process: load the function versions the
    # database actually has, or pins would come from the built-in defaults.
    refresh_function_registry_from_db_sync(db)
    playbooks = _compile_playbooks(catalog, _latest_documents(db, catalog), upgrade_calls)

    def load(session) -> None:
        for item in catalog.teams:
            _merge(db, Team(**item.model_dump()), session)
        for item in catalog.environments:
            _merge(db, Environment(**item.model_dump()), session)
        for item in catalog.source_systems:
            _merge(db, SourceSystem(**item.model_dump()), session)
        for item in catalog.system_footprints:
            _merge(db, SystemFootprint(**item.model_dump()), session)
        for item in catalog.incident_mapping_rules:
            _merge(db, IncidentMappingRule(**item.model_dump()), session)
        for item, tasks in playbooks:
            _merge(db, WorkflowDefinition(id=item.id, source_system_id=item.source_system_id, category=item.category), session)

            latest_doc = db[WorkflowDefinitionVersion.COLLECTION].find_one(
                {"workflow_definition_id": item.id}, sort=[("version_number", -1)], session=session
            )
            latest = WorkflowDefinitionVersion.from_doc(latest_doc) if latest_doc is not None else None

            if latest is not None and _semantic(latest.document) == _semantic(tasks):
                continue  # unchanged since last load -- idempotent, no new version

            next_version_number = (latest.version_number if latest else 0) + 1
            # Supersede whatever is approved before inserting the new
            # approved version (one approved per definition, index-enforced).
            db[WorkflowDefinitionVersion.COLLECTION].update_many(
                {"workflow_definition_id": item.id, "status": "approved"},
                {"$set": {"status": "superseded"}},
                session=session,
            )
            version = WorkflowDefinitionVersion(
                id=f"{item.id}_v{next_version_number}",
                workflow_definition_id=item.id,
                version_number=next_version_number,
                document=tasks,
                status="approved",
                source="static_authored",
                created_by=created_by,
            )
            db[WorkflowDefinitionVersion.COLLECTION].insert_one(version.to_doc(), session=session)

    with db.client.start_session() as session:
        session.with_transaction(load)
    return findings


def check_only(path: Path, use_db: bool = True, strict: bool = False, upgrade_calls: set[str] | None = None) -> int:
    """Everything a load would refuse or warn about, written to stdout,
    nothing written to the database. Returns the process exit code: 1 if
    the load would be blocked, else 0."""
    try:
        catalog = load_catalog_file(path)
    except (pydantic.ValidationError, yaml.YAMLError) as exc:
        print_report([Finding(ERROR, "invalid_yaml", str(exc).replace("\n", " "))], str(path))
        return 1
    return check_catalog_report(catalog, str(path), use_db=use_db, strict=strict, upgrade_calls=upgrade_calls)


def check_catalog_report(
    catalog: CatalogIn,
    source: str,
    use_db: bool = True,
    strict: bool = False,
    upgrade_calls: set[str] | None = None,
) -> int:
    """check_only for an already-parsed catalog (also used by
    load_default_playbooks --check-only)."""
    findings: list[Finding] = []
    client = make_sync_mongo_client() if use_db else None
    try:
        db = get_database(client) if client is not None else None
        try:
            _validate_fk_integrity(catalog, db)
        except ValueError as exc:
            findings.append(Finding(ERROR, "unknown_reference", str(exc)))
        findings.extend(check_catalog(catalog, db))
        # The same playbook compilation the load does (params, pins, code).
        if db is not None:
            refresh_function_registry_from_db_sync(db)
        try:
            _compile_playbooks(catalog, _latest_documents(db, catalog) if db is not None else {}, upgrade_calls)
        except ValueError as exc:
            findings.append(Finding(ERROR, "playbook_invalid", str(exc)))
    finally:
        if client is not None:
            client.close()

    findings.sort(key=lambda f: f.severity != ERROR)
    print_report(findings, source, strict)
    return 1 if blocking(findings, strict) else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=DEFAULT_CATALOG_PATH)
    parser.add_argument("--check-only", action="store_true", help="run the catalog check and exit; write nothing")
    parser.add_argument("--strict", action="store_true", help="treat catalog-check warnings as errors")
    parser.add_argument(
        "--upgrade-functions",
        nargs="*",
        metavar="CALL",
        default=None,
        help="re-pin playbook steps to the active check version: every step if no CALL is given, "
        "else only steps calling these check_types",
    )
    args = parser.parse_args()

    upgrade_calls = None
    if args.upgrade_functions is not None:
        upgrade_calls = set(args.upgrade_functions) or {ALL_CALLS}
    if args.check_only:
        sys.exit(check_only(args.file, strict=args.strict, upgrade_calls=upgrade_calls))

    catalog = load_catalog_file(args.file)
    client = make_sync_mongo_client()
    try:
        db = get_database(client)
        ensure_indexes_sync(db)
        try:
            findings = upsert_catalog(db, catalog, upgrade_calls, strict=args.strict)
        except CatalogCheckError as exc:
            print_report(exc.findings, str(args.file), args.strict)
            print(f"Nothing loaded from {args.file} -- fix the errors above, or check with --check-only.")
            sys.exit(1)
    finally:
        client.close()

    if findings:
        print_report(findings, str(args.file))
    print(
        f"Loaded {len(catalog.teams)} teams, "
        f"{len(catalog.environments)} environments, "
        f"{len(catalog.source_systems)} source systems, "
        f"{len(catalog.system_footprints)} footprints, "
        f"{len(catalog.incident_mapping_rules)} mapping rules, "
        f"{len(catalog.rca_playbooks)} playbooks from {args.file}"
    )

    if LLM_FALLBACK_ENABLED:
        from scripts.rebuild_vector_index import rebuild_sync

        print(rebuild_sync(only=["footprints"], missing=True))


if __name__ == "__main__":
    main()
