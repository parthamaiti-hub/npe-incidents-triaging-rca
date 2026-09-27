"""Idempotently load the NPE triage catalog (teams, environments, source
systems, footprints, mapping rules, RCA playbooks) from a YAML file into
Postgres.

Usage:
    uv run python -m dataloadscripts.load_catalog --file dataloadscripts/source_systems.yaml
"""

import argparse
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.checks import has_implementation
from app.db import Base, make_engine, make_session_factory
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


def upsert_catalog(session: Session, catalog: CatalogIn) -> None:
    """Upsert every row by primary key. Safe to call repeatedly."""
    _validate_fk_integrity(catalog)

    for item in catalog.teams:
        session.merge(Team(**item.model_dump()))
    for item in catalog.environments:
        session.merge(Environment(**item.model_dump()))
    for item in catalog.source_systems:
        session.merge(SourceSystem(**item.model_dump()))
    for item in catalog.system_footprints:
        session.merge(SystemFootprint(**item.model_dump()))
    for item in catalog.incident_mapping_rules:
        session.merge(IncidentMappingRule(**item.model_dump()))
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

        session.merge(
            WorkflowDefinition(id=item.id, source_system_id=item.source_system_id, category=item.category)
        )
        session.flush()

        latest = (
            session.scalars(
                select(WorkflowDefinitionVersion)
                .where(WorkflowDefinitionVersion.workflow_definition_id == item.id)
                .order_by(WorkflowDefinitionVersion.version_number.desc())
            )
        ).first()

        if latest is not None and latest.document == tasks:
            continue  # unchanged since last load -- idempotent, no new version

        next_version_number = (latest.version_number if latest else 0) + 1
        if latest is not None and latest.status == "approved":
            latest.status = "superseded"

        session.add(
            WorkflowDefinitionVersion(
                id=f"{item.id}_v{next_version_number}",
                workflow_definition_id=item.id,
                version_number=next_version_number,
                document=tasks,
                status="approved",
                source="static_authored",
                created_by="catalog_loader",
            )
        )

    session.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=DEFAULT_CATALOG_PATH)
    args = parser.parse_args()

    catalog = load_catalog_file(args.file)

    engine = make_engine()
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)

    with session_factory() as session:
        upsert_catalog(session, catalog)

    print(
        f"Loaded {len(catalog.teams)} teams, "
        f"{len(catalog.environments)} environments, "
        f"{len(catalog.source_systems)} source systems, "
        f"{len(catalog.system_footprints)} footprints, "
        f"{len(catalog.incident_mapping_rules)} mapping rules, "
        f"{len(catalog.rca_playbooks)} playbooks from {args.file}"
    )


if __name__ == "__main__":
    main()
