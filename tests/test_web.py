import json
import re

from sqlalchemy import select

from app import mailer
from app.models import AlertSubscription, Job, Source, Submission, TrackedJob, User
from app.sources import FetchResult

from conftest import csrf_from, last_link
from helpers import make_source, raw
from test_pipeline import apply


def seed(session):
    source = make_source(session)
    apply(session, source, raw(1, title="Office Assistant II"),
          raw(2, title="Line Cook", description="Cook on the line"),
          raw(3, title="Deputy Sheriff", location="Sonora, CA"))
    return source


def signup(client, email="pat@example.com", name="Pat Doe", password="correct horse battery"):
    token = csrf_from(client, "/signup")
    return client.post("/signup", data={"csrf_token": token, "name": name, "email": email, "password": password},
                       follow_redirects=False)


def verify(client):
    link = last_link(r"https://jobs\.example\.org/verify/\S+")
    return client.get(link.replace("https://jobs.example.org", ""), follow_redirects=False)


def login(client, email, password="correct horse battery"):
    token = csrf_from(client, "/login")
    return client.post("/login", data={"csrf_token": token, "email": email, "password": password},
                       follow_redirects=False)


def make_admin(client):
    signup(client, email="admin@example.com", name="Admin")
    verify(client)


# --------------------------------------------------------------------------
# public board
# --------------------------------------------------------------------------


def test_public_pages_render_without_an_account(client, session):
    seed(session)
    for path in ("/", "/jobs", "/employers", "/alerts", "/for-employers", "/about", "/privacy", "/signup", "/login"):
        assert client.get(path).status_code == 200, path


def test_search_and_filters(client, session):
    seed(session)
    assert "Line Cook" in client.get("/jobs?q=cook").text
    assert "Office Assistant II" not in client.get("/jobs?q=cook").text
    tuolumne = client.get("/jobs?county=tuolumne").text
    assert "Deputy Sheriff" in tuolumne and "Line Cook" not in tuolumne
    assert "Deputy Sheriff" in client.get("/jobs?town=sonora").text
    assert client.get("/in/sonora", follow_redirects=False).headers["location"] == "/jobs?town=sonora"


def test_job_page_has_structured_data_and_canonical_url(client, session):
    seed(session)
    job = session.scalar(select(Job).where(Job.title == "Office Assistant II"))
    response = client.get(f"/jobs/{job.id}", follow_redirects=False)
    assert response.headers["location"] == f"/jobs/{job.id}/{job.slug}"
    html = client.get(f"/jobs/{job.id}/{job.slug}").text
    data = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S).group(1))
    assert data["@type"] == "JobPosting" and data["title"] == "Office Assistant II"
    assert data["jobLocation"]["address"]["addressLocality"] == "San Andreas"
    assert data["baseSalary"]["value"]["unitText"] == "HOUR"
    assert f'rel="canonical" href="https://jobs.example.org/jobs/{job.id}/{job.slug}"' in html


def test_closed_jobs_are_noindexed_and_hidden_are_gone(client, session):
    seed(session)
    job = session.scalar(select(Job).where(Job.title == "Line Cook"))
    job.status = "closed"
    session.commit()
    html = client.get(f"/jobs/{job.id}/{job.slug}").text
    assert 'name="robots" content="noindex"' in html and "application/ld+json" not in html
    job.status = "hidden"
    session.commit()
    assert client.get(f"/jobs/{job.id}/{job.slug}").status_code == 404


def test_sitemap_and_robots(client, session):
    seed(session)
    sitemap = client.get("/sitemap.xml").text
    assert "/jobs/" in sitemap and "town=san-andreas" in sitemap
    assert "Sitemap: https://jobs.example.org/sitemap.xml" in client.get("/robots.txt").text


def test_anonymous_pages_are_cacheable_and_set_no_cookie(client, session):
    seed(session)
    response = client.get("/jobs")
    assert response.headers.get("cache-control") == "public, max-age=300"
    assert "set-cookie" not in response.headers


