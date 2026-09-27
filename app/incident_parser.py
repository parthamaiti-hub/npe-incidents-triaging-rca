"""Parses the free-text DSG-Triage-NPE incident template and
extracts typed signal candidates (urls, hostnames, table names, pipeline
names, jira keys, keywords) for classification.
"""

import re
from urllib.parse import urlparse

TEMPLATE_FIELD_MAP = {
    "environment": "environment",
    "category / sub-type": "category_subtype_raw",
    "impact": "impact",
    "start time (approx.)": "start_time",
    "start time": "start_time",
    "current status": "current_status",
    "owner": "owner",
    "reference tickets / links": "reference_tickets",
    "initial suspected root cause (if known)": "initial_root_cause",
    "initial suspected root cause": "initial_root_cause",
    "next action / eta": "next_action_eta",
}

LINE_RE = re.compile(r"^\s*([A-Za-z][A-Za-z /()]*?)\s*:\s*(.*)$")
HTML_TAG_RE = re.compile(r"<[^>]+>")

URL_RE = re.compile(r"https?://[^\s)\]]+")
TABLE_NAME_RE = re.compile(r"\bDSNADEV\.[A-Za-z0-9_]+\b")
PIPELINE_NAME_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*[_-]npe[_-][A-Za-z0-9_-]+\b", re.IGNORECASE)
JIRA_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")

# Real incident text often mentions hostnames bare (e.g. Splunk log fields
# like "host: api.qlab03.fiber.t-mobile.com"), not only wrapped in a URL.
BARE_HOSTNAME_RE = re.compile(r"\b(?:[a-zA-Z0-9-]+\.)+t-mobile\.com\b", re.IGNORECASE)

# Structured, high-confidence fields embedded in pasted log
# lines -- much stronger evidence than a keyword substring hit.
APPLICATION_ID_RE = re.compile(r"APPLICATIONID=([A-Za-z0-9_]+)", re.IGNORECASE)
ERROR_SYSTEM_RE = re.compile(r'errorSystem"?\s*[:=]\s*"?([A-Za-z0-9_]+)', re.IGNORECASE)
TARGET_SYSTEM_RE = re.compile(r"TARGET_SYSTEM=([A-Za-z0-9_]+)", re.IGNORECASE)


def strip_html(html_body: str) -> str:
    """Both Teams message bodies and Jira's renderedFields.description are
    HTML -- strip tags before treating either as plain text."""
    return re.sub(r"\s+", " ", HTML_TAG_RE.sub(" ", html_body)).strip()


def parse_template(raw_text: str) -> dict[str, str | None]:
    """Best-effort line-based parser for the incident template. Tolerates the
    markdown-bold ("**Subject:**") and same-line/next-line subject variants
    that show up across real sample incidents."""
    lines = [line.replace("**", "") for line in raw_text.splitlines()]

    fields: dict[str, str | None] = {name: None for name in set(TEMPLATE_FIELD_MAP.values())}
    subject: str | None = None
    subject_line_idx: int | None = None

    for i, line in enumerate(lines):
        match = LINE_RE.match(line)
        if match and match.group(1).strip().lower() == "subject":
            subject_line_idx = i
            value = match.group(2).strip()
            if value:
                subject = value
            else:
                for later_line in lines[i + 1 :]:
                    if later_line.strip():
                        subject = later_line.strip()
                        break
            break

    if subject is None:
        for line in lines:
            if line.strip():
                subject = line.strip()
                break

    start_idx = 0 if subject_line_idx is None else subject_line_idx + 1
    for line in lines[start_idx:]:
        match = LINE_RE.match(line)
        if not match:
            continue
        label = match.group(1).strip().lower()
        if label in TEMPLATE_FIELD_MAP:
            fields[TEMPLATE_FIELD_MAP[label]] = match.group(2).strip()

    fields["subject"] = subject
    return fields


def extract_signals(raw_text: str) -> list[dict[str, str]]:
    """Returns typed signal candidates: [{"signal_type": ..., "value": ...}].

    url/hostname/table_name/pipeline_name/jira_key are discrete substrings
    pulled out of the text. "keyword" uses the whole raw text as its single
    candidate, since keyword mapping-rule patterns (e.g. \\bDCD\\b) are
    already full-text search patterns.
    """
    signals: list[dict[str, str]] = []

    for match in URL_RE.finditer(raw_text):
        url = match.group(0)
        signals.append({"signal_type": "url", "value": url})
        host = urlparse(url).netloc
        if host:
            signals.append({"signal_type": "hostname", "value": host})

    for match in BARE_HOSTNAME_RE.finditer(raw_text):
        signals.append({"signal_type": "hostname", "value": match.group(0)})

    for match in TABLE_NAME_RE.finditer(raw_text):
        signals.append({"signal_type": "table_name", "value": match.group(0)})

    for match in PIPELINE_NAME_RE.finditer(raw_text):
        signals.append({"signal_type": "pipeline_name", "value": match.group(0)})

    for match in JIRA_KEY_RE.finditer(raw_text):
        signals.append({"signal_type": "jira_key", "value": match.group(0)})

    for match in APPLICATION_ID_RE.finditer(raw_text):
        signals.append({"signal_type": "application_id", "value": match.group(1)})

    for pattern in (ERROR_SYSTEM_RE, TARGET_SYSTEM_RE):
        for match in pattern.finditer(raw_text):
            signals.append({"signal_type": "error_system", "value": match.group(1)})

    for segment in extract_title_segments(raw_text):
        signals.append({"signal_type": "title_segment", "value": segment})

    signals.append({"signal_type": "keyword", "value": raw_text})

    return signals


def extract_title_segments(raw_text: str) -> list[str]:
    """Real Jira titles are pipe-delimited; each
    segment is often itself an environment code or a system/initiative name."""
    title = parse_template(raw_text)["subject"] or ""
    return [segment.strip() for segment in title.split("|") if segment.strip()]


def extract_application_id_hint(raw_text: str) -> str | None:
    match = APPLICATION_ID_RE.search(raw_text)
    return match.group(1) if match else None


def extract_error_system_hint(raw_text: str) -> str | None:
    for pattern in (ERROR_SYSTEM_RE, TARGET_SYSTEM_RE):
        match = pattern.search(raw_text)
        if match:
            return match.group(1)
    return None
