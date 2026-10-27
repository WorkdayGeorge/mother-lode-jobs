"""Made-up jobs for local development and screenshots. Never run in production."""
from __future__ import annotations

from .clock import local_today
import random
from datetime import timedelta

from sqlalchemy import select

from .db import session_scope
from .models import Source
from .pipeline import apply_result
from .sources import FetchResult, RawJob
from .models import SourceRun

SAMPLES = [
    ("Office Assistant II", "Calaveras County", "San Andreas, CA", "$19.85 – $24.13 per hour", "Full-time"),
    ("Accounting Technician", "Calaveras County Water District", "San Andreas, CA", "$27.10 – $32.94 per hour", "Full-time"),
    ("Registered Nurse – Med/Surg", "Mark Twain Medical Center", "San Andreas, CA", "$62.00 – $88.00 per hour", "Full-time"),
    ("Certified Nursing Assistant", "Golden San Andreas Care Center", "San Andreas, CA", "$21.00 – $24.00 per hour", "Part-time"),
    ("Paraeducator – Special Education", "Bret Harte Union High School District", "Angels Camp, CA", "$18.40 – $22.10 per hour", "Part-time"),
    ("Lift Operator (Winter Seasonal)", "Bear Valley Mountain Resort", "Bear Valley, CA", "$18.00 per hour", "Seasonal"),
    ("Line Cook", "Greenhorn Creek Resort", "Angels Camp, CA", "$19.00 – $22.00 per hour", "Full-time"),
    ("Tasting Room Associate", "Ironstone Vineyards", "Murphys, CA", "$17.50 per hour", "Part-time"),
    ("Equipment Operator I", "Tuolumne County", "Sonora, CA", "$24.60 – $29.90 per hour", "Full-time"),
    ("Eligibility Worker I", "Amador County", "Jackson, CA", "$22.75 – $27.65 per hour", "Full-time"),
    ("Deputy Sheriff Trainee", "Tuolumne County", "Sonora, CA", "$31.20 – $37.90 per hour", "Full-time"),
    ("Water Treatment Plant Operator", "Tuolumne Utilities District", "Sonora, CA", "$33.00 – $40.10 per hour", "Full-time"),
    ("Forklift Operator / Yard Worker", "Calaveras & Sonora Lumber", "Sonora, CA", "$20.00 – $23.00 per hour", "Full-time"),
    ("Machinist", "Insight Manufacturing", "Murphys, CA", "$25.00 – $34.00 per hour", "Full-time"),
    ("Front Desk Agent", "WorldMark Angels Camp", "Angels Camp, CA", "$18.50 per hour", "Full-time"),
    ("Medical Assistant", "MACT Health Board", "Angels Camp, CA", "$20.00 – $25.00 per hour", "Full-time"),
    ("Case Manager", "The Resource Connection", "San Andreas, CA", "$23.00 – $27.00 per hour", "Full-time"),
    ("Bookkeeper", "Copperopolis Ace Hardware", "Copperopolis, CA", "$22.00 – $26.00 per hour", "Part-time"),
    ("Custodian", "Calaveras Unified School District", "San Andreas, CA", "$18.10 – $21.90 per hour", "Full-time"),
    ("IT Support Technician", "Amador County Office of Education", "Jackson, CA", "$28.00 – $34.00 per hour", "Full-time"),
    ("Firefighter I (Seasonal)", "Tuolumne County", "Columbia, CA", "$21.00 per hour", "Seasonal"),
    ("Pharmacy Technician", "Adventist Health Sonora", "Sonora, CA", "$22.00 – $27.50 per hour", "Per diem"),
    ("Accounts Payable Clerk", "Amador Water Agency", "Sutter Creek, CA", "$24.00 – $29.00 per hour", "Full-time"),
    ("Preschool Teacher", "Tuolumne County Superintendent of Schools", "Jamestown, CA", "$20.10 – $24.60 per hour", "Full-time"),
    ("Barista", "Twain Harte Coffee Co.", "Twain Harte, CA", "$17.00 per hour plus tips", "Part-time"),
    ("Delivery Driver (Class B)", "Mother Lode Propane", "Jackson, CA", "$24.00 – $28.00 per hour", "Full-time"),
    ("Administrative Analyst", "City of Angels Camp", "Angels Camp, CA", "$30.10 – $36.60 per hour", "Full-time"),
    ("Groundskeeper", "Greenhorn Creek Resort", "Angels Camp, CA", "$18.00 – $20.00 per hour", "Seasonal"),
    ("Customer Service Representative (Remote)", "Sierra Telecom", "Remote — USA", "$19.00 – $21.00 per hour", "Full-time"),
    ("Data Analyst (Remote)", "Golden State Health Plan", "Remote — California", "$70,000 – $85,000 per year", "Full-time"),
]

BLURB = (
    "Join a local team serving the foothill communities. You'll {duty}. "
    "We're looking for someone dependable, friendly, and ready to learn.\n\n"
    "What you'll do\n• {duty_cap}\n• Keep accurate records and communicate clearly with coworkers\n"
    "• Follow safety procedures and help out where needed\n\n"
    "What you'll need\n• High school diploma or equivalent\n• Valid California driver's license may be required\n"
    "• Experience with {skill} is a plus"
)
DUTIES = {
    "Office": ("answer phones, greet visitors and keep files organized", "Microsoft Excel and data entry"),
    "Account": ("process invoices, reconcile accounts and prepare reports", "QuickBooks, Excel and bookkeeping"),
    "Nurs": ("provide direct patient care alongside a supportive nursing team", "patient care and charting"),
    "Para": ("support students one-on-one and in small groups", "working with children"),
    "Cook": ("prepare menu items and keep the kitchen spotless", "food safety and line cooking"),
    "default": ("handle day-to-day duties for the position", "customer service"),
}


def load_demo() -> int:
    random.seed(7)
    with session_scope() as session:
        source = session.scalar(select(Source).where(Source.key == "demo"))
        if source is None:
            source = Source(key="demo", name="Demo data", kind="employer", adapter="manual", config={}, active=False)
            session.add(source)
            session.flush()
        jobs = []
        for i, (title, employer, location, pay, kind) in enumerate(SAMPLES):
            duty, skill = next((v for k, v in DUTIES.items() if k in title), DUTIES["default"])
            posted = local_today() - timedelta(days=random.randint(0, 20))
            jobs.append(
                RawJob(
                    external_id=f"demo-{i}",
                    title=title,
                    apply_url=f"https://example.com/jobs/{i}",
                    employer=employer,
                    location=location,
                    description=BLURB.format(duty=duty, duty_cap=duty[0].upper() + duty[1:], skill=skill),
                    employment_type=kind,
                    salary_text=pay,
                    posted_on=posted,
                    closes_on=posted + timedelta(days=random.choice([21, 30, 45])) if i % 3 else None,
                )
            )
        run = SourceRun(source_id=source.id)
        session.add(run)
        apply_result(session, source, FetchResult(jobs), run)
        return run.jobs_kept
