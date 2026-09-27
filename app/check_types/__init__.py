"""One module per (check_type, version) -- e.g. pipeline_status_v2.py --
each exporting a single `async def run(params: dict) -> dict`. Registered
into app.check_implementations.CHECK_IMPLEMENTATIONS_V2, which is what
app.checks.run_check actually dispatches through; this package is where
each check_type's own code lives, not the dispatch mechanism itself.
"""
