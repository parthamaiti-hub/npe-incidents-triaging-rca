"""Registry of check_type code implementations, keyed by (check_type, version).

Sol-104: every module in app/check_types/ named `<check_type>_v<N>.py` that
exports `async def run(params: dict) -> dict` is discovered at import time
and registered as the implementation of (check_type, N). Adding a new
version of a check -- or the first version of a brand-new check_type -- is
adding one file; nothing here or in app.checks needs editing.

Version 1 of the original 20 check_types is a template stub in
app.checks.STUB_RESULTS rather than a module; app.checks resolves both
sources through one lookup (get_implementation). A (check_type, version)
must come from exactly one source -- app.checks refuses to import if a
module and a template claim the same pair.
"""

import importlib
import inspect
import pkgutil
import re
from collections.abc import Awaitable, Callable

import app.check_types

CheckFn = Callable[[dict], Awaitable[dict]]

_MODULE_NAME_RE = re.compile(r"^(?P<check_type>[a-z][a-z0-9_]*?)_v(?P<version>[1-9]\d*)$")


def _discover() -> dict[tuple[str, int], CheckFn]:
    found: dict[tuple[str, int], CheckFn] = {}
    for module_info in pkgutil.iter_modules(app.check_types.__path__):
        match = _MODULE_NAME_RE.match(module_info.name)
        if match is None:
            continue  # _shared.py and other helpers
        module = importlib.import_module(f"{app.check_types.__name__}.{module_info.name}")
        run = getattr(module, "run", None)
        if run is None or not inspect.iscoroutinefunction(run):
            raise RuntimeError(
                f"app/check_types/{module_info.name}.py must export `async def run(params: dict) -> dict`"
            )
        found[(match["check_type"], int(match["version"]))] = run
    return found


# (check_type, version) -> run(). Mutated only by tests (monkeypatch);
# app.checks reads it on every lookup, so an injected entry is live at once.
CHECK_IMPLEMENTATIONS: dict[tuple[str, int], CheckFn] = _discover()

# The v2 "functional dummy" modules, by check_type -- kept for
# dataloadscripts/seed_functional_dummy_versions.py, which publishes exactly
# these as version 2.
CHECK_IMPLEMENTATIONS_V2: dict[str, CheckFn] = {
    check_type: fn for (check_type, version), fn in CHECK_IMPLEMENTATIONS.items() if version == 2
}
