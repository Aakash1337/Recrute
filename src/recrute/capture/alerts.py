"""Parse job-alert emails into RawJobs (PLAN.md §3.2 Tier 3, passive capture).

Supported: LinkedIn job alerts (source="linkedin_alert"), plus generic Indeed
("indeed_alert") and Glassdoor ("glassdoor_alert") alerts. HTML parts are preferred (cards with
links); LinkedIn's text/plain part is a fallback. Alert emails are read-only input: no links are
followed and nothing is fetched from the sites.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup, Tag

from recrute.capture.urls import linkedin_job_id, linkedin_job_url, unwrap_redirect
from recrute.schemas import RawJob
from recrute.track.mail import MailMessage

# Lines inside an alert card that are not title/company/location.
_NOISE = re.compile(
    r"^(?:view job|view jobs?|see (?:all )?jobs?|apply(?: now)?|easy apply|quick apply|"
    r"easily apply|actively recruiting|promoted|new|just posted|be an early applicant|"
    r"early applicant|responds? within.*|\d+ (?:connections?|alumni|applicants?|school alumni).*|"
    r"(?:\d+|one) (?:company )?alumn.*|your profile matches.*|top applicant|"
    r"\d+\s*(?:[hdwm]|hours?|days?|weeks?|months?)\s*ago|posted .*ago|today|"
    r"urgently hiring|hiring multiple candidates|responsive employer|sponsored|"
    r"[\d.]+\s*★?|[\d.]+ out of 5 stars?|rating .*|save|dismiss|unsubscribe.*|"
    r"see more jobs.*|jobs? similar to.*|manage (?:job )?alerts?.*|premium.*|"
    r"this company is actively hiring|medical, dental.*|401\(k\).*|·)$",
    re.I,
)
_SALARY = re.compile(
    r"\$\s?(?P<lo>\d[\d,.]*)\s*(?P<lok>[kK])?(?:\s*(?:-|–|to)\s*\$?\s?(?P<hi>\d[\d,.]*)\s*"
    r"(?P<hik>[kK])?)?\s*(?:/\s*|an?\s+|per\s+)?(?P<unit>yr|year|hr|hour|mo|month)?", re.I)
_UNIT = {"yr": 1, "year": 1, "hr": 2080, "hour": 2080, "mo": 12, "month": 12}
_REMOTE = re.compile(r"\((remote|hybrid|on-?site)\)|\b(remote|hybrid|on-?site)\b", re.I)


@dataclass
class _Card:
    job_id: str
    url: str
    lines: list[str]  # starts with the title
    pre: list[str] = field(default_factory=list)  # card text before the title (Glassdoor)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace(" ", " ")).strip()


def _salary(text: str) -> tuple[int | None, int | None]:
    m = _SALARY.search(text)
    if not m:
        return None, None

    def val(num: str | None, k: str | None) -> float | None:
        if not num:
            return None
        try:
            v = float(num.replace(",", ""))
        except ValueError:
            return None
        return v * 1000 if k else v

    lo = val(m.group("lo"), m.group("lok") or (m.group("hik") if m.group("hi") else None))
    hi = val(m.group("hi"), m.group("hik"))
    factor = _UNIT.get((m.group("unit") or "yr").lower(), 1)
    if lo is not None and lo < 1000 and factor == 1 and not m.group("lok"):
        factor = 2080 if lo < 300 else 1  # "$45 - $60" with no unit: hourly
    return (int(lo * factor) if lo else None, int(hi * factor) if hi else None)


def _remote(text: str) -> str | None:
    m = _REMOTE.search(text)
    if not m:
        return None
    v = (m.group(1) or m.group(2)).lower().replace("-", "")
    return {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}[v]


def _card_container(a: Tag, id_of: Callable[[str], str | None], job_id: str) -> Tag:
    """Largest ancestor of `a` whose job links all point to the same job (= that job's card)."""
    best: Tag = a
    node = a.parent
    while isinstance(node, Tag) and node.name not in ("body", "html", "[document]"):
        ids = {id_of(x.get("href", "")) for x in node.find_all("a", href=True)}
        ids.discard(None)
        if ids and ids != {job_id}:
            break
        best = node
        node = node.parent
    return best


def _lines(el: Tag) -> list[str]:
    for br in el.find_all("br"):
        br.replace_with("\n")
    raw = el.get_text("\n")
    out: list[str] = []
    for ln in raw.split("\n"):
        ln = _clean(ln)
        if not ln or _NOISE.match(ln):
            continue
        if not out or out[-1] != ln:
            out.append(ln)
    return out


def _html_cards(html: str, id_of: Callable[[str], str | None],
                canonical: Callable[[str, str], str]) -> list[_Card]:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["style", "script", "head"]):
        t.decompose()
    order: list[str] = []
    anchors: dict[str, list[Tag]] = {}
    for a in soup.find_all("a", href=True):
        jid = id_of(a["href"])
        if jid is None:
            continue
        if jid not in anchors:
            order.append(jid)
            anchors[jid] = []
        anchors[jid].append(a)
    cards: list[_Card] = []
    for jid in order:
        first = anchors[jid][0]
        container = _card_container(first, id_of, jid)
        lines = _lines(container)
        # Prefer the anchor text that looks like a title (not "View job", not a logo alt).
        title = next((_clean(a.get_text(" ")) for a in anchors[jid]
                      if _clean(a.get_text(" ")) and not _NOISE.match(_clean(a.get_text(" ")))),
                     None)
        pre: list[str] = []
        if title and title in lines:
            i = lines.index(title)
            pre, lines = lines[:i], lines[i:]
        elif title:
            lines = [title, *lines]
        if lines:
            cards.append(_Card(jid, canonical(jid, first["href"]), lines, pre))
    return cards


