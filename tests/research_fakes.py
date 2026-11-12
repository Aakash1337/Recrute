"""Fakes shared by test_research.py and test_web_research.py (made-up data only)."""

import copy
from types import SimpleNamespace

from recrute.llm.base import LLMError
from recrute.llm.router import Completion
from recrute.models import utcnow

POSTING = """\
## About Acme
Acme builds rocket-powered delivery drones for small shops. We are a team of 40 in Austin, TX.

## What you'll do
- Design and run detection rules for our cloud platform.
- Respond to security incidents with the on-call team.

## Requirements
- 3+ years of experience in security operations.
- Strong Python skills.
- This role requires an active **Secret clearance**.

## Nice to have
- Experience with Kubernetes.

## Pay and benefits
The salary range for this role is $120,000 - $150,000 per year. We offer equity and a 401(k).
We are unable to sponsor visas for this position.
"""

# distinctive made-up profile facts: none of them may ever reach a prompt with web tools
PROFILE_MARKS = ("Zeppelin navigator", "Lighthouse Logistics", "Morse code", "Babbage Institute")
PROFILE_YAML = """\
headline: Zeppelin navigator turned security analyst
experience:
  - id: exp-1
    title: Signals officer
    company: Lighthouse Logistics
    start: "2020"
    bullets: []
education:
  - id: edu-1
    degree: BSc
    field: Navigation
    school: Babbage Institute
    end: "2019"
skills:
  core: [Morse code, Semaphore]
"""


def brief_answer(**over):
    out = {
        "summary": "A security operations role at a drone delivery company.",
        "role": {"seniority": "mid", "team": "Security", "reports_to": ""},
        "responsibilities": ["Design and run detection rules", "Respond to security incidents"],
        "requirements": [
            {"text": "3+ years in security operations", "kind": "must", "category": "experience",
             "years": 3, "quote": "3+ years of experience in security operations."},
            {"text": "Strong Python", "kind": "must", "category": "skill", "years": None,
             "quote": "Strong Python skills."},
            {"text": "Kubernetes experience", "kind": "nice", "category": "skill",
             "years": None, "quote": "Experience with Kubernetes."},
        ],
        "offer": {
            "pay": {"min": 120000, "max": 150000, "currency": "USD", "period": "year",
                    "quote": "The salary range for this role is $120,000 - $150,000 per year."},
            "equity": "yes", "equity_quote": "We offer equity and a 401(k).",
            "benefits": ["401(k)"], "work_mode": "not_stated", "office_days": None,
            "locations": ["Austin, TX"], "employment_type": "full_time",
            "sponsorship": "will_not",
            "sponsorship_quote": "We are unable to sponsor visas for this position.",
        },
        "about_company": "Acme builds rocket-powered delivery drones for small shops. "
                         "A team of 40 in Austin, TX.",
        "about_team": "",
        "interview_process": [],
        "red_flags": [],
    }
    out.update(over)
    return out


def research_answer(**over):
    out = {
        "matched": "confident", "official_name": "Acme Corporation",
        "website": "https://www.acme.example/", "industry": "Logistics", "hq": "Austin, TX",
        "founded": "2019", "identity_source_index": 1,
        "what_they_do": "Acme builds delivery drones for small shops.",
        "products": [{"name": "RoadRunner", "description": "A delivery drone.",
                      "source_index": 1}],
        "size": {"employees": "40", "source_index": 1},
        "stage": "early_startup",
        "funding": {"summary": "Raised a seed round.", "last_round": "Seed",
                    "last_round_date": "2025-06", "source_index": 2},
        "security_ai_relevance": {"level": "some", "explanation": "Runs its own cloud."},
        "recent_news": [
            {"headline": "Acme opens a second hangar", "date": "2026-01-15",
             "summary": "More drones.", "source_index": 2},
            {"headline": "Acme ships its 1000th drone", "date": "2026-08-02",
             "summary": "A milestone.", "source_index": 2},
        ],
        "culture_signals": [{"signal": "Remote-friendly engineering team", "source_index": 1}],
        "employee_sentiment": {"summary": "", "rating": "", "source_index": None},
        "visa_signals": "",
        "sources": [
            {"url": "https://www.acme.example/about", "title": "About Acme", "published": ""},
            {"url": "https://news.example/acme-hangar", "title": "Acme news",
             "published": "2026-01-15"},
        ],
    }
    out.update(over)
    return out


class FakeRouter:
    """Canned answers per task. A response is a value, an Exception (raised), a list (used in
    order) or a callable(call) -> value. Keys are a task name, or (task, web)."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def complete_meta(self, task, prompt, *, schema=None, system=None, use_cache=True,
                      validate=None, web=False, timeout=None):
        call = SimpleNamespace(task=task, prompt=prompt, system=system, web=web,
                               use_cache=use_cache, timeout=timeout, schema=schema)
        self.calls.append(call)
        r = self.responses[(task, web)] if (task, web) in self.responses else self.responses[task]
        if isinstance(r, list):
            r = r.pop(0)
        if callable(r):
            r = r(call)
        if isinstance(r, Exception):
            raise r
        r = copy.deepcopy(r)
        if validate is not None:
            try:
                validate(r)
            except ValueError as e:
                raise LLMError(f"{task}: answer rejected: {e}", kind="rejected") from e
        return Completion(r, "claude", "haiku" if task != "company_research" else "sonnet",
                          False, utcnow())

    def tasks(self):
        return [(c.task, c.web) for c in self.calls]


class FakeHttp:
    """get_json/get_text from a dict of url-substring -> value (an Exception is raised)."""

    def __init__(self, pages=None):
        self.pages = pages or {}
        self.urls = []

    def _get(self, url):
        self.urls.append(url)
        for part, value in self.pages.items():
            if part in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise RuntimeError("no such page")

    get_json = get_text = _get

    def close(self):
        pass
