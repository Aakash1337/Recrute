"""What the job page shows, worked out from what is already stored (no AI call):

- the posting split into readable parts by its own headings: the role, what you'd do, what they
  ask for, what they offer, about the company, the fine print;
- "at a glance" facts (pay, where, work mode, experience, how you'd apply);
- the fit summary in plain words;
- watch-outs found in the posting text (clearance, citizenship, export control, no sponsorship)
  and benefits it mentions;
- a readable history.

Everything here is display-only: nothing is written back to the job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from recrute.web import humanize as hz

# --------------------------------------------------------------------------- posting sections

# A heading is a markdown heading, or a short line that is bold from end to end ("**About us**").
_MD_HEADING = re.compile(r"^#{1,6}\s+(?P<t>.+?)\s*#*$")
_BOLD_HEADING = re.compile(r"^(?:\*\*|__)(?P<t>[^*_].{0,88}?)(?:\*\*|__)\s*:?$")
_HEADING_JUNK = re.compile(r"[*_#`]+")
# (a leading emoji or bullet in a heading says nothing about what the section is)
_HEADING_LEAD = re.compile(r"^[^\w(\"'“]+")

GROUPS = [  # key, title, one-line hint
    ("role", "The role", "What the job is, in the employer's words."),
    ("do", "What you'd do", "The day-to-day work and responsibilities."),
    ("need", "What they're asking for", "Skills, experience and qualifications."),
    ("offer", "What they offer", "Pay, benefits and perks."),
    ("company", "About the company", "Who they are and what they do."),
    ("logistics", "Location and logistics", "Where and how the work happens."),
    ("other", "More from the posting", None),
    ("fine", "Fine print", "Legal notices and policies."),
]
GROUP_TITLES = {k: t for k, t, _ in GROUPS}

# Checked in order: the first rule that matches a heading decides its group.
_RULES: list[tuple[str, re.Pattern[str]]] = [(g, re.compile(rx, re.IGNORECASE)) for g, rx in [
    ("fine", r"equal opportunit|\beeo\b|accommodation|privacy|\bnotice\b|disclaimer|consent|"
             r"e-?verify|pay transparency|anti-discrimination|safe hiring|fair chance|"
             r"inclusion|diversity|background check|recruit(?:ing|ment) (?:fraud|scam)"),
    ("nice", r"nice[- ]to[- ]haves?|preferred|bonus|\bplus(?:es)?\b|even better|extra credit|"
             r"desired|\bideally\b|great if|may also have|additional experience|stand ?out"),
    ("offer", r"benefit|perk|what we offer|we offer|compensation|salary|\bpay\b|equity|"
              r"total rewards|what you(?:'|’)?ll get|time off|wellness|"
              r"why (?:join|work|you should|you(?:'|’)ll love)|what(?:'|’)s in it for you|"
              r"reasons to (?:join|work)"),
    ("team", r"about the team|\bthe team\b|team fit|your team"),
    ("role", r"about the (?:role|job|position|opportunity)|^the (?:role|opportunity|job)\b|"
             r"^your role|(?:job|position|role) (?:summary|overview|description)|"
             r"general description|^(?:summary|overview)\b|summary/objectives"),
    ("do", r"what you(?:'|’)?(?:ll| will) (?:do|be doing|work on|own)|responsibilit|"
           r"in this role,? you(?:'|’)?(?:ll| will)|^you(?:'|’)?(?:ll| will)\b|"
           r"essential (?:job )?functions|key duties|example initiatives|"
           r"what success looks like|day[- ]to[- ]day|your (?:impact|opportunity)|"
           r"how you(?:'|’)?ll|what you(?:'|’)?ll achieve"),
    ("logistics", r"location|logistics|travel|schedule|work environment|physical requirements|"
                  r"interview|to apply|how to apply|working hours|on-?site"),
    ("need", r"requirement|qualification|what (?:we(?:'|’)?re|we are|we) (?:looking for|require|"
             r"value|need)|what you(?:'|’)?(?:ll)? (?:bring|need|have)|who you are|"
             r"you (?:might|may|(?:'|’)ll|will) (?:thrive|be a (?:good |great )?fit)|about you|"
             r"skills|experience|must[- ]haves?|^required\b|^technical\b|candidate criteria|"
             r"we(?:'|’)d love to hear from you|you have\b|you might excel|"
             r"your background|\bideal\b|candidates (?:must|may)|^you are\b|"
             r"core competencies|how you operate"),
    ("company", r"^about\b|who we are|who is\b|our (?:mission|values|story|culture)|"
                r"company overview|how we(?:'|’)re different|what makes .+ (?:special|great)|"
                r"life at|^why\b|a world-changing|come work with us|our team|^we are\b|"
                r"^the mission\b"),
]]


@dataclass
class Section:
    heading: str  # as the posting wrote it ("" for text before the first heading)
    body: str     # markdown
    group: str


@dataclass
class Group:
    key: str
    title: str
    hint: str | None
    sections: list[Section] = field(default_factory=list)
    extra: list[Section] = field(default_factory=list)  # "nice to have", "about the team"

    @property
    def count(self) -> int:
        return len(self.sections) + len(self.extra)


def _heading_text(line: str) -> str | None:
    line = line.strip()
    if not line or len(line) > 110:
        return None
    m = _MD_HEADING.match(line) or _BOLD_HEADING.match(line)
    if not m:
        return None
    text = _HEADING_JUNK.sub("", m.group("t")).strip(" :​")
    # a bold SENTENCE is emphasis, not a heading
    if not text or text.endswith((".", ",", ";")) or len(text.split()) > 12:
        return None
    return text


def classify_heading(heading: str) -> str:
    """The group a posting's heading belongs to ("Preferred qualifications" -> nice)."""
    text = _HEADING_LEAD.sub("", heading).strip()
    for group, rx in _RULES:
        if rx.search(text):
            return group
    return "other"


