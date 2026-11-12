"""Job summaries and company research: the two partials shown next to a job.

    GET  /jobs/{id}/brief            the summary card (any state, including "not asked yet")
    POST /jobs/{id}/brief            ask for it (refresh=1: a fresh answer); returns the card
    GET  /companies/{id}/research    the company card
    POST /companies/{id}/research    ask for it (refresh=1; website=...: say which company)

A POST queues the work and starts it on a background thread, then returns the card in its
"working" state, which polls its own GET (`?poll=1`) every few seconds. When the work is done
the poll answers with HTTP 286, which tells htmx to stop polling. A GET never calls the AI.

Everything shown is worded here: stored values are categories (status, error kind, enum
values), never error text.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from recrute.db import session_scope
from recrute.models import Company, CompanyResearch, Job, JobBrief, utcnow
from recrute.research import briefs, claims, company, runner
from recrute.research.gather import clean_host
from recrute.research.validation import clean_url
from recrute.web import humanize
from recrute.web.common import templates

router = APIRouter()

STOP_POLLING = 286  # htmx: swap this response, then stop the `every Ns` trigger

# ------------------------------------------------------------------------------ wording

SENIORITY = {"intern": "Internship", "entry": "Entry level", "mid": "Mid level",
             "senior": "Senior", "lead": "Lead or staff"}
CATEGORY = {"skill": "Skills", "experience": "Experience", "education": "Education",
            "certification": "Certifications", "clearance": "Clearance",
            "language": "Languages", "other": "Other"}
EMPLOYMENT = {"full_time": "Full-time", "part_time": "Part-time", "contract": "Contract",
              "internship": "Internship"}
SPONSORSHIP = {"offers": "The posting says they sponsor visas",
               "will_not": "The posting says they don't sponsor visas"}
SPONSORSHIP_TONE = {"offers": "ok", "will_not": "danger", "will_sponsor": "ok",
                    "no_sponsorship": "danger"}
EQUITY = {"yes": "Equity offered", "no": "No equity"}
RED_FLAG = {"clearance": "Security clearance", "us_person": "US persons only",
            "citizenship": "Citizenship", "export_control": "Export controls",
            "years_mismatch": "Experience asked", "unpaid_or_commission": "Unpaid or commission",
            "heavy_travel": "Travel", "relocation": "Relocation", "other": "Worth knowing"}
STAGE = {"public": "Public company", "late_private": "Late-stage private company",
         "growth": "Growth-stage company", "early_startup": "Early-stage startup",
         "nonprofit": "Nonprofit", "government": "Government", "academic": "Academic"}
RELEVANCE = {"core": ("Security / AI is their core business", "accent"),
             "significant": ("Significant security / AI work", "accent"),
             "some": ("Some security / AI work", None),
             "none": ("Little security / AI work", None)}
MODEL_FAMILIES = ("haiku", "sonnet", "opus", "fable")

# error kind -> (title, what to do) per feature; "*" is the fallback. Fixed sentences only.
_SETUP = ("The AI isn't set up on this computer",
          "Recrute couldn't use Claude or Codex. Check that one of them is installed and "
          "signed in, then try again.")
_SLOW = ("The AI took too long to answer", "Nothing was saved. Try again in a moment.")
ERRORS: dict[str, dict[str, tuple[str, str]]] = {
    "brief": {
        "unavailable": _SETUP, "auth": _SETUP, "timeout": _SLOW,
        "bad_output": ("The summary didn't pass our checks",
                       "The AI's answer wasn't in a usable form, so it was thrown away rather "
                       "than shown. Try again."),
        "rejected": ("The summary didn't pass our checks",
                     "Too much of the AI's answer couldn't be traced back to the posting, so "
                     "it was thrown away rather than shown. Try again."),
        "*": ("Couldn't summarize this posting",
              "Something went wrong while the AI was reading it. Try again."),
    },
    "research": {
        "unavailable": _SETUP, "auth": _SETUP, "timeout": _SLOW,
        "max_turns": ("The research ran out of steps",
                      "The AI couldn't finish within the number of searches it's allowed. "
                      "Try again."),
        "tools_blocked": ("Web research isn't available right now",
                          "The AI wasn't able to search the web, and there was no careers "
                          "page or encyclopedia entry to fall back on. Try again later."),
        "no_sources": ("Nothing to go on",
                       "Web search for company research is turned off in Settings, and this "
                       "company has no careers-page text or encyclopedia entry to summarize."),
        "bad_output": ("The research didn't pass our checks",
                       "The AI's answer wasn't in a usable form, so it was thrown away rather "
                       "than shown. Try again."),
        "rejected": ("The research didn't pass our checks",
                     "The AI's answer wasn't backed by sources, so it was thrown away rather "
                     "than shown. Try again."),
        "*": ("Couldn't research this company",
              "Something went wrong while looking it up. Try again."),
    },
}


def error_copy(feature: str, kind: str | None) -> dict[str, str]:
    table = ERRORS[feature]
    title, detail = table.get(kind or "", table["*"])
    return {"title": title, "detail": detail}


def model_label(provider: str | None, model: str | None) -> str:
    """"Claude Haiku", "Claude Sonnet", "Codex": who produced an answer, in plain words."""
    name = humanize.provider_label(provider) if provider else "the AI"
    family = next((f for f in MODEL_FAMILIES if f in (model or "").lower()), None)
    return f"{name} {family.title()}" if family and provider == "claude" else name


def research_date(value: Any) -> str:
    """"2026-03-14" -> "Mar 14, 2026"; "2026-03" -> "Mar 2026"; "2026" -> "2026"."""
    from recrute.research.validation import parse_date

    text = str(value or "").strip()
    d = parse_date(text)
    if d is None:
        return ""
    if len(text) == 4:
        return text
    return d.strftime("%b %Y") if len(text) == 7 else f"{d.strftime('%b')} {d.day}, {d.year}"


templates.env.filters["research_date"] = research_date


def _state(row: Any, model: Any) -> str:
    """none | queued | running | paused (usage limit) | stalled (nobody picked it up) | the
    row's final status."""
    if row is None or row.status == "none":
        return "none"
    now = utcnow()
    if row.status == "running":
        started = claims.aware(row.claimed_at)
        return "running" if started and started >= now - claims.claim_ttl(model) else "stalled"
    if row.status == "queued":
        retry = claims.aware(row.retry_after)
        if retry and retry > now:
            return "paused"
        asked = claims.aware(row.requested_at or row.updated_at)
        return "stalled" if asked and asked < now - claims.STALLED_AFTER else "queued"
    return row.status


