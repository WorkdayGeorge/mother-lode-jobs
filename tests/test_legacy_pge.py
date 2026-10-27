from app.sources.legacy.pge import PGEProvider


JOB_HTML = """
<script type="application/ld+json">
{
  "@context": "http://schema.org",
  "@type": "JobPosting",
  "datePosted": "2026-9-11",
  "description": "<p>Supports field operations &amp; reporting.</p>",
  "employmentType": "Full-time",
  "identifier": "174434-en_US",
  "title": "Field Safety Specialist",
  "url": "https://jobs.pge.com/job/auburn/field-safety/29673/100",
  "hiringOrganization": {"name": "PG&E Corporation"},
  "jobLocation": [{"address": {
    "addressLocality": "Auburn", "addressRegion": "CA"
  }}]
}
</script>
"""


class Response:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


def test_pge_job_maps_date_as_unverified(monkeypatch):
    provider = PGEProvider()
    monkeypatch.setattr(provider.session, "get", lambda *args, **kwargs: Response(JOB_HTML))

    job = provider._fetch_job("https://jobs.pge.com/job/auburn/example")

    assert job is not None
    assert job.provider_job_id == "174434-en_US"
    assert job.location == "Auburn, CA"
    assert job.description == "Supports field operations & reporting."
    assert job.posted_at is None
    assert job.metadata["date_posted"] == "2026-9-11"
    assert "unverified" in job.metadata["timestamp_note"]


def test_pge_rejects_nonlocal_job(monkeypatch):
    provider = PGEProvider()
    page = JOB_HTML.replace("Auburn", "Oakland")
    monkeypatch.setattr(provider.session, "get", lambda *args, **kwargs: Response(page))
    assert provider._fetch_job("https://jobs.pge.com/job/oakland/example") is None


def test_pge_listing_links_include_location():
    page = """
    <a class="search-results-list__job-link" href="/job/auburn/a/29673/100">A</a>
    <li class="search-results-list__job-info job-location"> Auburn, CA </li>
    """
    assert PGEProvider._listing_links(page) == [
        ("/job/auburn/a/29673/100", "Auburn, CA")
    ]


def test_pge_search_deduplicates_links(monkeypatch):
    provider = PGEProvider()
    listing = """
    <section data-total-pages="1"></section>
    <a class="search-results-list__job-link" href="/job/auburn/a/29673/100">A</a>
    <li class="search-results-list__job-info job-location">Auburn, CA</li>
    <a class="search-results-list__job-link" href="/job/auburn/a/29673/100">A</a>
    <li class="search-results-list__job-info job-location">Auburn, CA</li>
    """
    monkeypatch.setattr(provider.session, "get", lambda *args, **kwargs: Response(listing))
    monkeypatch.setattr(
        provider,
        "_fetch_job",
        lambda url: type("Job", (), {"apply_url": url})(),
    )

    jobs = provider.search(role="accounting", location="Calaveras County, CA")
    assert len(jobs) == 1