# Paragraphs that postings tack on under whatever heading came last: pay and benefits boilerplate,
# legal statements, and tracking tags. They are moved to where they belong.
_PARA_OFFER = re.compile(r"\b(?:base )?(?:pay|salary|compensation) range\b|"
                         r"benefits (?:which |that )?include|\bbase (?:pay|salary)\b|"
                         r"total (?:rewards|compensation)|"
                         r"eligible for (?:variable|a bonus|equity|benefits)", re.IGNORECASE)
_PARA_FINE = re.compile(r"equal (?:employment )?opportunit|\bEEO\b|reasonable accommodation|"
                        r"e-?verify|background check|without regard to|protected veteran|"
                        r"privacy (?:notice|policy)|fair chance", re.IGNORECASE)
_PARA_AUTH = re.compile(r"\bsponsor(?:ship)?\b|authori[sz]ed to work|work authori[sz]ation|"
                        r"\b(?:OPT|CPT|H-?1B)\b", re.IGNORECASE)
_TAG_LINE = re.compile(r"^(?:#[A-Za-z][\w-]*\s*)+$")
_LIST_LINE = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s")


def _rehome(sections: list[Section]) -> list[Section]:
    """Move tacked-on pay, legal and tag paragraphs out of the sections they trail."""
    out: list[Section] = []
    offer: list[str] = []
    fine: list[str] = []
    auth: list[str] = []
    for s in sections:
        if s.group in ("offer", "fine"):
            out.append(s)
            continue
        kept: list[str] = []
        for para in re.split(r"\n\s*\n", s.body):
            text = para.strip()
            if not text or _TAG_LINE.match(text):
                continue
            if _LIST_LINE.match(text):  # (a requirement mentioning "background check" stays)
                kept.append(para)
            elif _PARA_AUTH.search(text) and s.group != "need" and len(text) < 700:
                auth.append(text)
            elif _PARA_FINE.search(text):
                fine.append(text)
            elif _PARA_OFFER.search(text) or _PAY_IN_TEXT.fullmatch(text.strip("*_ ")):
                offer.append(text)
            else:
                kept.append(para)
        if kept:
            out.append(Section(s.heading, "\n\n".join(kept).strip(), s.group))
    if auth:
        out.append(Section("Work authorization", "\n\n".join(auth), "need"))
    if offer:
        out.append(Section("Pay and benefits", "\n\n".join(offer), "offer"))
    if fine:
        out.append(Section("", "\n\n".join(fine), "fine"))
    return out


