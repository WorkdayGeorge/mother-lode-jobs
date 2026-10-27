import json

from app.sources.legacy.greenhorn_creek import GreenhornCreekProvider


class Response:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


def test_greenhorn_maps_harri_job_and_skips_expired(monkeypatch):
    company = '''
      <a href="/Yad-BmDiBaycxfQT/job/2707158-line-cook">view details</a>
      <a href="/Yad-BmDiBaycxfQT/job/2707174-facility-maintenance">view details</a>
    '''
    posting = {
        "@type": "JobPosting",
        "title": "Line Cook",
        "description": "<p>Prepare menu items.</p>",
        "datePosted": "2026-09-30T15:00:00Z",
        "employmentType": ["FULL_TIME", "PART_TIME"],
        "jobLocation": {
            "address": {"addressLocality": "Angels Camp", "addressRegion": "CA"}
        },
    }
    detail = f'<script type="application/ld+json">{json.dumps(posting)}</script>'
    provider = GreenhornCreekProvider()

    def get(url, **kwargs):
        if url == provider.COMPANY_URL:
            return Response(company)
        if "2707174" in url:
            return Response("expired job post")
        return Response(detail)

    monkeypatch.setattr(provider.session, "get", get)
    jobs = provider.search(role="cook", location="Calaveras")

    assert len(jobs) == 1
    assert jobs[0].provider_job_id == "2707158"
    assert jobs[0].employment_type == "FULL_TIME, PART_TIME"
    assert jobs[0].location == "Angels Camp, CA"
    assert jobs[0].source == "greenhorn_creek"


def test_greenhorn_uses_sitemap_when_company_page_is_dynamic(monkeypatch):
    provider = GreenhornCreekProvider()
    sitemap = '''<urlset>
      <url><loc>https://harri.com/Yad-BmDiBaycxfQT/job/2707158-line-cook</loc></url>
      <url><loc>https://harri.com/AnotherEmployer/job/999-other-job</loc></url>
    </urlset>'''
    posting = {"@type": "JobPosting", "title": "Line Cook"}

    def get(url, **kwargs):
        if url == provider.COMPANY_URL:
            return Response("<html><body>JavaScript application</body></html>")
        if url == provider.SITEMAP_URL:
            return Response(sitemap)
        return Response(
            f'<script type="application/ld+json">{json.dumps(posting)}</script>'
        )

    monkeypatch.setattr(provider.session, "get", get)
    jobs = provider.search(role="", location="")
    assert [job.provider_job_id for job in jobs] == ["2707158"]


def test_greenhorn_reads_indexed_sitemap_and_accepts_www(monkeypatch):
    provider = GreenhornCreekProvider()
    sitemap_index = '''<sitemapindex>
      <sitemap><loc>https://harri.com/jobs-sitemap.xml</loc></sitemap>
    </sitemapindex>'''
    child = '''<urlset>
      <url><loc>https://www.harri.com/Yad-BmDiBaycxfQT/job/2707164-pro-shop</loc></url>
    </urlset>'''
    posting = {"@type": "JobPosting", "title": "Pro-Shop Attendant"}

    def get(url, **kwargs):
        if url == provider.COMPANY_URL:
            return Response("dynamic")
        if url == provider.SITEMAP_URL:
            return Response(sitemap_index)
        if url.endswith("jobs-sitemap.xml"):
            return Response(child)
        return Response(
            f'<script type="application/ld+json">{json.dumps(posting)}</script>'
        )

    monkeypatch.setattr(provider.session, "get", get)
    jobs = provider.search(role="", location="")
    assert [job.provider_job_id for job in jobs] == ["2707164"]


def test_greenhorn_deduplicates_company_links(monkeypatch):
    company = '''
      <a href="/Yad-BmDiBaycxfQT/job/2707158-line-cook">details</a>
      <a href="/Yad-BmDiBaycxfQT/job/2707158-line-cook">apply</a>
    '''
    posting = {"@type": "JobPosting", "title": "Line Cook"}
    provider = GreenhornCreekProvider()

    def get(url, **kwargs):
        return Response(
            company
            if url == provider.COMPANY_URL
            else f'<script type="application/ld+json">{json.dumps(posting)}</script>'
        )

    monkeypatch.setattr(provider.session, "get", get)
    assert len(provider.search(role="", location="", results_per_page=25)) == 1
