"""Source connector interface, shared context, and the source registry.

A source is anything with a ``name`` and a ``fetch(ctx) -> Iterator[RawJob]``. Sources never
touch the DB: they yield ``schemas.RawJob`` and the pipeline (normalize/dedup) takes it from there.

Per-company ATS board sources (greenhouse, lever, ...) read ``ctx.companies``; keyword-search
sources (aggregators, LinkedIn) read ``ctx.criteria.all_search_queries()``.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import yaml

from recrute.schemas import RawJob

if TYPE_CHECKING:  # heavy/optional imports only for type checkers
    from recrute.criteria import Criteria
    from recrute.http import Http
    from recrute.llm.router import LLMRouter

log = logging.getLogger(__name__)

SEED_FILE = Path(__file__).with_name("seed_companies.yaml")


class SourceBlocked(RuntimeError):  # noqa: N818 - public name requested by the spec
    """A site showed a block signal (CAPTCHA, checkpoint, "unusual activity", logout, 429/999).

    The caller must stop using that source and back off for ``backoff`` before trying again.
    """

    def __init__(self, source: str, reason: str, url: str | None = None,
                 backoff: timedelta = timedelta(days=3)):
        super().__init__(f"{source} blocked: {reason}" + (f" ({url})" if url else ""))
        self.source = source
        self.reason = reason
        self.url = url
        self.backoff = backoff


@dataclass(frozen=True)
class CompanyRef:
    """A company whose public ATS board we poll (mirrors models.Company's ats/ats_token)."""

    name: str
    ats: str  # greenhouse | lever | ashby | workable | smartrecruiters
    ats_token: str
    tags: tuple[str, ...] = ()


@dataclass
class SourceContext:
    http: Http
    criteria: Criteria  # search queries per track: criteria.all_search_queries()
    companies: list[CompanyRef] = field(default_factory=list)  # for per-company ATS boards
    router: LLMRouter | None = None  # LLM extraction (HN); None -> skip LLM parts
    since: datetime | None = None  # only postings newer than this (unknown dates pass)
    max_items: int | None = None  # per-source cap on yielded jobs
    # Filled by sources: "<ats>:<token>" (or "<source>") -> error message. Lets the caller set
    # Company.poll_error without one bad board aborting the whole run.
    errors: dict[str, str] = field(default_factory=dict)
    # Boards ("<ats>:<token>") whose listing was cut short (e.g. page cap). Their results are
    # not a full snapshot, so missing postings must NOT be treated as closed.
    incomplete: set[str] = field(default_factory=set)

    def companies_for(self, ats: str) -> list[CompanyRef]:
        return [c for c in self.companies if c.ats == ats]

    def is_new(self, posted_at: datetime | None) -> bool:
        if self.since is None or posted_at is None:
            return True
        since = self.since if self.since.tzinfo else self.since.astimezone()
        return posted_at >= since


@runtime_checkable
class Source(Protocol):
    name: str

    def fetch(self, ctx: SourceContext) -> Iterator[RawJob]: ...


def limited(ctx: SourceContext, jobs: Iterable[RawJob],
            apply_since: bool = True) -> Iterator[RawJob]:
    """Apply ``ctx.since`` (unless the source already filtered server-side at coarser
    granularity) and ``ctx.max_items`` to a stream of jobs."""
    n = 0
    for job in jobs:
        if apply_since and not ctx.is_new(job.posted_at):
            continue
        yield job
        n += 1
        if ctx.max_items is not None and n >= ctx.max_items:
            return


# --------------------------------------------------------------------------- registry


def _lazy(module: str, cls: str, **kwargs) -> Callable[[], Source]:
    def factory() -> Source:
        mod = importlib.import_module(f"recrute.sources.{module}")
        return getattr(mod, cls)(**kwargs)

    return factory


SOURCES: dict[str, Callable[[], Source]] = {
    # Tier 1: public ATS job boards (per company)
    "greenhouse": _lazy("greenhouse", "GreenhouseSource"),
    "lever": _lazy("lever", "LeverSource"),
    "ashby": _lazy("ashby", "AshbySource"),
    "workable": _lazy("workable", "WorkableSource"),
    "smartrecruiters": _lazy("smartrecruiters", "SmartRecruitersSource"),
    # Tier 2: aggregators / free APIs
    "remotive": _lazy("remotive", "RemotiveSource"),
    "remoteok": _lazy("remoteok", "RemoteOKSource"),
    "himalayas": _lazy("himalayas", "HimalayasSource"),
    "adzuna": _lazy("adzuna", "AdzunaSource"),
    "hn_whoshiring": _lazy("hn", "HNWhoIsHiringSource"),
    # Tier 3: walled gardens
    "linkedin_guest": _lazy("linkedin_guest", "LinkedInGuestSource"),
    "linkedin_session": _lazy("linkedin_session", "LinkedInSessionSource"),
}

ATS_BOARD_SOURCES = ("greenhouse", "lever", "ashby", "workable", "smartrecruiters")


def get_source(name: str) -> Source:
    try:
        factory = SOURCES[name]
    except KeyError:
        raise KeyError(f"unknown source {name!r}; known: {', '.join(sorted(SOURCES))}") from None
    return factory()


def load_seed_companies(path: Path | None = None) -> list[CompanyRef]:
    """The bundled seed registry (sources/seed_companies.yaml) as CompanyRefs."""
    data = yaml.safe_load((path or SEED_FILE).read_text(encoding="utf-8")) or {}
    return [CompanyRef(name=c["name"], ats=c["ats"], ats_token=str(c["token"]),
                       tags=tuple(c.get("tags") or ()))
            for c in data.get("companies", [])]