def split_posting(description_md: str | None) -> list[Section]:
    """The posting cut at its own headings, each part classified. Text before the first heading
    is the introduction."""
    lines = (description_md or "").splitlines()
    sections: list[Section] = []
    heading, body = "", []

    def close() -> None:
        text = "\n".join(body).strip()
        if text:
            sections.append(Section(heading, text,
                                    classify_heading(heading) if heading else "intro"))

    for line in lines:
        h = _heading_text(line)
        if h is not None:
            close()
            heading, body = h, []
        else:
            body.append(line)
    close()
    return _rehome(sections)


def group_posting(description_md: str | None) -> list[Group]:
    """The posting's parts under plain titles, in reading order; [] when the posting has too
    little structure to split (then it is shown whole)."""
    sections = split_posting(description_md)
    headed = [s for s in sections if s.heading]
    if len(headed) < 2:
        return []
    by_key = {k: Group(k, t, h) for k, t, h in GROUPS}
    for s in sections:
        if s.group == "intro":
            by_key["role"].sections.insert(0, s)
        elif s.group == "nice":
            by_key["need"].extra.append(s)
        elif s.group == "team":
            by_key["role"].extra.append(s)
        else:
            by_key[s.group].sections.append(s)
    return [g for g in by_key.values() if g.count]


# --------------------------------------------------------------------------- posting insights

_PERKS: list[tuple[str, re.Pattern[str]]] = [(label, re.compile(rx, re.IGNORECASE))
                                             for label, rx in [
    ("Health coverage", r"\b(?:medical|health|dental|vision)\s+(?:insurance|coverage|plans?|"
                        r"benefits|care)\b|\bhealthcare\b|\bmedical, dental\b|"
                        r"\bdental (?:and|&|,) vision\b"),
    ("401(k)", r"\b401\s*\(?k\)?"),
    ("Equity", r"\bequity\b|\bstock options?\b|\bRSUs?\b"),
    ("Bonus", r"\b(?:annual|performance|signing|sign-on|target)\s+bonus|\bbonus(?:es)? "
              r"(?:eligible|plan|program)"),
    ("Paid time off", r"\bPTO\b|paid time off|unlimited (?:vacation|time off)|"
                      r"flexible (?:vacation|time off)|paid (?:vacation|holidays)"),
    ("Parental leave", r"parental leave|maternity|paternity|family leave"),
    ("Learning budget", r"(?:learning|education|professional development|tuition|training)\s+"
                        r"(?:budget|stipend|reimbursement|allowance|assistance)"),
    ("Home-office stipend", r"(?:home[- ]office|remote|wfh|work[- ]from[- ]home)\s+"
                            r"(?:stipend|budget|allowance|setup)"),
    ("Wellness support", r"wellness (?:stipend|benefit|program|allowance)|mental health"),
    ("Relocation help", r"relocation (?:assistance|support|package|bonus)"),
]]

# "$120,000 - $150,000", "$120k–$150k", "$55 to $70 per hour": shown exactly as written
_PAY_IN_TEXT = re.compile(
    r"(?:USD\s*)?\$\s?\d{1,3}(?:,\d{3})*(?:\.\d+)?\s?[kK]?\s*(?:-|–|—|to)\s*(?:USD\s*)?\$?\s?"
    r"\d{1,3}(?:,\d{3})*(?:\.\d+)?\s?[kK]?(?:\s*(?:USD|per (?:year|hour)|/\s?(?:yr|year|hr|hour)|"
    r"annually|an hour|a year))?")

