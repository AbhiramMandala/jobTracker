"""Tracker integration API: job list + Tracker-shaped export."""

from app.models import Company, Job, Search


def _seed(client):
    from app.database import SessionLocal

    db = SessionLocal()
    search = Search(role="Python Backend Developer", location="Hyderabad",
                    experience="Fresher", query_hash="tracker-test-hash")
    db.add(search)
    db.flush()
    company = Company(name_raw="Acme Technologies", name_norm="acme tech")
    db.add(company)
    db.flush()
    job = Job(
        search_id=search.id,
        company_id=company.id,
        title_raw="Python Backend Developer",
        title_norm="python backend developer",
        location_raw="Hyderabad",
        location_norm="hyderabad",
        apply_link="https://example.com/apply/1",
        description="Build APIs with FastAPI.",
        salary_text="₹6 LPA",
        posted_text="2 days ago",
        source_key="tracker-seed-key-1",
    )
    db.add(job)
    db.commit()
    search_id, job_id = search.id, job.id
    db.close()
    return search_id, job_id


def test_list_jobs(client):
    search_id, job_id = _seed(client)
    res = client.get(f"/api/jobs?search_id={search_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["search_id"] == search_id
    assert len(body["jobs"]) == 1
    card = body["jobs"][0]
    assert card["id"] == job_id
    assert card["company"] == "Acme Technologies"
    assert card["title"] == "Python Backend Developer"
    assert card["apply_link"] == "https://example.com/apply/1"


def test_list_jobs_unknown_search(client):
    res = client.get("/api/jobs?search_id=999999")
    assert res.status_code == 404


def test_tracker_export_shape(client):
    _, job_id = _seed(client)
    res = client.get(f"/api/jobs/{job_id}/tracker-export")
    assert res.status_code == 200
    body = res.json()
    # Must match POST /api/applications fields on the Tracker Worker.
    assert body["company"] == "Acme Technologies"
    assert body["job_title"] == "Python Backend Developer"
    assert body["location"] == "Hyderabad"
    assert body["job_url"] == "https://example.com/apply/1"
    assert body["status"] == "SAVED"
    assert body["job_type"] == "FULL_TIME"
    assert "JobSetu" in body["notes"]
    assert body["jobsetu"]["job_id"] == job_id


def test_tracker_export_unknown_job(client):
    res = client.get("/api/jobs/999999/tracker-export")
    assert res.status_code == 404


def test_list_searches(client):
    search_id, _ = _seed(client)
    res = client.get("/api/searches")
    assert res.status_code == 200
    body = res.json()
    assert len(body["searches"]) >= 1
    entry = next(s for s in body["searches"] if s["id"] == search_id)
    assert entry["role"] == "Python Backend Developer"
    assert entry["location"] == "Hyderabad"
    assert entry["job_count"] == 1
