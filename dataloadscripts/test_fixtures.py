"""Test-only fixture data and DB-seeding helpers.

Lives here (not in tests/) so tests/ contains only test logic and can be
pushed to GitHub without carrying data-loading code or catalog paths -- it
only calls into this module.
"""

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from pymongo.asynchronous.database import AsyncDatabase
from pymongo.database import Database

from app.db import ensure_indexes_sync, make_mongo_client, make_sync_mongo_client
from app.models import Document, Incident
from app.repositories.incidents import insert_incident_sync
from dataloadscripts.load_catalog import DEFAULT_CATALOG_PATH, load_catalog_file, upsert_catalog


def seed_catalog_db(db: Database, catalog_path: Path = DEFAULT_CATALOG_PATH) -> Database:
    """Creates the indexes (if needed) and upserts the catalog at
    catalog_path into db. The caller owns the database's teardown."""
    ensure_indexes_sync(db)
    upsert_catalog(db, load_catalog_file(catalog_path))
    return db


@contextmanager
def seeded_catalog_database(mongo_url: str, catalog_path: Path = DEFAULT_CATALOG_PATH) -> Iterator[str]:
    """A throwaway database with the catalog loaded, for module-scoped
    fixtures shared by several tests; yields its name, drops it afterwards."""
    client = make_sync_mongo_client(mongo_url)
    name = f"t_{uuid.uuid4().hex[:16]}"
    try:
        seed_catalog_db(client[name], catalog_path)
        yield name
    finally:
        client.drop_database(name)
        client.close()


@asynccontextmanager
async def open_db(mongo_url: str, db_name: str) -> AsyncIterator[AsyncDatabase]:
    """An async handle on a test database. Opened inside the running event
    loop -- the async client belongs to the loop it was created in."""
    client = make_mongo_client(mongo_url)
    try:
        yield client[db_name]
    finally:
        await client.close()


def insert_docs(db: Database, *docs: Document) -> None:
    """Seeds documents in order. Incidents go through the real insert
    path, so they get their incident_key exactly as the app assigns it."""
    for doc in docs:
        if isinstance(doc, Incident):
            insert_incident_sync(db, doc)
        else:
            db[doc.COLLECTION].insert_one(doc.to_doc())


def get_doc(db: Database, model: type[Document], id: str):
    doc = db[model.COLLECTION].find_one({"_id": id})
    return model.from_doc(doc) if doc is not None else None


def find_docs(db: Database, model: type[Document], filter: dict | None = None, sort: list | None = None) -> list:
    return [model.from_doc(doc) for doc in db[model.COLLECTION].find(filter or {}, sort=sort)]


def set_fields(db: Database, model: type[Document], id: str, **fields) -> None:
    db[model.COLLECTION].update_one({"_id": id}, {"$set": fields})


# --- Sample incident texts (tests/test_incident_parser.py) -----------------

# Bold-labeled, same-line subject
E2E_SAMPLE = """\
**Subject:** DATA QUALITY / TEST DATA – DATA – NULLS IN UAT DATA – Billing UAT failing
**Environment:** NPE
**Category / Sub-type:** DATA QUALITY / TEST DATA – DATA – MISSING / NULL / INCOMPLETE
**Impact:** UAT billing scenarios TC-BILL-012/013 failing due to nulls in billing_amount column; QA blocked on regression run.
**Start Time:** 2026-08-20 09:30 ET
**Current Status:** New – under initial investigation
**Owner:** DSG-Data Squad
**Reference Tickets / Links:** JIRA FDM-1234
**Initial Suspected Root Cause:** Recent data pipeline change in NPE not applying default values for missing billing records (TBD).
"""

# No Subject label, first line is the subject
ARCHITECTURE_DOC_SAMPLE = """\
DATA QUALITY / TEST DATA – DATA – NULLS IN UAT DATA – Billing UAT failing
Body:
Environment: NPE
Category / Sub-type: DATA QUALITY / TEST DATA – DATA – MISSING / NULL / INCOMPLETE
Impact: UAT billing scenarios TC-BILL-012/013 failing due to nulls in billing_amount column; QA blocked on regression run.
Start Time (approx.): 2026-08-20 09:30 ET
Current Status: New – under initial investigation
Owner: DSG-Data Squad – @FirstName LastName
Reference Tickets / Links: JIRA FDM-1234, UAT script link
Initial Suspected Root Cause (if known): Recent data pipeline change in NPE not applying default values for missing billing records (TBD pending log review).
Next Action / ETA: Data engineer reviewing pipeline logs and contracts by 11:00 ET; update to channel after findings.
"""

