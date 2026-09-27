import httpx

from app.config import OPA_URL


async def evaluate_mapping_rules(signals: list[dict], rules: list[dict]) -> list[dict]:
    """Calls the generic OPA mapping policy with the extracted incident
    signals and the current INCIDENT_MAPPING_RULE rows. Returns every rule
    whose signal_pattern matched a same-typed signal (priority tie-break is
    left to the caller)."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.post(
            f"{OPA_URL}/v1/data/npe/triage/matches",
            json={"input": {"signals": signals, "rules": rules}},
        )
        response.raise_for_status()
        return response.json().get("result") or []
