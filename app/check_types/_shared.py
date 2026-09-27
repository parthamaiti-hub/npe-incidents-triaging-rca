"""Shared helpers for every check_type's v2 ("functional dummy") module in
this package -- OS-user/caller/timestamp introspection, embedded in the
returned evidence, so running a workflow proves exactly which check module
+ version was invoked, by whom, and when. Not monitoring logic itself --
each check_type's own <name>_v2.py module owns that; swap a single
module's run() body for a real integration whenever one exists, this stays
unchanged.
"""

import getpass
import inspect
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


def invocation_trace(check_type: str, params: dict) -> dict:
    """OS user, the caller two frames up, and a timestamp. Logs it and
    returns it for embedding in the evidence "details" string.

    Must be called *directly* from a check_type module's run() -- no
    wrapper in between -- since f_back.f_back depends on that exact frame
    depth (frame 0 = this function, frame 1 = run(), frame 2 = whoever
    called run(), e.g. app.checks.run_check in production or a test's own
    caller). Moving each check into its own module doesn't change this:
    frame depth follows the call chain, not file boundaries."""
    os_user = getpass.getuser()
    caller_frame = inspect.currentframe().f_back.f_back
    caller_name = caller_frame.f_code.co_name if caller_frame is not None else "unknown"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(
        f"[{check_type} v2] User: {os_user} | Executed at {timestamp} | "
        f"Called by: {caller_name!r} | params={params}"
    )
    return {"user": os_user, "caller": caller_name, "timestamp": timestamp}


def evidence(status: str, check_type: str, trace: dict) -> dict:
    return {
        "status": status,
        "details": (
            f"[FUNCTIONAL DUMMY v2] {check_type} invoked by {trace['caller']!r} "
            f"(os_user={trace['user']}) at {trace['timestamp']}"
        ),
    }