def test_email_alert_double_opt_in_and_unsubscribe(client, session):
    token = csrf_from(client, "/alerts")
    client.post("/alerts", data={"csrf_token": token, "email": "Sam@Example.com", "keywords": "cook",
                                 "counties": ["Calaveras County"], "frequency": "daily"})
    sub = session.scalar(select(AlertSubscription))
    assert sub.email == "sam@example.com" and sub.confirmed_at is None
    link = last_link(r"https://jobs\.example\.org/alerts/confirm/\S+").replace("https://jobs.example.org", "")
    assert "alert is on" in client.get(link).text
    session.refresh(sub)
    assert sub.confirmed_at is not None
    page = client.get(f"/alerts/unsubscribe/{sub.token}")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    client.post(f"/alerts/unsubscribe/{sub.token}", data={"csrf_token": token})
    session.refresh(sub)
    assert sub.unsubscribed_at is not None


def test_honeypot_blocks_bots(client, session):
    token = csrf_from(client, "/alerts")
    client.post("/alerts", data={"csrf_token": token, "email": "bot@spam.com", "website": "http://spam"})
    assert session.scalar(select(AlertSubscription)) is None


def test_employer_submission(client, session):
    token = csrf_from(client, "/for-employers")
    response = client.post("/for-employers", data={
        "csrf_token": token, "kind": "careers_page", "employer": "Murphys Winery",
        "contact_email": "owner@winery.com", "url": "https://winery.com/jobs"}, follow_redirects=False)
    assert response.headers["location"] == "/for-employers/thanks"
    assert session.scalar(select(Submission)).employer == "Murphys Winery"


def test_forms_without_csrf_are_rejected(client):
    response = client.post("/alerts", data={"email": "a@b.co"})
    assert response.status_code == 400


# --------------------------------------------------------------------------
# accounts: approval-gated sign-up
# --------------------------------------------------------------------------


def test_signup_waits_for_admin_approval(client, session):
    signup(client)
    user = session.scalar(select(User).where(User.email == "pat@example.com"))
    assert user.status == "unverified"
    verify(client)
    session.refresh(user)
    assert user.status == "pending"
    assert any("waiting for approval" in m.subject for m in mailer.OUTBOX)
    page = client.get("/me").text
    assert "waiting for approval" in page
    assert client.get("/me/profile", follow_redirects=False).headers["location"] == "/me"


def test_admin_email_becomes_admin_on_verify(client, session):
    make_admin(client)
    admin = session.scalar(select(User).where(User.email == "admin@example.com"))
    assert admin.is_admin and admin.status == "active"
    assert client.get("/admin").status_code == 200


def test_admin_approves_user_and_user_gets_matches(client, session):
    seed(session)
    signup(client)
    verify(client)
    pat = session.scalar(select(User).where(User.email == "pat@example.com"))
    client.cookies.clear()

    make_admin(client)
    token = csrf_from(client, "/admin/users")
    client.post(f"/admin/users/{pat.id}", data={"csrf_token": token, "action": "approve"})
    session.refresh(pat)
    assert pat.status == "active"
    assert mailer.OUTBOX[-1].to == "pat@example.com" and "approved" in mailer.OUTBOX[-1].subject
    client.cookies.clear()

    login(client, "pat@example.com")
    token = csrf_from(client, "/me/profile")
    response = client.post("/me/profile", data={
        "csrf_token": token, "target_roles": "office assistant", "skills": "Microsoft Excel\nfilings",
        "counties": ["Calaveras County"], "alert_frequency": "daily", "alert_min_score": "60"})
    assert response.status_code == 200
    dashboard = client.get("/me").text
    assert "Office Assistant II" in dashboard
    assert "Deputy Sheriff" not in dashboard  # Tuolumne isn't in the profile


def test_open_signup_mode_skips_approval(client, session, settings_override):
    settings_override(signup_mode="open")
    signup(client)
    verify(client)
    assert session.scalar(select(User).where(User.email == "pat@example.com")).status == "active"


def test_duplicate_signup_does_not_reveal_account(client, session):
    signup(client)
    response = signup(client, password="another long password")
    assert response.headers["location"] == "/signup/check-email"
    assert "already have an account" in mailer.OUTBOX[-1].subject


