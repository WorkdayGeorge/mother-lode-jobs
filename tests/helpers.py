from __future__ import annotations

import json

from app.clock import local_today
from app.models import Source
from app.sources import FetchResult, RawJob


class FakeResponse:
    def __init__(self, body, status=200):
        self.status_code = status
        if isinstance(body, (dict, list)):
            self._json = body
            self.text = json.dumps(body)
        else:
            self._json = None
            self.text = body
        self.content = self.text.encode()

    def json(self):
        if self._json is None:
            return json.loads(self.text)
        return self._json


class FakeSession:
    """Stands in for requests.Session: routes URLs (by substring) to canned bodies."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.headers = {}
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((url, kwargs.get("params")))
        for needle, body in self.routes.items():
            if needle in url:
                if callable(body):
                    body = body(url, kwargs.get("params") or {})
                return FakeResponse(body)
        return FakeResponse("not found", status=404)


def make_source(session, **kw) -> Source:
    defaults = dict(key="test_src", name="Calaveras County", kind="government", adapter="neogov",
                    config={"agency": "x"}, county="Calaveras County", active=True)
    defaults.update(kw)
    source = Source(**defaults)
    session.add(source)
    session.commit()
    return source


def raw(i, **kw) -> RawJob:
    defaults = dict(external_id=str(i), title=f"Office Assistant {i}", apply_url=f"https://example.com/{i}",
                    location="San Andreas, CA", description="Answer phones and use Microsoft Excel.",
                    salary_text="$20.00 - $24.00 per hour", employment_type="Full Time", posted_on=local_today())
    defaults.update(kw)
    return RawJob(**defaults)


def result(*jobs) -> FetchResult:
    return FetchResult(list(jobs))