_WATCH = {
    "clearance_required": ("Security clearance",
                           "The posting asks for a security clearance, or the ability to get one."),
    "citizenship_required": ("US citizenship",
                             "The posting says US citizenship or permanent residency is "
                             "required."),
    "itar_us_person": ("Export-control rules",
                       "The posting mentions ITAR/EAR or \"U.S. person\" rules, which limit "
                       "who can hold the role."),
}


def perks(text: str | None) -> list[str]:
    """Benefits the posting mentions by name (labels only; the wording is in the posting)."""
    return [label for label, rx in _PERKS if text and rx.search(text)]


def pay_mentions(text: str | None, limit: int = 2) -> list[str]:
    """Pay ranges written in the posting, exactly as written."""
    seen: dict[str, None] = {}
    for m in _PAY_IN_TEXT.finditer(text or ""):
        seen[" ".join(m.group(0).split())] = None
        if len(seen) >= limit:
            break
    return list(seen)


_STUDENT_VISA = re.compile(r"\b(?:F-?1[- ]?)?(?:OPT|CPT)\b")
_NO_SPONSOR = re.compile(r"\b(?:unable|not able|cannot|can(?:'|’)t|do(?:es)? not|will not|"
                         r"won(?:'|’)t|without|no)\b[^.]{0,80}\bsponsor", re.IGNORECASE)


def student_visa_exclusion(text: str | None) -> str:
    """The posting's own sentence(s) ruling out OPT/CPT ("unable to sponsor ... This includes
    F1-OPT, F1-CPT, H-1B"), or "". Some employers that "don't sponsor" still take OPT; this
    only fires when the posting names it."""
    plain = re.sub(r"[*_`]+", "", text or "")
    for para in re.split(r"\n\s*\n", plain):
        para = " ".join(para.split())
        if len(para) < 900 and _STUDENT_VISA.search(para) and _NO_SPONSOR.search(para):
            return para if len(para) <= 320 else para[:317].rsplit(" ", 1)[0] + "…"
    return ""


def watch_outs(job: Any) -> list[dict[str, str]]:
    """Things in the posting worth a second look before applying."""
    from recrute.badges.sponsorship import eligibility_flags

    out = [{"title": t, "text": d, "tone": "danger"}
           for flag, (t, d) in _WATCH.items() if flag in eligibility_flags(job.description_md)]
    badges = job.badges or {}
    excluded = student_visa_exclusion(job.description_md)
    if excluded:
        out.append({"title": "May not accept OPT or CPT work authorization",
                    "text": f"The posting says: “{excluded}”", "tone": "danger"})
    elif badges.get("sponsorship") == "no_sponsorship":
        out.append({"title": "No visa sponsorship",
                    "text": "The posting says they won't sponsor a work visa.", "tone": "warn"})
    return out


# --------------------------------------------------------------------------- fit

# (the scorer only reads the start of a long posting and used to list that as a "gap")
_ARTIFACT = re.compile(r"\btruncat|description (?:is |was )?(?:cut|incomplete|partial)|"
                       r"(?:full|complete) (?:description|posting) (?:not|un)", re.IGNORECASE)


def fit_view(score: Any) -> dict | None:
    """The fit score as words: a label, the reason, where you match, gaps, concerns."""
    if score is None:
        return None
    details = score.details or {}

    def items(key: str) -> list[str]:
        return [str(x).strip() for x in (details.get(key) or [])
                if str(x).strip() and not _ARTIFACT.search(str(x))]

    n = score.score or 0
    if n >= 80:
        label, tone = "Strong match", "ok"
    elif n >= 65:
        label, tone = "Good match", "info"
    elif n >= 50:
        label, tone = "Possible match", "warn"
    else:
        label, tone = "Weak match", "neutral"
    seniority = {"entry": "Entry level", "mid": "Mid level",
                 "senior": "Senior level"}.get(details.get("seniority") or "")
    return {"score": n, "label": label, "tone": tone, "reason": score.reason,
            "meets": items("meets"), "gaps": items("gaps"), "concerns": items("red_flags"),
            "seniority": seniority, "scored_at": score.created_at}


