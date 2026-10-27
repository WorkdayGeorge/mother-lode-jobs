from datetime import timezone

import pytest

from app.sources.legacy.usajobs import USAJobsProvider


def descriptor(location="Sonora, California", latitude=37.9841, longitude=-120.3821):
    return {
        "MatchedObjectId": "123456",
        "MatchedObjectDescriptor": {
            "PositionID": "25-TEMP-R5-123456",
            "PositionTitle": "Administrative Support Assistant",
            "PositionURI": "https://www.usajobs.gov/job/123456",
            "PositionLocationDisplay": location,
            "PositionLocation": [{
                "LocationName": location,
                "Latitude": latitude,
                "Longitude": longitude,
            }],
            "OrganizationName": "Forest Service",
            "DepartmentName": "Department of Agriculture",
            "PositionSchedule": [{"Name": "Full-time", "Code": "1"}],
            "JobCategory": [{"Name": "Miscellaneous Clerk", "Code": "0303"}],
            "JobGrade": [{"Code": "GS"}],
            "QualificationSummary": "One year of specialized experience.",
            "PublicationStartDate": "2026-09-15T14:30:00Z",
            "ApplicationCloseDate": "2026-09-22T23:59:59Z",
            "UserArea": {"Details": {
                "SubAgencyName": "Forest Service",
                "JobSummary": "Support a forest office.",
                "MajorDuties": "Maintain records.",
                "Requirements": "U.S. citizenship required.",
                "Education": "See announcement.",
                "OrganizationCodes": "AG/AG11",
                "WhoMayApply": {"Name": "The public"},
            }},
        },
    }


def test_usajobs_maps_forest_service_job(monkeypatch):
    monkeypatch.setenv("USAJOBS_API_KEY", "test-key")
    monkeypatch.setenv("USAJOBS_USER_AGENT", "test@example.com")
    provider = USAJobsProvider()
    job = provider._to_job(descriptor())

    assert job is not None
    assert job.provider_job_id == "25-TEMP-R5-123456"
    assert job.company == "Forest Service"
    assert job.location == "Sonora, California"
    assert job.posted_at.tzinfo == timezone.utc
    assert job.source == "usajobs"
    assert "Maintain records" in job.description


def test_usajobs_midnight_timestamp_is_unverified(monkeypatch):
    monkeypatch.setenv("USAJOBS_API_KEY", "test-key")
    monkeypatch.setenv("USAJOBS_USER_AGENT", "test@example.com")
    item = descriptor()
    item["MatchedObjectDescriptor"]["PublicationStartDate"] = (
        "2026-09-15T00:00:00Z"
    )
    job = USAJobsProvider()._to_job(item)
    assert job is not None
    assert job.posted_at is None
    assert "unverified" in job.metadata["timestamp_note"]


def test_usajobs_rejects_nonlocal_job(monkeypatch):
    monkeypatch.setenv("USAJOBS_API_KEY", "test-key")
    monkeypatch.setenv("USAJOBS_USER_AGENT", "test@example.com")
    item = descriptor("Los Angeles, California", 34.0522, -118.2437)
    assert USAJobsProvider()._to_job(item) is None


def test_usajobs_requires_credentials(monkeypatch):
    monkeypatch.delenv("USAJOBS_API_KEY", raising=False)
    monkeypatch.delenv("USAJOBS_USER_AGENT", raising=False)
    provider = USAJobsProvider()
    with pytest.raises(RuntimeError, match="must be configured"):
        provider._headers()


def test_usajobs_search_deduplicates(monkeypatch):
    monkeypatch.setenv("USAJOBS_API_KEY", "test-key")
    monkeypatch.setenv("USAJOBS_USER_AGENT", "test@example.com")
    provider = USAJobsProvider()
    item = descriptor()
    monkeypatch.setattr(
        provider,
        "_search_page",
        lambda page: {
            "SearchResult": {
                "SearchResultItems": [item, item],
                "UserArea": {"NumberOfPages": "1"},
            }
        },
    )
    jobs = provider.search(role="administrative", location="Calaveras County")
    assert len(jobs) == 1
