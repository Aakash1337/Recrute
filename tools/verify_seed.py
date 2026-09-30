"""Verify the seed company registry against live public ATS job-board APIs.

    uv run python tools/verify_seed.py                  # verify every entry
    uv run python tools/verify_seed.py --only ashby     # one ATS only
    uv run python tools/verify_seed.py --jobs           # also print a few job titles per board

Exits non-zero if any board fails (HTTP error, zero jobs) or an (ats, token) pair is duplicated.
"""

import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from recrute.http import Http, HttpError

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FILE = ROOT / "src" / "recrute" / "sources" / "seed_companies.yaml"

ENDPOINTS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
    "lever": "https://api.lever.co/v0/postings/{token}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{token}",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{token}",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{token}/postings",
}


def extract_jobs(ats: str, data: Any) -> list[dict]:
    if ats == "lever":
        return data if isinstance(data, list) else []
    if not isinstance(data, dict):
        return []
    return data.get("content" if ats == "smartrecruiters" else "jobs") or []


def job_title(job: dict) -> str:
    return str(job.get("title") or job.get("text") or job.get("name") or "?")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE, help="registry YAML path")
    parser.add_argument("--only", choices=sorted(ENDPOINTS), help="verify a single ATS")
    parser.add_argument("--jobs", action="store_true", help="print sample job titles")
    args = parser.parse_args()

    companies = yaml.safe_load(args.file.read_text(encoding="utf-8"))["companies"]
    failures = 0
    pairs = Counter((c["ats"], c["token"]) for c in companies)
    dups = [(pair, n) for pair, n in pairs.items() if n > 1]
    for (ats, token), n in dups:
        print(f"DUP   {ats}:{token} appears {n} times")

    selected = [c for c in companies if not args.only or c["ats"] == args.only]
    http = Http(min_interval=0.5, retries=2)
    try:
        for c in selected:
            ats, token = c["ats"], c["token"]
            if ats not in ENDPOINTS:
                print(f"FAIL  {c['name']:<28} {ats:<16} {token:<24} unknown ats")
                failures += 1
                continue
            try:
                jobs = extract_jobs(ats, http.get_json(ENDPOINTS[ats].format(token=token)))
                detail = str(len(jobs))
            except (HttpError, ValueError) as e:
                jobs, detail = [], f"error: {e}"[:60]
            ok = bool(jobs)
            failures += not ok
            print(f"{'OK' if ok else 'FAIL':<5} {c['name']:<28} {ats:<16} {token:<24} {detail}")
            if args.jobs:
                for job in jobs[:3]:
                    print(f"        - {job_title(job)}")
    finally:
        http.close()

    print(f"\n{len(selected) - failures} ok, {failures} failed, {len(dups)} duplicates, "
          f"{len(selected)} checked ({len(companies)} companies in {args.file.name})")
    return 1 if failures or dups else 0


if __name__ == "__main__":
    sys.exit(main())
