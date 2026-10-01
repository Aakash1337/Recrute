"""Discovery tasks: poll ATS boards per company, run search/aggregator sources, and the
budgeted logged-in LinkedIn session. Everything found goes through the normal ingest/dedup."""

import logging
import math
from datetime import UTC, date, datetime, timedelta

from sqlmodel import Session, select

from recrute.http import Http, HttpError
from recrute.models import Company, utcnow
from recrute.pipeline.ingest import ingest, mark_missing_closed
from recrute.settings import get_setting, get_state, set_state, update_state
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
        stats["closed"] = mark_missing_closed(
            session, company.ats, company.id, {r.url for r in raws},
            {r.ats_job_id for r in raws if r.ats_job_id})
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
                if hasattr(src, "done"):  # thread progress replaces the date cursor
                    src.done = state.get("done") or {}
                if name == "linkedin_guest":  # postings already fetched in full: not again
                    src.seen_ids = set(state.get("seen_ids") or [])
                rotating = hasattr(src, "query_offset")
                if rotating:
                    import math

                    src.query_offset = int(state.get("query_offset", 0))
                    src.given_up = {q for q, n in (state.get("query_failures") or {}).items()
                                    if n >= 3}
                    nq = max(1, len(ctx.criteria.all_search_queries()))
                    cycles = math.ceil(nq / max(1, getattr(src, "max_searches", nq)))
                # delayed feeds publish old postings late: widen the window by that delay
                delay = getattr(src, "feed_delay", timedelta(0))
                since = last - timedelta(hours=1) - delay if last else None
                if rotating and last:
                    # each query only comes round every few runs, and runs can be missed or
                    # fail: look back to the OLDEST successful search of the queries due now
                    qs = [q for _, q in ctx.criteria.all_search_queries()]
                    budget = max(1, getattr(src, "max_searches", len(qs) or 1))
                    start = src.query_offset % len(qs) if qs else 0
                    due = (qs[start:] + qs[:start])[:budget]
                    ok = state.get("query_ok") or {}
                    if due and all(q in ok for q in due):
                        since = min(datetime.fromisoformat(ok[q]) for q in due) \
                            - timedelta(hours=1) - delay
                    else:  # never searched (or older state): the widest rotation window
                        since = min(last, now - cadence * cycles) - timedelta(hours=1) - delay
                sctx = SourceContext(http=http, criteria=ctx.criteria, router=ctx.router,
                                     since=since)
                offset_before = state.get("query_offset", 0)
                ingested_ok = False
                try:
                    raws = list(src.fetch(sctx))
                    if sctx.errors:
                        # (partial) failure swallowed by the source: keep what we got, but DON'T
                        # advance the completion cursor, so the failed part is retried later
                        if raws:
                            out[name] = ingest(s, raws).as_dict() | {"partial": True}
                        ingested_ok = True  # (with nothing found there is nothing to lose)
                        msg = " ".join(sctx.errors.values())
                        limited = any(code in msg for code in ("429", "999", "403"))
                        wait = 6 * 3600 if limited else 1800
                        state["backoff_until"] = (now + timedelta(seconds=wait)).isoformat()
                        out.setdefault(name, "failed: backing off")
                    else:
                        out[name] = ingest(s, raws).as_dict()
                        state = {"last_ok": now.isoformat(),
                                 **{k: state[k] for k in ("query_ok", "query_failures", "done",
                                                          "seen_ids") if k in state}}
                        ingested_ok = True
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
                if ingested_ok and name == "linkedin_guest":
                    # their postings are stored now: remember them (bounded, newest kept)
                    known = list(state.get("seen_ids") or [])
                    known += [i for i in sorted(src.seen_ids) if i not in set(known)]
                    state["seen_ids"] = known[-5000:]
                if ingested_ok and getattr(src, "thread_id", None):
                    prev = state.get("done") or {}
                    ids = set(prev.get("ids") or []) \
                        if str(prev.get("thread")) == src.thread_id else set()
                    state["done"] = {"thread": src.thread_id,
                                     "ids": sorted(ids | set(src.processed))}
                if rotating:
                    if ingested_ok:  # these searches' results are stored: they're covered
                        ok = dict(state.get("query_ok") or {})
                        ok.update({q: now.isoformat() for q in getattr(src, "searched_ok", [])})
                        state["query_ok"] = ok
                        state["query_offset"] = getattr(src, "next_offset", 0)
                        fails = dict(state.get("query_failures") or {})
                        for q in getattr(src, "searched_ok", []):
                            fails.pop(q, None)
                        for key in sctx.errors:
                            q = key.partition(":")[2]
                            if q:
                                fails[q] = fails.get(q, 0) + 1
                        state["query_failures"] = fails
                    else:  # nothing was stored: search the same queries again next time
                        state["query_offset"] = offset_before
                set_state(s, f"source:{name}", state)
    finally:
        http.close()
    return out


class _GuardStop(Exception):
    pass


class GuardedPage:
    """Wraps the automation page: every navigation first re-checks (with fresh DB state) the
    LinkedIn kill switch and active hours, so a suspension set by the apply thread while
    discovery waited for the browser stops it before it touches LinkedIn again."""

    def __init__(self, page, guard):
        self._page, self._guard = page, guard

    def goto(self, url, *args, **kwargs):
        if reason := self._guard():
            raise _GuardStop(reason)
        return self._page.goto(url, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._page, name)


