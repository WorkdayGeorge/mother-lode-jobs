"""Plain-language job categories for browsing.

Keyword scoring: a term found in the title counts 4, in the body 1, and the
source's default category adds a nudge.  Good enough for browsing filters;
personal matching uses the full text instead.
"""
from __future__ import annotations

import re

CATEGORIES: dict[str, tuple[str, ...]] = {
    "Healthcare": (
        "nurse", "rn", "lvn", "cna", "nursing", "medical", "clinic", "clinical", "patient", "hospital",
        "pharmacy", "pharmacist", "dental", "physician", "therapist", "caregiver", "health care",
        "healthcare", "behavioral health", "radiology", "phlebotom", "emt", "paramedic", "lab assistant",
    ),
    "Education & Childcare": (
        "teacher", "teaching", "instructional", "paraeducator", "para-educator", "school", "student",
        "tutor", "principal", "education", "childcare", "child care", "preschool", "early childhood",
        "classroom", "librarian", "substitute", "coach",
    ),
    "Office & Administrative": (
        "administrative", "admin assistant", "office assistant", "office manager", "receptionist",
        "clerk", "clerical", "secretary", "data entry", "records", "scheduler", "executive assistant",
        "legal assistant", "front office",
    ),
    "Accounting & Finance": (
        "accounting", "accountant", "bookkeep", "accounts payable", "accounts receivable", "payroll",
        "billing", "finance", "financial", "auditor", "tax", "fiscal", "budget", "teller", "loan",
    ),
    "Sales, Retail & Customer Service": (
        "sales", "retail", "cashier", "customer service", "customer support", "store", "merchandis",
        "call center", "account representative", "clerk - retail", "associate",
    ),
    "Hospitality, Food & Recreation": (
        "cook", "chef", "kitchen", "server", "bartender", "barista", "dishwasher", "food service",
        "restaurant", "hotel", "resort", "housekeep", "front desk", "guest", "lift operator", "ski",
        "golf", "recreation", "lodging", "tasting room", "hospitality", "event",
    ),
    "Trades, Construction & Maintenance": (
        "maintenance", "mechanic", "electrician", "plumber", "carpenter", "construction", "hvac",
        "technician", "operator", "equipment", "facilities", "custodian", "janitor", "groundskeeper",
        "welder", "lineman", "line worker", "water treatment", "wastewater", "utility worker",
        "road", "public works", "painter", "laborer",
    ),
    "Warehouse, Driving & Manufacturing": (
        "warehouse", "driver", "delivery", "forklift", "shipping", "receiving", "inventory",
        "production", "manufacturing", "assembler", "machinist", "mill", "lumber", "loader",
        "material handler", "cdl", "transportation", "bus driver",
    ),
    "Public Safety & Corrections": (
        "sheriff", "deputy", "police", "officer", "correctional", "corrections", "firefighter",
        "fire captain", "dispatcher", "probation", "public safety", "security guard", "security officer",
        "emergency services", "animal control", "code enforcement",
    ),
    "Social & Community Services": (
        "social worker", "social services", "case manager", "eligibility", "counselor", "mental health",
        "community", "family", "youth", "outreach", "peer support", "child welfare", "advocate",
        "nonprofit", "housing", "employment specialist",
    ),
    "Agriculture, Forestry & Wine": (
        "forestry", "forester", "forest", "fire crew", "fuels", "wildland", "vineyard", "winery",
        "cellar", "agricultur", "ranch", "farm", "nursery", "arborist", "natural resources", "park",
    ),
    "Technology & Data": (
        "software", "developer", "programmer", "information technology", "it support", "help desk",
        "network", "systems administrator", "systems analyst", "data analyst", "database", "gis",
        "cyber", "web", "computer",
    ),
    "Management & Professional": (
        "manager", "director", "supervisor", "coordinator", "analyst", "engineer", "attorney",
        "planner", "specialist", "administrator", "superintendent", "chief", "program manager",
    ),
}

OTHER = "Other"
CATEGORY_NAMES = [*CATEGORIES.keys(), OTHER]

_PATTERNS = {
    name: [re.compile(rf"\b{re.escape(term)}", re.I) for term in terms] for name, terms in CATEGORIES.items()
}


def categorize(title: str | None, description: str | None = None, hint: str | None = None) -> str:
    title = title or ""
    body = (description or "")[:4000]
    scores = {name: 0 for name in CATEGORIES}
    for name, patterns in _PATTERNS.items():
        for pattern in patterns:
            if pattern.search(title):
                scores[name] += 4
            if body and pattern.search(body):
                scores[name] += 1
    # Management words are weak signals; let a real occupation win ties.
    scores["Management & Professional"] = int(scores["Management & Professional"] * 0.6)
    if hint in scores:
        scores[hint] += 5
    best = max(scores, key=lambda name: scores[name])
    if scores[best] == 0:
        return hint if hint in CATEGORY_NAMES else OTHER
    return best


def work_arrangement(title: str | None, location: str | None, description: str | None) -> str:
    text = " ".join(filter(None, (title, location, description))).lower()
    if "hybrid" in text:
        return "hybrid"
    if re.search(r"\bremote\b", (location or "").lower()) or any(
        p in text for p in ("fully remote", "100% remote", "work from home", "telework", "telecommute")
    ):
        return "remote"
    return "onsite"


EMPLOYMENT_TYPES = ("Full-time", "Part-time", "Seasonal", "Temporary", "Per diem", "Internship")


def employment_type(raw: str | None, title: str | None = None) -> str | None:
    text = f"{raw or ''} {title or ''}".lower()
    if not text.strip():
        return None
    if "per diem" in text or "on call" in text or "on-call" in text:
        return "Per diem"
    if "seasonal" in text:
        return "Seasonal"
    if "intern" in text:
        return "Internship"
    if any(w in text for w in ("temporary", "temp ", "extra help", "limited term", "limited-term")):
        return "Temporary"
    if "part" in text:
        return "Part-time"
    if "full" in text or "regular" in text or "permanent" in text:
        return "Full-time"
    return None