def _split_company_location(lines: list[str]) -> tuple[str | None, str | None, str]:
    """lines[0] is the title. LinkedIn uses 'Company · Location'; Indeed/Glassdoor use separate
    lines. Returns (company, location, remaining text for salary/remote hints)."""
    rest = lines[1:]
    if not rest:
        return None, None, ""
    first = rest[0]
    if " · " in first or "•" in first:
        parts = [p.strip() for p in re.split(r"\s+[·•]\s+", first) if p.strip()]
        company = parts[0]
        location = parts[1] if len(parts) > 1 else (rest[1] if len(rest) > 1 else None)
        extra = " ".join(parts[2:] + rest[1:])
        return company, location, extra
    company = first
    location = rest[1] if len(rest) > 1 and not _SALARY.search(rest[1]) else None
    extra = " ".join(rest[1:])
    return company, location, extra


def _rating_strip(company: str) -> str:
    return re.sub(r"\s+\d(?:\.\d)?\s*★?\s*$", "", company).strip()


def _to_raw(card: _Card, source: str, *, company_first: bool = False) -> RawJob | None:
    """company_first: the card shows the company above the title (Glassdoor)."""
    title = card.lines[0]
    if company_first and card.pre:
        company: str | None = card.pre[-1]
        rest = card.lines[1:]
        location = rest[0] if rest and not _SALARY.search(rest[0]) else None
        extra = " ".join(rest)
    else:
        company, location, extra = _split_company_location(card.lines)
    if not company:
        return None
    company = _rating_strip(company)
    lo, hi = _salary(extra)
    remote = _remote(f"{location or ''} {extra}")
    loc = re.sub(r"\s*\((?:remote|hybrid|on-?site)\)\s*$", "", location or "", flags=re.I)
    return RawJob(source=source, source_job_id=card.job_id, url=card.url, title=title[:300],
                  company=company[:200], locations=[loc] if loc else [], remote=remote,
                  salary_min=lo, salary_max=hi, salary_currency="USD" if lo or hi else None)


# --------------------------------------------------------------------------- LinkedIn


def _li_id(href: str) -> str | None:
    return linkedin_job_id(unwrap_redirect(href))


def _li_text_cards(text: str) -> list[_Card]:
    """LinkedIn text/plain alert: blocks of title / company / location / 'View job: URL'."""
    cards: list[_Card] = []
    buf: list[str] = []
    for raw_line in text.splitlines():
        line = _clean(raw_line)
        m = re.search(r"(https?://\S*linkedin\.com/\S*jobs/view/\S+)", line)
        if m:
            jid = _li_id(m.group(1))
            lines = [x for x in buf if not _NOISE.match(x)]
            if jid and lines:
                cards.append(_Card(jid, linkedin_job_url(jid), lines))
            buf = []
            continue
        if not line or re.fullmatch(r"[-=_*]{3,}", line):
            buf = []  # cards are runs of consecutive lines
            continue
        buf.append(line)
    return cards


