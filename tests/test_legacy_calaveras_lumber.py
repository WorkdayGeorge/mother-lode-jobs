import json

from app.sources.legacy.calaveras_lumber import CalaverasLumberProvider


class FakeResponse:
    def __init__(self, payload=None, text=""):
        self._payload = payload
        self.text = text

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_calaveras_lumber_reads_official_paycom_jobs(monkeypatch):
    provider = CalaverasLumberProvider()
    portal_html = '<script>var configsFromHost = {"sessionJWT":"token"};</script>'
    previews = {
        "jobPostingPreviews": [
            {
                "jobId": 117088,
                "jobTitle": "Stocker/Load Builder - Calaveras Lumber",
                "locations": "Calaveras Lumber - Angels Camp, CA 95221",
            },
            {
                "jobId": 999,
                "jobTitle": "Sales - Another Store",
                "locations": "Texas",
            },
        ]
    }
    detail = {
        "jobPosting": {
            "jobId": 117088,
            "jobTitle": "Stocker/Load Builder - Calaveras Lumber",
            "location": "Calaveras Lumber - Angels Camp, CA 95221",
            "salaryRange": "$19.00 - $22.00 Hourly",
            "jobCategory": "Calaveras & Sonora Lumber",
            "description": "<p>Help customers.</p><ul><li>Load materials</li></ul>",
            "qualifications": "<p>Forklift experience preferred.</p>",
            "positionType": "Full Time",
            "googleJobJson": json.dumps({"datePosted": "2026-09-28"}),
        }
    }

    def fake_get(url, **kwargs):
        if url == provider.PORTAL_URL:
            return FakeResponse(text=portal_html)
        assert url.endswith("/api/ats/job-postings/117088")
        assert kwargs["headers"]["Authorization"] == "token"
        return FakeResponse(detail)

    def fake_post(url, **kwargs):
        assert url.endswith("/api/ats/job-posting-previews/search")
        assert kwargs["json"]["take"] == 250
        return FakeResponse(previews)

    monkeypatch.setattr(provider.session, "get", fake_get)
    monkeypatch.setattr(provider.session, "post", fake_post)

    jobs = provider.search(role="cashier", location="Angels Camp", results_per_page=25)

    assert len(jobs) == 1
    job = jobs[0]
    assert job.provider_job_id == "117088"
    assert job.company == "Calaveras & Sonora Lumber"
    assert job.source == "calaveras_lumber"
    assert job.posted_at.isoformat() == "2026-09-28T00:00:00"
    assert job.metadata["salary"] == "$19.00 - $22.00 Hourly"
    assert job.apply_url.endswith("/jobs/117088")
    assert "Load materials" in job.description


def test_calaveras_lumber_caches_paycom_results(monkeypatch):
    provider = CalaverasLumberProvider()
    calls = {"portal": 0, "search": 0}

    def fake_get(url, **kwargs):
        calls["portal"] += 1
        return FakeResponse(
            {"sessionJWT": "token"},
            text='{"sessionJWT":"token"}',
        )

    def fake_post(url, **kwargs):
        calls["search"] += 1
        return FakeResponse({"jobPostingPreviews": []})

    monkeypatch.setattr(provider.session, "get", fake_get)
    monkeypatch.setattr(provider.session, "post", fake_post)
    provider.search(role="cashier", location="Angels Camp")
    provider.search(role="sales", location="Angels Camp")
    assert calls == {"portal": 1, "search": 1}


def test_calaveras_lumber_rejects_invalid_portal_configuration(monkeypatch):
    provider = CalaverasLumberProvider()
    monkeypatch.setattr(
        provider.session,
        "get",
        lambda *args, **kwargs: FakeResponse(text="<html></html>"),
    )

    try:
        provider._authorization_headers()
    except RuntimeError as exc:
        assert "session token" in str(exc)
    else:
        raise AssertionError("Expected invalid Paycom configuration to fail")
