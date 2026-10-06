"""Tracker integration API.

Lets the Cloudflare Student Job Tracker import JobSetu discoveries without
coupling the two codebases:

  GET /api/jobs?search_id=N
    -> {"search_id": N, "jobs": [{id, company, title, location, apply_link,
        salary, posted, description_snippet, match_total}]}

  GET /api/jobs/{job_id}/tracker-export
    -> application-shaped payload ready for POST /api/applications on the
       Tracker Worker: {company, job_title, location, job_url, job_type,
       salary, application_date, status, notes, jobsetu:{...}}.

Read-only, no auth (same dev-only posture as /debug/usage), never exposes
server secrets.
"""

import datetime as dt
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.models import Company, Job, Match, Search
from app.routes.profile import get_active_candidate
from app.services.job_search import JobSearchService
from app.services.matcher import job_signals
from app.services.serpapi_client import SerpApiError
from app.utils import safe_url

logger = logging.getLogger(__name__)

router = APIRouter()


def _job_card(db: Session, job: Job, totals: dict[int, int], match_map: dict[int, Match] | None = None) -> dict:
    company = job.company.name_raw if job.company else ""
    desc = job.description or ""
    card = {
        "id": job.id,
        "company": company,
        "title": job.title_raw or "",
        "location": job.location_raw or "",
        "apply_link": safe_url(job.apply_link or ""),
        "salary": job.salary_text or "",
        "posted": job.posted_text or "",
        "description_snippet": desc[:300],
        "match_total": totals.get(job.id),
        "signals": job_signals(job.title_norm or "", desc),
        "evidence_url": f"/jobs/{job.id}/evidence",
    }
    if match_map is not None and job.id in match_map:
        m = match_map[job.id]
        card["match"] = {
            "total": m.total,
            "matched_skills": list(m.matched_skills or []),
            "missing_skills": list(m.missing_skills or [])[:8],
            "reasons": list(m.reasons or [])[:6],
        }
    return card


def _matches_for(db: Session, jobs: list[Job]) -> tuple[dict[int, int], dict[int, Match]]:
    candidate = get_active_candidate(db)
    if candidate is None or not jobs:
        return {}, {}
    ids = [job.id for job in jobs]
    rows = (
        db.query(Match)
        .filter(Match.job_id.in_(ids), Match.candidate_id == candidate.id)
        .all()
    )
    return {r.job_id: r.total for r in rows}, {r.job_id: r for r in rows}


@router.post("/api/search")
def run_search_api(payload: dict, db: Session = Depends(get_db)):
    """Embedded-discovery search for the Tracker frontend.

    Thin wrapper over JobSearchService (same validation, discovery, dedup,
    matching as POST /search). Fast path only: no VERIFY/news enrichment —
    cards link to each job's evidence page for that. Returns JSON.
    """
    role = str((payload or {}).get("role") or "")
    location = str((payload or {}).get("location") or "")
    experience = str((payload or {}).get("experience") or "Fresher")
    try:
        result = JobSearchService(db).run(role, location, experience)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    except SerpApiError:
        logger.warning("tracker search failed q=%r", role)
        return JSONResponse(
            status_code=503,
            content={"detail": "Job search is temporarily unavailable. Please try again."},
        )
    totals, match_map = _matches_for(db, result.jobs)
    logger.info("tracker api search role=%r jobs=%d live=%s", role, len(result.jobs), result.is_live)
    return {
        "search_id": result.search.id,
        "role": result.search.role,
        "location": result.search.location,
        "experience": result.search.experience,
        "is_live": result.is_live,
        "stale": result.stale,
        "raw_count": result.raw_count,
        "dup_removed": result.dup_removed,
        "has_profile": result.has_profile,
        "jobs": [_job_card(db, job, totals, match_map) for job in result.jobs],
    }


@router.get("/api/searches")
def list_searches(limit: int = 20, db: Session = Depends(get_db)):
    """Recent searches for the Tracker's Discover picker (newest first)."""
    limit = max(1, min(int(limit or 20), 50))
    searches = db.query(Search).order_by(Search.id.desc()).limit(limit).all()
    ids = [s.id for s in searches]
    counts: dict[int, int] = {}
    if ids:
        from sqlalchemy import func

        for sid, count in (
            db.query(Job.search_id, func.count(Job.id))
            .filter(Job.search_id.in_(ids), Job.is_active == True)  # noqa: E712
            .group_by(Job.search_id)
            .all()
        ):
            counts[sid] = count
    return {
        "searches": [
            {
                "id": s.id,
                "role": s.role,
                "location": s.location,
                "experience": s.experience,
                "job_count": counts.get(s.id, 0),
                "retrieved_at": s.retrieved_at.isoformat() if s.retrieved_at else None,
            }
            for s in searches
        ]
    }


@router.get("/api/jobs")
def list_jobs(search_id: int, db: Session = Depends(get_db)):
    search = db.query(Search).filter_by(id=search_id).one_or_none()
    if search is None:
        return JSONResponse(status_code=404, content={"detail": "Search not found."})
    jobs = (
        db.query(Job)
        .options(selectinload(Job.company))
        .filter(Job.search_id == search_id, Job.is_active == True)  # noqa: E712
        .order_by(Job.id)
        .all()
    )
    totals, match_map = _matches_for(db, jobs)
    logger.info("tracker list search_id=%d jobs=%d", search_id, len(jobs))
    return {"search_id": search_id, "jobs": [_job_card(db, job, totals, match_map) for job in jobs]}


@router.get("/api/jobs/{job_id}/tracker-export")
def export_job(job_id: int, db: Session = Depends(get_db)):
    job = (
        db.query(Job)
        .options(selectinload(Job.company))
        .filter_by(id=job_id)
        .one_or_none()
    )
    if job is None or not job.is_active:
        return JSONResponse(status_code=404, content={"detail": "Job not found."})
    company_obj = job.company
    if company_obj is None:
        company_obj = db.query(Company).filter_by(id=job.company_id).one_or_none()
    company = company_obj.name_raw if company_obj else ""
    candidate = get_active_candidate(db)
    match_total = None
    if candidate is not None:
        row = (
            db.query(Match)
            .filter_by(candidate_id=candidate.id, job_id=job.id)
            .one_or_none()
        )
        match_total = row.total if row else None
    notes_lines = [f"Imported from JobSetu (job #{job.id}, search #{job.search_id})."]
    if match_total is not None:
        notes_lines.append(f"JobSetu match score: {match_total}/100.")
    if job.description:
        notes_lines.append((job.description[:500] + "…") if len(job.description) > 500 else job.description)
    logger.info("tracker export job_id=%d", job_id)
    return {
        "company": company,
        "job_title": job.title_raw or "",
        "location": job.location_raw or "",
        "job_url": safe_url(job.apply_link or ""),
        "job_type": "FULL_TIME",
        "salary": job.salary_text or "",
        "application_date": dt.date.today().isoformat(),
        "status": "SAVED",
        "notes": "\n\n".join(notes_lines),
        "jobsetu": {
            "job_id": job.id,
            "search_id": job.search_id,
            "match_total": match_total,
            "evidence_url": f"/jobs/{job.id}/evidence",
        },
    }
