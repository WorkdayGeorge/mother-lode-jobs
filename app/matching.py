"""Personal matching: score open jobs against each approved user's profile.

Two passes:
1. A quick, free keyword score for every job that passes the user's own
   filters (place, category, job type, pay floor, words to avoid).
2. If OPENAI_API_KEY is set, a detailed AI review of the best quick matches,
   capped per user per day so costs stay predictable as sign-ups grow.
"""
from __future__ import annotations

from .clock import local_today
import json
import logging
import re
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import AiUsage, Job, Match, Profile, User, utcnow
from .text import hourly_equivalent

log = logging.getLogger(__name__)

QUICK_FLOOR_FOR_AI = 35
_TOKEN = re.compile(r"[a-z][a-z0-9+#.\-]{2,}")
_STOP = set(
    """and the for with you your our are will this that from have has job jobs work working position
    able must may can all any other per including include such who their they them its not but
    experience required preferred skills ability knowledge years year duties responsibilities
    california county city state employee employees employer team new time full part""".split()
)


def tokens(text: str | None) -> set[str]:
    return {t.strip(".-") for t in _TOKEN.findall((text or "").lower())} - _STOP


def _phrase_in(phrase: str, text: str) -> bool:
    words = [w for w in re.findall(r"[a-z0-9+#]+", phrase.lower()) if w not in _STOP]
    return bool(words) and all(re.search(rf"\b{re.escape(w)}", text) for w in words)


# --------------------------------------------------------------------------
# filters
# --------------------------------------------------------------------------


def passes_filters(job: Job, profile: Profile) -> bool:
    if job.status != "open":
        return False
    place_ok = (
        (job.is_remote and profile.include_remote)
        or (job.county and job.county in (profile.counties or []))
        or (job.town and job.town in (profile.towns or []))
    )
    if not place_ok:
        return False
    if profile.categories and job.category not in profile.categories:
        return False
    if profile.employment_types and job.employment_type and job.employment_type not in profile.employment_types:
        return False
    title = job.title.lower()
    if any(word.strip() and word.strip().lower() in title for word in profile.must_avoid or []):
        return False
    if profile.min_hourly_pay:
        top = hourly_equivalent(job.salary_max or job.salary_min, job.salary_unit)
        if top is not None and top < profile.min_hourly_pay:
            return False
    return True


# --------------------------------------------------------------------------
# quick score
# --------------------------------------------------------------------------


def resume_keywords(resume_text: str | None, limit: int = 60) -> list[str]:
    counts = Counter(t for t in _TOKEN.findall((resume_text or "").lower()) if t not in _STOP and len(t) > 3)
    return [word for word, _ in counts.most_common(limit)]


def quick_score(job: Job, profile: Profile) -> tuple[int, list[str], str]:
    title = job.title.lower()
    body = f"{title} {(job.description or '').lower()}"

    roles = [r for r in profile.target_roles or [] if r.strip()]
    role_hits = [r for r in roles if _phrase_in(r, title)]
    role_partial = 0.0
    if roles and not role_hits:
        title_tokens = tokens(title)
        role_partial = max((len(tokens(r) & title_tokens) / max(1, len(tokens(r))) for r in roles), default=0)
    role_score = 1.0 if role_hits else role_partial * 0.7

    skills = [s for s in profile.skills or [] if s.strip()]
    skill_hits = [s for s in skills if _phrase_in(s, body)]
    skill_score = min(1.0, len(skill_hits) / max(1, min(6, len(skills)))) if skills else 0.0

    resume_words = resume_keywords(profile.resume_text)
    body_tokens = tokens(body)
    resume_hits = [w for w in resume_words if w in body_tokens]
    resume_score = min(1.0, len(resume_hits) / 12) if resume_words else 0.0

    # Weight what the person actually gave us.
    parts = []
    if roles:
        parts.append((role_score, 50))
    if skills:
        parts.append((skill_score, 30))
    if resume_words:
        parts.append((resume_score, 20))
    if not parts:
        return 0, [], "Add target jobs or skills to your profile to get scores."
    total_weight = sum(w for _, w in parts)
    score = round(100 * sum(s * w for s, w in parts) / total_weight)
    if profile.categories and job.category in profile.categories:
        score = min(100, score + 5)

    strengths = [*role_hits, *skill_hits][:8]
    if role_hits:
        summary = f"Matches your target “{role_hits[0]}”"
    elif skill_hits:
        summary = f"Mentions {len(skill_hits)} of your skills"
    else:
        summary = "Fits your area and filters"
    return score, strengths, summary


# --------------------------------------------------------------------------
# AI review
# --------------------------------------------------------------------------

