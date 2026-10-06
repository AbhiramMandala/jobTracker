"""Deterministic candidate↔job scoring. No LLM, no randomness.

Total 100 = Skills 50 + Title 20 + Experience 15 + Location 10 + Type 5.
Same candidate + same job always yields the same result. Missing data
produces explicit `limited` flags and neutral sub-scores, never fake precision.
"""

import logging
import re
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models import Candidate, CandidateSkill, Job, JobSkill, Match, SkillGap
from app.services.normalizer import normalize_location

logger = logging.getLogger(__name__)

WEIGHTS = {"skills": 50, "title": 20, "experience": 15, "location": 10, "type": 5}

_SENIORITY_TOKENS = {
    "senior", "sr", "lead", "junior", "jr", "fresher", "trainee",
    "entry", "principal", "staff", "associate", "i", "ii", "iii", "iv",
}
_CATEGORY_TOKENS = {
    "backend", "frontend", "fullstack", "full", "stack", "data", "devops",
    "ml", "ai", "mobile", "qa", "sde", "developer", "engineer", "scientist",
}
_SENIOR_TITLES = {"senior", "lead", "staff", "principal", "architect", "head", "manager"}
_FRESHER_MARKERS = {
    "fresher", "freshers", "entry level", "entry-level", "trainee",
    "0-1", "0 to 1", "0 – 1", "junior", "graduate", "intern", "internship",
}
_YEARS_RE = re.compile(r"(\d+)\s*(?:\+)?\s*(?:years?|yrs?|yoe)\b")


@dataclass
class ScoreResult:
    total: int
    skill_pts: int
    title_pts: int
    exp_pts: int
    loc_pts: int
    type_pts: int
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    limited: list[str] = field(default_factory=list)


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9+#]+", (text or "").lower())) - _SENIORITY_TOKENS


def score_skills(candidate_skills: set[str], job_skills: list[str]) -> tuple[int, list, list, list, list]:
    if not job_skills:
        return (
            25, [], [],
            ["skill requirements not listed — score is provisional"],
            ["skills-limited"],
        )
    matched = [s for s in job_skills if s in candidate_skills]
    missing = [s for s in job_skills if s not in candidate_skills]
    pts = round(WEIGHTS["skills"] * len(matched) / len(job_skills))
    reasons = [f"{len(matched)}/{len(job_skills)} detected skills match"]
    return pts, matched, missing, reasons, []


def score_title(role: str, title_norm: str) -> tuple[int, list[str]]:
    role_tokens = _tokens(role)
    if not role_tokens:
        return 10, ["no role preference set"]
    title_tokens = _tokens(title_norm)
    shared = role_tokens & title_tokens
    if shared:
        pts = min(WEIGHTS["title"], round(WEIGHTS["title"] * len(shared) / len(role_tokens)))
        return pts, [f"title shares: {', '.join(sorted(shared))}"]
    return 0, ["title does not overlap your preferred role"]


def _job_min_years(text: str) -> int | None:
    years = [int(m.group(1)) for m in _YEARS_RE.finditer(text or "")]
    return min(years) if years else None


def score_experience(candidate_years: float, title_norm: str, description: str) -> tuple[int, list[str], list[str]]:
    blob = f"{title_norm} {(description or '')[:2000]}".lower()
    min_years = _job_min_years(blob)
    # NOTE: raw tokens here — _tokens() strips seniority words, which would
    # make senior titles undetectable.
    raw_title_tokens = set(re.findall(r"[a-z0-9+#]+", (title_norm or "").lower()))
    senior_only = (
        bool(raw_title_tokens & _SENIOR_TITLES) or (min_years is not None and min_years >= 3)
    ) and not any(m in blob for m in _FRESHER_MARKERS)
    if min_years is not None:
        if candidate_years >= min_years:
            return 15, [f"meets stated minimum ({min_years}+ years)"], []
        if candidate_years >= min_years - 1:
            return 8, [f"close to stated minimum ({min_years}+ years)"], []
        return 3, [f"listing asks for {min_years}+ years"], []
    if senior_only:
        if candidate_years <= 1:
            return 0, ["senior-level listing"], []
        return 12, ["senior-titled but no explicit minimum"], []
    if any(m in blob for m in _FRESHER_MARKERS):
        return 15, ["mentions freshers/entry-level hiring"], []
    # No signal either way: neutral score with an explicit flag rather than
    # pretending a fresher is (or isn't) compatible.
    return 10, ["no experience requirements stated"], ["experience-unclear"]


def infer_job_type(title: str, description: str) -> str:
    blob = f"{title or ''} {(description or '')[:2000]}".lower()
    if "internship" in blob or re.search(r"\bintern\b", blob):
        return "internship"
    if "part-time" in blob or "part time" in blob:
        return "part-time"
    if "contract" in blob:
        return "contract"
    if "full-time" in blob or "full time" in blob or "fulltime" in blob:
        return "full-time"
    return ""


def job_signals(title_norm: str, description: str) -> dict:
    """Display signals for tracker cards, reusing the matcher's own keyword
    sets so classification never drifts from scoring. Experience mirrors
    score_experience: entry markers win unless the listing is senior-only
    (senior title or 3+ year minimum with no fresher markers)."""
    blob = f"{title_norm or ''} {(description or '')[:2000]}".lower()
    raw_title_tokens = set(re.findall(r"[a-z0-9+#]+", (title_norm or "").lower()))
    min_years = _job_min_years(blob)
    fresher = any(m in blob for m in _FRESHER_MARKERS)
    senior_only = (bool(raw_title_tokens & _SENIOR_TITLES)
                   or (min_years is not None and min_years >= 3)) and not fresher
    experience = "experienced" if senior_only else "entry" if fresher else ""
    return {"experience": experience, "min_years": min_years, "job_type": infer_job_type(title_norm, description)}


