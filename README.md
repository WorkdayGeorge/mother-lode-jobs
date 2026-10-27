# Mother Lode Careers

A free, public job board for Calaveras, Amador and Tuolumne counties, with
optional personal matching for job seekers who sign up.

It replaces `calaveras_job_agent`. The site-specific scrapers that worked there
were carried over unchanged (with their tests); everything around them was
rebuilt for a public audience.

## What it does

**For anyone, no account**
- Browse and search every local opening by town, county, kind of work, hours and date.
- Each job has its own page with the employer's apply link, plus search-engine
  job data (schema.org `JobPosting`), a sitemap and town/county landing pages so
  people find the board from Google.
- Email job alerts with just an email address (double opt-in, one-click unsubscribe).
- Employers can send their careers page or a single opening for review.

**For people who sign up**
- Sign-up is approval-gated while you test (`SIGNUP_MODE=approval`); switch to
  `open` when you're ready to go public.
- A profile: kinds of jobs wanted, skills, places, hours, pay floor, words to avoid,
  and an optional resume (only its text is kept, never the file).
- Every open job that fits their filters is scored against their profile, best first,
  with a short "what lines up" note. With `OPENAI_API_KEY` set, the best matches also
  get a detailed AI review, capped per person per day.
- Daily or weekly match emails, and a tracker for saved and applied jobs with dates and notes.
- Self-service password reset and account deletion.

**For admins**
- **Sources** are database rows, not code. Paste a careers-page link and the board
  recognises GovernmentJobs (NEOGOV), EdJoin, ADP, ApplicantPro and Apploi, fills in
  the settings, and shows a preview of the jobs it would add before you save.
- Sites that publish standard job data are read by a generic reader; sites with
  nothing machine-readable can be *watched* so you're told when the page changes.
- A health view flags sources that are failing or suddenly return no jobs, so a
  broken scraper is noticed instead of silently emptying the board.
- Approve, deny or suspend people; review employer submissions; post a job by hand;
  hide a bad listing; see recent runs and an audit log.

## How it fits together

```
Cloud Scheduler (every 15 min) ──> Cloud Run Job: python -m app.cli run
                                       │  1. read every active source once (in parallel)
                                       │  2. normalise, keep jobs in the area, update/close
                                       │  3. hide aggregator duplicates, close past deadline
                                       │  4. score new jobs for approved users
                                       │  5. send due alert emails
                                       ▼
                                 Cloud SQL (PostgreSQL)
                                       ▲
Visitors ──> Cloud Run Service: uvicorn app.web.main:app (public board, accounts, admin)
```

| Folder | What's in it |
| --- | --- |
| `app/sources/platforms.py` | Readers for shared hiring platforms (NEOGOV, EdJoin, ADP, ApplicantPro, Apploi), the generic schema.org reader, page watching, Adzuna |
| `app/sources/legacy/` | One-off scrapers carried over from calaveras_job_agent (CalCareers, USAJOBS, Remotive, CommonSpirit, Adventist, PG&E, CCWD, Greenhorn Creek, WorldMark, Calaveras Lumber, Angels Camp, Ironstone) |
| `app/seed/sources.yaml` | The starting list of 28 sources |
| `app/region.py` | Counties and towns the board covers, and how a job's location is matched to them |
| `app/pipeline.py` | The worker |
| `app/matching.py` | Personal scoring |
| `app/alerts.py` | Alert emails |
| `app/web/` | Pages, templates, styles |
| `migrations/` | Database migrations (Alembic) |

## Run it locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -c requirements.lock
cp .env.example .env            # set ADMIN_EMAILS to your address
python -m app.cli migrate
python -m app.cli seed-sources
python -m app.cli run           # reads the real job sites (needs internet)
uvicorn app.web.main:app --reload
```

Open http://127.0.0.1:8000, sign up with the address in `ADMIN_EMAILS`, and
click the confirmation link that's printed in the terminal (emails are logged
until SMTP is configured). You'll be an admin.

`python -m app.cli demo-data` adds 30 made-up jobs if you want to try the site
without fetching anything.

Tests: `pip install pytest && pytest`

## Everyday admin tasks

- **Add an employer:** Admin → Sources → paste their careers link → *Check this link* → *Preview jobs* → *Add source*.
- **A source shows "Failing":** open it to see the error and recent runs. Pause it if the employer changed sites; most fixes are a settings change, not code.
- **"Page changed" on a watched source:** look at their page, post any new opening with *Jobs → Post a job by hand*, then mark it reviewed.
- **Approve testers:** Admin → People → *Approve*. They get an email.
- **Go public:** set `SIGNUP_MODE=open` and redeploy.

## Deploying

See [deploy/README.md](deploy/README.md) for Google Cloud (Cloud Run + Cloud SQL + Cloud Scheduler).

## Privacy choices

Resumes: only extracted text is stored, never the file. AI reviews (when enabled)
send a job description and that person's profile to OpenAI. Users can delete their
account and all of its data from their account page.
