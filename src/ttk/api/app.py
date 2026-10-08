from __future__ import annotations

from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ttk.api.common import (
    WEB_DIR,
    WRITE_METHODS,
)
from ttk.api.routes import ROUTERS
from ttk.config import Settings, get_settings
from ttk.db.session import make_engine, make_session_factory


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="Time-to-Kill", version="0.1.0")
    allowed_hosts = settings.allowed_hosts

    @app.middleware("http")
    async def same_origin_writes(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """No auth, so block cross-site writes: another website open in the browser
        must not be able to POST to this loopback API. Writes need a JSON body,
        and any Origin header must be this app's own host."""
        if request.method in WRITE_METHODS:
            origin = request.headers.get("origin")
            if origin is not None and urlsplit(origin).hostname not in {
                h.strip("[]") for h in allowed_hosts
            }:
                return JSONResponse({"detail": "Cross-origin write refused"}, status_code=403)
            content_type = request.headers.get("content-type", "")
            if request.headers.get("content-length", "0") != "0" and not content_type.startswith(
                "application/json"
            ):
                return JSONResponse({"detail": "Writes must be application/json"}, status_code=415)
        return await call_next(request)

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    app.state.settings = settings
    app.state.session_factory = make_session_factory(make_engine(settings.database_url))
    for router in ROUTERS:
        app.include_router(router)
    if WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app
