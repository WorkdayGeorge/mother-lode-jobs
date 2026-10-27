from datetime import timezone

from app.sources.legacy.adventist_health import AdventistHealthProvider


def test_adventist_detail_maps_verified_timestamp():
    provider = AdventistHealthProvider()
    job = provider._to_job(
        {"Id": "72005", "PrimaryLocation": "Sonora, CA, United States"},
        {
            "Id": "72005",
            "Title": "Accounting Clerk, Full-time",
            "PrimaryLocation": "Sonora, CA, United States",
            "ExternalPostedStartDate": "2026-09-10T22:57:24+00:00",
            "ExternalDescriptionStr": "<p>Process invoices &amp; records.</p>",
            "RequisitionId": 300002053862247,
        },
    )

    assert job is not None
    assert job.provider_job_id == "72005"
    assert job.source == "adventist_health"
    assert job.description == "Process invoices & records."
    assert job.posted_at.tzinfo == timezone.utc
    assert job.apply_url.endswith("/job/72005")


def test_adventist_rejects_nonlocal_job():
    provider = AdventistHealthProvider()
    job = provider._to_job(
        {"Id": "1"},
        {
            "Id": "1",
            "Title": "Accounting Clerk",
            "PrimaryLocation": "Portland, OR, United States",
        },
    )
    assert job is None


def test_adventist_search_deduplicates_requisitions(monkeypatch):
    provider = AdventistHealthProvider()
    pages = [
        {
            "TotalJobsCount": 2,
            "requisitionList": [
                {"Id": "10", "PrimaryLocation": "Sonora, CA, United States"},
                {"Id": "10", "PrimaryLocation": "Sonora, CA, United States"},
            ],
        }
    ]
    monkeypatch.setattr(provider, "_search_page", lambda offset, limit: pages[0])
    monkeypatch.setattr(
        provider,
        "_fetch_detail",
        lambda job_id: {
            "Id": job_id,
            "Title": "Administrative Assistant",
            "PrimaryLocation": "Sonora, CA, United States",
        },
    )

    jobs = provider.search(
        role="administrative assistant",
        location="Calaveras County, CA",
    )
    assert len(jobs) == 1
