"""Recognize applicant-tracking-system URLs.

``parse_ats_url(url)`` tells you which ATS a posting/apply URL lives on, the company's board token
there, and the ATS job id when present. Used for:

- company-registry expansion (a Greenhouse link seen on HN/LinkedIn/an aggregator reveals a board
  we can poll directly), and
- canonical URLs for dedup (the same Lever job linked as ``.../apply`` or with ``?lever-source=``
  tracking params collapses to one canonical URL).

Token formats per ATS:
  greenhouse, lever, ashby, workable, smartrecruiters, jobvite, recruitee: board slug
  workday:   ``{tenant}/{wdN}/{site}``   (all three are needed for the /wday/cxs/ JSON API)
  icims:     the subdomain before .icims.com (e.g. ``careers-acme``)
  bamboohr:  the subdomain before .bamboohr.com
"""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit

KNOWN_ATS = ("greenhouse", "lever", "ashby", "workable", "smartrecruiters", "workday", "icims",
             "bamboohr", "jobvite", "recruitee")


@dataclass(frozen=True)
class AtsRef:
    ats: str
    token: str | None  # company board token/slug; None when the URL doesn't reveal it
    job_id: str | None = None
    # Normalized posting URL, kept for ATSs (Workday) whose canonical URL can't be rebuilt from
    # token + job id alone.
    url: str | None = None

    @property
    def canonical_url(self) -> str | None:
        """Stable posting URL for dedup, or None if token/job id are unknown."""
        if not self.job_id:
            return None
        t, j = self.token, self.job_id
        match self.ats:
            case "greenhouse" if t:
                return f"https://job-boards.greenhouse.io/{t}/jobs/{j}"
            case "lever" if t:
                return f"https://jobs.lever.co/{t}/{j}"
            case "ashby" if t:
                return f"https://jobs.ashbyhq.com/{t}/{j}"
            case "workable":
                return (f"https://apply.workable.com/{t}/j/{j}/" if t
                        else f"https://apply.workable.com/j/{j}")
            case "smartrecruiters" if t:
                return f"https://jobs.smartrecruiters.com/{t}/{j}"
            case "workday":
                return self.url
            case "icims" if t:
                return f"https://{t}.icims.com/jobs/{j}/job"
            case "bamboohr" if t:
                return f"https://{t}.bamboohr.com/careers/{j}"
            case "jobvite" if t:
                return f"https://jobs.jobvite.com/{t}/job/{j}"
            case "recruitee" if t:
                return f"https://{t}.recruitee.com/o/{j}"
        return None

    @property
    def board_url(self) -> str | None:
        """Public board API/landing URL for the company, when the token is known."""
        t = self.token
        if not t:
            return None
        return {
            "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{t}/jobs",
            "lever": f"https://api.lever.co/v0/postings/{t}?mode=json",
            "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{t}",
            "workable": f"https://apply.workable.com/api/v1/widget/accounts/{t}",
            "smartrecruiters": f"https://api.smartrecruiters.com/v1/companies/{t}/postings",
        }.get(self.ats)


_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_SEG = r"[^/?#]+"
_IGNORED_TOKENS = {"embed", "api", "v0", "v1", "jobs", "j", "careers", "search", "o"}


def _workday_job(segs: list[str]) -> str | None:
    """Requisition id from a Workday job path: ``.../job/{location}/{Title}_{JR123}``."""
    if "job" not in segs or segs[-1] == "job" or "_" not in segs[-1]:
        return None
    return segs[-1].rsplit("_", 1)[1] or None


def _workday_url(host: str, segs: list[str]) -> str | None:
    if "job" not in segs or _workday_job(segs) is None:
        return None
    if segs and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", segs[0]):  # drop locale prefix
        segs = segs[1:]
    return f"https://{host}/" + "/".join(segs)