# ------------------------------------------------------------------------------ job brief


def _fact(label: str, value: str, *, quote: str = "", note: str = "", tone: str = "") -> dict:
    return {"label": label, "value": value, "quote": quote, "note": note, "tone": tone}


def _years(n: Any) -> str:
    return f"{n}+ {'year' if n == 1 else 'years'}" if isinstance(n, int) and n > 0 else ""


def glance(job: Job, data: dict[str, Any] | None) -> list[dict]:
    """The "At a glance" strip. Facts come from the brief when it states them (with the
    posting's own words) and otherwise from what the job board listed; with a brief, things
    the posting doesn't say are shown as such."""
    offer = (data or {}).get("offer") or {}
    pay = offer.get("pay") or {}
    facts = []
    if pay.get("min") is not None or pay.get("max") is not None:
        period = pay.get("period") if pay.get("period") != "unknown" else None
        facts.append(_fact("Pay", humanize.money(pay.get("min"), pay.get("max"),
                                                 pay.get("currency") or None, period),
                           quote=pay.get("quote", "")))
    elif listed := humanize.job_pay(job):
        facts.append(_fact("Pay", listed, note="as listed on the job board"))
    elif data:
        facts.append(_fact("Pay", "Not stated in the posting", tone="muted"))

    mode = offer.get("work_mode")
    work = humanize.remote_label(mode if mode and mode != "not_stated" else job.remote) or ""
    if work and (days := offer.get("office_days")):
        work += f" · {days} {'day' if days == 1 else 'days'} a week in the office"
    if work:
        facts.append(_fact("Work style", work))
    where = humanize.locations(offer.get("locations") or job.locations, 3)
    if where:
        facts.append(_fact("Location", where))

    listed_type = (job.employment_type or "").strip().lower().replace("-", "_").replace(" ", "_")
    kind = EMPLOYMENT.get(offer.get("employment_type") or "") or EMPLOYMENT.get(listed_type) or (
        humanize.humanize_enum(job.employment_type) if job.employment_type else "")
    if kind:
        facts.append(_fact("Job type", kind))

    badge = (job.badges or {}).get("sponsorship")
    if offer.get("sponsorship") in SPONSORSHIP:
        facts.append(_fact("Visa sponsorship", SPONSORSHIP[offer["sponsorship"]],
                           quote=offer.get("sponsorship_quote", ""),
                           tone=SPONSORSHIP_TONE[offer["sponsorship"]]))
    elif badge in ("will_sponsor", "no_sponsorship"):
        facts.append(_fact("Visa sponsorship", humanize.sponsorship_label(badge),
                           quote=job.sponsorship_note or "", tone=SPONSORSHIP_TONE[badge]))
    elif data:
        facts.append(_fact("Visa sponsorship", "Not mentioned in the posting", tone="muted"))

    musts = [r["years"] for r in (data or {}).get("requirements") or []
             if r.get("kind") == "must" and isinstance(r.get("years"), int)]
    years = _years(max(musts) if musts else job.years_required)
    level = SENIORITY.get(((data or {}).get("role") or {}).get("seniority") or "", "")
    if years or level:
        facts.append(_fact("Experience", " · ".join(x for x in (level, years) if x)))
    return facts


