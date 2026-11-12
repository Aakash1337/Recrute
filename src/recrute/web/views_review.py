"""Job review (CP1): the queue, a job's details and decisions, and the filtered-out list."""

from dataclasses import dataclass
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import case, false, func, or_
from sqlmodel import col, select

from recrute.db import session_scope
from recrute.models import (
    Company,
    Job,
    JobScore,
    JobSource,
    JobStatus,
    Priority,
    StatusEvent,
    utcnow,
)
from recrute.pipeline.stages import restore_filtered
from recrute.review import REJECT_REASONS, ReviewError, decide, latest_score, queue_conditions
from recrute.web import review_labels
from recrute.web.common import error_response, friendly_html, msg_html, page, templates
from recrute.web.humanize import priority_label, source_label, status_label
from recrute.web.job_view import job_view
from recrute.web.review_labels import (
    CATEGORY_HELP,
    CATEGORY_LABELS,
    FILTER_CATEGORIES,
    filter_reason_category,
)

router = APIRouter()
review_labels.register(templates.env)  # fit_band, filter_reason, … for these templates


# ------------------------------------------------------------------------------ the review list
# /queue: the jobs awaiting your review, with search, filters and sort as query parameters
# (?q=&track=&mode=&pay=1&min_fit=&source=&sort=). An htmx request aimed at the list (#list, or
# its "Show more" button) gets only the rows; anything else gets the whole page.

QUEUE_PAGE = 200
QUEUE_SORTS = {"best": "Best match", "fit": "Highest fit score", "newest": "Newest",
               "pay": "Highest pay"}
WORK_MODES = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site",
              "unknown": "Not stated"}
FIT_MINIMUMS = (80, 70, 60)


def _qint(value, lo: int = 0, hi: int | None = None) -> int | None:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if n < lo or (hi is not None and n > hi):
        return None
    return n


def _contains(column, text: str):
    """Case-insensitive substring match; % and _ in what you typed are literal."""
    return func.lower(column).contains(text.lower(), autoescape=True)


@dataclass
class QueueFilters:
    q: str = ""
    track: str = ""  # a Priority value (P0..P3)
    mode: str = ""  # a WORK_MODES key
    pay: bool = False  # only jobs that list pay
    min_fit: int | None = None
    source: str = ""  # a JobSource.source key
    sort: str = "best"

    @classmethod
    def from_params(cls, params) -> "QueueFilters":
        """Unknown or malformed values are ignored, so a stale link never errors."""
        track = (params.get("track") or "").strip().upper()
        mode = (params.get("mode") or "").strip().lower()
        sort = (params.get("sort") or "").strip().lower()
        return cls(
            q=" ".join((params.get("q") or "").split())[:100],
            track=track if track in Priority.__members__ else "",
            mode=mode if mode in WORK_MODES else "",
            pay=(params.get("pay") or "") in ("1", "on", "true", "yes"),
            min_fit=_qint(params.get("min_fit"), 1, 100),
            source=(params.get("source") or "").strip().lower()[:40],
            sort=sort if sort in QUEUE_SORTS else "best",
        )

    @property
    def active(self) -> bool:
        """Any filter narrowing the list (the sort order doesn't count)."""
        return bool(self.q or self.track or self.mode or self.pay or self.min_fit
                    or self.source)

    def conditions(self) -> list:
        conds: list = []
        if self.q:
            conds.append(or_(_contains(Job.title, self.q), _contains(Company.name, self.q)))
        if self.track:
            conds.append(Job.priority == Priority[self.track])
        if self.mode == "unknown":
            conds.append(or_(col(Job.remote).is_(None), Job.remote == ""))
        elif self.mode:
            conds.append(Job.remote == self.mode)
        if self.pay:
            conds.append(or_(col(Job.salary_min) > 0, col(Job.salary_max) > 0))
        if self.min_fit:
            conds.append(col(Job.score) >= self.min_fit)
        if self.source:
            conds.append(select(JobSource.id).where(JobSource.job_id == Job.id,
                                                    JobSource.source == self.source).exists())
        return conds

    def order_by(self) -> list:
        newest = func.coalesce(Job.posted_at, Job.first_seen).desc()
        if self.sort == "fit":
            return [col(Job.score).desc(), Job.priority, newest]
        if self.sort == "newest":
            return [newest, col(Job.score).desc()]
        if self.sort == "pay":
            top = func.coalesce(Job.salary_max, Job.salary_min)
            yearly = case((top < 1000, top * 2080), else_=top)  # an hourly rate, as a year
            return [yearly.desc().nulls_last(), col(Job.score).desc(), newest]
        # best match: your priority tracks first, then fit (the order the queue always had)
        return [Job.priority, col(Job.score).desc(), col(Job.first_seen).desc()]

    def query(self, **changes) -> str:
        """The query string for these filters ("" when nothing is set), with `changes`."""
        values = {"q": self.q, "track": self.track, "mode": self.mode,
                  "pay": "1" if self.pay else "", "min_fit": self.min_fit or "",
                  "source": self.source, "sort": "" if self.sort == "best" else self.sort}
        values.update(changes)
        return urlencode({k: v for k, v in values.items() if v not in ("", None)})


