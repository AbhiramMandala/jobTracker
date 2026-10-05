"""JobSetu FastAPI entrypoint. Routes stay thin; no business logic here."""

import logging
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.database import init_db
from app.routes import authenticity, debug, enrich, evidence, health, pages, profile, search, tracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# httpx logs full request URLs at INFO, and SerpApi URLs carry api_key as a
# query param — so the key would land in server logs on every live call.
# Keep httpx at WARNING: errors still surface, keys never do.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("jobsetu")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        init_db()
        logger.info(
            "startup complete db=%s news=%s maps=%s trends=%s",
            settings.DATABASE_URL.split("://")[0],
            settings.ENABLE_NEWS,
            settings.ENABLE_MAPS,
            settings.ENABLE_TRENDS,
        )
        yield

    app = FastAPI(title="JobSetu", lifespan=_lifespan)

    # Tracker integration: the Cloudflare Student Job Tracker frontend
    # (http://localhost:5173 in local dev) fetches /api/jobs* cross-origin.
    # Read-only GET endpoints, no credentials — same posture as /debug/usage.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    app.include_router(health.router)
    app.include_router(authenticity.router)
    app.include_router(enrich.router)
    app.include_router(pages.router)
    app.include_router(search.router)
    app.include_router(tracker.router)
    app.include_router(evidence.router)
    app.include_router(debug.router)
    app.include_router(profile.router)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        logger.exception("unhandled error path=%s", request.url.path)
        if request.url.path.startswith("/api/"):
            return JSONResponse(status_code=500, content={"detail": "Internal error"})
        return templates.TemplateResponse(
            request, "error.html", {"message": "Something went wrong."}, status_code=500
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        # Friendly pages for unknown routes. API and explicit JSON 404s
        # elsewhere are untouched: this only handles unmatched paths.
        if exc.status_code != 404 or request.url.path.startswith("/api/"):
            return JSONResponse(status_code=exc.status_code,
                                content={"detail": exc.detail})
        return templates.TemplateResponse(
            request, "error.html",
            {"code": 404,
             "heading": "Page not found.",
             "message": "This address doesn't lead anywhere. The page may have "
                        "moved, or the link has a typo."},
            status_code=404,
        )

    return app


app = create_app()
