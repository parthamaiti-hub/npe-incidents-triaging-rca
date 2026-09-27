from openai import AsyncOpenAI
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import CLASSIFICATION_CONFIDENCE_THRESHOLD, LLM_FALLBACK_ENABLED
from app.embeddings import ClassificationEmbedding, retrieve_classification_candidates
from app.incident_parser import extract_signals
from app.llm_client import make_openai_client, structured_completion
from app.models import IncidentMappingRule, WorkflowDefinition
from app.opa_client import evaluate_mapping_rules

RESOLVED = "resolved"
ANY_CATEGORY_PENDING_LLM = "any_category_pending_llm"
MANUAL_TRIAGE = "manual_triage"
LLM_RESOLVED = "llm_resolved"

# Both fields count as "fully classified" for every
# downstream consumer that used to check only RESOLVED (incidents.classified
# publish gate in app.worker, the playbook-lookup branch in
# app.e2e_pipeline). One source of truth so a future third call site can't
# silently miss the LLM_RESOLVED case, the same discipline
# app.checks.has_implementation already applies.
FULLY_CLASSIFIED_STATUSES = frozenset({RESOLVED, LLM_RESOLVED})


class LlmClassificationSuggestion(BaseModel):
    """Structured LLM output -- source_system_id/category are
    plain strings here, not a Pydantic Literal enum, because the valid set
    is only known at call time (retrieved candidates / catalog categories).
    The closed-set constraint is enforced by validating the returned value
    against that set after the call, in classify_with_llm_fallback -- never
    trusted from the prompt instruction alone."""

    source_system_id: str | None = None
    category: str | None = None
    confidence: float
    rationale: str


async def _classify_via_rules(session: AsyncSession, raw_text: str) -> dict:
    """Deterministic OPA-evaluated mapping rules. Never second-guessed by
    the LLM fallback: a winning rule here
    means classify_raw_text returns immediately, no LLM call at all."""
    signals = extract_signals(raw_text)

    rules = (await session.scalars(select(IncidentMappingRule))).all()
    rule_dicts = [
        {
            "id": rule.id,
            "source_system_id": rule.source_system_id,
            "category": rule.category,
            "signal_type": rule.signal_type,
            "signal_pattern": rule.signal_pattern,
            "priority": rule.priority,
            "action": rule.action,
        }
        for rule in rules
    ]

    matches = await evaluate_mapping_rules(signals, rule_dicts)
    if not matches:
        return {"status": MANUAL_TRIAGE, "matched_rule_id": None, "source_system_id": None, "category": None}

    winner = min(matches, key=lambda rule: (rule["priority"], rule["id"]))

    if winner["category"] == "ANY":
        return {
            "status": ANY_CATEGORY_PENDING_LLM,
            "matched_rule_id": winner["id"],
            "source_system_id": winner["source_system_id"],
            "category": None,
        }

    return {
        "status": RESOLVED,
        "matched_rule_id": winner["id"],
        "source_system_id": winner["source_system_id"],
        "category": winner["category"],
    }


async def classify_raw_text(session: AsyncSession, raw_text: str, client: AsyncOpenAI | None = None) -> dict:
    """Resolves (source_system_id, category) for an incident's raw text.

    Deterministic rules run first and unconditionally win if they resolve
    outright (RESOLVED) -- the LLM fallback is never
    consulted in that case. It's only invoked for the two dead-end
    outcomes: MANUAL_TRIAGE (no rule matched at all) and
    ANY_CATEGORY_PENDING_LLM (a keyword-fallback rule resolved
    source_system_id but not category), and only when LLM_FALLBACK_ENABLED.

    Always returns classification_method ("rule" | "llm" | None) and
    llm_confidence (float | None) alongside status/matched_rule_id/
    source_system_id/category, so callers can persist them onto Incident
    without extra branching.
    """
    result = await _classify_via_rules(session, raw_text)

    if result["status"] == RESOLVED:
        return {**result, "classification_method": "rule", "llm_confidence": None}

    if not LLM_FALLBACK_ENABLED:
        return {**result, "classification_method": None, "llm_confidence": None}

    llm_client = client or make_openai_client()
    return await classify_with_llm_fallback(session, llm_client, raw_text, result)


def apply_classification(incident, result: dict) -> None:
    """Writes a classify_raw_text result onto an Incident -- the one place
    the worker, POST /rca and retry all record classification."""
    incident.classification_status = result["status"]
    incident.matched_rule_id = result["matched_rule_id"]
    incident.source_system_id = result["source_system_id"]
    incident.category = result["category"]
    incident.classification_method = result["classification_method"]
    incident.llm_confidence = result["llm_confidence"]


