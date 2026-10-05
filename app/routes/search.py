"""POST /search. Fast path: discovery → normalize → dedup → match → render.

Heavy enrichment (VERIFY evidence, news, interviews) does NOT block this
response. Cards render loading placeholders; the page fetches each section
from GET /api/enrich/{verify,news,interview} in parallel. Pure derivations
(authenticity score, company type) stay inline — one bounded local query,
zero SerpApi calls.
"""

import json
import logging
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import get_settings
from app.data.skills import display_skill
from app.database import get_db
from app.models import Evidence, JobSkill, Match
from app.routes.profile import get_active_candidate
from app.services.authenticity import analyze_job, display_dict
from app.services.company import classify_company_type, filter_value
from app.services.job_search import JobSearchService
from app.services.serpapi_client import SerpApiError
from app.utils import age_text, safe_url

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="app/templates")
router = APIRouter()

_FRIENDLY = {
    "config": "Job search is not configured yet (missing API key). Please try again later.",
    "auth": "Job search is temporarily unavailable. Please try again.",
    "rate_limit": "Job search is busy right now. Please try again in a while.",
    "timeout": "Job search is temporarily unavailable. Please try again.",
    "http": "Job search is temporarily unavailable. Please try again.",
    "parse": "Job search returned an unexpected response. Please try again.",
}


@router.post("/search", response_class=HTMLResponse)
async def run_search(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    role = str(form.get("role") or "")
    location = str(form.get("location") or "")
    experience = str(form.get("experience") or "Fresher")
    discovery_started = time.monotonic()
    try:
        result = JobSearchService(db).run(role, location, experience)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "role": role or "Python Backend Developer",
                "location": location or "Hyderabad",
                "experience": experience,
                "search_available": True,
                "error": str(exc),
            },
            status_code=400,
        )
    except SerpApiError as exc:
        logger.warning("search failed kind=%s q=%r", exc.kind, role)
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": _FRIENDLY.get(exc.kind, _FRIENDLY["http"])},
            status_code=503,
        )
    candidate = get_active_candidate(db)
    job_ids = [job.id for job in result.jobs]
    fast_started = time.monotonic()
    matches: dict[int, Match] = {}
    skills_by_job: dict[int, list[str]] = {}
    if job_ids:
        for row in db.query(Match).filter(Match.job_id.in_(job_ids)).all():
            if candidate is not None and row.candidate_id == candidate.id:
                matches[row.job_id] = row
        for row in (
            db.query(JobSkill)
            .filter(JobSkill.job_id.in_(job_ids))
            .order_by(JobSkill.id)
            .all()
        ):
            skills_by_job.setdefault(row.job_id, []).append(row.skill_norm)
    gap_display = [
        {
            "skill": display_skill(gap["skill"]),
            "missing_in": gap["missing_in"],
            "total_jobs": gap["total_jobs"],
        }
        for gap in result.gaps
    ]
    # VERIFY pillar: enrich top jobs by match score (or result order).
    totals = {job_id: match.total for job_id, match in matches.items()}
    # Enrichment happens lazily via /api/enrich — but the page needs to know
    # WHICH cards get loading placeholders. Same ranking the services use
    # (match totals, result order fallback), same per-service caps.
    settings = get_settings()
    ranked = sorted(result.jobs, key=lambda j: totals.get(j.id, 0), reverse=True)
    enrich_ids = {
        "verify": {job.id for job in ranked[: max(0, settings.EVIDENCE_MAX_JOBS)]},
        "news": {job.id for job in ranked[: max(0, settings.NEWS_MAX_JOBS)]},
        "interview": {job.id for job in ranked[: max(0, settings.INTERVIEW_MAX_JOBS)]},
    }
    extra_links: dict[int, list[dict]] = {}
    for job in result.jobs:
        try:
            links = json.loads(job.extra_links or "[]")
        except ValueError:
            links = []
        extra_links[job.id] = [
            {"source": e.get("source", "listing"), "url": safe_url(e.get("link", ""))}
            for e in links
            if isinstance(e, dict) and safe_url(e.get("link", ""))
        ][:3]
    # AUTHENTICITY: pure derivation over stored rows + job fields. One bounded
    # query, zero SerpApi calls, never blocks or breaks search.
    auth_display: dict[int, dict] = {}
    auth_by_job: dict[int, list] = {}
    if job_ids:
        try:
            auth_rows = db.query(Evidence).filter(Evidence.job_id.in_(job_ids)).all()
        except Exception:
            logger.exception("authenticity evidence lookup failed; continuing")
            auth_rows = []
        for row in auth_rows:
            auth_by_job.setdefault(row.job_id, []).append(row)
        for job in result.jobs:
            rows = auth_by_job.get(job.id, [])
            news_ok = any(
                (r.evidence_type or "").startswith("news_") and r.source_url
                for r in rows
            )
            try:
                auth_display[job.id] = display_dict(analyze_job(job, rows, news_ok=news_ok))
            except Exception:
                logger.exception("authenticity analysis failed job=%s", job.id)
    # COMPANY TYPE: evidence-based estimate per card (pure, same rows).
    company_display: dict[int, dict] = {}
    if job_ids:
        for job in result.jobs:
            company = job.company
            try:
                classification = classify_company_type(
                    company.name_norm if company else "",
                    company.name_raw if company else "",
                    auth_by_job.get(job.id, []) if job_ids else [],
                )
            except Exception:
                logger.exception("company classification failed job=%s", job.id)
                classification = {"type": "Private Company", "confidence": "Low"}
            company_display[job.id] = {
                "type": classification["type"],
                "confidence": classification["confidence"],
                "filter": filter_value(classification),
            }
    fast_ms = int((time.monotonic() - fast_started) * 1000)
    discovery_ms = int((fast_started - discovery_started) * 1000)
    logger.info("search fast role=%r jobs=%d discovery_ms=%d fast_ms=%d",
                role, len(result.jobs), discovery_ms, fast_ms)
    return templates.TemplateResponse(
        request,
        "results.html",
        {
            "search_id": result.search.id,            "enrich_ids": enrich_ids,
            "role": result.search.role,
            "location": result.search.location,
            "experience": result.search.experience,
            "jobs": result.jobs,
            "raw_count": result.raw_count,
            "canonical_count": result.canonical_count,
            "dup_removed": result.dup_removed,
            "is_live": result.is_live,
            "stale": result.stale,
            "age": age_text(result.search.retrieved_at),
            "has_profile": result.has_profile,
            "matches": matches,
            "skills_by_job": skills_by_job,
            "gaps": gap_display,
            "display_skill": display_skill,
            "verify": {},
            "safe_url": safe_url,
            "extra_links": extra_links,
            "news": {},
            "authenticity": auth_display,
            "interview": {},
            "company": company_display,
            "tracker_api_url": settings.TRACKER_API_URL or "http://127.0.0.1:8787",
        },
    )
