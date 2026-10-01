"""One module per (check_type, version) -- e.g. pipeline_status_v2.py --
each exporting a single `async def run(params: dict) -> dict`.

app.check_implementations discovers every `<check_type>_v<N>.py` here at
import time and registers it as the implementation of (check_type, N);
app.checks.run_check dispatches through that registry. So:

  - a new version of an existing check   -> add <check_type>_v<N+1>.py
  - a brand-new check_type               -> add <new_name>_v1.py
                                            (and POST /functions its contract)

No other code changes. Modules not matching the pattern (e.g. _shared.py)
are helpers and are ignored.
"""