async def _valid_categories_for(session: AsyncSession, source_system_id: str) -> list[str]:
    """Closed set for the category-only LLM mode -- categories that already
    have an IncidentMappingRule or WorkflowDefinition row for this system,
    never an open catalog-wide list, so the LLM can never pick a category
    with no playbook to run."""
    rule_categories = (
        await session.scalars(
            select(IncidentMappingRule.category).where(
                IncidentMappingRule.source_system_id == source_system_id,
                IncidentMappingRule.category != "ANY",
            )
        )
    ).all()
    workflow_categories = (
        await session.scalars(
            select(WorkflowDefinition.category).where(WorkflowDefinition.source_system_id == source_system_id)
        )
    ).all()
    return sorted({*rule_categories, *workflow_categories})


def _format_candidates(candidates: list[ClassificationEmbedding]) -> str:
    if not candidates:
        return "(no retrieved candidates)"
    return "\n".join(f"- [{c.kind}] source_system_id={c.source_system_id} category={c.category}: {c.text}" for c in candidates)


async def classify_with_llm_fallback(
    session: AsyncSession, client: AsyncOpenAI, raw_text: str, prior_result: dict
) -> dict:
    """Retrieval-augmented, closed-set classification --
    the LLM only ever picks among retrieved real candidates / catalog
    categories, validated against that set after the call returns.
    "Never a silent override": below CLASSIFICATION_CONFIDENCE_THRESHOLD,
    or an invalid/declined pick, returns prior_result unchanged -- the
    incident stays at its pre-LLM status, exactly as if this were never
    called."""
    if prior_result["status"] == ANY_CATEGORY_PENDING_LLM:
        return await _classify_category_only(session, client, raw_text, prior_result)
    return await _classify_full(session, client, raw_text, prior_result)


async def _classify_category_only(
    session: AsyncSession, client: AsyncOpenAI, raw_text: str, prior_result: dict
) -> dict:
    source_system_id = prior_result["source_system_id"]
    categories = await _valid_categories_for(session, source_system_id)
    fallback = {**prior_result, "classification_method": None, "llm_confidence": None}
    if not categories:
        return fallback

    candidates = await retrieve_classification_candidates(session, client, raw_text, source_system_id=source_system_id)
    suggestion = await structured_completion(
        client,
        system_prompt=(
            "You resolve the category of a non-production-environment incident for a "
            f"known application (source_system_id={source_system_id}). Valid categories: "
            f"{categories}. Pick exactly one from that list, or return category=null if "
            "none apply with reasonable confidence. Never invent a category outside the "
            "given list."
        ),
        user_prompt=f"Incident text:\n{raw_text}\n\nRetrieved context:\n{_format_candidates(candidates)}",
        response_model=LlmClassificationSuggestion,
    )

    if suggestion.category not in categories or suggestion.confidence < CLASSIFICATION_CONFIDENCE_THRESHOLD:
        return fallback

    return {
        "status": LLM_RESOLVED,
        "matched_rule_id": prior_result["matched_rule_id"],
        "source_system_id": source_system_id,
        "category": suggestion.category,
        "classification_method": "llm",
        "llm_confidence": suggestion.confidence,
    }


async def _classify_full(session: AsyncSession, client: AsyncOpenAI, raw_text: str, prior_result: dict) -> dict:
    fallback = {**prior_result, "classification_method": None, "llm_confidence": None}
    candidates = await retrieve_classification_candidates(session, client, raw_text)
    if not candidates:
        return fallback

    valid_system_ids = sorted({c.source_system_id for c in candidates})
    suggestion = await structured_completion(
        client,
        system_prompt=(
            "You classify a non-production-environment incident against a set of "
            f"retrieved candidate applications: {valid_system_ids}. Pick the application "
            "and a category consistent with the retrieved context, or return "
            "source_system_id=null and category=null if nothing applies with reasonable "
            "confidence. Never invent an application outside the given list."
        ),
        user_prompt=f"Incident text:\n{raw_text}\n\nRetrieved context:\n{_format_candidates(candidates)}",
        response_model=LlmClassificationSuggestion,
    )

    if suggestion.source_system_id not in valid_system_ids or suggestion.category is None:
        return fallback

    valid_categories = await _valid_categories_for(session, suggestion.source_system_id)
    if suggestion.category not in valid_categories or suggestion.confidence < CLASSIFICATION_CONFIDENCE_THRESHOLD:
        return fallback

    return {
        "status": LLM_RESOLVED,
        "matched_rule_id": None,
        "source_system_id": suggestion.source_system_id,
        "category": suggestion.category,
        "classification_method": "llm",
        "llm_confidence": suggestion.confidence,
    }
