from datetime import timezone

from app.sources.legacy.remotive import RemotiveProvider


def listing(location="USA"):
    return {
        "id": 123456,
        "url": "https://remotive.com/remote-jobs/finance/bookkeeper-123456",
        "title": "Remote Bookkeeper",
        "company_name": "Example Company",
        "category": "Finance / Legal",
        "tags": ["Accounting", "Bookkeeping", "QuickBooks"],
        "job_type": "full_time",
        "publication_date": "2026-09-30T14:30:00Z",
        "candidate_required_location": location,
        "salary": "$55,000-$65,000",
        "description": "<p>Maintain financial records.</p><ul><li>Reconcile accounts</li></ul>",
    }


class FakeResponse:
    def __init__(self, jobs, status_code=200):
        self._jobs = jobs
        self.status_code = status_code
        self.ok = 200 <= status_code < 300

    def json(self):
        return {"jobs": self._jobs}


def test_remotive_maps_eligible_us_job():
    job = RemotiveProvider()._to_job(listing())
    assert job is not None
    assert job.provider_job_id == "123456"
    assert job.location == "Remote — USA"
    assert job.posted_at.tzinfo == timezone.utc
    assert job.source == "remotive"
    assert job.apply_url.startswith("https://remotive.com/")
    assert "Reconcile accounts" in job.description
    assert job.metadata["remote_eligibility"] == "california"


def test_remotive_accepts_california_worldwide_and_north_america():
    provider = RemotiveProvider()
    assert provider._to_job(listing("California, USA")) is not None
    assert provider._to_job(listing("Worldwide")) is not None
    assert provider._to_job(listing("North America")) is not None


def test_remotive_rejects_geographically_restricted_job():
    provider = RemotiveProvider()
    assert provider._to_job(listing("Europe only")) is None
    assert provider._to_job(listing("United Kingdom")) is None
    assert provider._to_job(listing("Canada only")) is None
    assert provider._to_job(listing("")) is None


def test_remotive_search_filters_and_deduplicates(monkeypatch):
    provider = RemotiveProvider()
    eligible = listing()
    duplicate = dict(eligible)
    restricted = listing("Europe only")
    restricted["id"] = 999
    monkeypatch.setattr(
        provider.session,
        "get",
        lambda *args, **kwargs: FakeResponse([eligible, duplicate, restricted]),
    )
    jobs = provider.search(
        role="bookkeeper", location="Arnold, CA", results_per_page=25
    )
    assert len(jobs) == 1
    assert jobs[0].provider_job_id == "123456"


def test_remotive_search_honors_results_limit(monkeypatch):
    provider = RemotiveProvider()
    first = listing()
    second = listing()
    second["id"] = 654321
    second["title"] = "Accounting Assistant"
    monkeypatch.setattr(
        provider.session,
        "get",
        lambda *args, **kwargs: FakeResponse([first, second]),
    )
    jobs = provider.search(
        role="accounting", location="Avery, CA", results_per_page=1
    )
    assert len(jobs) == 1


def test_remotive_filters_role_locally_and_caches_feed(monkeypatch):
    provider = RemotiveProvider()
    accounting = listing()
    developer = listing()
    developer["id"] = 777
    developer["title"] = "Python Developer"
    developer["tags"] = ["Python", "Software Development"]
    calls = []

    def fake_get(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeResponse([accounting, developer])

    monkeypatch.setattr(provider.session, "get", fake_get)

    accounting_jobs = provider.search(
        role="accounting", location="Arnold, CA", results_per_page=25
    )
    developer_jobs = provider.search(
        role="python developer", location="Arnold, CA", results_per_page=25
    )

    assert [job.provider_job_id for job in accounting_jobs] == ["123456"]
    assert [job.provider_job_id for job in developer_jobs] == ["777"]
    assert len(calls) == 1


def test_remotive_rejects_incomplete_listing():
    item = listing()
    item["url"] = ""
    assert RemotiveProvider()._to_job(item) is None
