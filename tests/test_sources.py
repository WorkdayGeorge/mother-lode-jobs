from datetime import date

import pytest

from app.region import locate
from app.sources import Http, SourceError, detect
from app.sources.base import ConfigError, iter_job_postings, parse_date
from app.sources.platforms import (
    AdpAdapter,
    ApplicantProAdapter,
    EdJoinAdapter,
    JsonLdAdapter,
    NeogovAdapter,
    PageWatchAdapter,
)

from helpers import FakeSession

NEOGOV_XML = """<?xml version="1.0"?>
<rss xmlns:joblisting="http://www.neogov.com/namespaces/JobListing"><channel>
<item>
  <title>Office Assistant II</title>
  <link>https://www.governmentjobs.com/careers/calaverascounty/jobs/123</link>
  <description>&lt;p&gt;Answer phones&lt;/p&gt;</description>
  <joblisting:jobId>123</joblisting:jobId>
  <joblisting:location>San Andreas</joblisting:location>
  <joblisting:jobType>Full Time</joblisting:jobType>
  <joblisting:minimumSalary>19.85</joblisting:minimumSalary>
  <joblisting:maximumSalary>24.13</joblisting:maximumSalary>
  <joblisting:salaryInterval>Hourly</joblisting:salaryInterval>
  <joblisting:advertiseFromDateUTC>2026-10-20T00:00:00Z</joblisting:advertiseFromDateUTC>
  <joblisting:qualifications>&lt;li&gt;Typing&lt;/li&gt;</joblisting:qualifications>
</item>
</channel></rss>"""


def adapter(cls, config, routes, **kw):
    return cls(config, source_name=kw.get("name", "Calaveras County"), county=kw.get("county"),
               http=Http(FakeSession(routes), pause=0))


def test_neogov_reads_feed():
    jobs = adapter(NeogovAdapter, {"agency": "calaverascounty"}, {"JobsFeed": NEOGOV_XML}).fetch().jobs
    assert len(jobs) == 1
    job = jobs[0]
    assert job.external_id == "123"
    assert job.location == "San Andreas, CA"
    assert job.salary_text == "$19.85 – $24.13 per hour"
    assert job.posted_on == date(2026, 10, 20)
    assert "Answer phones" in job.description and "Typing" in job.description


def test_missing_required_setting_is_a_config_error():
    with pytest.raises(ConfigError):
        adapter(NeogovAdapter, {}, {})


def test_http_error_becomes_source_error():
    with pytest.raises(SourceError):
        adapter(NeogovAdapter, {"agency": "x"}, {}).fetch()


def test_edjoin_reads_each_district():
    payload = {
        "totalPages": 1,
        "data": [{
            "postingID": 555, "positionTitle": "Paraeducator", "districtName": "Bret Harte Union High School District",
            "city": "Angels Camp", "JobSummary": "Help students", "salaryInfo": "$18 - $22 per hour",
            "FullTimePartTime": "Part Time", "postingDate": "/Date(1792540800000)/",
        }],
    }

    edjoin = adapter(EdJoinAdapter, {"districts": [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]},
                     {"LoadJobsPortalList": payload})
    jobs = edjoin.fetch().jobs
    assert len(jobs) == 1  # same posting id from both districts is de-duplicated
    assert jobs[0].employer == "Bret Harte Union High School District"
    assert jobs[0].apply_url.endswith("/JobPosting/555")
    assert jobs[0].posted_on is not None


def test_adp_filters_to_local_terms():
    listing = {"meta": {"totalNumber": 2}, "jobRequisitions": [{"itemID": "1"}, {"itemID": "2"}]}
    details = {
        "1": {"requisitionTitle": "Lift Operator", "requisitionLocations": [{"address": {"cityName": "Bear Valley", "countrySubdivisionLevel1": {"codeValue": "CA"}}}],
              "payGradeRange": {"minimumRate": {"amountValue": 18}}, "postDate": "2026-10-01T10:00:00Z"},
        "2": {"requisitionTitle": "Accountant", "requisitionLocations": [{"address": {"cityName": "Reno", "countrySubdivisionLevel1": {"codeValue": "NV"}}}]},
    }

    def route(url, params):
        job_id = url.rsplit("/", 1)[-1]
        return details[job_id] if job_id in details else listing

    adp = adapter(AdpAdapter, {"cid": "abc", "local_terms": ["bear valley"]}, {"job-requisitions": route})
    jobs = adp.fetch().jobs
    assert [j.title for j in jobs] == ["Lift Operator"]
    assert "cid=abc" in jobs[0].apply_url and "jobId=1" in jobs[0].apply_url
    assert jobs[0].salary_text == "$18"


