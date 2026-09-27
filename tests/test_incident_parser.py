from app.incident_parser import (
    extract_application_id_hint,
    extract_error_system_hint,
    extract_signals,
    extract_title_segments,
    parse_template,
)
from dataloadscripts.test_fixtures import (
    ARCHITECTURE_DOC_SAMPLE,
    E2E_SAMPLE,
    ERROR_SYSTEM_SAMPLE,
    LOG_EXCERPT_SAMPLE,
    PIPE_DELIMITED_TITLE_SAMPLE,
    SPLIT_LABEL_SAMPLE,
    TARGET_SYSTEM_SAMPLE,
    WORKED_TRACE_SAMPLE,
)


def test_parse_template_same_line_bold_subject():
    fields = parse_template(E2E_SAMPLE)
    assert fields["subject"] == "DATA QUALITY / TEST DATA – DATA – NULLS IN UAT DATA – Billing UAT failing"
    assert fields["environment"] == "NPE"
    assert fields["owner"] == "DSG-Data Squad"
    assert fields["reference_tickets"] == "JIRA FDM-1234"


def test_parse_template_no_subject_label_uses_first_line():
    fields = parse_template(ARCHITECTURE_DOC_SAMPLE)
    assert fields["subject"] == "DATA QUALITY / TEST DATA – DATA – NULLS IN UAT DATA – Billing UAT failing"
    assert fields["environment"] == "NPE"
    assert fields["next_action_eta"].startswith("Data engineer reviewing")


def test_parse_template_label_and_value_on_separate_lines():
    fields = parse_template(SPLIT_LABEL_SAMPLE)
    assert fields["subject"] == "ENVIRONMENT / CONFIG – ENV – NPE POINTS TO PROD – Badge emails sent from NPE"
    assert fields["environment"] == "NPE"


def test_extract_signals_finds_jira_key():
    # The generic PROJECT-NUMBER pattern also catches "TC-BILL-012" (a test
    # case ID, not a Jira key) -- expected, since no current mapping rule
    # uses signal_type=jira_key so this false positive is harmless today.
    signals = extract_signals(E2E_SAMPLE)
    jira_keys = [s["value"] for s in signals if s["signal_type"] == "jira_key"]
    assert "FDM-1234" in jira_keys


def test_extract_signals_finds_table_name_in_worked_trace():
    signals = extract_signals(WORKED_TRACE_SAMPLE)
    table_names = [s["value"] for s in signals if s["signal_type"] == "table_name"]
    assert table_names == ["DSNADEV.dcd_billing_summary"]


def test_extract_signals_no_table_name_in_original_sample():
    signals = extract_signals(E2E_SAMPLE)
    table_names = [s["value"] for s in signals if s["signal_type"] == "table_name"]
    assert table_names == []


def test_extract_signals_url_and_hostname():
    signals = extract_signals("Check https://dcd-npe.t-mobile.com/health for status")
    assert {"signal_type": "url", "value": "https://dcd-npe.t-mobile.com/health"} in signals
    assert {"signal_type": "hostname", "value": "dcd-npe.t-mobile.com"} in signals


def test_extract_signals_finds_bare_hostname_without_url_scheme():
    # Real incident text (Splunk log fields) often mentions a hostname with
    # no http:// wrapper at all.
    signals = extract_signals("host: api.qlab03.fiber.t-mobile.com")
    hostnames = [s["value"] for s in signals if s["signal_type"] == "hostname"]
    assert "api.qlab03.fiber.t-mobile.com" in hostnames


def test_extract_signals_always_includes_full_text_as_keyword():
    signals = extract_signals("mentions DCD somewhere")
    keyword_signals = [s for s in signals if s["signal_type"] == "keyword"]
    assert keyword_signals == [{"signal_type": "keyword", "value": "mentions DCD somewhere"}]


def test_extract_application_id_hint_from_log_excerpt():
    assert extract_application_id_hint(LOG_EXCERPT_SAMPLE) == "TAPESTRY"
    assert extract_application_id_hint(ERROR_SYSTEM_SAMPLE) is None


def test_extract_error_system_hint_from_error_system_field():
    assert extract_error_system_hint(ERROR_SYSTEM_SAMPLE) == "RSP"


def test_extract_error_system_hint_from_target_system_field():
    assert extract_error_system_hint(TARGET_SYSTEM_SAMPLE) == "SAPPIREST"


def test_extract_signals_include_application_id_and_error_system():
    signals = extract_signals(LOG_EXCERPT_SAMPLE)
    assert {"signal_type": "application_id", "value": "TAPESTRY"} in signals

    signals = extract_signals(ERROR_SYSTEM_SAMPLE)
    assert {"signal_type": "error_system", "value": "RSP"} in signals


def test_extract_title_segments_splits_pipe_delimited_subject():
    segments = extract_title_segments(PIPE_DELIMITED_TITLE_SAMPLE)
    assert segments == ["TFB QE", "QLAB03", "CIT-16", "Ecomm", "NBYOD EIP", "RSP", "Activation is Pending post Fulfillment"]


def test_extract_signals_include_title_segments():
    signals = extract_signals(PIPE_DELIMITED_TITLE_SAMPLE)
    title_segments = [s["value"] for s in signals if s["signal_type"] == "title_segment"]
    assert "QLAB03" in title_segments
    assert "RSP" in title_segments
