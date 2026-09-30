"""URL helpers for captured pages / alert links: ATS detection and canonical job URLs."""

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse


@dataclass(frozen=True)
class AtsRef:
    ats: str
    token: str | None = None  # company board slug
    job_id: str | None = None


_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("greenhouse", re.compile(
        r"^https?://(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_app\?for=)?"
        r"(?P<token>[\w-]+)/jobs/(?P<id>\d+)", re.I)),
    ("lever", re.compile(
        r"^https?://jobs(?:\.eu)?\.lever\.co/(?P<token>[\w.-]+)/(?P<id>[0-9a-f-]{36})", re.I)),
    ("ashby", re.compile(
        r"^https?://jobs\.ashbyhq\.com/(?P<token>[^/?#]+)/(?P<id>[0-9a-f-]{36})", re.I)),
    ("workable", re.compile(
        r"^https?://apply\.workable\.com/(?P<token>[\w-]+)/j/(?P<id>[0-9A-F]+)", re.I)),
    ("smartrecruiters", re.compile(
        r"^https?://(?:jobs|careers)\.smartrecruiters\.com/(?P<token>[\w-]+)/(?P<id>\d+)", re.I)),
    ("workday", re.compile(
        r"^https?://(?P<token>[\w-]+)\.wd\d+\.myworkdayjobs\.com/(?:[\w-]+/)*job/.*?_"
        r"(?P<id>[A-Z]*-?\d[\w-]*)(?:[/?#]|$)", re.I)),
    ("recruitee", re.compile(
        r"^https?://(?P<token>[\w-]+)\.recruitee\.com/o/(?P<id>[\w-]+)", re.I)),
    ("teamtailor", re.compile(
        r"^https?://(?P<token>[\w-]+)\.teamtailor\.com/jobs/(?P<id>\d+)", re.I)),
    ("bamboohr", re.compile(
        r"^https?://(?P<token>[\w-]+)\.bamboohr\.com/careers/(?P<id>\d+)", re.I)),
    ("jobvite", re.compile(
        r"^https?://jobs\.jobvite\.com/(?P<token>[\w-]+)/job/(?P<id>\w+)", re.I)),
    ("icims", re.compile(
        r"^https?://(?P<token>[\w-]+)\.icims\.com/jobs/(?P<id>\d+)", re.I)),
]

_LINKEDIN_VIEW = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?#]*?-)?(?P<id>\d{6,})",
                            re.I)


def detect_ats(url: str | None) -> AtsRef | None:
    if not url:
        return None
    u = url.strip()
    for ats, pat in _PATTERNS:
        m = pat.search(u)
        if m:
            return AtsRef(ats, m.group("token"), m.group("id"))
    # Greenhouse-embedded company career pages: ?gh_jid=123
    q = parse_qs(urlparse(u).query)
    if "gh_jid" in q and q["gh_jid"][0].isdigit():
        return AtsRef("greenhouse", None, q["gh_jid"][0])
    return None


def linkedin_job_id(url: str | None) -> str | None:
    if not url:
        return None
    m = _LINKEDIN_VIEW.search(url)
    if m:
        return m.group("id")
    parsed = urlparse(url)
    if "linkedin.com" in parsed.netloc.lower():
        q = parse_qs(parsed.query)
        for key in ("currentJobId", "jobId"):
            if key in q and q[key][0].isdigit():
                return q[key][0]
    return None


def linkedin_job_url(job_id: str) -> str:
    return f"https://www.linkedin.com/jobs/view/{job_id}/"


def unwrap_redirect(url: str) -> str:
    """Follow common tracking wrappers that carry the target in a query parameter."""
    parsed = urlparse(url)
    q = parse_qs(parsed.query)
    for key in ("url", "u", "dest", "destination", "target", "redirect", "q"):
        if key in q:
            target = unquote(q[key][0])
            if target.startswith(("http://", "https://")):
                return target
    return url
