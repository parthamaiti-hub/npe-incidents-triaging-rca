"""Referential integrity that foreign keys used to give for free. Mongo has
no FKs, so "can this row be deleted?" is answered here, explicitly. Every
reference an entity can have is listed; a delete is refused while any of
them still points at it.

Check-then-delete isn't atomic against a concurrent insert that adds a new
reference. Accepted at catalog-edit volume.
"""

from pymongo.asynchronous.database import AsyncDatabase

# referenced collection -> [(referencing collection, field)]
REFERENCES: dict[str, list[tuple[str, str]]] = {
    "team": [("source_system", "owning_team_id"), ("incident", "addressed_team_id")],
    "environment": [("incident", "environment_id")],
    "source_system": [
        ("system_footprint", "source_system_id"),
        ("incident_mapping_rule", "source_system_id"),
        ("workflow_definition", "source_system_id"),
        ("workflow_build_request", "source_system_id"),
        ("correlation_group", "source_system_id"),
        ("incident", "source_system_id"),
    ],
    "incident_mapping_rule": [("incident", "matched_rule_id")],
    "rca_pattern_type": [("rca_feedback", "corrected_pattern_id")],
}


async def references_to(db: AsyncDatabase, collection: str, id: str) -> list[str]:
    """Every "collection.field" that still references `id` -- empty means
    it's safe to delete."""
    found = []
    for referencing, field in REFERENCES.get(collection, []):
        if await db[referencing].find_one({field: id}, projection={"_id": 1}) is not None:
            found.append(f"{referencing}.{field}")
    return found