# Label on its own line, value on the next
SPLIT_LABEL_SAMPLE = """\
Subject:
 ENVIRONMENT / CONFIG – ENV – NPE POINTS TO PROD – Badge emails sent from NPE
Body:
Environment: NPE
"""

# Worked-trace variant with the failing table name
WORKED_TRACE_SAMPLE = """\
**Subject:** DATA QUALITY / TEST DATA – DATA – NULLS IN UAT DATA – Billing UAT failing
**Environment:** NPE
**Impact:** Nulls in contract_value found in DSNADEV.dcd_billing_summary; QA blocked.
**Reference Tickets / Links:** JIRA FDM-1234
"""


# --- Sample incident texts (tests/test_ingestion_pipeline.py) --------------

# Same incident as E2E_SAMPLE but with the failing table name added, which is what
# lets it resolve to SYS_DCD / DATA QUALITY / TEST DATA instead of falling
# through to manual triage.
WORKED_TRACE_TEXT = """\
**Subject:** DATA QUALITY / TEST DATA – DATA – NULLS IN UAT DATA – Billing UAT failing
**Environment:** NPE
**Impact:** Nulls in contract_value found in DSNADEV.dcd_billing_summary; QA blocked.
**Start Time:** 2026-08-20 09:30 ET
**Reference Tickets / Links:** JIRA FDM-1234
"""

# Distinct subject/start_time from WORKED_TRACE_TEXT so its dedup fingerprint
# doesn't collide with the worked-trace test's (the Redis container is shared
# across the whole test session).
DUPLICATE_TEST_TEXT = """\
**Subject:** DATA QUALITY / TEST DATA – DATA – DUPLICATE DEDUP TEST
**Environment:** NPE
**Impact:** Nulls in contract_value found in DSNADEV.dcd_billing_summary; QA blocked.
**Start Time:** 2026-08-20 11:15 ET
"""

NO_FOOTPRINT_TEXT = """\
**Subject:** Some entirely unrelated incident with no known footprint
**Environment:** NPE
**Impact:** Nothing here matches any registered system.
**Start Time:** 2026-08-20 10:00 ET
"""


# --- Signal-extraction samples (tests/test_incident_parser.py) -----------
# Real snippets (trimmed) from RS-170861 (TAPESTRY), RS-170265 (RSP), and
# RS-170675 (SAPPIREST).
LOG_EXCERPT_SAMPLE = """\
Subject: RS-170861 | Elevate_R2_UAT|QLAB01|Tapestry|Unable to bypass the customer
2026-08-12 15:02:32 - c.t.dsg.common.logger.service.Log - TRACEID=ac9f193ba8fa257f - \
GUID=ac9f193ba8fa257f||APPBUILDVERSION=tmo-main.1680||APPLICATIONID=TAPESTRY||BAN=990836961||EVENT_TYPE=service_request
"""

ERROR_SYSTEM_SAMPLE = """\
Subject: RSP is not returning ban status in response
Response: {"error":{},"statusCategory":"C701","errorSystem":"RSP","errorTransaction":"activateSubscriber"}
"""

TARGET_SYSTEM_SAMPLE = """\
Subject: Order not activated after SAP fulfilment
TARGET_OPERATION=getSapRestResponse||SERVICE_TRANSACTION_ID=c3a1d3d8||TARGET_SYSTEM=SAPPIREST||TARGET_STATUS=SUCCESS
"""

# Real pipe-delimited title from RS-171404 in the 2nd ticket set.
PIPE_DELIMITED_TITLE_SAMPLE = """\
Subject: TFB QE | QLAB03 |CIT-16| Ecomm | NBYOD EIP | RSP | Activation is Pending post Fulfillment
Body:
Steps to Reproduce: place order, fulfill, validate activation.
"""


