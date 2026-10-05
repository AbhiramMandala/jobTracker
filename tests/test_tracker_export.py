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


def test_results_page_exposes_tracker_config(client, monkeypatch):
    """Results HTML carries the search ID and the Tracker API default."""
    from app.services import serpapi_client as client_module
    from tests._fixtures import EMPTY, PAGE_1

    def fake_google_jobs(self, q, location, gl="in", hl="en", next_page_token=""):
        return PAGE_1 if not next_page_token else EMPTY

    monkeypatch.setattr(client_module.SerpApiClient, "google_jobs", fake_google_jobs)
    res = client.post(
        "/search",
        data={"role": "Python Backend Developer", "location": "Hyderabad",
              "experience": "Fresher"},
    )
    assert res.status_code == 200
    assert "Search #" in res.text
    assert "data-tracker-save" in res.text
    assert "http://127.0.0.1:8787" in res.text  # TRACKER_API_URL default