def _count(s, *conds) -> int:
    return s.exec(select(func.count()).select_from(Job)
                  .join(Company, Company.id == Job.company_id, isouter=True)
                  .where(*conds)).one()


def _queue_facets(s, base: list) -> dict:
    """Counts for the filter menus, over everything awaiting review."""
    by_track = dict(s.exec(select(Job.priority, func.count()).where(*base)
                           .group_by(Job.priority)).all())
    by_mode = dict(s.exec(select(Job.remote, func.count()).where(*base)
                          .group_by(Job.remote)).all())
    jobs = func.count(func.distinct(JobSource.job_id))
    by_source = s.exec(select(JobSource.source, jobs).join(Job, Job.id == JobSource.job_id)
                       .where(*base).group_by(JobSource.source).order_by(jobs.desc())).all()
    with_pay = s.exec(select(func.count()).select_from(Job).where(
        *base, or_(col(Job.salary_min) > 0, col(Job.salary_max) > 0))).one()
    modes = {k: by_mode.get(k, 0) for k in ("remote", "hybrid", "onsite")}
    modes["unknown"] = by_mode.get(None, 0) + by_mode.get("", 0)
    return {
        "tracks": [(p.value, priority_label(p), by_track.get(p, 0)) for p in Priority
                   if by_track.get(p)],
        "modes": [(k, WORK_MODES[k], n) for k, n in modes.items()],
        "sources": [(src, source_label(src), n) for src, n in by_source if src],
        "with_pay": with_pay,
    }


def _snoozed(s) -> tuple[int, object]:
    """How many jobs are snoozed, and when the first one comes back."""
    conds = [Job.status == JobStatus.DISCOVERED, col(Job.score).is_not(None),
             col(Job.closed_at).is_(None), col(Job.snoozed_until) > utcnow()]
    n, first = s.exec(select(func.count(), func.min(Job.snoozed_until)).where(*conds)).one()
    return n or 0, first


