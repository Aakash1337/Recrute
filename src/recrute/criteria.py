"""Search criteria: tracks (priority tiers), hard filters, eligibility, score thresholds.

Personal values live in resources/criteria.yaml (gitignored); resources/criteria.example.yaml is
the template. Missing file -> built-in defaults below.
"""

from functools import lru_cache

import yaml
from pydantic import BaseModel, Field

from recrute.models import Priority
from recrute.paths import Paths, get_paths


class Track(BaseModel):
    priority: Priority
    name: str
    # Title keywords (case-insensitive substring/word match). A job matches the highest-priority
    # track whose title keywords hit; description keywords are a weaker secondary signal.
    title_keywords: list[str]
    description_keywords: list[str] = Field(default_factory=list)
    # Searches sent to keyword-search sources (aggregators, LinkedIn).
    search_queries: list[str] = Field(default_factory=list)


class Eligibility(BaseModel):
    """Toggles that drop jobs legally closed to the user. Visa/sponsorship is never a filter."""

    drop_clearance_required: bool = True
    drop_citizenship_required: bool = True
    drop_itar_us_person: bool = True


class Criteria(BaseModel):
    country: str = "US"
    allow_remote: bool = True
    locations: list[str] = Field(default_factory=list)  # empty = anywhere in country
    employment_types: list[str] = Field(default_factory=lambda: ["full-time"])
    exclude_title_keywords: list[str] = Field(default_factory=lambda: [
        "intern", "internship", "co-op", "senior", "sr.", "sr ", "staff", "principal", "lead",
        "manager", "director", "head of", "vp", "vice president", "chief", "architect",
        "distinguished", "part-time", "part time", "contract", "temporary",
    ])
    max_years_required: int = 5
    exclude_companies: list[str] = Field(default_factory=list)
    salary_floor: int | None = None
    eligibility: Eligibility = Field(default_factory=Eligibility)
    # Minimum LLM fit score for a job to reach the review queue, per priority.
    min_score: dict[Priority, int] = Field(default_factory=lambda: {
        Priority.P0: 55, Priority.P1: 55, Priority.P2: 65, Priority.P3: 75,
    })
    tracks: list[Track] = Field(default_factory=lambda: DEFAULT_TRACKS)

    def track_for(self, priority: Priority) -> Track | None:
        return next((t for t in self.tracks if t.priority == priority), None)

    def all_search_queries(self) -> list[tuple[Priority, str]]:
        return [(t.priority, q) for t in sorted(self.tracks, key=lambda t: t.priority)
                for q in t.search_queries]


DEFAULT_TRACKS = [
    Track(priority=Priority.P0, name="Security x AI",
          title_keywords=["ai security", "ml security", "llm security", "ai red team",
                          "adversarial ml", "ai safety engineer", "machine learning security",
                          "security machine learning", "security data scientist",
                          "detection data scientist", "ai threat"],
          description_keywords=["adversarial machine learning", "prompt injection", "llm security",
                                "model security", "ai red teaming"],
          search_queries=["AI security engineer", "LLM security", "AI red team"]),
    Track(priority=Priority.P1, name="Cybersecurity",
          title_keywords=["security", "soc analyst", "soc engineer", "cyber", "infosec",
                          "incident response", "threat", "detection engineer", "penetration",
                          "pentest", "red team", "blue team", "appsec", "application security",
                          "cloud security", "vulnerability", "grc", "iam engineer",
                          "identity and access", "forensic", "malware", "dfir", "siem",
                          "offensive security", "product security", "security operations"],
          description_keywords=["siem", "soc", "incident response", "threat hunting", "splunk",
                                "vulnerability management", "nist", "mitre att&ck"],
          search_queries=["security analyst", "security engineer", "SOC analyst",
                          "cybersecurity", "incident response", "detection engineer",
                          "penetration tester", "application security engineer",
                          "cloud security engineer", "GRC analyst"]),
    Track(priority=Priority.P2, name="AI / ML",
          title_keywords=["machine learning", "ml engineer", "ai engineer",
                          "artificial intelligence", "deep learning", "llm", "genai",
                          "generative ai", "applied scientist",
                          "mlops", "nlp", "computer vision", "data scientist", "research engineer"],
          description_keywords=["pytorch", "tensorflow", "llm", "transformers", "rag",
                                "fine-tuning"],
          search_queries=["machine learning engineer", "AI engineer", "LLM engineer",
                          "MLOps engineer", "applied scientist"]),
    Track(priority=Priority.P3, name="Adjacent",
          title_keywords=["data analyst", "data engineer", "business intelligence", "bi analyst",
                          "analytics engineer", "software engineer", "backend engineer",
                          "cloud engineer", "devops", "site reliability", "network engineer",
                          "systems administrator", "sysadmin", "it support", "systems engineer",
                          "platform engineer", "python developer"],
          search_queries=["data analyst", "data engineer", "cloud engineer",
                          "network engineer"]),
]


def load_criteria(paths: Paths | None = None) -> Criteria:
    paths = paths or get_paths()
    f = paths.resources / "criteria.yaml"
    if not f.exists():
        return Criteria()
    data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    return Criteria.model_validate(data)


@lru_cache
def get_criteria() -> Criteria:
    return load_criteria()
