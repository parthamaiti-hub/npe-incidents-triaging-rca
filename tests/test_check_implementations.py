from app.check_implementations import CHECK_IMPLEMENTATIONS_V2
from app.function_registry import FUNCTION_REGISTRY


def test_every_registered_function_has_v2_code():
    assert set(CHECK_IMPLEMENTATIONS_V2.keys()) == set(FUNCTION_REGISTRY.keys())


async def test_v2_implementation_returns_ok_with_trace_embedded_in_details():
    result = await CHECK_IMPLEMENTATIONS_V2["error_logs"]({"app": "X", "lookback_minutes": 60})
    assert result["status"] == "OK"
    assert "FUNCTIONAL DUMMY v2" in result["details"]
    assert "error_logs" in result["details"]
    assert "os_user=" in result["details"]


async def test_v2_implementation_reports_its_actual_caller():
    async def my_own_caller():
        return await CHECK_IMPLEMENTATIONS_V2["service_health"]({"env": "NPE", "endpoint": "https://x/health"})

    result = await my_own_caller()
    assert "'my_own_caller'" in result["details"]


async def test_every_v2_implementation_is_independently_callable():
    for check_type, fn in CHECK_IMPLEMENTATIONS_V2.items():
        result = await fn({})
        assert result["status"] == "OK", f"{check_type} v2 did not return OK"
        assert check_type in result["details"]
