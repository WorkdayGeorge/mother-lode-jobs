"""The geography the board covers, and how to tell whether a job is in it.

To widen or narrow the board, edit COUNTIES and TOWNS.  ``primary`` counties
show up first in filters; ``nearby`` ones are kept but labeled as nearby.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .text import slugify

COUNTIES: dict[str, dict] = {
    "Calaveras County": {"primary": True},
    "Amador County": {"primary": True},
    "Tuolumne County": {"primary": True},
    "Alpine County": {"primary": False},
    "El Dorado County": {"primary": False},
    "Placer County": {"primary": False},
}

PRIMARY_COUNTIES = [name for name, meta in COUNTIES.items() if meta["primary"]]

TOWNS: dict[str, str] = {
    # Calaveras
    "Angels Camp": "Calaveras County",
    "Arnold": "Calaveras County",
    "Avery": "Calaveras County",
    "Burson": "Calaveras County",
    "Camp Connell": "Calaveras County",
    "Copperopolis": "Calaveras County",
    "Dorrington": "Calaveras County",
    "Douglas Flat": "Calaveras County",
    "Forest Meadows": "Calaveras County",
    "Glencoe": "Calaveras County",
    "Hathaway Pines": "Calaveras County",
    "Milton": "Calaveras County",
    "Mokelumne Hill": "Calaveras County",
    "Mountain Ranch": "Calaveras County",
    "Murphys": "Calaveras County",
    "Rail Road Flat": "Calaveras County",
    "Rancho Calaveras": "Calaveras County",
    "San Andreas": "Calaveras County",
    "Sheep Ranch": "Calaveras County",
    "Vallecito": "Calaveras County",
    "Valley Springs": "Calaveras County",
    "Wallace": "Calaveras County",
    "West Point": "Calaveras County",
    "Wilseyville": "Calaveras County",
    # Amador
    "Amador City": "Amador County",
    "Buckhorn": "Amador County",
    "Drytown": "Amador County",
    "Fiddletown": "Amador County",
    "Ione": "Amador County",
    "Jackson": "Amador County",
    "Martell": "Amador County",
    "Pine Grove": "Amador County",
    "Pioneer": "Amador County",
    "Plymouth": "Amador County",
    "River Pines": "Amador County",
    "Sutter Creek": "Amador County",
    "Volcano": "Amador County",
    # Tuolumne
    "Big Oak Flat": "Tuolumne County",
    "Chinese Camp": "Tuolumne County",
    "Cold Springs": "Tuolumne County",
    "Columbia": "Tuolumne County",
    "East Sonora": "Tuolumne County",
    "Groveland": "Tuolumne County",
    "Jamestown": "Tuolumne County",
    "Long Barn": "Tuolumne County",
    "Mi-Wuk Village": "Tuolumne County",
    "Moccasin": "Tuolumne County",
    "Phoenix Lake": "Tuolumne County",
    "Pinecrest": "Tuolumne County",
    "Sonora": "Tuolumne County",
    "Soulsbyville": "Tuolumne County",
    "Strawberry": "Tuolumne County",
    "Tuolumne": "Tuolumne County",
    "Twain Harte": "Tuolumne County",
    # Nearby
    "Bear Valley": "Alpine County",
    "Placerville": "El Dorado County",
    "Camino": "El Dorado County",
    "Pollock Pines": "El Dorado County",
    "Auburn": "Placer County",
}

TOWN_SLUGS = {slugify(name): name for name in TOWNS}

_CA = re.compile(r"\b(ca|california)\b", re.I)
_REMOTE = re.compile(r"\bremote\b|work from home|telework|telecommute", re.I)
_REMOTE_OK_REGIONS = re.compile(
    r"california|\bca\b|united states|\busa?\b|u\.s\.|north america|americas|worldwide|anywhere|global",
    re.I,
)


def _town_pattern(name: str) -> re.Pattern[str]:
    # "Tuolumne" the town must not match inside "Tuolumne County".
    escaped = re.escape(name).replace(r"\-", "[- ]?").replace(r"\ ", r"\s+")
    tail = r"(?!\s+county)" if name in COUNTIES or f"{name} County" in COUNTIES else ""
    return re.compile(rf"\b{escaped}\b{tail}", re.I)


_TOWN_PATTERNS = [(name, _town_pattern(name)) for name in sorted(TOWNS, key=len, reverse=True)]
_COUNTY_PATTERNS = [
    (name, re.compile(rf"\b{re.escape(name.replace(' County', ''))}\s+(county|co\.)", re.I))
    for name in COUNTIES
]


@dataclass(frozen=True)
class Place:
    town: str | None
    county: str | None
    is_remote: bool = False

    @property
    def in_region(self) -> bool:
        return self.is_remote or self.county is not None

    @property
    def is_primary(self) -> bool:
        return self.county in PRIMARY_COUNTIES


def locate(location: str | None, *, county_hint: str | None = None, require_state: bool = True) -> Place:
    """Work out where a job is from its free-text location.

    ``county_hint`` comes from the source (a Calaveras County government feed
    is in Calaveras even if the row only says "San Andreas").  Without a hint
    a bare town name must be accompanied by "CA"/"California", which keeps
    Jackson, Mississippi off the board.
    """
    text = location or ""

    if _REMOTE.search(text):
        if _REMOTE_OK_REGIONS.search(text):
            return Place(None, None, is_remote=True)
        return Place(None, None, is_remote=False)

    town = None
    for name, pattern in _TOWN_PATTERNS:
        if pattern.search(text):
            town = name
            break

    county = None
    for name, pattern in _COUNTY_PATTERNS:
        if pattern.search(text):
            county = name
            break

    if town and not county:
        if county_hint or not require_state or _CA.search(text):
            county = TOWNS[town]
        else:
            town = None
    if town and county and TOWNS[town] != county:
        # e.g. "Columbia, Tuolumne County" fine; "Auburn / Calaveras County" -> trust county
        town = town if TOWNS[town] == county else None

    if county is None and county_hint and (not text.strip() or town is None):
        # Source is pinned to a county and the row doesn't contradict it.
        if not _mentions_other_place(text):
            county = county_hint

    return Place(town, county)


def _mentions_other_place(text: str) -> bool:
    """True if the text clearly names a city outside the region ("Fresno, CA")."""
    match = re.search(r"([A-Za-z .'-]+),\s*(CA|California)\b", text)
    if not match:
        return False
    city = match.group(1).strip().split("/")[-1].strip()
    return bool(city) and city.lower() not in {t.lower() for t in TOWNS} and "county" not in city.lower()
