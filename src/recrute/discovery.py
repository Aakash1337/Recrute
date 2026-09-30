"""Discovery tasks: poll ATS boards per company, run search/aggregator sources, and the
budgeted logged-in LinkedIn session. Everything found goes through the normal ingest/dedup."""

import logging
from datetime import UTC, date, datetime, timedelta

from sqlmodel import Session, select

from recrute.http import Http, HttpError
from recrute.models import Company, utcnow
from recrute.pipeline.ingest import ingest, mark_missing_closed
from recrute.settings import get_setting, get_state, set_state
from recrute.sources import (
    ATS_BOARD_SOURCES,
    CompanyRef,
    SourceBlocked,
    SourceContext,
    get_source,
    load_seed_companies,
)

log = logging.getLogger("recrute.discovery")

LINKEDIN_CHANNEL = "linkedin_easy_apply"
SEARCH_SOURCES = ("remotive", "remoteok", "himalayas", "hn_whoshiring", "adzuna",
                  "linkedin_guest")


def _enabled(session: Session, name: str) -> bool:
    return bool(get_setting(session, "sources_enabled").get(name, False))


def sync_seed_companies(session: Session) -> int:
    """Adds bundled seed companies that aren't in the registry yet (never re-enables ones the
    user paused)."""
    added = 0
    for ref in load_seed_companies():
        exists = session.exec(select(Company).where(Company.ats == ref.ats,
                                                    Company.ats_token == ref.ats_token)).first()
        if exists is None:
            session.add(Company(name=ref.name, ats=ref.ats, ats_token=ref.ats_token,
                                origin="seed"))
            added += 1
    session.commit()
    return added


def poll_company(session: Session, http: Http, ctx, company: Company) -> dict:
    ref = CompanyRef(name=company.name, ats=company.ats, ats_token=company.ats_token)
    sctx = SourceContext(http=http, criteria=ctx.criteria, companies=[ref])
    raws = list(get_source(company.ats).fetch(sctx))
    company.last_polled_at = utcnow()
    error = next(iter(sctx.errors.values()), None)
    company.poll_error = error[:300] if error else None
    session.add(company)
    session.commit()
    if error:
        return {"error": 1}
    stats = ingest(session, raws).as_dict()
    if sctx.incomplete:  # partial listing: absence proves nothing
        stats["closed"] = 0
    else:
        stats["closed"] = mark_missing_closed(session, company.ats, company.id,
                                              {r.url for r in raws})
    return stats


def discover_boards(ctx) -> dict:
    totals = {"companies": 0, "new": 0, "updated": 0, "merged": 0, "closed": 0, "errors": 0}
    http = Http(min_interval=1.0)
    try:
        with ctx.session() as s:
            totals["seeded"] = sync_seed_companies(s)
            companies = s.exec(select(Company).where(
                Company.active == True, Company.ats.in_(ATS_BOARD_SOURCES))).all()  # noqa: E712
            for company in companies:
                if ctx.stop.is_set():
                    break
                if not _enabled(s, company.ats):
                    continue
                try:
                    st = poll_company(s, http, ctx, company)
                except Exception as e:  # one broken board must not stop the rest
                    s.rollback()
                    log.warning("board %s:%s failed: %s", company.ats, company.ats_token, e)
                    company.poll_error = f"{e.__class__.__name__}: {str(e)[:200]}"
                    s.add(company)
                    s.commit()
                    st = {"error": 1}
                totals["companies"] += 1
                totals["errors"] += st.pop("error", 0)
                for k, v in st.items():
                    totals[k] = totals.get(k, 0) + v
    finally:
        http.close()
    return totals


