"""Upsert RawJobs into the DB with deduplication and company-registry maintenance."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from recrute.models import Company, Job, JobSource, JobStatus, StatusEvent, utcnow
from recrute.pipeline.normalize import (
    canonical_url,
    content_hash,
    description_markdown,
    fuzzy_key,
    normalize_company,
)
from recrute.schemas import RawJob

# A fuzzy (company+title) match only merges with jobs seen this recently; older ones are
# treated as a new opening (reposts are common).
FUZZY_WINDOW = timedelta(days=45)


@dataclass
class IngestStats:
    new: int = 0
    updated: int = 0
    merged: int = 0  # same job seen on another source
    new_job_ids: list[int] = field(default_factory=list)
    # the job each input resolved to (new, updated or merged), in input order
    job_ids: list[int] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"new": self.new, "updated": self.updated, "merged": self.merged}


def upsert_company(session: Session, raw: RawJob) -> Company:
    company = None
    if raw.ats and raw.ats_token:
        company = session.exec(select(Company).where(
            Company.ats == raw.ats, Company.ats_token == raw.ats_token)).first()
    if company is None:
        norm = normalize_company(raw.company)
        for c in session.exec(select(Company).where(Company.name == raw.company)).all():
            company = c
            break
        if company is None and norm:
            # Name variants ("Acme, Inc." vs "Acme"): compare normalized names.
            for c in session.exec(select(Company)).all():
                if normalize_company(c.name) == norm:
                    company = c
                    break
    if company is None:
        ats_known = raw.ats and raw.ats_token and raw.ats not in ("linkedin_easy_apply",)
        company = Company(name=raw.company, domain=raw.company_domain,
                          ats=raw.ats if ats_known else None,
                          ats_token=raw.ats_token if ats_known else None,
                          origin="discovered")
        session.add(company)
        session.flush()
    else:
        learn_company_board(session, company, raw)
    return company


def learn_company_board(session: Session, company: Company, raw: RawJob) -> None:
    """Record where a company hosts its ATS board, so it can be polled directly."""
    if not (raw.ats and raw.ats_token) or company.ats or raw.ats == "linkedin_easy_apply":
        return
    clash = session.exec(select(Company).where(
        Company.ats == raw.ats, Company.ats_token == raw.ats_token)).first()
    if clash is None:
        company.ats, company.ats_token = raw.ats, raw.ats_token
        session.add(company)


def source_url(raw: RawJob) -> str:
    """Per-posting identity within a source. Some sources emit several roles from one page
    (e.g. one HN comment listing three jobs); their role id is folded into the URL fragment so
    each role stays distinct (the link itself still works)."""
    sid = raw.source_job_id
    if sid and sid not in raw.url:
        return f"{raw.url}#{sid}"
    return raw.url


# Sources that emit several roles from one page (and often one shared careers link).
MULTI_ROLE_SOURCES = {"hn_whoshiring"}


def job_canonical(raw: RawJob) -> tuple[str, str]:
    """(apply target, canonical key). A role id keeps roles apart when they share a page URL
    (no apply URL) or, for multi-role sources, a generic careers link (anything that isn't a
    specific requisition on a known ATS)."""
    target = raw.apply_url or raw.url
    canon = canonical_url(target)
    sid = raw.source_job_id
    if not sid or sid in canon:
        return target, canon
    if raw.apply_url is None:
        return target, f"{canon}#{sid}"
    from recrute.sources.ats_url import parse_ats_url

    ref = parse_ats_url(target)
    # a company's whole board (no requisition id), from ANY source: several postings share
    # it, so it can't be one job's identity
    if ref is not None and not ref.job_id:
        return target, f"{canon}#{sid}"
    if raw.source in MULTI_ROLE_SOURCES and ref is None:  # a careers page, not one job
        return target, f"{canon}#{sid}"
    return target, canon


def _find_existing(session: Session, raw: RawJob, canon: str, fkey: str) -> Job | None:
    job = session.exec(select(Job).where(Job.canonical_url == canon)).first()
    if job:
        return job
    src = session.exec(select(JobSource).where(JobSource.source == raw.source,
                                               JobSource.url == source_url(raw))).first()
    if src:
        return session.get(Job, src.job_id)
    if raw.ats and raw.ats_job_id:
        # Requisition ids are only unique within one tenant (company board).
        candidates = session.exec(
            select(Job, Company).join(Company, Company.id == Job.company_id, isouter=True)
            .where(Job.ats == raw.ats, Job.ats_job_id == raw.ats_job_id)).all()
        norm = normalize_company(raw.company)
        for job, company in candidates:
            if company is None:
                continue
            if raw.ats_token and company.ats_token:
                if company.ats_token == raw.ats_token:
                    return job
            elif normalize_company(company.name) == norm:
                return job
    since = utcnow() - FUZZY_WINDOW
    for job in session.exec(select(Job).where(Job.fuzzy_key == fkey)).all():
        # Two postings on the same ATS with different requisition ids are distinct openings.
        if raw.ats and job.ats == raw.ats and raw.ats_job_id and job.ats_job_id \
                and raw.ats_job_id != job.ats_job_id:
            continue
        last_seen = job.last_seen if job.last_seen.tzinfo else job.last_seen.replace(
            tzinfo=since.tzinfo)
        if last_seen >= since:
            return job
    return None


# Statuses whose derived data (filters/score) is recomputed when the posting's content changes.
RESCORABLE = {JobStatus.DISCOVERED, JobStatus.FILTERED_OUT}


def _decision_inputs(job: Job) -> tuple:
    """Everything the rule filters and LLM triage look at."""
    return (job.title, job.description_hash, tuple(job.locations or ()), job.remote,
            job.employment_type, job.salary_min, job.salary_max)


def _prefer(raw: RawJob) -> bool:
    """Whether this source's data should overwrite a merged job's apply target: direct ATS
    postings beat aggregator/LinkedIn listings because we have adapters for them."""
    return raw.ats in {"greenhouse", "lever", "ashby", "workable", "smartrecruiters"}


def _retarget_unsent_application(session: Session, job: Job) -> None:
    """The apply target moved (e.g. a LinkedIn listing merged into the company's own ATS
    posting). An unsent packet was built for the old form: void any approval and rebuild it for
    the new target (new questions, new adapter)."""
    from sqlalchemy import update

    from recrute.models import Application

    if job.status == JobStatus.SHORTLISTED:
        # a build in progress fetched the OLD form's questions: void its claim so it can't
        # publish (the next packets run rebuilds for the new target)
        session.execute(update(Application).where(Application.job_id == job.id,
                                                  col(Application.submitted_at).is_(None))
                        .values(build_token="")
                        .execution_options(synchronize_session=False))
        return
    if job.status == JobStatus.APPLYING:
        # an attempt is running against the OLD form: revoke its approval (the submit gate
        # then refuses to click) so it ends at CP3; the packet is rebuilt for the new target
        res = session.execute(update(Application).where(
            Application.job_id == job.id, col(Application.submitted_at).is_(None),
            col(Application.approved_at).is_not(None))
            .values(approved_at=None, scheduled_for=None)
            .execution_options(synchronize_session=False))
        if res.rowcount:
            session.add(StatusEvent(job_id=job.id, status=JobStatus.APPLYING,
                                    note="apply target changed during the attempt: approval "
                                         "revoked, it will not be submitted"))
        return
    from sqlalchemy.orm.attributes import set_committed_value

    # conditional on the CURRENT row: a decision made meanwhile (you marked it applied or
    # skipped it) is never overwritten, and a sent application is never reopened
    unsent = ~select(Application.id).where(Application.job_id == job.id,
                                           col(Application.submitted_at).is_not(None)).exists()
    res = session.execute(
        update(Job).where(Job.id == job.id,
                          col(Job.status).in_([JobStatus.PACKET_READY, JobStatus.APPROVED]),
                          unsent)
        .values(status=JobStatus.SHORTLISTED).execution_options(synchronize_session=False))
    if res.rowcount != 1:
        return
    set_committed_value(job, "status", JobStatus.SHORTLISTED)
    session.execute(update(Application).where(Application.job_id == job.id,
                                              col(Application.submitted_at).is_(None))
                    .values(approved_at=None, scheduled_for=None, build_token="")
                    .execution_options(synchronize_session=False))
    session.add(StatusEvent(job_id=job.id, status=JobStatus.SHORTLISTED,
                            note="apply target changed; packet will be rebuilt for the new form"))


def _ingest_one(session: Session, raw: RawJob, stats: IngestStats, now) -> None:
    """Upsert one posting (runs inside a savepoint; see ingest)."""
    target, canon = job_canonical(raw)
    fkey = fuzzy_key(raw.company, raw.title, raw.locations)
    desc = description_markdown(raw.description_html, raw.description_text)
    job = _find_existing(session, raw, canon, fkey)
    if job is None:
        company = upsert_company(session, raw)
        job = Job(company_id=company.id, title=raw.title.strip(), locations=raw.locations,
                  remote=raw.remote, employment_type=raw.employment_type,
                  salary_min=raw.salary_min, salary_max=raw.salary_max,
                  salary_currency=raw.salary_currency, description_md=desc,
                  description_hash=content_hash(desc), apply_url=target,
                  canonical_url=canon, ats=raw.ats, ats_job_id=raw.ats_job_id,
                  posted_at=raw.posted_at, department=raw.department, fuzzy_key=fkey)
        session.add(job)
        session.flush()
        session.add(StatusEvent(job_id=job.id, status=JobStatus.DISCOVERED,
                                note=f"source={raw.source}"))
        stats.new += 1
        stats.new_job_ids.append(job.id)
    else:
        known = session.exec(select(JobSource).where(JobSource.job_id == job.id,
                                                     JobSource.source == raw.source)).first()
        if known is None:
            stats.merged += 1
        else:
            stats.updated += 1
        job.last_seen = now
        if job.status == JobStatus.CLOSED:
            job.status = JobStatus.DISCOVERED
            job.priority = None  # re-run rules and triage for the reopened posting
            job.score = None
            job.filter_reason = None
            session.add(StatusEvent(job_id=job.id, status=JobStatus.DISCOVERED,
                                    note=f"posting reopened (source={raw.source})"))
        job.closed_at = None
        company = session.get(Company, job.company_id) if job.company_id else None
        if company is not None:
            learn_company_board(session, company, raw)
        same_posting = (raw.ats and raw.ats == job.ats and raw.ats_job_id
                        and raw.ats_job_id == job.ats_job_id and canon != job.canonical_url
                        # a guessed URL for the same posting is no evidence the target moved
                        and not raw.apply_url_is_fallback)
        if (_prefer(raw) and job.ats != raw.ats) or same_posting:
            existing = session.exec(select(Job).where(Job.canonical_url == canon)).first()
            if existing is None or existing.id == job.id:
                target_changed = ((job.apply_url, job.ats, job.ats_job_id)
                                  != (target, raw.ats, raw.ats_job_id))
                job.apply_url, job.canonical_url = target, canon
                job.ats, job.ats_job_id = raw.ats, raw.ats_job_id
                if target_changed:
                    _retarget_unsent_application(session, job)
        before = _decision_inputs(job)
        authoritative = known is not None and (raw.ats == job.ats or not job.ats)
        if authoritative:
            # Re-poll of the same source: its current data replaces what we had.
            if desc:
                job.description_md, job.description_hash = desc, content_hash(desc)
            job.title = raw.title.strip() or job.title
            for attr in ("salary_min", "salary_max", "salary_currency", "employment_type",
                         "remote", "department", "posted_at"):
                if getattr(raw, attr) is not None:
                    setattr(job, attr, getattr(raw, attr))
            if raw.locations:
                job.locations = raw.locations
        else:
            # A different source for the same job: only fill gaps.
            if len(desc) > len(job.description_md):
                job.description_md, job.description_hash = desc, content_hash(desc)
            for attr in ("salary_min", "salary_max", "salary_currency", "employment_type",
                         "remote", "department", "posted_at"):
                if getattr(job, attr) is None and getattr(raw, attr) is not None:
                    setattr(job, attr, getattr(raw, attr))
            if not job.locations and raw.locations:
                job.locations = raw.locations
        if _decision_inputs(job) != before:
            company = session.get(Company, job.company_id) if job.company_id else None
            job.fuzzy_key = fuzzy_key(company.name if company else raw.company, job.title,
                                      job.locations)
            if job.status in RESCORABLE:
                # Inputs to the rules/triage changed: decide again from scratch.
                job.status = JobStatus.DISCOVERED
                job.priority = job.score = job.filter_reason = None
                job.years_required = None
        session.add(job)
    surl = source_url(raw)
    src = session.exec(select(JobSource).where(JobSource.source == raw.source,
                                               JobSource.url == surl)).first()
    if src is None:
        session.add(JobSource(job_id=job.id, source=raw.source,
                              source_job_id=raw.source_job_id, url=surl))
    else:
        src.seen_at = now
        session.add(src)
    session.flush()
    stats.job_ids.append(job.id)


def ingest(session: Session, raws: Iterable[RawJob]) -> IngestStats:
    stats = IngestStats()
    now = utcnow()
    for raw in raws:
        # Savepoint per posting: if a concurrent writer (web capture, another discovery run)
        # inserted the same job/company/source between our lookup and insert, the uniqueness
        # conflict is retried once, which then finds and merges the winner's row.
        for attempt in (1, 2):
            snap = (stats.new, stats.updated, stats.merged, len(stats.new_job_ids),
                    len(stats.job_ids))
            try:
                with session.begin_nested():
                    _ingest_one(session, raw, stats, now)
                break
            except IntegrityError:
                stats.new, stats.updated, stats.merged = snap[:3]
                del stats.new_job_ids[snap[3]:]
                del stats.job_ids[snap[4]:]
                if attempt == 2:
                    raise
    session.commit()
    return stats


def mark_missing_closed(session: Session, source: str, company_id: int,
                        seen_urls: set[str], seen_ids: set[str] | None = None) -> int:
    """After a successful full poll of one company's board, jobs from that board that
    disappeared are closed (only while not yet applied). Conditional updates: a status change a
    human made after this poll read the job (e.g. marking it applied) is never overwritten."""
    from sqlalchemy import update

    open_states = [JobStatus.DISCOVERED, JobStatus.SHORTLISTED, JobStatus.SNOOZED,
                   JobStatus.PACKET_READY, JobStatus.FILTERED_OUT]
    closed = 0
    now = utcnow()
    rows = session.exec(select(Job, JobSource).join(JobSource, JobSource.job_id == Job.id).where(
        Job.company_id == company_id, JobSource.source == source)).all()
    # presence is decided per JOB: a posting whose URL changed still has its old source row, so
    # it's present if ANY of its identities (source URLs, ATS job id) is in the snapshot
    by_job: dict[int, tuple[Job, list[str]]] = {}
    for job, src in rows:
        by_job.setdefault(job.id, (job, []))[1].append(src.url)
    seen_ids = seen_ids or set()
    for job, urls in by_job.values():
        present = any(u in seen_urls for u in urls) or (
            job.ats_job_id is not None and job.ats_job_id in seen_ids)
        if present or job.closed_at is not None:
            continue
        res = session.execute(
            update(Job).where(Job.id == job.id, col(Job.status).in_(open_states),
                              col(Job.closed_at).is_(None))
            .values(status=JobStatus.CLOSED, closed_at=now)
            .execution_options(synchronize_session=False))
        if res.rowcount == 1:
            session.add(StatusEvent(job_id=job.id, status=JobStatus.CLOSED,
                                    note="posting removed from board"))
            closed += 1
        else:  # already applied/in flight: just remember the posting is gone
            session.execute(update(Job).where(Job.id == job.id, col(Job.closed_at).is_(None))
                            .values(closed_at=now).execution_options(synchronize_session=False))
    session.commit()
    session.expire_all()
    return closed
