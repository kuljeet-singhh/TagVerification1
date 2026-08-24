"""
The FastAPI application.

One process serves everything: the public JSON API under /api/v1, the HTML pages, and the
HTMX fragment endpoints under /ui. That is the main structural difference from the previous
deployment, where the API and the UI were separate serverless invocations that could not
share a connection pool or an in-process cache.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from tagverify.api.v1 import analyze, health, tags, usage
from tagverify.config import settings
from tagverify.db.session import dispose_engine, init_engine, is_configured
from tagverify.db.usage import prune_usage_counters
from tagverify.errors import ApiError, api_error
from tagverify.templating import STATIC_DIR, render
from tagverify.web import admin as admin_web
from tagverify.web import docs as docs_web
from tagverify.web import playground as playground_web

log = logging.getLogger(__name__)


async def _prune_daily() -> None:
    """
    Sweep expired rate-limit rows once a day.

    The previous implementation had this function and never called it, so `usage_counters`
    grew for the life of the database. A long-lived process can simply own the schedule.
    """
    while True:
        with suppress(Exception):
            if is_configured():
                await prune_usage_counters()
        await asyncio.sleep(24 * 60 * 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=settings().log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if is_configured():
        try:
            init_engine()
        except Exception:
            # A bad DATABASE_URL must not stop the process from booting, or /health can
            # never report the problem. Every route that needs the database will surface it.
            log.exception("could not initialise the database engine")
    else:
        log.warning("DATABASE_URL is not set — database-backed routes will report degraded")

    pruner = asyncio.create_task(_prune_daily())
    try:
        yield
    finally:
        pruner.cancel()
        with suppress(asyncio.CancelledError):
            await pruner
        await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(
        title="DOOH Tag Verification",
        version="1.0.0",
        lifespan=lifespan,
        # The public contract is documented at /docs (our own page), so FastAPI's generated
        # docs would be a second, competing, less accurate description of the same API.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    for router in (analyze.router, tags.router, usage.router, health.router):
        app.include_router(router, prefix="/api/v1", tags=["api"])

    app.include_router(playground_web.router)
    app.include_router(docs_web.router)
    app.include_router(admin_web.router)

    install_error_handlers(app)
    return app


def _wants_html(request: Request) -> bool:
    return not request.url.path.startswith("/api/")


def install_error_handlers(app: FastAPI) -> None:
    """
    Keep the JSON envelope `{"error": CODE, "message": ...}` uniform.

    FastAPI's defaults return `{"detail": ...}`, so without this a validation failure would
    have a different shape from every other error and break callers that branch on `error`.
    HTML routes get an HTML error page instead — a JSON body rendered in a browser tab is a
    bad experience and tells the user nothing.
    """

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse | HTMLResponse:
        if _wants_html(request):
            return render(
                request,
                "pages/error.html",
                {"code": exc.code, "message": exc.message},
                status_code=exc.response().status_code,
            )
        return exc.response()

    @app.exception_handler(admin_web.NotAuthorised)
    async def _not_authorised(request: Request, exc: Exception) -> HTMLResponse:
        # A stale session or a missing CSRF token on an admin POST. Re-render the login page
        # rather than a bare 403: the user's next action is to sign in again, so show them
        # the form that lets them.
        return render(
            request,
            "pages/admin_login.html",
            {"error": "Your session has expired. Sign in again."},
            status_code=403,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc", ())[1:])
        message = first.get("msg", "Invalid request.")
        return api_error("BAD_REQUEST", f"{location}: {message}" if location else message)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        code = {401: "MISSING_KEY", 404: "BAD_REQUEST", 429: "RATE_LIMITED"}.get(
            exc.status_code, "INTERNAL"
        )
        if _wants_html(request):
            return render(
                request,
                "pages/error.html",
                {"code": str(exc.status_code), "message": str(exc.detail)},
                status_code=exc.status_code,
            )
        return JSONResponse(
            {"error": code, "message": str(exc.detail)}, status_code=exc.status_code
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        if _wants_html(request):
            return render(
                request,
                "pages/error.html",
                {"code": "500", "message": "Something went wrong on our side."},
                status_code=500,
            )
        return api_error("INTERNAL", "Unexpected error while handling the request.")


app = create_app()