AI_PROMPT = """You help a job seeker in rural California decide which local openings are worth applying to.

Compare the JOB with the PERSON. Rules:
- Use only facts stated in the person's profile and resume. Never invent credentials, licenses, or experience.
- Treat coursework and training as training, not job experience.
- A different past job title is not a reason to rule someone out if their skills transfer.
- If the posting requires a license or certification the person doesn't list, call it out as a gap.
- Be encouraging but honest. Write for the person ("you"), in plain language.

Return JSON only:
{"score": 0-100 integer, "summary": "one sentence verdict",
 "strengths": ["short phrases, max 5"], "gaps": ["short phrases, max 4"]}"""


def _ai_review(job: Job, profile: Profile) -> dict | None:
    settings = get_settings()
    if not settings.ai_enabled:
        return None
    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key)
    payload = {
        "job": {
            "title": job.title,
            "employer": job.employer,
            "location": job.location,
            "type": job.employment_type,
            "pay": job.salary_text,
            "description": (job.description or "")[:6000],
        },
        "person": {
            "looking_for": profile.target_roles,
            "skills": profile.skills,
            "experience_summary": profile.experience_summary,
            "resume": (profile.resume_text or "")[:8000],
        },
    }
    response = client.responses.create(
        model=settings.openai_model, instructions=AI_PROMPT, input=json.dumps(payload)
    )
    text = (response.output_text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text).strip()
    data = json.loads(text)
    return {
        "score": max(0, min(100, int(data["score"]))),
        "summary": str(data.get("summary") or "")[:500],
        "strengths": [str(x)[:120] for x in (data.get("strengths") or [])][:5],
        "gaps": [str(x)[:120] for x in (data.get("gaps") or [])][:4],
    }


def ai_budget_left(session: Session, user_id: str) -> int:
    cap = get_settings().ai_scores_per_user_per_day
    usage = session.scalar(select(AiUsage).where(AiUsage.user_id == user_id, AiUsage.day == local_today()))
    return max(0, cap - (usage.count if usage else 0))


def _spend_ai(session: Session, user_id: str) -> None:
    usage = session.scalar(select(AiUsage).where(AiUsage.user_id == user_id, AiUsage.day == local_today()))
    if usage is None:
        usage = AiUsage(user_id=user_id, day=local_today(), count=0)
        session.add(usage)
    usage.count += 1


def review_with_ai(session: Session, match: Match, profile: Profile) -> bool:
    """Upgrade one match to an AI review if budget allows. Returns True if done."""
    if not get_settings().ai_enabled or ai_budget_left(session, profile.user_id) <= 0:
        return False
    try:
        review = _ai_review(match.job, profile)
    except Exception:  # model/network/JSON problems: keep the quick score
        log.exception("AI review failed for match %s", match.id)
        return False
    if not review:
        return False
    _spend_ai(session, profile.user_id)
    match.score = review["score"]
    match.summary = review["summary"]
    match.strengths = review["strengths"]
    match.gaps = review["gaps"]
    match.method = "ai"
    return True


# --------------------------------------------------------------------------
# scoring runs
# --------------------------------------------------------------------------


def score_user(session: Session, user: User, *, ai: bool = True) -> dict:
    profile = user.profile
    if not user.is_active or profile is None or not profile.is_complete:
        return {"scored": 0, "ai": 0}

    current = {
        m.job_id: m for m in session.scalars(select(Match).where(Match.user_id == user.id))
    }
    jobs = session.scalars(select(Job).where(Job.status == "open"))
    fresh: list[Match] = []
    for job in jobs:
        existing = current.get(job.id)
        if existing and existing.profile_version == profile.version:
            continue
        if not passes_filters(job, profile):
            if existing and not existing.dismissed:
                session.delete(existing)
            continue
        score, strengths, summary = quick_score(job, profile)
        if existing is None:
            existing = Match(user_id=user.id, job_id=job.id, job=job)
            session.add(existing)
        existing.score, existing.strengths, existing.gaps = score, strengths, []
        existing.summary, existing.method, existing.profile_version = summary, "quick", profile.version
        existing.created_at = existing.created_at or utcnow()
        fresh.append(existing)
    session.flush()

    ai_done = 0
    if ai and get_settings().ai_enabled:
        for match in sorted(fresh, key=lambda m: m.score, reverse=True):
            if match.score < QUICK_FLOOR_FOR_AI:
                break
            if not review_with_ai(session, match, profile):
                break
            ai_done += 1
    session.commit()
    return {"scored": len(fresh), "ai": ai_done}


def score_all(session: Session) -> dict:
    users = session.scalars(select(User).where(User.status == "active"))
    totals = {"users": 0, "scored": 0, "ai": 0}
    for user in users:
        result = score_user(session, user)
        totals["users"] += 1
        totals["scored"] += result["scored"]
        totals["ai"] += result["ai"]
    return totals
