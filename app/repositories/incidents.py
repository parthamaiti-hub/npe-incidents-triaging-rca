"""Incident inserts, which own the incident_key rule.

An incident that arrives with a Jira key is known by it. Every other
incident gets int_<MMDDYYYYHHMMSS in UTC>_<5-digit running number>, e.g.
int_09272026142530_00001. The running number comes from one counter
document shared by the API, the worker and the poller: an atomic $inc, so
no two processes hand out the same number and restarts don't reset it. The
stored counter grows without bound (no reset race at the wrap point); only
the rendered number cycles 00001..99999.
"""

import datetime

from pymongo import ReturnDocument
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.database import Database

from app.models import Incident, utcnow

INCIDENT_KEY_COUNTER_ID = "incident_key_seq"
INCIDENT_KEY_MAX = 99999


def render_incident_key(counter_value: int, now: datetime.datetime) -> str:
    number = ((counter_value - 1) % INCIDENT_KEY_MAX) + 1
    return f"int_{now:%m%d%Y%H%M%S}_{number:05d}"


async def next_incident_key(db: AsyncDatabase, session: AsyncClientSession | None = None) -> str:
    doc = await db.counters.find_one_and_update(
        {"_id": INCIDENT_KEY_COUNTER_ID},
        {"$inc": {"n": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
        session=session,
    )
    return render_incident_key(doc["n"], utcnow())


async def insert_incident(db: AsyncDatabase, incident: Incident, session: AsyncClientSession | None = None) -> Incident:
    if incident.incident_key is None:
        incident.incident_key = incident.jira_key or await next_incident_key(db, session)
    await db[Incident.COLLECTION].insert_one(incident.to_doc(), session=session)
    return incident


def insert_incident_sync(db: Database, incident: Incident) -> Incident:
    """Same rule for the synchronous load/seed scripts and test fixtures."""
    if incident.incident_key is None:
        if incident.jira_key is not None:
            incident.incident_key = incident.jira_key
        else:
            doc = db.counters.find_one_and_update(
                {"_id": INCIDENT_KEY_COUNTER_ID}, {"$inc": {"n": 1}}, upsert=True, return_document=ReturnDocument.AFTER
            )
            incident.incident_key = render_incident_key(doc["n"], utcnow())
    db[Incident.COLLECTION].insert_one(incident.to_doc())
    return incident
