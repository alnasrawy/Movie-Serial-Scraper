"""Run the middleware:  python -m middleware  (uvicorn on 0.0.0.0).

Binds to 0.0.0.0 so container/PaaS deployments (Render, Docker) can reach it.
Port resolution order: $PORT (Render/Heroku) -> $MIDDLEWARE_PORT -> 8000.
"""

from __future__ import annotations

import os

if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("MIDDLEWARE_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT") or os.environ.get("MIDDLEWARE_PORT") or "8000")
    uvicorn.run("middleware.server:app", host=host, port=port, log_level="info")