def parse_linkedin_alert(msg: MailMessage) -> list[RawJob]:
    cards: list[_Card] = []
    if msg.html:
        cards = _html_cards(msg.html, _li_id, lambda jid, _href: linkedin_job_url(jid))
    if not cards and msg.text:
        cards = _li_text_cards(msg.text)
    return _dedupe([_to_raw(c, "linkedin_alert") for c in cards])


# --------------------------------------------------------------------------- Indeed / Glassdoor


def _indeed_id(href: str) -> str | None:
    u = unwrap_redirect(href)
    p = urlparse(u)
    if "indeed." not in p.netloc.lower():
        return None
    q = parse_qs(p.query)
    for key in ("jk", "vjk"):
        if key in q and re.fullmatch(r"[0-9a-f]{8,20}", q[key][0], re.I):
            return q[key][0].lower()
    return None


def parse_indeed_alert(msg: MailMessage) -> list[RawJob]:
    if not msg.html:
        return []
    cards = _html_cards(msg.html, _indeed_id,
                        lambda jid, _h: f"https://www.indeed.com/viewjob?jk={jid}")
    return _dedupe([_to_raw(c, "indeed_alert") for c in cards])


def _glassdoor_id(href: str) -> str | None:
    u = unwrap_redirect(href)
    p = urlparse(u)
    if "glassdoor." not in p.netloc.lower():
        return None
    q = parse_qs(p.query)
    for key in ("jobListingId", "jl"):
        if key in q and q[key][0].isdigit():
            return q[key][0]
    m = re.search(r"_JV_\w*?(\d{6,})|[-_](\d{6,})\.htm", p.path)
    if m:
        return m.group(1) or m.group(2)
    return None


def parse_glassdoor_alert(msg: MailMessage) -> list[RawJob]:
    if not msg.html:
        return []
    cards = _html_cards(
        msg.html, _glassdoor_id,
        lambda jid, _h: f"https://www.glassdoor.com/job-listing/index.htm?jl={jid}")
    return _dedupe([_to_raw(c, "glassdoor_alert", company_first=True) for c in cards])


# --------------------------------------------------------------------------- dispatch


def _dedupe(jobs: list[RawJob | None]) -> list[RawJob]:
    seen: set[str] = set()
    out: list[RawJob] = []
    for j in jobs:
        if j is None or j.url in seen:
            continue
        seen.add(j.url)
        out.append(j)
    return out


_STATUS_SUBJECT = re.compile(r"\b(applications?|applied|applying|interview\w*|offer|"
                             r"assessment|candidacy|status|received|viewed|messages?)\b")


def alert_kind(msg: MailMessage) -> str | None:
    """"linkedin" | "indeed" | "glassdoor" | None. The ONE job-alert classifier: inbox routing
    (track.classify.is_alert_mail) uses it too, so a parseable alert is never sent to
    application tracking, and an application update is never mistaken for an alert."""
    d = msg.sender_domain
    subj = msg.subject.lower()
    if _STATUS_SUBJECT.search(subj):
        return None  # "Indeed Application: ...", "Your application was viewed", interviews
    if d.endswith("linkedin.com") and (
            msg.sender.startswith(("jobalerts-noreply", "jobs-alerts", "jobs-listings"))
            or "job alert" in subj or re.search(r"\bnew jobs?\b|\bjobs? (?:similar|for you)",
                                                subj)):
        return "linkedin"
    if "indeed." in d and ("alert" in msg.sender or "job" in subj):
        return "indeed"
    if "glassdoor." in d and ("job" in subj or "alert" in msg.sender):
        return "glassdoor"
    return None


def parse_alert(msg: MailMessage) -> list[RawJob]:
    """RawJobs from a job-alert email ([] if it isn't one)."""
    kind = alert_kind(msg)
    if kind == "linkedin":
        return parse_linkedin_alert(msg)
    if kind == "indeed":
        return parse_indeed_alert(msg)
    if kind == "glassdoor":
        return parse_glassdoor_alert(msg)
    return []
