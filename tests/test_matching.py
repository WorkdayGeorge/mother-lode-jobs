from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import alerts, mailer, matching
from app.models import AlertSubscription, Job, Match, Profile, User, utcnow

from helpers import make_source, raw
from test_pipeline import apply


def make_user(session, **profile):
    user = User(email="pat@example.com", name="Pat Doe", password_hash="x", status="active")
    session.add(user)
    session.flush()
    defaults = dict(user_id=user.id, target_roles=["office assistant"], skills=["Microsoft Excel", "QuickBooks"],
                    counties=["Calaveras County"], towns=[], categories=[], employment_types=[], must_avoid=[],
                    include_remote=False, version=1, alert_frequency="daily", alert_min_score=50)
    defaults.update(profile)
    session.add(Profile(**defaults))
    session.commit()
    return user


def seed_jobs(session):
    source = make_source(session)
    apply(session, source,
          raw(1, title="Office Assistant II"),
          raw(2, title="Line Cook", description="Cook food on the line", salary_text="$17 per hour"),
          raw(3, title="Accounting Clerk", description="QuickBooks and Microsoft Excel daily", location="Sonora, CA"))
    return source


def test_quick_score_prefers_target_role(session):
    seed_jobs(session)
    user = make_user(session)
    jobs = {j.title: j for j in session.scalars(select(Job))}
    office = matching.quick_score(jobs["Office Assistant II"], user.profile)[0]
    cook = matching.quick_score(jobs["Line Cook"], user.profile)[0]
    assert office > cook
    assert office >= 70


def test_filters_respect_place_pay_and_words(session):
    seed_jobs(session)
    user = make_user(session, min_hourly_pay=18, must_avoid=["cook"])
    jobs = {j.title: j for j in session.scalars(select(Job))}
    p = user.profile
    assert matching.passes_filters(jobs["Office Assistant II"], p)
    assert not matching.passes_filters(jobs["Line Cook"], p)
    # Sonora is in Tuolumne, outside the chosen county
    sonora = jobs["Accounting Clerk"]
    assert sonora.county == "Tuolumne County" and not matching.passes_filters(sonora, p)
    p.towns = ["Sonora"]
    assert matching.passes_filters(sonora, p)


def test_score_user_creates_and_refreshes_matches(session):
    seed_jobs(session)
    user = make_user(session)
    first = matching.score_user(session, user)
    assert first["scored"] == 2  # Sonora job is outside the profile's places
    again = matching.score_user(session, user)
    assert again["scored"] == 0  # nothing new
    user.profile.counties = ["Calaveras County", "Tuolumne County"]
    user.profile.version += 1
    session.commit()
    third = matching.score_user(session, user)
    assert third["scored"] == 3


def test_pending_users_are_not_scored(session):
    seed_jobs(session)
    user = make_user(session)
    user.status = "pending"
    session.commit()
    assert matching.score_user(session, user) == {"scored": 0, "ai": 0}


def test_ai_budget_caps_reviews(session, monkeypatch, settings_override):
    seed_jobs(session)
    user = make_user(session)
    settings_override(openai_api_key="sk-test", ai_scores_per_user_per_day=1)
    calls = []
    monkeypatch.setattr(matching, "_ai_review", lambda job, profile: calls.append(job.id) or
                        {"score": 88, "summary": "Strong fit", "strengths": ["Excel"], "gaps": []})
    matching.score_user(session, user)
    assert len(calls) == 1
    reviewed = session.scalar(select(Match).where(Match.method == "ai"))
    assert reviewed.score == 88 and reviewed.summary == "Strong fit"
    assert matching.ai_budget_left(session, user.id) == 0


def morning():
    return datetime(2026, 10, 27, 16, 0, tzinfo=timezone.utc)  # 9am Pacific


def test_personal_alert_sends_once(session):
    seed_jobs(session)
    user = make_user(session)
    matching.score_user(session, user)
    assert alerts.send_personal_alerts(session, now=morning()) == 1
    assert "Office Assistant II" in mailer.OUTBOX[-1].text
    assert alerts.send_personal_alerts(session, now=morning() + timedelta(days=1)) == 0


def test_no_alerts_before_7am(session):
    seed_jobs(session)
    user = make_user(session)
    matching.score_user(session, user)
    early = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)  # 5am Pacific
    assert alerts.send_personal_alerts(session, now=early) == 0


def test_public_alert_matches_filters(session):
    sub = AlertSubscription(email="a@b.co", keywords="cook, chef", counties=["Calaveras County"], categories=[],
                            token="t", confirmed_at=utcnow() - timedelta(hours=1), frequency="daily")
    unconfirmed = AlertSubscription(email="c@d.co", counties=[], categories=[], token="u", frequency="daily")
    session.add_all([sub, unconfirmed])
    session.commit()
    seed_jobs(session)
    assert alerts.send_public_alerts(session, now=morning()) == 1
    message = mailer.OUTBOX[-1]
    assert message.to == "a@b.co"
    assert "Line Cook" in message.text and "Office Assistant" not in message.text
    assert "/alerts/unsubscribe/t" in message.text