def _is_remote(job_loc_norm: str, title: str, description: str) -> bool:
    if "remot" in (job_loc_norm or ""):
        return True
    blob = f"{title or ''} {(description or '')[:500]}".lower()
    return "work from home" in blob or "wfh" in blob or "remote" in blob


def score_location(candidate_loc: str, job: Job) -> tuple[int, list[str], list[str]]:
    job_norm = job.location_norm or ""
    if not job_norm:
        return 5, ["job location not specified"], ["location-unknown"]
    cand_norm = normalize_location(candidate_loc or "")
    if cand_norm and cand_norm == job_norm:
        return 10, [f"location match ({job.location_raw or job_norm})"], []
    if _is_remote(job_norm, job.title_raw or "", job.description or ""):
        return 7, ["remote-friendly listing"], []
    if cand_norm:
        return 0, [f"listed in {job.location_raw or job_norm}"], []
    return 5, ["no location preference set"], ["location-unknown"]


def score_type(candidate_pref: str, job: Job) -> tuple[int, list[str], list[str]]:
    pref = (candidate_pref or "any").lower()
    if pref == "any":
        return 5, ["no employment-type preference"], []
    job_type = infer_job_type(job.title_raw or "", job.description or "")
    if not job_type:
        return 3, ["listing does not state employment type"], ["type-unknown"]
    if job_type == pref:
        return 5, [f"matches preferred type ({pref})"], []
    return 0, [f"listing is {job_type}, you prefer {pref}"], []


def score_job(
    candidate: Candidate,
    candidate_skills: set[str],
    job: Job,
    job_skills: list[str],
    role_text: str,
) -> ScoreResult:
    skill_pts, matched, missing, skill_reasons, skill_limited = score_skills(
        candidate_skills, job_skills
    )
    title_pts, title_reasons = score_title(role_text, job.title_norm or "")
    exp_pts, exp_reasons, exp_limited = score_experience(
        candidate.experience_years or 0.0, job.title_norm or "", job.description or ""
    )
    loc_pts, loc_reasons, loc_limited = score_location(candidate.location or "", job)
    type_pts, type_reasons, type_limited = score_type(
        candidate.job_type_pref or "any", job
    )
    total = skill_pts + title_pts + exp_pts + loc_pts + type_pts
    return ScoreResult(
        total=min(100, total),
        skill_pts=skill_pts,
        title_pts=title_pts,
        exp_pts=exp_pts,
        loc_pts=loc_pts,
        type_pts=type_pts,
        matched=matched,
        missing=missing,
        reasons=skill_reasons + title_reasons + exp_reasons + loc_reasons + type_reasons,
        limited=skill_limited + exp_limited + loc_limited + type_limited,
    )


def refresh_matches(
    db: Session, search_id: int, jobs: list[Job], candidate: Candidate | None
) -> list[dict]:
    """Recompute matches + skill-gap aggregate. Idempotent. Returns gaps."""
    if candidate is None or not jobs:
        return []
    cand_skills = {
        row.skill_norm
        for row in db.query(CandidateSkill).filter_by(candidate_id=candidate.id).all()
    }
    job_ids = [j.id for j in jobs]
    skill_rows = (
        db.query(JobSkill).filter(JobSkill.job_id.in_(job_ids)).order_by(JobSkill.id).all()
    )
    skills_by_job: dict[int, list[str]] = {}
    for row in skill_rows:
        skills_by_job.setdefault(row.job_id, []).append(row.skill_norm)
    role_text = candidate.preferred_role or ""
    gap_counter: Counter = Counter()
    for job in jobs:
        result = score_job(candidate, cand_skills, job, skills_by_job.get(job.id, []), role_text)
        match = db.query(Match).filter_by(candidate_id=candidate.id, job_id=job.id).one_or_none()
        if match is None:
            match = Match(candidate_id=candidate.id, job_id=job.id)
            db.add(match)
        match.total = result.total
        match.skill_pts = result.skill_pts
        match.title_pts = result.title_pts
        match.exp_pts = result.exp_pts
        match.loc_pts = result.loc_pts
        match.type_pts = result.type_pts
        match.matched_skills = result.matched
        match.missing_skills = result.missing
        match.reasons = result.reasons + [f"limited:{f}" for f in result.limited]
        for skill in result.missing:
            gap_counter[skill] += 1
    db.query(SkillGap).filter_by(search_id=search_id, candidate_id=candidate.id).delete()
    gaps = [
        {"skill": skill, "missing_in": count, "total_jobs": len(jobs)}
        for skill, count in gap_counter.most_common(8)
    ]
    for gap in gaps:
        db.add(
            SkillGap(
                search_id=search_id,
                candidate_id=candidate.id,
                skill_norm=gap["skill"],
                missing_in_count=gap["missing_in"],
                total_jobs=gap["total_jobs"],
            )
        )
    db.flush()
    logger.info("scored %d jobs for candidate %s", len(jobs), candidate.id)
    return gaps
