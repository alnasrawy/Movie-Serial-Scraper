"""Run the middleware:  python -m middleware  (uvicorn on 127.0.0.1:8000)."""

from __future__ import annotations

import os

if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("MIDDLEWARE_HOST", "127.0.0.1")
    port = int(os.environ.get("MIDDLEWARE_PORT", "8000"))
    uvicorn.run("middleware.server:app", host=host, port=port, log_level="info")
