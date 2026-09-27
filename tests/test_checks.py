import pytest

from app.checks import STUB_RESULTS, has_implementation, run_check


async def test_run_check_fills_template_from_params():
    result = await run_check("error_logs", {"app": "FIBER", "lookback_minutes": 240})
    assert result["status"] == "WARN"
    assert "FIBER" in result["details"]
    assert "240" in result["details"]


async def test_run_check_falls_back_to_raw_template_on_missing_param():
    result = await run_check("service_health", {})
    assert result["status"] == "OK"
    assert result["details"]  # doesn't raise, just leaves the template unfilled


async def test_run_check_unknown_check_type_raises():
    with pytest.raises(ValueError):
        await run_check("not_a_real_check", {})


async def test_run_check_unknown_version_raises():
    with pytest.raises(ValueError, match="version 3"):
        await run_check("error_logs", {"app": "FIBER", "lookback_minutes": 240}, version_number=3)


async def test_run_check_dispatches_v2_to_real_code_not_the_v1_template():
    result = await run_check("error_logs", {"app": "FIBER", "lookback_minutes": 240}, version_number=2)
    assert result["status"] == "OK"
    assert "FUNCTIONAL DUMMY v2" in result["details"]
    # proves it's genuinely different code, not v1's canned WARN template
    assert "ERROR-level log entries" not in result["details"]


async def test_run_check_defaults_to_version_1():
    explicit = await run_check("error_logs", {"app": "FIBER", "lookback_minutes": 240}, version_number=1)
    default = await run_check("error_logs", {"app": "FIBER", "lookback_minutes": 240})
    assert explicit == default


def test_has_implementation():
    assert has_implementation("error_logs", 1) is True
    assert has_implementation("error_logs", 2) is True  # app.check_implementations.error_logs_v2
    assert has_implementation("error_logs", 3) is False
    assert has_implementation("not_a_real_check", 1) is False


def test_every_stub_status_is_ok_warn_or_error():
    for check_type, versions in STUB_RESULTS.items():
        for version_number, (status, _template) in versions.items():
            assert status in ("OK", "WARN", "ERROR"), f"{check_type} v{version_number} has invalid status {status!r}"