@router.get("/queue", response_class=HTMLResponse)
def queue_page(request: Request):
    f = QueueFilters.from_params(request.query_params)
    offset = _qint(request.query_params.get("offset")) or 0
    target = request.headers.get("hx-target") or ""
    rows_only = (request.headers.get("hx-request") == "true"
                 and request.headers.get("hx-history-restore-request") != "true"
                 and (target == "list" or target.startswith("queue-more")))
    with session_scope() as s:
        base = queue_conditions()
        total = _count(s, *base)
        conds = base + f.conditions()
        matched = _count(s, *conds) if f.active else total

        def ordered(stmt):
            return (stmt.join(Company, Company.id == Job.company_id, isouter=True)
                    .where(*conds).order_by(*f.order_by()))

        rows = s.exec(ordered(select(Job, Company)).offset(offset).limit(QUEUE_PAGE)).all()
        # a job moved back from Filtered before it was scored carries a placeholder score of 0
        zero = [j.id for j, _ in rows if not j.score]
        unscored = set(zero) - set(s.exec(select(JobScore.job_id)
                                          .where(col(JobScore.job_id).in_(zero))).all())
        group_counts: dict = {}
        prev_group = None
        if f.sort == "best" and rows:  # "Cybersecurity · 109 jobs" headers between tracks
            group_counts = {p.value if p else "": n for p, n in s.exec(
                select(Job.priority, func.count()).select_from(Job)
                .join(Company, Company.id == Job.company_id, isouter=True)
                .where(*conds).group_by(Job.priority)).all()}
            if offset:  # the track of the row above this page, so its header isn't repeated
                prev = s.exec(ordered(select(Job.priority)).offset(offset - 1).limit(1)).first()
                prev_group = prev.value if prev else ""
        shown = min(offset + len(rows), matched)
        ctx = {"rows": rows, "reasons": REJECT_REASONS, "f": f, "total": total,
               "matched": matched, "shown": shown, "offset": offset,
               "more": shown < matched, "next_offset": offset + len(rows),
               "unscored_ids": unscored, "group_counts": group_counts, "prev_group": prev_group,
               "sorts": QUEUE_SORTS, "fit_minimums": FIT_MINIMUMS, "rows_only": rows_only}
        if rows_only:
            resp = templates.TemplateResponse(request, "_queue_list.html", ctx)
            if not offset:  # the address bar keeps only the filters that are set
                resp.headers["HX-Push-Url"] = "/queue" + (f"?{f.query()}" if f.query() else "")
            return resp
        ctx["facets"] = _queue_facets(s, base)
        ctx["snoozed"], ctx["snoozed_next"] = _snoozed(s)
        ctx["filtered_total"] = _count(s, Job.status == JobStatus.FILTERED_OUT)
        return page(request, "queue.html", ctx, s)


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: int):
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            raise HTTPException(404)
        company = s.get(Company, job.company_id) if job.company_id else None
        sources = s.exec(select(JobSource).where(JobSource.job_id == job_id)).all()
        events = s.exec(select(StatusEvent).where(StatusEvent.job_id == job_id)
                        .order_by(col(StatusEvent.id).desc())).all()
        siblings = []
        if job.company_id:  # other live roles at the same employer, best fit first
            siblings = s.exec(
                select(Job).where(Job.company_id == job.company_id, Job.id != job_id,
                                  col(Job.status).not_in([JobStatus.FILTERED_OUT, JobStatus.CLOSED,
                                                          JobStatus.REJECTED]),
                                  col(Job.closed_at).is_(None))
                .order_by(col(Job.score).desc().nulls_last()).limit(6)).all()
        score = latest_score(s, job_id)
        ctx = {"job": job, "company": company, "score": score,
               "sources": sources, "events": events, "reasons": REJECT_REASONS,
               "view": job_view(job, company, score, sources, events, siblings)}
        if request.headers.get("hx-request") == "true":
            return templates.TemplateResponse(request, "_job_detail.html", ctx)
        # opened from a notification / digest link: a full page whose buttons work on their own
        return page(request, "job_page.html", ctx, s)


@router.post("/jobs/{job_id}/decide", response_class=HTMLResponse)
def job_decide(request: Request, job_id: int, action: Annotated[str, Form()],
               reason: Annotated[str | None, Form()] = None):
    with session_scope() as s:
        try:
            job = decide(s, job_id, action, reason or None)
        except ReviewError as e:
            return error_response(str(e), 409)
        label = {"approve": "Approved. A tailored application packet is being built; you'll "
                            "find it under Packets. Nothing is sent until you approve it.",
                 "reject": "Rejected. It won't come back.",
                 "snooze": "Snoozed. It returns to this list in 7 days.",
                 "manual": "Taken off the list: you'll apply to this one yourself."}[action]
        resp = HTMLResponse(msg_html(f"{job.title}: {label[0].lower()}{label[1:]}"))
        resp.headers["HX-Trigger"] = "decided"
        return resp


# ------------------------------------------------------------------------------ filtered jobs
# /filtered: jobs the rules or the fit score set aside, grouped by why (?reason=<category>),
# searchable (?q=) and paginated (?page=). "Move back to review" restores one.

FILTERED_PAGE = 50
NOT_TARGET = "not_target"
# "near": everything except titles outside your target roles (the bulk of what's filtered)
FILTERED_VIEWS = {"near": "Near misses", "all": "Everything"}
VIEW_HELP = {
    "near": "Jobs in your target roles that a rule or a low fit score set aside: the ones most "
            "worth a second look. Titles outside your target roles are left out.",
    "all": "Every job Recrute set aside, including titles outside your target roles.",
}
FILTERED_SORTS = {"newest": "Newest first", "fit": "Highest fit score first"}


def _reason_groups(s, conds: list) -> tuple[dict[str, list], dict[str, int]]:
    """{category: [stored reasons]} and {category: job count} for the jobs matching `conds`
    (one group-by over the distinct reason strings, which are few)."""
    reasons: dict[str, list] = {}
    counts: dict[str, int] = {}
    for reason, n in s.exec(select(Job.filter_reason, func.count()).select_from(Job)
                            .join(Company, Company.id == Job.company_id, isouter=True)
                            .where(*conds).group_by(Job.filter_reason)).all():
        cat = filter_reason_category(reason)
        reasons.setdefault(cat, []).append(reason)
        counts[cat] = counts.get(cat, 0) + n
    return reasons, counts