def _grouped(reqs: list[dict], kind: str) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for r in reqs:
        if r.get("kind") == kind:
            groups.setdefault(r.get("category") or "other", []).append(r)
    return [{"label": CATEGORY.get(cat, "Other"), "items": groups[cat]}
            for cat in CATEGORY if cat in groups]


def brief_context(session: Session, job: Job) -> dict[str, Any]:
    """Everything `_brief.html` needs for `job` (also usable with {% include %})."""
    row = session.exec(select(JobBrief).where(JobBrief.job_id == job.id)).first()
    state = _state(row, JobBrief)
    data = (row.data or None) if row is not None and row.status != "skipped" else None
    if data and not data.get("summary") and not data.get("requirements"):
        data = None
    offer = (data or {}).get("offer") or {}
    reqs = (data or {}).get("requirements") or []
    perks = [EQUITY[offer["equity"]]] if offer.get("equity") in EQUITY else []
    comp = session.get(Company, job.company_id) if job.company_id else None
    return {
        "job": job, "brief": row, "state": state, "data": data,
        "working": state in ("queued", "running"),
        "glance": glance(job, data),
        "must": _grouped(reqs, "must"), "nice": _grouped(reqs, "nice"),
        "perks": perks + list(offer.get("benefits") or []),
        "equity_quote": offer.get("equity_quote", ""),
        "flags": [{**f, "label": RED_FLAG.get(f.get("kind"), "Worth knowing")}
                  for f in (data or {}).get("red_flags") or []],
        "company_name": comp.name if comp else "",
        "stale": bool(data) and briefs.is_stale(row, job),
        "by": model_label(row.provider, row.model) if row is not None else "",
        "error": error_copy("brief", row.error_kind) if state == "failed" else None,
        "retry_at": row.retry_after if state == "paused" else None,
        "posting_url": job.apply_url or job.canonical_url,
        "too_short": state == "skipped" or (
            state == "none" and len(briefs.posting_text(job)) < briefs.MIN_DESCRIPTION),
    }