def discover_search(ctx) -> dict:
    """Aggregators, HN and logged-out LinkedIn, each on its own cadence and backoff."""
    out: dict = {}
    now = datetime.now(UTC)
    http = Http(min_interval=2.0)
    try:
        with ctx.session() as s:
            for name in SEARCH_SOURCES:
                if ctx.stop.is_set() or not _enabled(s, name):
                    continue
                src = get_source(name)
                state = get_state(s, f"source:{name}", {}) or {}
                if state.get("backoff_until") and \
                        datetime.fromisoformat(state["backoff_until"]) > now:
                    out[name] = "backing off"
                    continue
                last = datetime.fromisoformat(state["last_ok"]) if state.get("last_ok") else None
                cadence = getattr(src, "cadence", timedelta(hours=2))
                if last and last + cadence > now:
                    continue
                if name == "hn_whoshiring":
                    src.task = "extract_postings"
                # delayed feeds publish old postings late: widen the window by that delay
                delay = getattr(src, "feed_delay", timedelta(0))
                sctx = SourceContext(http=http, criteria=ctx.criteria, router=ctx.router,
                                     since=last - timedelta(hours=1) - delay if last else None)
                try:
                    raws = list(src.fetch(sctx))
                    if sctx.errors:
                        # (partial) failure swallowed by the source: keep what we got, but DON'T
                        # advance the completion cursor, so the failed part is retried later
                        if raws:
                            out[name] = ingest(s, raws).as_dict() | {"partial": True}
                        msg = " ".join(sctx.errors.values())
                        limited = any(code in msg for code in ("429", "999", "403"))
                        wait = 6 * 3600 if limited else 1800
                        state["backoff_until"] = (now + timedelta(seconds=wait)).isoformat()
                        out.setdefault(name, "failed: backing off")
                    else:
                        out[name] = ingest(s, raws).as_dict()
                        state = {"last_ok": now.isoformat()}
                except HttpError as e:
                    s.rollback()
                    wait = e.retry_after or (6 * 3600 if e.status in (429, 999) else 1800)
                    state["backoff_until"] = (now + timedelta(seconds=wait)).isoformat()
                    out[name] = f"http {e.status}: backing off"
                except Exception as e:
                    s.rollback()
                    log.warning("source %s failed: %s", name, e)
                    out[name] = f"error: {e.__class__.__name__}"
                if sctx.errors:
                    state["errors"] = dict(list(sctx.errors.items())[:5])
                set_state(s, f"source:{name}", state)
    finally:
        http.close()
    return out


def discover_linkedin(ctx) -> dict:
    """Logged-in, read-only LinkedIn browsing with a persisted daily budget, seen-ID cache and
    kill-switch backoff (PLAN 3.2 Tier 3). Off unless enabled in Settings."""
    from recrute.sources.linkedin_session import LinkedInSessionSource, SessionBudget

    with ctx.session() as s:
        if not _enabled(s, "linkedin_session"):
            return {"skipped": "disabled"}
        from recrute.apply.state import suspend, suspension

        state = get_state(s, "linkedin_session", {}) or {}
        now = datetime.now(UTC)
        if state.get("blocked_until") and datetime.fromisoformat(state["blocked_until"]) > now:
            return {"skipped": f"blocked until {state['blocked_until']}",
                    "reason": state.get("block_reason")}
        # One LinkedIn account: a security signal while APPLYING also stops browsing.
        if applying_block := suspension(s, LINKEDIN_CHANNEL, now):
            return {"skipped": "LinkedIn applying is suspended",
                    "reason": applying_block.get("reason")}
        today = date.today().isoformat()
        if state.get("day") != today:
            state.update(day=today, searches=0, views=0)
        budget_cfg = get_setting(s, "linkedin_session_budget")
        budget = SessionBudget(max_searches=int(budget_cfg["searches"]),
                               max_views=int(budget_cfg["views"]),
                               searches_used=state["searches"], views_used=state["views"])
        seen = set(state.get("seen_ids", []))
        hours = get_setting(s, "active_hours")
        src = LinkedInSessionSource(budget=budget, seen_ids=seen,
                                    active_hours=(int(hours[0]), int(hours[1])))
        sctx = SourceContext(http=Http(), criteria=ctx.criteria, router=ctx.router)
        found = []
        result: dict = {}
        try:
            for raw in src.fetch(sctx):
                found.append(raw)
        except SourceBlocked as e:
            until = now + e.backoff
            state.update(blocked_until=until.isoformat(), block_reason=e.reason)
            result["blocked"] = e.reason
            # ...and a security signal while BROWSING stops automated Easy Apply too.
            suspend(s, LINKEDIN_CHANNEL, now, f"LinkedIn browsing: {e.reason}", e.backoff)
            from recrute.tasks import notify

            notify(s, "LinkedIn browsing paused",
                   f"LinkedIn showed a security check ({e.reason}). Browsing is paused until "
                   f"{until:%Y-%m-%d}. Log in manually and check your account.", "high")
        finally:
            state["searches"] = src.budget.searches_used
            state["views"] = src.budget.views_used
            state["seen_ids"] = sorted(src.seen_ids)[-5000:]
            set_state(s, "linkedin_session", state)
        if found:
            result.update(ingest(s, found).as_dict())
        result.update(searches=state["searches"], views=state["views"])
        return result