# --- Real ticket excerpts (tests/test_real_catalog.py) ---------------------
# Trimmed from real tickets (RS-173234 and others).

REAL_TICKET_FIBER = """\
Subject: RS-173234 - QLABO3||Fiber||Tapestry/D2C/ATLAS||Dealer code validation failing
Body:
After entering the dealer code getting the error message "Invalid code. please verify \
and enter it again. Contact your manager if you need assistance"
host: api.qlab03.fiber.t-mobile.com
request: POST /api/v3/osspilotfiber/osa/dcs/verify_dealer_code HTTP/1.1
"""

REAL_TICKET_IDS = """\
Subject: HSI NRF Ph#1 : QE Test Data not Present in IDS Source Views for Collections Process
Body:
As Part of E2E testing QE team send DO events to IDS, but for the date provided events \
are not processed to IDS Views. CDW_IDW_DB_QAT.IDW_CORE_V.DIGITAL_SALES_ORDER and \
CDW_IDW_DB_QAT.IDW_CORE_V.DIGITAL_SALES_ORDER_LINE. Due to data not available in the \
above views, Collections team unable to process the data.
"""


# --- Graph/Jira mock fixtures (tests/test_graph_client.py, -----------------
# tests/test_jira_client.py, tests/test_poller.py) --------------------------
# Modeled on the real RS-173234 ticket (Fiber dealer-code-validation defect)
# in Microsoft Graph chatMessage /
# Jira Cloud REST v3 issue response shape.

GRAPH_MESSAGE_RS_173234 = {
    "id": "1692655036294",
    "createdDateTime": "2026-08-21T22:17:16.294Z",
    "from": {"user": {"displayName": "Dongale, Kapil"}},
    "body": {
        "contentType": "html",
        "content": (
            '<div>Hi dpo-dice , Could you please help in checking this defect. '
            '<a href="https://t-mobile.atlassian.net/browse/RS-173234">'
            "https://t-mobile.atlassian.net/browse/RS-173234</a></div>"
        ),
    },
}

GRAPH_MESSAGES_PAGE_RESPONSE = {"value": [GRAPH_MESSAGE_RS_173234]}

JIRA_ISSUE_RS_173234 = {
    "key": "RS-173234",
    "fields": {
        "summary": (
            "RS-173234 - QLABO3||Fiber||Tapestry/D2C/ATLAS||Dealer code validation "
            "failing - After entering the dealer code getting the error message"
        ),
        "status": {"name": "In Progress"},
        "priority": {"name": "Urgent/Blocker"},
        "issuetype": {"name": "THE Bug"},
        "reporter": {"displayName": "Indrani Akula"},
        "assignee": {"displayName": "Jvalant Dave"},
        "labels": ["FIBER_QE_AUG_QLAB03", "Fiber_Automation"],
        "resolution": None,
        "created": "2026-08-21T14:04:00.000-0700",
        "updated": "2026-08-21T16:09:00.000-0700",
    },
    "renderedFields": {
        "description": (
            '<p>After entering the dealer code getting the error message "Invalid code. '
            "please verify and enter it again. Contact your manager if you need "
            'assistance"</p><p>host: api.qlab03.fiber.t-mobile.com</p>'
            "<p>request: POST /api/v3/osspilotfiber/osa/dcs/verify_dealer_code HTTP/1.1</p>"
        )
    },
}


# --- Sample OPA mapping rules (tests/test_opa_mapping_policy.py) -----------

DCD_TABLE_RULE = {
    "id": "IMR_DCD_TABLE_DATA",
    "source_system_id": "SYS_DCD",
    "category": "DATA QUALITY / TEST DATA",
    "signal_type": "table_name",
    "signal_pattern": r"\bDSNADEV\.dcd_[A-Za-z0-9_]+\b",
    "priority": 1,
    "action": "assign_to_DCD",
}
DCD_KEYWORD_FALLBACK_RULE = {
    "id": "IMR_DCD_KEY_FALLBACK",
    "source_system_id": "SYS_DCD",
    "category": "ANY",
    "signal_type": "keyword",
    "signal_pattern": r"\bDCD\b",
    "priority": 3,
    "action": "suggest_DCD",
}
