"""Local dev server runner -- the API on :8421, same as
`uvicorn app.main:app --host 0.0.0.0 --port 8421`.

(It used to force a SelectorEventLoop on Windows for psycopg's async
driver; PyMongo's async client and the Chroma HTTP client run on the
default loop, so that's gone.)

Usage:
    uv run python -m scripts.run_dev_server
"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8421, reload=False)