def _reasons_in(values: list):
    """filter_reason IN values (NULL included when None is one of them)."""
    present = [v for v in values if v is not None]
    conds = [col(Job.filter_reason).in_(present)] if present else []
    if None in values:
        conds.append(col(Job.filter_reason).is_(None))
    return or_(*conds) if conds else false()


@router.get("/filtered", response_class=HTMLResponse)
def filtered_page(request: Request):
    params = request.query_params
    q = " ".join((params.get("q") or "").split())[:100]
    view = (params.get("reason") or "").strip().lower()
    if view not in FILTERED_VIEWS and view not in CATEGORY_LABELS:
        view = "all" if q else "near"  # a search looks through everything
    sort = (params.get("sort") or "").strip().lower()
    if sort not in FILTERED_SORTS:
        sort = "fit" if view in ("near", "low_score") else "newest"
    with session_scope() as s:
        conds: list = [Job.status == JobStatus.FILTERED_OUT]
        if q:
            # (the stored reason too: the dashboard's "why jobs were filtered" rows link here)
            conds.append(or_(_contains(Job.title, q), _contains(Company.name, q),
                             _contains(Job.filter_reason, q)))
        reasons, counts = _reason_groups(s, conds)
        matched_all = sum(counts.values())
        near = matched_all - counts.get(NOT_TARGET, 0)
        if view == "near":
            conds.append(_reasons_in([r for cat, rs in reasons.items() if cat != NOT_TARGET
                                      for r in rs]))
            matched = near
        elif view in CATEGORY_LABELS:
            conds.append(_reasons_in(reasons.get(view, [])))
            matched = counts.get(view, 0)
        else:
            matched = matched_all
        pages = max(1, -(-matched // FILTERED_PAGE))
        page_no = min(_qint(params.get("page"), 1) or 1, pages)
        order = ([col(Job.score).desc().nulls_last(), col(Job.first_seen).desc()]
                 if sort == "fit" else [col(Job.first_seen).desc()])
        rows = s.exec(select(Job, Company)
                      .join(Company, Company.id == Job.company_id, isouter=True)
                      .where(*conds).order_by(*order)
                      .offset((page_no - 1) * FILTERED_PAGE).limit(FILTERED_PAGE)).all()
        total = matched_all if not q else _count(s, Job.status == JobStatus.FILTERED_OUT)
        chips = [("near", FILTERED_VIEWS["near"], near)]
        chips += [(k, label, counts.get(k, 0)) for k, label, _ in FILTER_CATEGORIES
                  if counts.get(k) or k == view]
        chips.append(("all", FILTERED_VIEWS["all"], matched_all))
        first = (page_no - 1) * FILTERED_PAGE + 1 if rows else 0

        def link(**changes) -> str:
            values = {"q": q, "reason": view, "sort": sort, "page": page_no}
            values.update(changes)
            if values.get("page") == 1:
                values["page"] = ""
            return "/filtered?" + urlencode({k: v for k, v in values.items() if v})

        ctx = {"rows": rows, "q": q, "view": view, "sort": sort, "sorts": FILTERED_SORTS,
               "chips": chips, "matched": matched, "total": total, "page_no": page_no,
               "pages": pages, "first": first, "last": first + len(rows) - 1 if rows else 0,
               "link": link,
               "view_label": FILTERED_VIEWS.get(view) or CATEGORY_LABELS.get(view, ""),
               "view_help": VIEW_HELP.get(view) or CATEGORY_HELP.get(view, "")}
        return page(request, "filtered.html", ctx, s)


@router.post("/jobs/{job_id}/restore", response_class=HTMLResponse)
def job_restore(job_id: int):
    """Moves a filtered-out job back to the review list, and says so only when it moved."""
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            return HTMLResponse(friendly_html({
                "title": "This job isn't in Recrute any more.",
                "detail": "It may have been merged with a duplicate. Reload the page."}), 404)
        if job.status != JobStatus.FILTERED_OUT:
            where = ("It's already in your review list." if job.status == JobStatus.DISCOVERED
                     else f"Its status is now “{status_label(job.status)}”.")
            return HTMLResponse(friendly_html({"title": "Not filtered out any more.",
                                               "detail": where}), 409)
        restore_filtered(s, job_id)
        s.expire_all()
        job = s.get(Job, job_id)
        if job is None or job.status != JobStatus.DISCOVERED:
            return HTMLResponse(friendly_html({
                "title": "Couldn't move it back.",
                "detail": "It changed while you were looking. Reload the page."}), 409)
    return HTMLResponse(
        '<span class="msg msg-ok ok" role="status">Job restored: it\'s back in Review</span> '
        f'<a class="restore-next" href="/jobs/{job_id}">Review it now →</a>')
