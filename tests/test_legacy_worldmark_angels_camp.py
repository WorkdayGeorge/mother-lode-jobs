from datetime import timezone

from app.sources.legacy.worldmark_angels_camp import WorldMarkAngelsCampProvider


JOB_HTML = """
<script type="application/ld+json">
{
  "@context": "http://schema.org/",
  "@type": "JobPosting",
  "title": "Guest Services Associate",
  "description": "<p>Welcome guests &amp; support resort operations.</p>",
  "datePosted": "2026-09-15T07:25:20Z",
  "employmentType": "FULL_TIME",
  "validThrough": "2026-12-14T14:46:47Z",
  "identifier": {"value": "job-123"},
  "jobLocation": [{"address": {
    "addressLocality": "Angels Camp", "addressRegion": "California"
  }}]
}
</script>
"""


class Response:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


def test_worldmark_maps_official_job_and_exact_timestamp(monkeypatch):
    provider = WorldMarkAngelsCampProvider()
    monkeypatch.setattr(provider.session, "get", lambda *args, **kwargs: Response(JOB_HTML))

    job = provider._fetch_job("https://careers.travelandleisureco.com/jobs/example")

    assert job is not None
    assert job.provider_job_id == "job-123"
    assert job.location == "Angels Camp, California"
    assert job.description == "Welcome guests & support resort operations."
    assert job.posted_at is not None
    assert job.posted_at.tzinfo == timezone.utc
    assert job.metadata["date_posted"] == "2026-09-15T07:25:20Z"
    assert "Verified exact UTC" in job.metadata["timestamp_note"]


def test_worldmark_rejects_nonlocal_job(monkeypatch):
    provider = WorldMarkAngelsCampProvider()
    page = JOB_HTML.replace("Angels Camp", "Orlando")
    monkeypatch.setattr(provider.session, "get", lambda *args, **kwargs: Response(page))
    assert provider._fetch_job("https://careers.travelandleisureco.com/jobs/example") is None


def test_worldmark_listing_links_are_local_and_deduplicated():
    page = """
    <tr data-job-url="https://careers.travelandleisureco.com/jobs/local">
      <td aria-label="Location: Angels Camp, California, United States"></td>
    </tr>
    <tr data-job-url="https://careers.travelandleisureco.com/jobs/local">
      <td aria-label="Location: Angels Camp, California, United States"></td>
    </tr>
    <tr data-job-url="https://careers.travelandleisureco.com/jobs/remote">
      <td aria-label="Location: Orlando, Florida, United States"></td>
    </tr>
    """
    assert WorldMarkAngelsCampProvider._listing_links(page) == [
        "https://careers.travelandleisureco.com/jobs/local"
    ]


def test_worldmark_search_deduplicates_provider_job_ids(monkeypatch):
    provider = WorldMarkAngelsCampProvider()
    listing = """
    <tr data-job-url="https://careers.travelandleisureco.com/jobs/one">
      <td aria-label="Location: Angels Camp, California, United States"></td>
    </tr>
    <tr data-job-url="https://careers.travelandleisureco.com/jobs/two">
      <td aria-label="Location: Angels Camp, California, United States"></td>
    </tr>
    """
    monkeypatch.setattr(provider.session, "get", lambda *args, **kwargs: Response(listing))
    monkeypatch.setattr(
        provider,
        "_fetch_job",
        lambda url: type("Job", (), {"provider_job_id": "same-id"})(),
    )

    jobs = provider.search(role="hospitality", location="Angels Camp, CA")
    assert len(jobs) == 1
