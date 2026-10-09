"""Experience-level + job-type filters on the search form and results page."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.routes.search import JOB_TYPES, _clean_job_type

HARNESS = Path(__file__).with_name("filter_harness.js")
TEMPLATE = Path(__file__).parent.parent / "app" / "templates" / "results.html"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node required")


def _search(client, monkeypatch, **form):
    from app.services import serpapi_client as client_module
    from tests._fixtures import EMPTY, PAGE_1

    def fake_google_jobs(self, q, location, gl="in", hl="en", next_page_token=""):
        return PAGE_1 if not next_page_token else EMPTY

    monkeypatch.setattr(client_module.SerpApiClient, "google_jobs", fake_google_jobs)
    data = {"role": "Dev", "location": "Hyderabad", "experience": "Fresher"}
    data.update(form)
    return client.post("/search", data=data)


def test_clean_job_type_allowlists():
    assert _clean_job_type("contract") == "contract"
    assert _clean_job_type("FullTime") == "fulltime"
    assert _clean_job_type("bogus") == "any"
    assert _clean_job_type(None) == "any"
    assert set(JOB_TYPES) == {"any", "fulltime", "contract", "parttime", "internship"}


def test_landing_form_has_new_filters(client):
    html = client.get("/").text
    assert 'name="job_type"' in html
    assert "Entry-level (0–1 years)" in html
    assert "Experienced (2+ years)" in html
    assert "Full-time" in html and "Contract" in html and "Part-time" in html


def test_results_preselects_job_type(client, monkeypatch):
    html = _search(client, monkeypatch, job_type="contract").text
    assert 'id="filter-jobtype"' in html
    assert 'id="filter-exp"' in html
    assert '<option value="contract" selected>Contract</option>' in html


def test_results_rejects_bad_job_type(client, monkeypatch):
    res = _search(client, monkeypatch, job_type="ceo")
    assert res.status_code == 200
    assert '<option value="any" selected>Any type</option>' in res.text


def test_new_experience_values_accepted(client, monkeypatch):
    for experience in ("Fresher", "Entry-level (0–1 years)", "Experienced (2+ years)"):
        res = _search(client, monkeypatch, experience=experience)
        assert res.status_code == 200, experience


@needs_node
def test_filter_counts_match_visible_cards(client, monkeypatch, tmp_path):
    """Run the shipped filter JS against real rendered HTML: every option's
    count label must equal the cards actually visible when only it is set,
    and the neutral state must show everything."""
    html = _search(client, monkeypatch, job_type="contract").text
    page = tmp_path / "results.html"
    page.write_text(html, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(HARNESS), str(TEMPLATE), str(page)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["neutral"] == report["total"] > 0
    for select_id, options in report["options"].items():
        assert options, f"{select_id} has no options"
        for value, label_count in options:
            assert label_count is not None, f"{select_id} option missing count"
            assert report["single"][f"{select_id}:{value}"] == label_count, select_id
