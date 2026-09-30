"""Shared helpers for the tailor tests: a deterministic FakeRouter and the fictional profile.

(No tests here; pytest imports it like the other test modules, so tests can import from it.)
"""

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from recrute.schemas import Profile
from recrute.tailor.answers import AnswerBank
from recrute.tailor.common import JobContext
from recrute.tailor.ingest import _Extracted, to_profile

FIXTURES = Path(__file__).parent / "fixtures" / "tailor"

JOB_DESCRIPTION = """We are hiring an AI Security Analyst to join our detection team.
You will triage alerts in Splunk, write Sigma detection rules, automate phishing triage with
Python, and red-team our LLM applications for prompt injection. Experience with adversarial
machine learning and PyTorch is a plus. Security+ preferred."""


def assert_strict(schema: dict[str, Any], path: str = "$") -> None:
    """Codex-style strictness: every object lists all properties as required, no extras."""
    if schema.get("type") == "object":
        props = schema.get("properties", {})
        assert schema.get("additionalProperties") is False, f"{path}: additionalProperties"
        assert sorted(schema.get("required", [])) == sorted(props), f"{path}: required"
        for k, v in props.items():
            assert_strict(v, f"{path}.{k}")
    if schema.get("type") == "array":
        assert_strict(schema["items"], f"{path}[]")


class FakeRouter:
    """Canned JSON per task. The "tailor" task is split into "select" and "cover" by schema.

    A response may be a value (deep-copied), a list (consumed in order) or a callable
    (prompt, schema) -> value.
    """

    def __init__(self, responses: dict[str, Any]):
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def complete(self, task: str, prompt: str, *, schema: dict[str, Any] | None = None,
                 system: str | None = None, use_cache: bool = True) -> Any:
        assert schema is not None
        assert_strict(schema)
        key = task
        if task == "tailor":
            key = "cover" if "paragraphs" in schema["properties"] else "select"
        self.calls.append((key, prompt, schema))
        r = self.responses[key]
        if isinstance(r, list):
            return r.pop(0)
        if callable(r):
            return r(prompt, schema)
        return copy.deepcopy(r)

    def keys(self) -> list[str]:
        return [c[0] for c in self.calls]


Respond = Callable[[str, dict[str, Any]], Any]


def extract_output() -> dict[str, Any]:
    return json.loads((FIXTURES / "extract.json").read_text(encoding="utf-8"))


def make_profile() -> Profile:
    return to_profile(_Extracted.model_validate(extract_output()))


def make_bank() -> AnswerBank:
    import yaml

    return AnswerBank.model_validate(
        yaml.safe_load((FIXTURES / "answers.yaml").read_text(encoding="utf-8")))


def make_job(**kw: Any) -> JobContext:
    base = {"title": "AI Security Analyst", "description": JOB_DESCRIPTION,
            "company": "Contoso Labs", "priority": "P0", "job_id": 7}
    return JobContext(**{**base, **kw})


SELECT_OUT: dict[str, Any] = {
    "summary": "Security analyst with SOC triage experience and graduate research in ML-based "
               "intrusion detection. Builds detection rules and LLM red-team tooling.",
    "experience": [
        {"id": "exp-lakeside-state-university",
         "bullet_ids": ["exp-lakeside-state-university-b2", "exp-lakeside-state-university-b1"],
         "rewrites": []},
        {"id": "exp-northwind-health",
         "bullet_ids": ["exp-northwind-health-b2", "exp-northwind-health-b3",
                        "exp-northwind-health-b1"],
         "rewrites": [{"bullet_id": "exp-northwind-health-b3",
                       "text": "Automated phishing triage enrichment in Python using the "
                               "VirusTotal API, cutting handling time from 15 to 4 minutes "
                               "per report."}]},
    ],
    "projects": [{"id": "proj-promptguard",
                  "bullet_ids": ["proj-promptguard-b1", "proj-promptguard-b2"], "rewrites": []}],
    "education_ids": ["edu-lakeside-state-university", "edu-riverbend-college"],
    "certification_ids": ["cert-comptia-security"],
    "skills": ["Splunk", "Sigma", "Python", "PyTorch", "Adversarial ML", "LLM Security"],
}

COVER_OUT: dict[str, Any] = {
    "paragraphs": [
        "I am applying for the AI Security Analyst role at Contoso Labs.",
        "At Northwind Health I wrote 12 Sigma detection rules that cut false positives by 38%, "
        "and I automated phishing-report enrichment with Python and the VirusTotal API.",
        "I also built PromptGuard, which replays 250 prompt-injection payloads against LLM apps.",
        "I would welcome the chance to talk.",
    ],
    "cited_ids": ["exp-northwind-health-b2", "exp-northwind-health-b3", "proj-promptguard-b1"],
}

VERIFY_OUT: dict[str, Any] = {"flags": []}
