import httpx

from app.opa_client import evaluate_mapping_rules
from dataloadscripts.test_fixtures import DCD_KEYWORD_FALLBACK_RULE, DCD_TABLE_RULE


async def _query(opa_url, signals, rules):
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.post(
            f"{opa_url}/v1/data/npe/triage/matches",
            json={"input": {"signals": signals, "rules": rules}},
        )
        response.raise_for_status()
        return response.json().get("result") or []


async def test_table_name_signal_matches_dcd_rule(opa_url):
    signals = [{"signal_type": "table_name", "value": "DSNADEV.dcd_billing_summary"}]
    matches = await _query(opa_url, signals, [DCD_TABLE_RULE, DCD_KEYWORD_FALLBACK_RULE])
    assert [m["id"] for m in matches] == ["IMR_DCD_TABLE_DATA"]


async def test_keyword_signal_matches_fallback_rule_only(opa_url):
    signals = [{"signal_type": "keyword", "value": "something mentions DCD in passing"}]
    matches = await _query(opa_url, signals, [DCD_TABLE_RULE, DCD_KEYWORD_FALLBACK_RULE])
    assert [m["id"] for m in matches] == ["IMR_DCD_KEY_FALLBACK"]


async def test_no_signal_match_returns_empty(opa_url):
    signals = [{"signal_type": "table_name", "value": "DSNADEV.unrelated_table"}]
    matches = await _query(opa_url, signals, [DCD_TABLE_RULE, DCD_KEYWORD_FALLBACK_RULE])
    assert matches == []


async def test_evaluate_mapping_rules_client_against_live_opa(opa_url, monkeypatch):
    import app.opa_client as opa_client_module

    monkeypatch.setattr(opa_client_module, "OPA_URL", opa_url)
    signals = [{"signal_type": "table_name", "value": "DSNADEV.dcd_billing_summary"}]
    matches = await evaluate_mapping_rules(signals, [DCD_TABLE_RULE])
    assert [m["id"] for m in matches] == ["IMR_DCD_TABLE_DATA"]