def test_login_failure_and_password_reset(client, session):
    signup(client)
    verify(client)
    client.cookies.clear()
    assert login(client, "pat@example.com", "wrong password!!").status_code == 400
    token = csrf_from(client, "/forgot")
    client.post("/forgot", data={"csrf_token": token, "email": "pat@example.com"})
    link = last_link(r"https://jobs\.example\.org/reset/\S+").replace("https://jobs.example.org", "")
    token = csrf_from(client, link)
    client.post(link, data={"csrf_token": token, "password": "a brand new passphrase"})
    client.cookies.clear()
    assert login(client, "pat@example.com", "a brand new passphrase").headers["location"] == "/me"
    # reset links only work once
    assert client.get(link).status_code == 400


def test_non_admins_cannot_see_admin(client, session):
    signup(client)
    verify(client)
    assert client.get("/admin").status_code == 404


def test_save_and_track_a_job(client, session, settings_override):
    settings_override(signup_mode="open")
    seed(session)
    signup(client)
    verify(client)
    job = session.scalar(select(Job).where(Job.title == "Line Cook"))
    token = csrf_from(client, f"/jobs/{job.id}/{job.slug}")
    client.post(f"/jobs/{job.id}/track", data={"csrf_token": token, "status": "applied"})
    tracked = session.scalar(select(TrackedJob))
    assert tracked.status == "applied" and tracked.applied_on is not None
    assert "Line Cook" in client.get("/me/jobs").text


def test_resume_upload_keeps_text_only(client, session, settings_override):
    settings_override(signup_mode="open")
    signup(client)
    verify(client)
    token = csrf_from(client, "/me/profile")
    text = b"Pat Doe\nOffice assistant with five years of QuickBooks and Microsoft Excel experience."
    client.post("/me/resume", data={"csrf_token": token}, files={"file": ("resume.txt", text, "text/plain")})
    profile = session.scalar(select(User).where(User.email == "pat@example.com")).profile
    session.refresh(profile)
    assert "QuickBooks" in profile.resume_text and profile.resume_filename == "resume.txt"


def test_delete_account_removes_everything(client, session, settings_override):
    settings_override(signup_mode="open")
    seed(session)
    signup(client)
    verify(client)
    token = csrf_from(client, "/me/account")
    client.post("/me/account/delete", data={"csrf_token": token, "confirm": "DELETE"})
    assert session.scalar(select(User).where(User.email == "pat@example.com")) is None


# --------------------------------------------------------------------------
# admin
# --------------------------------------------------------------------------


def test_admin_adds_source_from_url_with_preview(client, session, monkeypatch):
    from app import pipeline

    make_admin(client)
    page = client.get("/admin/sources/new?url=https://www.governmentjobs.com/careers/amadorgov").text
    assert 'value="amadorgov"' in page

    monkeypatch.setattr(pipeline, "_fetch", lambda source: FetchResult([raw(1, location="Jackson, CA")]))
    token = csrf_from(client, "/admin/sources/new")
    form = {"csrf_token": token, "name": "Amador County", "kind": "government", "adapter": "neogov",
            "cfg_agency": "amadorgov", "county": "Amador County"}
    preview = client.post("/admin/sources/new", data={**form, "action": "preview"}).text
    assert "Office Assistant 1" in preview and session.scalar(select(Source)) is None
    client.post("/admin/sources/new", data={**form, "action": "save"})
    source = session.scalar(select(Source))
    assert source.key == "amador_county" and source.config == {"agency": "amadorgov"}


def test_admin_posts_a_job_by_hand(client, session):
    make_admin(client)
    token = csrf_from(client, "/admin/jobs/new")
    response = client.post("/admin/jobs/new", data={
        "csrf_token": token, "title": "Ranch Hand", "employer": "Sheep Ranch Co", "town": "Sheep Ranch",
        "description": "Fix fences", "apply_url": "jobs@ranch.com", "closes_on": "2099-01-01"}, follow_redirects=False)
    job = session.scalar(select(Job))
    assert job.title == "Ranch Hand" and job.county == "Calaveras County"
    assert job.apply_url == "mailto:jobs@ranch.com"
    assert response.headers["location"] == f"/jobs/{job.id}/{job.slug}"


def test_admin_source_list_shows_health(client, session):
    make_admin(client)
    make_source(session, consecutive_failures=3, last_run_at=None)
    source = session.scalar(select(Source))
    from app.models import utcnow

    source.last_run_at = utcnow()
    session.commit()
    assert "Failing" in client.get("/admin/sources").text
    assert "Sources that need a look" in client.get("/admin").text
