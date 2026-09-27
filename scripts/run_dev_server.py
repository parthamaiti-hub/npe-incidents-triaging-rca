"""Local dev server runner. uvicorn's default Windows loop factory always
returns ProactorEventLoop (see uvicorn.loops.asyncio.asyncio_loop_factory),
which psycopg's async driver cannot run under -- and it ignores the ambient
asyncio event loop policy, so setting the policy alone doesn't fix it. Not
needed in the Linux Docker Compose deployment -- there,
`uvicorn app.main:app` works directly.

Usage:
    uv run python scripts/run_dev_server.py
"""

import asyncio
import sys

import uvicorn

if __name__ == "__main__":
    loop = asyncio.SelectorEventLoop if sys.platform == "win32" else "auto"
    uvicorn.run("app.main:app", host="0.0.0.0", port=8421, reload=False, loop=loop)
