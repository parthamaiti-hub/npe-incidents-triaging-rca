from app import classification_status


def test_resolved_maps_to_success():
    assert classification_status.display_status("resolved") == classification_status.SUCCESS


def test_any_category_pending_llm_maps_to_probable():
    assert classification_status.display_status("any_category_pending_llm") == classification_status.PROBABLE


def test_manual_triage_maps_to_not_determined():
    assert classification_status.display_status("manual_triage") == classification_status.NOT_DETERMINED


def test_unknown_status_passes_through_unchanged():
    assert classification_status.display_status("something_new") == "something_new"


def test_display_vocabulary_has_exactly_three_values():
    assert set(classification_status.DISPLAY_MAP.values()) == {
        classification_status.SUCCESS,
        classification_status.PROBABLE,
        classification_status.NOT_DETERMINED,
    }
