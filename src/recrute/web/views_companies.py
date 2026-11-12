"""Companies whose job boards Recrute checks: the list, one company's page, add, pause/resume."""

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlmodel import col, select

from recrute.db import session_scope
from recrute.models import Company, Job, JobStatus
from recrute.web.common import error_response, msg_response, page
from recrute.web.humanize import source_label

router = APIRouter()

PER_PAGE = 50
# Jobs that are still in play for you (in Review, or somewhere along an application).
_SET_ASIDE = {JobStatus.FILTERED_OUT, JobStatus.CLOSED, JobStatus.REJECTED}
_BOARD_URLS = {
    "greenhouse": "https://job-boards.greenhouse.io/{token}",
    "lever": "https://jobs.lever.co/{token}",
    "ashby": "https://jobs.ashbyhq.com/{token}",
    "workable": "https://apply.workable.com/{token}",
    "smartrecruiters": "https://jobs.smartrecruiters.com/{token}",
}
_ORIGINS = {"seed": "Built-in list", "discovered": "Found through a job posting",
            "manual": "Added by you"}
FILTERS = [("all", "All"), ("matches", "With jobs for you"), ("paused", "Paused"),
           ("problems", "Not reachable")]


def board_url(company: Company) -> str | None:
    """The employer's public job board, when its address can be told from the board id."""
    pattern = _BOARD_URLS.get((company.ats or "").lower())
    return pattern.format(token=company.ats_token) if pattern and company.ats_token else None


def origin_label(origin: str | None) -> str:
    return _ORIGINS.get((origin or "").lower(), "Added automatically")


def _job_counts(session) -> dict[int, dict[str, int]]:
    """Per company: how many of its jobs are live for you, and how many were seen in all."""
    counts: dict[int, dict[str, int]] = {}
    rows = session.exec(select(Job.company_id, Job.status, func.count())
                        .where(col(Job.company_id).is_not(None))
                        .group_by(Job.company_id, Job.status)).all()
    for company_id, status, n in rows:
        c = counts.setdefault(company_id, {"live": 0, "total": 0})
        c["total"] += n
        if status not in _SET_ASIDE:
            c["live"] += n
    return counts


@router.get("/companies", response_class=HTMLResponse)
def companies_page(request: Request, q: str = "", show: str = "all", sort: str = "name",
                   p: int = 1):
    with session_scope() as s:
        rows = s.exec(select(Company).order_by(func.lower(Company.name))).all()
        counts = _job_counts(s)
        total = len(rows)
        if q.strip():
            needle = q.strip().lower()
            rows = [c for c in rows if needle in c.name.lower()]
        if show == "matches":
            rows = [c for c in rows if counts.get(c.id, {}).get("live")]
        elif show == "paused":
            rows = [c for c in rows if not c.active]
        elif show == "problems":
            rows = [c for c in rows if c.poll_error]
        else:
            show = "all"
        if sort == "matches":
            rows.sort(key=lambda c: (-counts.get(c.id, {}).get("live", 0), c.name.lower()))
        elif sort == "checked":
            rows.sort(key=lambda c: (c.last_polled_at is None,
                                     -(c.last_polled_at.timestamp() if c.last_polled_at else 0)))
        else:
            sort = "name"
        found = len(rows)
        pages = max(1, -(-found // PER_PAGE))
        p = min(max(1, p), pages)
        start = (p - 1) * PER_PAGE
        ctx = {"rows": rows[start:start + PER_PAGE], "q": q, "show": show, "sort": sort,
               "page_no": p, "pages": pages, "found": found, "total": total, "start": start,
               "counts": counts, "filters": FILTERS, "board_url": board_url,
               "origin_label": origin_label}
        return page(request, "companies.html", ctx, s)


@router.get("/companies/{company_id}", response_class=HTMLResponse)
def company_page(request: Request, company_id: int):
    with session_scope() as s:
        company = s.get(Company, company_id)
        if company is None:
            raise HTTPException(404)
        jobs = s.exec(select(Job).where(Job.company_id == company_id)
                      .order_by(col(Job.score).desc().nulls_last(),
                                col(Job.first_seen).desc())).all()
        live = [j for j in jobs if j.status not in _SET_ASIDE]
        aside = [j for j in jobs if j.status in _SET_ASIDE]
        ctx = {"company": company, "live": live, "aside": aside[:40],
               "aside_total": len(aside), "board": board_url(company),
               "origin": origin_label(company.origin)}
        return page(request, "company.html", ctx, s)


@router.post("/companies/add", response_class=HTMLResponse)
def companies_add(url: Annotated[str, Form()] = "", name: Annotated[str, Form()] = ""):
    from recrute.registry import add_company_from_url

    if not url.strip():
        return error_response("Paste a link to one of the company's job postings first.", 422)
    with session_scope() as s:
        try:
            c = add_company_from_url(s, url.strip(), name.strip() or None)
        except ValueError as e:
            return error_response(str(e), 422)
    return msg_response(f"Added {c.name} ({source_label(c.ats)}). Its job board is checked on "
                        "the next run.")


@router.post("/companies/{company_id}/toggle", response_class=HTMLResponse)
def companies_toggle(company_id: int):
    with session_scope() as s:
        c = s.get(Company, company_id)
        if c is None:
            raise HTTPException(404)
        c.active = not c.active
        s.add(c)
        s.commit()
        # (the reply becomes the button's new label)
        return HTMLResponse("Pause" if c.active else "Resume checking")