# --------------------------------------------------------------------------- filter reasons

_FILTER_RULES: list[tuple[re.Pattern[str], Any]] = [(re.compile(p, re.IGNORECASE), r) for p, r in [
    (r"^title matches no target track$",
     lambda m: "The title doesn't match any of the roles you're looking for."),
    (r"^title excluded: (.+)$",
     lambda m: f"The title contains “{m.group(1).strip()}”, which you chose to leave out."),
    (r"^(?:LLM: )?location (?:outside the US|not US-eligible)$",
     lambda m: "The job isn't open to people working in the US."),
    (r"^security clearance required$", lambda m: "It requires a security clearance."),
    (r"^ITAR / US person required$",
     lambda m: "It is limited to “U.S. persons” under export-control rules."),
    (r"^(?:US )?citizenship required$", lambda m: "It requires US citizenship."),
    (r"^requires (\d+)\+ years$",
     lambda m: f"It asks for {m.group(1)}+ years of experience, more than your limit."),
    (r"^salary below floor$", lambda m: "The pay is below your minimum."),
    (r"^LLM: senior-level role$", lambda m: "It reads as a senior-level role."),
    (r"^score (\d+) below (\d+) for (P\d)$",
     lambda m: f"Its fit score of {m.group(1)} is below your minimum of {m.group(2)} for "
               f"{hz.priority_label(m.group(3))} roles."),
    (r"^employment type: (.+)$",
     lambda m: f"It is a {m.group(1).strip()} role, which you chose to leave out."),
    (r"^company excluded$", lambda m: "You chose to leave this company out."),
    (r"^LLM: (.+)$", lambda m: f"AI screening: {m.group(1).strip()}."),
]]


def filter_reason(reason: str | None) -> str:
    """Why a job was set aside, as a sentence."""
    text = (reason or "").strip()
    if not text:
        return ""
    for rx, render in _FILTER_RULES:
        m = rx.match(text)
        if m:
            return render(m)
    return text[0].upper() + text[1:] + ("" if text.endswith(".") else ".")


# --------------------------------------------------------------------------- history

_NOTE_RULES: list[tuple[re.Pattern[str], Any]] = [(re.compile(p, re.IGNORECASE), r) for p, r in [
    (r"^source=(.+)$", lambda m: f"Found on {hz.source_label(m.group(1).strip())}"),
    (r"^CP1 approve(?:: (.*))?$", lambda m: "You approved it, so a packet gets built"),
    (r"^CP1 reject(?:: (.*))?$",
     lambda m: "You rejected it" + (f" ({m.group(1).strip()})" if m.group(1) else "")),
    (r"^CP1 snooze(?:: (.*))?$", lambda m: "You snoozed it for a week"),
    (r"^CP1 manual(?:: (.*))?$", lambda m: "You chose to apply yourself"),
    (r"^CP2 approve(?:: (.*))?$", lambda m: "You approved the application packet"),
    (r"^CP2 skip(?:: (.*))?$", lambda m: "You skipped this application"),
    (r"^packet built(?: \((.*)\))?$",
     lambda m: "Application packet built" + (" with your note" if m.group(1) else "")),
    (r"^restored(?: .*)?$", lambda m: "You moved it back to review"),
    (r"^posting removed from board$", lambda m: "The employer took the posting down"),
]]


def event_text(event: Any) -> str:
    """One history line in plain words."""
    note = (event.note or "").strip()
    for rx, render in _NOTE_RULES:
        m = rx.match(note)
        if m:
            return render(m)
    label = hz.status_label(event.status)
    if not note:
        return label
    return f"{label}: {filter_reason(note).rstrip('.')}"


# --------------------------------------------------------------------------- the page

_EMPLOYMENT = {"full-time": "Full-time", "full_time": "Full-time", "fulltime": "Full-time",
               "part-time": "Part-time", "part_time": "Part-time", "contract": "Contract",
               "contractor": "Contract", "internship": "Internship", "intern": "Internship",
               "temporary": "Temporary"}