def _on(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _q(query: dict[str, list[str]], key: str) -> str | None:
    v = query.get(key)
    return v[0].strip() if v and v[0].strip() else None


def _tok(s: str | None) -> str | None:
    if not s:
        return None
    s = unquote(s).strip()
    return None if not s or s.lower() in _IGNORED_TOKENS else s


def parse_ats_url(url: str | None) -> AtsRef | None:
    """Return the ATS reference for a URL, or None if it isn't a recognized ATS URL."""
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if "://" not in url:
        url = "https://" + url.lstrip("/")
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    query = parse_qs(parts.query)
    segs = [s for s in path.split("/") if s]

    # ---- Greenhouse --------------------------------------------------------------------
    if _on(host, "greenhouse.io"):
        if host.startswith("boards-api.") or host.startswith("api."):
            # /v1/boards/{t}/jobs[/{id}]
            m = re.match(rf"^/v1/boards/({_SEG})(?:/jobs(?:/(\d+))?)?", path)
            if m:
                return AtsRef("greenhouse", _tok(m.group(1)), m.group(2))
            return None
        if segs[:1] == ["embed"]:  # /embed/job_app?for=t&token=id, /embed/job_board?for=t
            return AtsRef("greenhouse", _tok(_q(query, "for")),
                          _q(query, "token") or _q(query, "gh_jid"))
        if segs:
            token = _tok(segs[0])
            job_id = None
            m = re.match(rf"^/{_SEG}/jobs/(\d+)", path)
            if m:
                job_id = m.group(1)
            job_id = job_id or _q(query, "gh_jid") or _q(query, "token")
            if token:
                return AtsRef("greenhouse", token, job_id)
        return None
    if (jid := _q(query, "gh_jid")) and jid.isdigit():
        # company careers page embedding a Greenhouse board: token unknown from the URL
        return AtsRef("greenhouse", _tok(_q(query, "for")), jid)

    # ---- Lever ---------------------------------------------------------------------------
    if _on(host, "lever.co"):
        if host.startswith("api."):  # /v0/postings/{t}[/{id}]
            m = re.match(rf"^/v0/postings/({_SEG})(?:/({_UUID}))?", path, re.I)
            return AtsRef("lever", _tok(m.group(1)), m.group(2)) if m else None
        if host.startswith("jobs.") and segs:
            job = segs[1] if len(segs) > 1 and re.fullmatch(_UUID, segs[1], re.I) else None
            return AtsRef("lever", _tok(segs[0]), job and job.lower())
        return None

    # ---- Ashby ---------------------------------------------------------------------------
    if _on(host, "ashbyhq.com"):
        if host.startswith("api."):  # /posting-api/job-board/{t}
            m = re.match(rf"^/posting-api/job-board/({_SEG})", path)
            return AtsRef("ashby", _tok(m.group(1))) if m else None
        if host.startswith("jobs.") and segs:
            job = segs[1] if len(segs) > 1 and re.fullmatch(_UUID, segs[1], re.I) else None
            return AtsRef("ashby", _tok(segs[0]), job and job.lower())
        return None
    if (jid := _q(query, "ashby_jid")) and re.fullmatch(_UUID, jid, re.I):
        return AtsRef("ashby", None, jid.lower())

    # ---- Workable ------------------------------------------------------------------------
    if _on(host, "workable.com"):
        if host in ("apply.workable.com", "www.apply.workable.com"):
            if segs[:3] == ["api", "v1", "widget"] and len(segs) >= 5:  # /api/v1/widget/accounts/t
                return AtsRef("workable", _tok(segs[4]))
            if segs[:1] == ["api"]:
                m = re.match(rf"^/api/v\d/accounts/({_SEG})(?:/jobs/({_SEG}))?", path)
                return AtsRef("workable", _tok(m.group(1)), m.group(2)) if m else None
            if segs[:1] == ["j"] and len(segs) > 1:  # /j/{shortcode}
                return AtsRef("workable", None, segs[1].upper())
            if segs:
                job = segs[2].upper() if len(segs) > 2 and segs[1] == "j" else None
                return AtsRef("workable", _tok(segs[0]), job)
            return None
        sub = host.removesuffix(".workable.com")
        if sub and sub not in ("www", "apply", "jobs") and "." not in sub:  # {t}.workable.com
            job = segs[1].upper() if len(segs) > 1 and segs[0] in ("jobs", "j") else None
            return AtsRef("workable", sub, job)
        return None

    # ---- SmartRecruiters -----------------------------------------------------------------
    if _on(host, "smartrecruiters.com"):
        if host.startswith("api."):  # /v1/companies/{t}/postings[/{id}]
            m = re.match(rf"^/v1/companies/({_SEG})(?:/postings(?:/(\d+))?)?", path)
            return AtsRef("smartrecruiters", _tok(m.group(1)), m.group(2)) if m else None
        if host.split(".")[0] in ("jobs", "careers", "www") and segs:
            if segs[0] in ("oneclick-ui", "external-referrals"):
                return None
            job = None
            if len(segs) > 1 and (m := re.match(r"^(\d{6,})", segs[1])):
                job = m.group(1)
            return AtsRef("smartrecruiters", _tok(segs[0]), job)
        return None

    # ---- Workday -------------------------------------------------------------------------
    m = re.match(r"^([a-z0-9-]+)\.(wd\d+)\.myworkday(?:jobs|site)\.com$", host)
    if m:
        tenant, wd = m.group(1), m.group(2)
        rest = segs
        if rest[:3] == ["wday", "cxs", tenant] and len(rest) >= 4:  # JSON API
            site = rest[3]
        else:
            if rest and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", rest[0]):  # locale prefix
                rest = rest[1:]
            if rest[:1] == ["recruiting"] and len(rest) >= 3:  # /recruiting/{tenant}/{site}
                rest = rest[2:]
            site = rest[0] if rest else None
        if not site:
            return AtsRef("workday", None)
        return AtsRef("workday", f"{tenant}/{wd}/{site}", _workday_job(segs),
                      _workday_url(host, segs))
    m = re.match(r"^(wd\d+)\.myworkdaysite\.com$", host)
    if m and segs:
        # wd3.myworkdaysite.com/[locale/]recruiting/{tenant}/{site}/job/...
        if re.fullmatch(r"[a-z]{2}-[A-Z]{2}", segs[0]):
            segs = segs[1:]
        if segs[:1] != ["recruiting"] or len(segs) < 3:
            return None
        tenant, site = segs[1], segs[2]
        return AtsRef("workday", f"{tenant}/{m.group(1)}/{site}", _workday_job(segs),
                      _workday_url(host, segs))

    # ---- iCIMS ---------------------------------------------------------------------------
    if host.endswith(".icims.com"):
        sub = host.removesuffix(".icims.com")
        m = re.match(r"^/jobs/(\d+)", path)
        return AtsRef("icims", sub or None, m.group(1) if m else None)

    # ---- BambooHR ------------------------------------------------------------------------
    if host.endswith(".bamboohr.com"):
        sub = host.removesuffix(".bamboohr.com")
        if sub in ("www", "api"):
            return None
        job = None
        if (m := re.match(r"^/(?:careers|jobs)/(\d+)", path)):
            job = m.group(1)
        elif path.startswith("/jobs/view.php"):
            job = _q(query, "id")
        return AtsRef("bamboohr", sub, job)

    # ---- Jobvite -------------------------------------------------------------------------
    if _on(host, "jobvite.com"):
        if host.startswith("jobs.") and segs:
            job = segs[2] if len(segs) > 2 and segs[1] == "job" else None
            return AtsRef("jobvite", _tok(segs[0]), job)
        if host.startswith("app.") and (segs[:1] == ["j"] or "CompanyJobs" in path):
            return AtsRef("jobvite", _q(query, "c"), _q(query, "cj") or _q(query, "j"))
        return None

    # ---- Recruitee -----------------------------------------------------------------------
    if host.endswith(".recruitee.com"):
        sub = host.removesuffix(".recruitee.com")
        job = segs[1] if len(segs) > 1 and segs[0] == "o" else None
        return AtsRef("recruitee", sub, job) if sub not in ("www", "api") else None

    return None


def canonical_url(url: str | None) -> str | None:
    """Canonical ATS posting URL for ``url`` if recognizable, else None."""
    ref = parse_ats_url(url)
    return ref.canonical_url if ref else None


_HREF = re.compile(r"""href\s*=\s*["']([^"']+)["']|(https?://[^\s"'<>)\]]+)""", re.I)


def find_ats_link(text: str | None) -> tuple[str, AtsRef] | None:
    """First link in an HTML/plain-text blob that points at a posting on a known ATS.

    Links carrying a job id win over bare board links. Returns (url, ref) or None.
    """
    if not text:
        return None
    board_only: tuple[str, AtsRef] | None = None
    for m in _HREF.finditer(_html.unescape(text)):
        url = (m.group(1) or m.group(2) or "").strip().rstrip(".,;")
        ref = parse_ats_url(url) if url.lower().startswith("http") else None
        if ref is None or not ref.token and not ref.job_id:
            continue
        if ref.job_id and ref.token:
            return url, ref
        board_only = board_only or (url, ref)
    return board_only