def _brief_response(request: Request, session: Session, job: Job, *, poll: bool = False):
    ctx = brief_context(session, job)
    done = poll and not ctx["working"]
    return templates.TemplateResponse(request, "_brief.html", ctx,
                                      status_code=STOP_POLLING if done else 200)


def _job(session: Session, job_id: int) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404)
    return job


@router.get("/jobs/{job_id}/brief", response_class=HTMLResponse)
def brief_partial(request: Request, job_id: int, poll: int = 0):
    with session_scope() as s:
        return _brief_response(request, s, _job(s, job_id), poll=bool(poll))


@router.post("/jobs/{job_id}/brief", response_class=HTMLResponse)
def brief_request(request: Request, job_id: int, refresh: Annotated[str, Form()] = ""):
    with session_scope() as s:
        job = _job(s, job_id)
        row = briefs.request_brief(s, job_id, refresh=bool(refresh))
        if row.status == "queued":  # (not when someone is already building it)
            runner.start("brief", job_id)
        return _brief_response(request, s, job)


# ------------------------------------------------------------------------- company research


def _chips(data: dict[str, Any]) -> list[dict]:
    chips = []

    def add(text: str, tone: str | None = None, title: str = "", source: Any = None) -> None:
        if text:
            chips.append({"text": text, "tone": tone, "title": title, "source": source})

    add(STAGE.get(data.get("stage") or "", ""))
    add(data.get("industry") or "")
    size = data.get("size") or {}
    employees = (size.get("employees") or "").strip()
    # (a chip holds a figure, "about 600" or "201-500"; a hedged sentence was cut off mid-way)
    if employees and len(employees) <= 32 and any(ch.isdigit() for ch in employees):
        has_word = any(w in employees.lower() for w in ("employee", "people", "staff"))
        add(employees if has_word else f"{employees} employees", source=size.get("source"))
    if data.get("hq"):
        add(f"Based in {data['hq']}")
    if data.get("founded"):
        add(f"Founded {data['founded']}")
    rel = data.get("security_ai_relevance") or {}
    if rel.get("level") in RELEVANCE:
        text, tone = RELEVANCE[rel["level"]]
        add(text, tone, rel.get("explanation") or "")
    return chips


SNIPPET_KINDS = {"careers": "their careers page", "website": "their website",
                 "posting": "their job posting", "wikipedia": "Wikipedia"}


def _limited_from(row: CompanyResearch | None) -> str:
    """"their careers page and Wikipedia": what a tool-less summary was actually based on."""
    kinds = [s.get("kind") for s in (row.gathered if row is not None else None) or []]
    names = [SNIPPET_KINDS[k] for k in SNIPPET_KINDS if k in kinds] or ["public pages"]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def research_context(session: Session, comp: Company) -> dict[str, Any]:
    """Everything `_company_research.html` needs for `comp` (also usable with {% include %})."""
    row = session.exec(select(CompanyResearch)
                       .where(CompanyResearch.company_id == comp.id)).first()
    state = _state(row, CompanyResearch)
    data = (row.data or None) if row is not None else None
    if data and not data.get("what_they_do") and not data.get("sources"):
        data = None
    if state == "not_found":
        data = None
    limited = bool(data) and row is not None and not row.web
    fit = (row.fit or {}) if row is not None and data else {}
    funding = (data or {}).get("funding") or {}
    site_url = clean_url((data or {}).get("website")) or ""
    if (data or {}).get("matched") != "confident":
        site_url = ""
    return {
        "company": comp, "research": row, "state": state, "data": data,
        "working": state in ("queued", "running"),
        "chips": _chips(data) if data else [],
        "funding": funding if funding.get("summary") or funding.get("last_round") else None,
        "sources": (data or {}).get("sources") or [],
        "ambiguous": bool(data) and data.get("matched") == "ambiguous",
        "limited": limited, "limited_from": _limited_from(row) if limited else "",
        "fit_reasons": fit.get("reasons") or [], "fit_concerns": fit.get("concerns") or [],
        "fit_by": model_label(row.fit_provider, row.fit_model) if fit and row else "",
        "by": model_label(row.provider, row.model) if row is not None else "",
        "error": error_copy("research", row.error_kind) if state == "failed" else None,
        "retry_at": row.retry_after if state == "paused" else None,
        "website": clean_host(comp.domain) or "",
        # their official site, shown in the header only for a match we trust
        "site_url": site_url, "site_label": clean_host(site_url) or "",
    }