def test_applicantpro_discovers_board_id():
    routes = {
        "/core/jobs/7133/9/job-details": {"success": True, "data": {"description": "<p>Help families</p>"}},
        "/core/jobs/7133": {"success": True, "data": {"jobs": [
            {"id": 9, "title": "Case Manager", "jobUrl": "https://trcac.applicantpro.com/jobs/9", "jobLocation": "San Andreas, CA"}
        ]}},
        "applicantpro.com/jobs/": '<script src="/core/jobs/7133/list"></script>',
    }
    jobs = adapter(ApplicantProAdapter, {"subdomain": "trcac"}, routes).fetch().jobs
    assert jobs[0].title == "Case Manager"
    assert jobs[0].description == "Help families"


JSONLD_PAGE = """<html><head>
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[
 {"@type":"JobPosting","title":"Winery Cellar Worker","identifier":{"value":"W-1"},
  "hiringOrganization":{"name":"Murphys Cellars"},"datePosted":"2026-10-10",
  "jobLocation":{"address":{"addressLocality":"Murphys","addressRegion":"CA"}},
  "baseSalary":{"value":{"minValue":20,"maxValue":23,"unitText":"HOUR"}},
  "description":"<p>Help with harvest</p>"}]}
</script></head><body><a href="/jobs/2">Other</a></body></html>"""


def test_jsonld_reads_embedded_postings_and_follows_links():
    other = JSONLD_PAGE.replace("W-1", "W-2").replace("Winery Cellar Worker", "Tasting Room Host")
    routes = {"example.com/jobs/2": other, "example.com/careers": JSONLD_PAGE}
    jobs = adapter(JsonLdAdapter, {"url": "https://example.com/careers", "link_pattern": "/jobs/"}, routes).fetch().jobs
    assert {j.title for j in jobs} == {"Winery Cellar Worker", "Tasting Room Host"}
    first = next(j for j in jobs if j.external_id == "W-1")
    assert first.location == "Murphys, CA"
    assert first.salary_text == "$20 – $23 per hour"


def test_iter_job_postings_ignores_bad_json():
    assert iter_job_postings('<script type="application/ld+json">{oops</script>') == []


def test_page_watch_fingerprint_ignores_years():
    a = adapter(PageWatchAdapter, {"url": "https://x.org"}, {"x.org": "<p>Now hiring © 2025</p>"}).fetch()
    b = adapter(PageWatchAdapter, {"url": "https://x.org"}, {"x.org": "<p>Now hiring © 2026</p>"}).fetch()
    c = adapter(PageWatchAdapter, {"url": "https://x.org"}, {"x.org": "<p>Hiring a ranch hand</p>"}).fetch()
    assert a.page_fingerprint == b.page_fingerprint != c.page_fingerprint


@pytest.mark.parametrize("url, adapter_key, config", [
    ("https://www.governmentjobs.com/careers/amadorgov?page=2", "neogov", {"agency": "amadorgov"}),
    ("https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=abc-123&ccId=1900", "adp", {"cid": "abc-123", "ccid": "1900"}),
    ("https://trcac.applicantpro.com/jobs/", "applicantpro", {"subdomain": "trcac"}),
    ("https://apploi.click/golden-career-page", "apploi", {"url": "https://apploi.click/golden-career-page"}),
    ("https://www.edjoin.org/Home/Jobs?districtID=5063", "edjoin", None),
    ("https://calcareers.ca.gov/", "custom", {"scraper": "calcareers"}),
    ("https://www.ccwd.org/job-opportunities", "custom", {"scraper": "ccwd"}),
    ("https://example-winery.com/jobs", "jsonld", {"url": "https://example-winery.com/jobs"}),
])
def test_detect(url, adapter_key, config):
    found = detect(url)
    assert found.adapter == adapter_key
    if config is not None:
        assert found.config == config


def test_detect_edjoin_district():
    assert detect("https://www.edjoin.org/Home/Jobs?districtID=5063").config["districts"][0]["id"] == 5063


@pytest.mark.parametrize("value, expected", [
    ("2026-9-11", date(2026, 9, 11)),
    ("2026-10-20T00:00:00Z", date(2026, 10, 20)),
    ("10/05/2026", date(2026, 10, 5)),
    ("05-Oct-2026", date(2026, 10, 5)),
    ("not a date", None),
])
def test_parse_date(value, expected):
    assert parse_date(value) == expected


@pytest.mark.parametrize("text, hint, town, county, remote", [
    ("San Andreas, CA", None, "San Andreas", "Calaveras County", False),
    ("Jackson, MS", None, None, None, False),
    ("Jackson", "Amador County", "Jackson", "Amador County", False),
    ("Tuolumne County, CA", None, None, "Tuolumne County", False),
    ("Tuolumne, CA", None, "Tuolumne", "Tuolumne County", False),
    ("Mi Wuk Village, California", None, "Mi-Wuk Village", "Tuolumne County", False),
    ("Remote — USA", None, None, None, True),
    ("Remote - Germany", None, None, None, False),
    ("Fresno, CA", "Calaveras County", None, None, False),
    ("", "Calaveras County", None, "Calaveras County", False),
])
def test_locate(text, hint, town, county, remote):
    place = locate(text, county_hint=hint)
    assert (place.town, place.county, place.is_remote) == (town, county, remote)
