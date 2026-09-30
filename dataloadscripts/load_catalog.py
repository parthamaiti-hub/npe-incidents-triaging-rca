"""Idempotently load the NPE triage catalog (teams, environments, source
systems, footprints, mapping rules, RCA playbooks) from a YAML file into
MongoDB.

With LLM_FALLBACK_ENABLED, also embeds any footprint that has no vector yet
(scripts/rebuild_vector_index.py --only footprints --missing), the corpus
LLM fallback classification retrieves from.

Usage:
    uv run python -m dataloadscripts.load_catalog --file dataloadscripts/source_systems.yaml
"""

import argparse
from pathlib import Path

import yaml
from pymongo.database import Database

from app.checks import has_implementation
from app.config import LLM_FALLBACK_ENABLED
from app.db import ensure_indexes_sync, get_database, make_sync_mongo_client
from app.function_registry import FUNCTION_REGISTRY
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
from app.workflow_spec import TaskSpec

DEFAULT_CATALOG_PATH = Path(__file__).resolve().parent / "source_systems.yaml"


def load_catalog_file(path: Path) -> CatalogIn:
    raw = yaml.safe_load(path.read_text())
    return CatalogIn.model_validate(raw)


def _validate_fk_integrity(catalog: CatalogIn) -> None:
    system_ids = {s.id for s in catalog.source_systems}
    team_ids = {t.id for t in catalog.teams}

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


def _compile_playbooks(catalog: CatalogIn) -> list[tuple]:
    """Validates and pins every playbook's tasks up front, so a bad
    playbook fails the load before anything is written."""
    compiled = []
    for item in catalog.rca_playbooks:
        tasks = []
        for step in item.steps:
            raw = step.model_dump(by_alias=True, exclude_none=True)
            try:
                task = TaskSpec.model_validate(raw)
            except Exception as exc:
                raise ValueError(f"RCA_PLAYBOOK {item.id!r} step {raw.get('call')!r}: {exc}") from exc
            # Pin the function's current version and require it actually be
            # implemented, same as app.workflow_orchestrator._validate_tasks
            # does for dynamically-built workflows -- static playbooks get
            # the same traceability and the same fail-fast guarantee.
            spec = FUNCTION_REGISTRY[task.call]
            version_number = spec.version_number or 1
            if not has_implementation(task.call, version_number):
                raise ValueError(
                    f"RCA_PLAYBOOK {item.id!r} step {task.call!r} version {version_number} has a "
                    f"published contract but no matching implementation in app.checks.STUB_RESULTS yet"
                )
            task.function_version_id = spec.version_id
            task.function_version_number = version_number
            tasks.append(task.model_dump(by_alias=True, exclude_none=True))
        compiled.append((item, tasks))
    return compiled


def upsert_catalog(db: Database, catalog: CatalogIn) -> None:
    """Upsert every document by primary key, all in one transaction. Safe
    to call repeatedly."""
    _validate_fk_integrity(catalog)
    playbooks = _compile_playbooks(catalog)

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

            if latest is not None and latest.document == tasks:
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
                created_by="catalog_loader",
            )
            db[WorkflowDefinitionVersion.COLLECTION].insert_one(version.to_doc(), session=session)

    with db.client.start_session() as session:
        session.with_transaction(load)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=DEFAULT_CATALOG_PATH)
    args = parser.parse_args()

    catalog = load_catalog_file(args.file)

    client = make_sync_mongo_client()
    try:
        db = get_database(client)
        ensure_indexes_sync(db)
        upsert_catalog(db, catalog)
    finally:
        client.close()

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
