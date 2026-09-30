"""H-1B history badge from the USCIS H-1B Employer Data Hub export.

VISA BADGE: INFORMATIONAL ONLY. Never used to filter or rank jobs (PLAN.md §3.1).

The user downloads the data file(s) from https://www.uscis.gov/tools/reports-and-studies/
h-1b-employer-data-hub and imports them. Column names have changed across releases; both
layouts are handled (plus minor spelling/whitespace/encoding variants):

- FY2009–FY2023 layout: ``Fiscal Year, Employer, Initial Approval, Initial Denial,
  Continuing Approval, Continuing Denial, NAICS, Tax ID, State, City, ZIP``
- FY2024+ layout: ``Line by line, Fiscal Year, Employer (Petitioner) Name, Tax ID,
  Industry (NAICS) Code, Petitioner City, Petitioner State, Petitioner Zip Code,
  New Employment Approval, New Employment Denial, Continuation Approval, Continuation Denial,
  Change with Same Employer Approval, ..., Amended Approval, Amended Denial``
  (often UTF-16, tab separated)

Every "... Approval" column is summed. An employer appears on many rows (one per tax ID /
location / year); rows are aggregated by normalized employer name.
"""

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from recrute.badges._csv import find_col, read_rows, to_int
from recrute.badges.names import best_match, normalize_company

DEFAULT_RECENT_YEARS = 3


@dataclass
class H1BEmployer:
    name: str  # display name (first spelling seen)
    approvals_by_year: dict[int, int] = field(default_factory=dict)

    def approvals(self, years: list[int] | None = None) -> int:
        if years is None:
            return sum(self.approvals_by_year.values())
        return sum(self.approvals_by_year.get(y, 0) for y in years)


@dataclass
class H1BMatch:
    employer: str
    normalized: str
    recent_approvals: int
    score: float  # 100 = exact normalized match


class H1BIndex:
    def __init__(self, employers: dict[str, H1BEmployer] | None = None,
                 recent_years: int = DEFAULT_RECENT_YEARS):
        self.employers: dict[str, H1BEmployer] = employers or {}
        self.recent_years = recent_years

    # ---------------------------------------------------------------- import

    def add_csv(self, source: str | Path | bytes) -> int:
        """Import one Data Hub export; returns the number of rows read. Multiple files (one per
        fiscal year) can be added to the same index."""
        headers, rows = read_rows(source)
        emp_col = find_col(headers, "employer (petitioner) name", "employer", "employer name",
                           "petitioner name", contains=("employer",),
                           exclude=("city", "state", "zip", "approv", "denial", "change"))
        if emp_col is None:
            emp_col = find_col(headers, contains=("petitioner", "name"))
        fy_col = find_col(headers, "fiscal year", "fy", contains=("fiscal",))
        approval_cols = [h for h in headers if "approv" in h and "denial" not in h]
        if emp_col is None or not approval_cols:
            raise ValueError(f"unrecognized H-1B Data Hub columns: {headers}")
        n = 0
        for row in rows:
            name = row.get(emp_col, "")
            key = normalize_company(name)
            if not key:
                continue
            year = to_int(row.get(fy_col)) if fy_col else 0
            approvals = sum(to_int(row.get(c)) for c in approval_cols)
            emp = self.employers.get(key)
            if emp is None:
                emp = self.employers[key] = H1BEmployer(name=name.strip())
            emp.approvals_by_year[year] = emp.approvals_by_year.get(year, 0) + approvals
            n += 1
        return n

    @classmethod
    def from_csv(cls, *sources: str | Path | bytes,
                 recent_years: int = DEFAULT_RECENT_YEARS) -> "H1BIndex":
        idx = cls(recent_years=recent_years)
        for s in sources:
            idx.add_csv(s)
        return idx

    # ---------------------------------------------------------------- queries

    def latest_year(self) -> int:
        return max((y for e in self.employers.values() for y in e.approvals_by_year), default=0)

    def recent_window(self) -> list[int] | None:
        latest = self.latest_year()
        if latest <= 0:
            return None  # file had no fiscal-year column: count everything
        return list(range(latest - self.recent_years + 1, latest + 1))

    def match_company(self, name: str | None, *, cutoff: float = 92.0) -> H1BMatch | None:
        key = normalize_company(name)
        if not key:
            return None
        if key in self.employers:
            hit_key, score = key, 100.0
        else:
            hit = best_match(key, list(self.employers), cutoff=cutoff)
            if hit is None:
                return None
            hit_key, score = hit
        emp = self.employers[hit_key]
        return H1BMatch(emp.name, hit_key, emp.approvals(self.recent_window()), score)

    def recent_approvals(self, name: str | None) -> int | None:
        """Recent approvals for a company, 0 if the data is loaded but the name isn't in it
        (never filed / no approvals), None if no data is loaded."""
        if not self.employers:
            return None
        m = self.match_company(name)
        return m.recent_approvals if m else 0

    # ---------------------------------------------------------------- persistence (data/)

    def save(self, path: Path) -> None:
        payload = {"recent_years": self.recent_years,
                   "employers": {k: {"name": e.name, "years": e.approvals_by_year}
                                 for k, e in self.employers.items()}}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "H1BIndex":
        data = json.loads(path.read_text(encoding="utf-8"))
        emps = {k: H1BEmployer(v["name"], {int(y): int(n) for y, n in v["years"].items()})
                for k, v in data["employers"].items()}
        return cls(emps, recent_years=data.get("recent_years", DEFAULT_RECENT_YEARS))


def load_h1b_csv(*sources: str | Path | bytes,
                 recent_years: int = DEFAULT_RECENT_YEARS) -> H1BIndex:
    return H1BIndex.from_csv(*sources, recent_years=recent_years)


def match_company(name: str | None, index: H1BIndex) -> H1BMatch | None:
    return index.match_company(name)


def approvals_by_employer(index: H1BIndex) -> dict[str, int]:
    """Normalized employer name -> recent approvals (handy for bulk UI display)."""
    window = index.recent_window()
    out: dict[str, int] = defaultdict(int)
    for k, e in index.employers.items():
        out[k] = e.approvals(window)
    return dict(out)