def linkedin_guard(ctx) -> str | None:
    from recrute.apply.state import suspension

    with ctx.session() as s:
        now = datetime.now(UTC)
        state = get_state(s, "linkedin_session", {}) or {}
        if state.get("blocked_until") and datetime.fromisoformat(state["blocked_until"]) > now:
            return "LinkedIn browsing is blocked"
        if suspension(s, LINKEDIN_CHANNEL, now) is not None:
            return "LinkedIn applying is suspended"
        start, end = (int(h) for h in get_setting(s, "active_hours"))
        if not start <= datetime.now().hour < end:
            return "outside active hours"
    return None


def _guarded_page_factory(ctx):
    from contextlib import contextmanager

    from recrute.browser.runtime import open_context

    @contextmanager
    def factory():
        with open_context(ctx.config.browser, ctx.paths, headless=False) as bctx:
            # the lock is ours now: re-check before the first navigation too
            page = bctx.pages[0] if bctx.pages else bctx.new_page()
            yield GuardedPage(page, lambda: linkedin_guard(ctx))

    return factory


# how often the worker runs a logged-in LinkedIn session (worker.default_tasks)
LINKEDIN_SESSION_EVERY = timedelta(hours=12)


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
        caps = {"searches": int(budget_cfg["searches"]), "views": int(budget_cfg["views"])}
        bind = s.get_bind()

        def reserve(kind: str) -> bool:
            """Take one of today's slots in the DB before the page is opened: atomic across
            overlapping runs, and already counted if this process dies mid-browse."""
            def take(cur: dict):
                day = date.today().isoformat()
                if cur.get("day") != day:
                    cur.update(day=day, searches=0, views=0)
                if int(cur.get(kind, 0)) >= caps[kind]:
                    return None, False
                cur[kind] = int(cur.get(kind, 0)) + 1
                return cur, True
            return update_state(bind, "linkedin_session", take)

        budget = SessionBudget(max_searches=caps["searches"], max_views=caps["views"],
                               searches_used=state["searches"], views_used=state["views"],
                               reserve=reserve)
        seen = set(state.get("seen_ids", []))
        hours = get_setting(s, "active_hours")
        kwargs = {}
        if getattr(ctx, "config", None) is not None:
            kwargs["page_factory"] = _guarded_page_factory(ctx)
        cursor_before = int(state.get("query_cursor", 0))
        src = LinkedInSessionSource(budget=budget, seen_ids=seen,
                                    active_hours=(int(hours[0]), int(hours[1])),
                                    query_cursor=cursor_before, **kwargs)
        # each query only comes round every few sessions: look back to the OLDEST successful
        # search of the queries due now (a never-searched one: a whole rotation)
        queries = [q for _, q in ctx.criteria.all_search_queries()]
        per = max(1, int(getattr(src, "per_session_searches", 3)))
        rotated = (queries[cursor_before % len(queries):]
                   + queries[:cursor_before % len(queries)]) if queries else []
        due = rotated[:per]
        ok = state.get("query_ok") or {}
        rotation = LINKEDIN_SESSION_EVERY * math.ceil(len(queries) / per) if queries \
            else LINKEDIN_SESSION_EVERY
        since = min((datetime.fromisoformat(ok[q]) if q in ok else now - rotation
                     for q in due), default=None)
        sctx = SourceContext(http=Http(), criteria=ctx.criteria, router=ctx.router,
                             since=since - timedelta(hours=1) if since else None)
        found = []
        result: dict = {}
        crashed: Exception | None = None
        try:
            for raw in src.fetch(sctx):
                found.append(raw)
        except _GuardStop as e:
            result["stopped"] = str(e)  # quiet stop: someone else already raised the alarm
        except SourceBlocked as e:
            until, why = now + e.backoff, e.reason
            update_state(bind, "linkedin_session", lambda cur: (
                {**cur, "blocked_until": until.isoformat(), "block_reason": why}, None))
            result["blocked"] = e.reason
            # ...and a security signal while BROWSING stops automated Easy Apply too.
            suspend(s, LINKEDIN_CHANNEL, now, f"LinkedIn browsing: {e.reason}", e.backoff)
            from recrute.tasks import notify

            notify(s, "LinkedIn browsing paused",
                   f"LinkedIn showed a security check ({e.reason}). Browsing is paused until "
                   f"{until:%Y-%m-%d}. Log in manually and check your account.", "high")
        except Exception as e:  # e.g. a navigation timeout: keep what was already collected
            crashed = e
        # (the budget was reserved in the DB slot by slot, before each page: nothing to save)
        if found:
            result.update(ingest(s, found).as_dict())
        # only now are the postings stored: acknowledge their ids (a failure before this point
        # leaves them unseen, so the next session fetches them again)
        cursor_after = int(getattr(src, "query_cursor", cursor_before))
        # only queries whose postings were ALL delivered (and are now stored) are covered up to
        # `now`; one cut short keeps its older checkpoint so nothing it found is skipped
        if hasattr(src, "completed_queries"):
            searched = list(src.completed_queries())
        else:
            searched = [rotated[i % len(rotated)] for i in range(cursor_after - cursor_before)] \
                if rotated else []

        def finish(cur: dict):  # merged into the CURRENT state (another run may have saved)
            ids = set(cur.get("seen_ids") or []) | set(src.seen_ids)
            return {**cur, "seen_ids": sorted(ids)[-5000:],
                    "query_cursor": max(int(cur.get("query_cursor", 0)), cursor_after),
                    "query_ok": {**(cur.get("query_ok") or {}),
                                 **{q: now.isoformat() for q in searched}}}, cur

        final = update_state(bind, "linkedin_session", finish)
        result.update(searches=int(final.get("searches", 0)) if final else 0,
                      views=int(final.get("views", 0)) if final else 0)
        if crashed is not None:
            raise crashed
        return result