def _research_response(request: Request, session: Session, comp: Company, *,
                       poll: bool = False, website_error: str = ""):
    ctx = research_context(session, comp)
    ctx["website_error"] = website_error
    done = poll and not ctx["working"]
    return templates.TemplateResponse(request, "_company_research.html", ctx,
                                      status_code=STOP_POLLING if done else 200)


def _company(session: Session, company_id: int) -> Company:
    comp = session.get(Company, company_id)
    if comp is None:
        raise HTTPException(404)
    return comp


@router.get("/companies/{company_id}/research", response_class=HTMLResponse)
def research_partial(request: Request, company_id: int, poll: int = 0):
    with session_scope() as s:
        return _research_response(request, s, _company(s, company_id), poll=bool(poll))


@router.post("/companies/{company_id}/research", response_class=HTMLResponse)
def research_request(request: Request, company_id: int,
                     refresh: Annotated[str, Form()] = "",
                     website: Annotated[str, Form()] = ""):
    with session_scope() as s:
        comp = _company(s, company_id)
        if website.strip():  # "this is the company I mean": remember it and look again
            host = clean_host(website)
            if host is None:
                return _research_response(
                    request, s, comp,
                    website_error="That doesn't look like a website address. Enter "
                                  "something like acme.com.")
            comp.domain = host
            s.add(comp)
            s.commit()
            s.refresh(comp)
            refresh = "1"
        row = company.request_research(s, company_id, refresh=bool(refresh))
        if row.status == "queued":
            runner.start("research", company_id)
        return _research_response(request, s, comp)


# ------------------------------------------------------------------------------ dashboard
# How the two background tasks describe their last run (web/humanize.task_summary).


def _summary_briefs(st: dict) -> str:
    n = int(st.get("summarized") or 0)
    parts = [f"Summarized {humanize.plural(n, 'posting')}" if n else "No new postings to summarize"]
    if st.get("skipped"):
        parts.append(f"{humanize.plural(int(st['skipped']), 'posting')} had too little text")
    if st.get("failed"):
        parts.append(f"{humanize.plural(int(st['failed']), 'summary', 'summaries')} failed")
    if st.get("usage_limit"):
        parts.append("stopped early because the AI hit its usage limit")
    return "; ".join(parts) + "."


def _summary_company_research(st: dict) -> str:
    n = int(st.get("researched") or 0)
    parts = [f"Researched {humanize.plural(n, 'company', 'companies')} on the web" if n
             else "No companies researched on the web"]
    if st.get("limited"):
        parts.append(f"{int(st['limited'])} from their careers page only")
    if st.get("not_found"):
        parts.append(f"{int(st['not_found'])} couldn't be found")
    if st.get("failed"):
        parts.append(f"{int(st['failed'])} failed")
    if st.get("fit_notes"):
        parts.append(f"{humanize.plural(int(st['fit_notes']), 'fit note')} updated")
    if st.get("daily_cap_reached"):
        parts.append("today's limit is reached")
    if st.get("usage_limit"):
        parts.append("stopped early because the AI hit its usage limit")
    return "; ".join(parts) + "."


humanize._TASK_SUMMARIES.setdefault("briefs", _summary_briefs)
humanize._TASK_SUMMARIES.setdefault("company_research", _summary_company_research)
