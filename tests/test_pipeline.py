from datetime import timedelta

from sqlalchemy import select

from app.clock import local_today

from app import pipeline
from app.models import Job, Source, SourceRun, utcnow
from app.sources import FetchResult, SourceError

from helpers import make_source, raw, result


def apply(session, source, *jobs):
    run = SourceRun(source_id=source.id)
    session.add(run)
    pipeline.apply_result(session, source, result(*jobs), run)
    session.commit()
    return run


def open_titles(session):
    return sorted(session.scalars(select(Job.title).where(Job.status == "open")))


def test_new_jobs_are_added_and_normalised(session):
    source = make_source(session)
    run = apply(session, source, raw(1), raw(2, location="Fresno, CA"))
    assert (run.jobs_found, run.jobs_kept, run.jobs_new) == (2, 1, 1)
    job = session.scalar(select(Job))
    assert job.employer == "Calaveras County"  # single-employer source names the employer
    assert job.town == "San Andreas" and job.county == "Calaveras County"
    assert job.employment_type == "Full-time"
    assert (job.salary_min, job.salary_max, job.salary_unit) == (20.0, 24.0, "hour")
    assert job.category == "Office & Administrative"
    assert job.slug == "office-assistant-1-calaveras-county"


def test_jobs_that_disappear_are_closed_and_can_reopen(session):
    source = make_source(session)
    apply(session, source, raw(1), raw(2))
    run = apply(session, source, raw(1))
    assert run.jobs_closed == 1
    assert open_titles(session) == ["Office Assistant 1"]
    apply(session, source, raw(1), raw(2))
    assert open_titles(session) == ["Office Assistant 1", "Office Assistant 2"]


def test_sudden_empty_result_does_not_close_everything(session):
    source = make_source(session)
    apply(session, source, raw(1), raw(2))
    first_empty = apply(session, source)
    assert first_empty.jobs_closed == 0
    assert len(open_titles(session)) == 2
    second_empty = apply(session, source)
    assert second_empty.jobs_closed == 2
    assert source.health == "ok"  # it only warns after 3 empty runs


def test_health_states(session):
    source = make_source(session)
    assert source.health == "new"
    apply(session, source, raw(1))
    assert source.health == "ok"
    pipeline._mark_failure(source, "boom", utcnow())
    assert source.health == "warning"
    pipeline._mark_failure(source, "boom", utcnow())
    assert source.health == "failing"
    source.active = False
    assert source.health == "paused"


def test_run_sources_records_failures_without_stopping(session, monkeypatch):
    good = make_source(session, key="good")
    bad = make_source(session, key="bad", name="Broken")

    def fake_fetch(source):
        return SourceError("site down") if source.key == "bad" else FetchResult([raw(1)])

    monkeypatch.setattr(pipeline, "_fetch", fake_fetch)
    summary = pipeline.run_sources(session)
    assert summary["ok"] == 1 and summary["failed"] == 1
    assert summary["errors"]["bad"] == "site down"
    session.refresh(bad)
    assert bad.consecutive_failures == 1 and bad.last_error == "site down"
    assert session.scalar(select(Job).where(Job.source_id == good.id)) is not None


def test_multi_employer_source_keeps_each_employer(session):
    source = make_source(session, adapter="edjoin", config={"districts": [{"id": 1}]}, name="Schools")
    apply(session, source, raw(1, employer="Vallecito Union School District", location=""))
    job = session.scalar(select(Job))
    assert job.employer == "Vallecito Union School District"
    assert job.county == "Calaveras County"  # from the source's county


def test_remote_source_only_keeps_california_eligible(session):
    source = make_source(session, key="remote", kind="remote", adapter="custom",
                         config={"scraper": "remotive"}, county=None)
    apply(session, source, raw(1, location="Remote — USA", employer="Acme"),
          raw(2, location="Remote — Europe", employer="Acme"))
    jobs = session.scalars(select(Job)).all()
    assert len(jobs) == 1 and jobs[0].is_remote and jobs[0].work_arrangement == "remote"


def test_aggregator_copies_are_hidden(session):
    direct = make_source(session)
    agg = make_source(session, key="adzuna", name="Adzuna", kind="aggregator", adapter="adzuna",
                      config={"where": ["x"]}, county=None)
    apply(session, direct, raw(1, title="Office Assistant II"))
    apply(session, agg, raw("a", title="Office Assistant II", employer="Calaveras County", location="San Andreas, CA"),
          raw("b", title="Barista", employer="Twain Harte Coffee", location="Twain Harte, CA"))
    assert pipeline.hide_aggregator_duplicates(session) == 1
    hidden = session.scalar(select(Job).where(Job.status == "hidden"))
    assert hidden.external_id == "a" and hidden.duplicate_of_id is not None


def test_jobs_close_after_their_deadline(session):
    source = make_source(session)
    apply(session, source, raw(1, closes_on=local_today() - timedelta(days=1)), raw(2))
    assert pipeline.close_past_deadline(session) == 1
    assert open_titles(session) == ["Office Assistant 2"]


def test_listed_on_uses_believable_posting_dates(session):
    source = make_source(session)
    apply(session, source, raw(1, posted_on=local_today() - timedelta(days=3)),
          raw(2, posted_on=local_today() - timedelta(days=400)))
    jobs = {j.external_id: j for j in session.scalars(select(Job))}
    assert jobs["1"].listed_on == local_today() - timedelta(days=3)
    assert jobs["2"].listed_on == local_today()


def test_page_watch_flags_changes(session):
    source = make_source(session, adapter="page_watch", config={"url": "https://x"})
    run = SourceRun(source_id=source.id)
    pipeline.apply_result(session, source, FetchResult([], page_fingerprint="a"), run)
    assert source.page_changed_at is None  # first look just records it
    pipeline.apply_result(session, source, FetchResult([], page_fingerprint="b"), run)
    assert source.health == "changed"


def test_seed_file_loads_every_source(session):
    from app.cli import seed_sources
    from app.sources import ADAPTERS, build_adapter

    added = seed_sources()
    assert added >= 25
    assert seed_sources() == 0  # second load adds nothing
    for source in session.scalars(select(Source)):
        assert source.adapter in ADAPTERS
        build_adapter(source)  # config is complete enough to construct