# Application systems Recrute can fill in for you (after your approval).
_AUTOFILL = {"greenhouse", "lever", "ashby", "workable", "linkedin_easy_apply"}


def apply_route(job: Any) -> dict[str, str]:
    """How an application to this job would be sent."""
    ats = (job.ats or "").lower()
    if ats in _AUTOFILL:
        return {"label": f"Via {hz.channel_label(ats)}",
                "text": "Recrute can fill in this form for you once you approve the packet."}
    return {"label": "On the employer's site",
            "text": "You apply yourself; Recrute prepares a tailored resume for it."}


_US_PLACE = re.compile(r"(?i:\bunited states\b|\busa\b|\bu\.s\.)|,\s?[A-Z]{2}$|\bUS\b")


def glance(job: Any, company: Any, fit: dict | None) -> list[dict[str, Any]]:
    """The facts strip at the top of a job: label, value and an optional note."""
    out: list[dict[str, Any]] = []

    def add(label: str, value: Any, note: str | None = None, title: str | None = None) -> None:
        if value not in (None, "", []):
            out.append({"label": label, "value": value, "note": note, "title": title})

    pay = hz.job_pay(job)
    mentions = [] if pay else pay_mentions(job.description_md)
    add("Pay", pay or (mentions[0] if mentions else "Not listed"),
        "As written in the posting" if mentions and not pay else None)
    add("Work style", hz.remote_label(job.remote) if job.remote else None)
    places = [p.strip() for p in (job.locations or []) if p and p.strip()]
    if len(places) > 3:  # (a posting open in 50 countries: the US one, and how many more)
        first = next((p for p in places if _US_PLACE.search(p)), places[0])
        add("Location", first, f"and {len(places) - 1} more places (hover for the list)",
            ", ".join(places))
    else:
        add("Location", " · ".join(places) or None)
    add("Job type", _EMPLOYMENT.get((job.employment_type or "").strip().lower()))
    add("Team", job.department)
    add("Experience", f"{job.years_required}+ years" if job.years_required else None)
    add("Level", fit["seniority"] if fit else None)
    posted = hz.format_date(job.posted_at) if job.posted_at else None
    how_long = hz.relative_time(job.posted_at) if job.posted_at else None
    # (an old posting's "how long ago" is the date itself: said once)
    add("Posted", posted, how_long if how_long != posted else None)
    route = apply_route(job)
    add("How you'd apply", route["label"], route["text"])
    return out


def job_view(job: Any, company: Any, score: Any, sources: list, events: list,
             siblings: list | None = None) -> dict[str, Any]:
    """Everything the job page shows beyond the raw rows."""
    fit = fit_view(score)
    text = job.description_md or ""
    groups = group_posting(text)
    status = job.status.value if hasattr(job.status, "value") else str(job.status)
    closed = getattr(job, "closed_at", None) is not None
    until = hz._to_datetime(getattr(job, "snoozed_until", None))
    snoozed = until is not None and until > datetime.now(UTC)
    return {
        "fit": fit,
        "glance": glance(job, company, fit),
        "groups": groups,
        "has_text": len(text.strip()) >= 200,
        "perks": perks(text),
        "pay_mentions": pay_mentions(text),
        "watch_outs": watch_outs(job),
        "filter_reason": filter_reason(job.filter_reason),
        "can_decide": status == "discovered" and not closed and not snoozed,
        "snoozed_until": job.snoozed_until if snoozed else None,
        "closed": closed,
        "can_restore": status == "filtered_out",
        "has_packet": status in ("packet_ready", "approved", "applying", "needs_human",
                                 "applied", "acknowledged", "interviewing", "offer"),
        "history": [{"when": e.created_at, "text": event_text(e)} for e in events],
        "sources": [{"label": hz.source_label(s.source), "url": s.url, "when": s.seen_at}
                    for s in sources],
        "siblings": siblings or [],
    }
