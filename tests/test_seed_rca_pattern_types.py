from app.models import RcaPatternType
from dataloadscripts.seed_rca_pattern_types import DEFAULT_PATTERNS, seed_default_patterns
from dataloadscripts.test_fixtures import find_docs, get_doc, set_fields


def test_seeds_all_default_patterns(sync_db):
    inserted = seed_default_patterns(sync_db)
    assert inserted == len(DEFAULT_PATTERNS)

    rows = find_docs(sync_db, RcaPatternType)
    assert {r.id for r in rows} == {p["id"] for p in DEFAULT_PATTERNS}
    assert all(r.status == "active" for r in rows)
    assert all(r.created_by == "rca_pattern_type_seed" for r in rows)


def test_is_idempotent_and_never_overwrites_a_raised_ceiling(sync_db):
    seed_default_patterns(sync_db)

    # Simulate an admin having deliberately raised a pattern's ceiling.
    set_fields(sync_db, RcaPatternType, "environment_config", max_rca_status="Identified")

    second = seed_default_patterns(sync_db)
    assert second == 0

    refetched = get_doc(sync_db, RcaPatternType, "environment_config")
    assert refetched.max_rca_status == "Identified"  # not reset back to Probable
