"""[FUNCTIONAL DUMMY v2] data_source_connectivity -- app.checks.run_check dispatches
here for version_number == 2 (registered in app.check_implementations).
Swap this function's body for a real integration whenever one exists;
nothing else in the dispatch chain needs to change.
"""

from app.check_types._shared import evidence, invocation_trace


async def run(params: dict) -> dict:
    trace = invocation_trace("data_source_connectivity", params)
    return evidence("OK", "data_source_connectivity", trace)
