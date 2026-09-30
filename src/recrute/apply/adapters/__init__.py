"""Adapter registry: scripted adapters for known ATSs, the generic filler for everything else."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from recrute.apply.adapters.ashby import AshbyAdapter
from recrute.apply.adapters.generic import GenericAdapter
from recrute.apply.adapters.greenhouse import GreenhouseAdapter
from recrute.apply.adapters.lever import LeverAdapter
from recrute.apply.adapters.linkedin_easy_apply import LinkedInEasyApplyAdapter
from recrute.apply.base import Adapter

if TYPE_CHECKING:
    from recrute.models import Job

SCRIPTED: tuple[Adapter, ...] = (GreenhouseAdapter(), LeverAdapter(), AshbyAdapter(),
                                 LinkedInEasyApplyAdapter())
ADAPTERS: dict[str, Adapter] = {a.name: a for a in SCRIPTED} | {"generic": GenericAdapter()}


def adapter_for(job: Job, *, router: Any = None) -> Adapter:
    """By job.ats first, then by the apply URL's host; unknown -> generic (needs a router)."""
    ats = (job.ats or "").lower()
    for a in SCRIPTED:
        if ats and ats in getattr(a, "ats_names", ()):
            return a
    host = urlparse(job.apply_url or "").hostname or ""
    for a in SCRIPTED:
        if any(host == h or host.endswith("." + h) for h in getattr(a, "hosts", ())):
            return a
    return GenericAdapter(router)


def get_adapter(name: str, *, router: Any = None) -> Adapter:
    if name == "generic":
        return GenericAdapter(router)
    return ADAPTERS[name]


__all__ = ["ADAPTERS", "SCRIPTED", "AshbyAdapter", "GenericAdapter", "GreenhouseAdapter",
           "LeverAdapter", "LinkedInEasyApplyAdapter", "adapter_for", "get_adapter"]
