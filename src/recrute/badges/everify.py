"""E-Verify badge from a CSV export of the E-Verify employer search
(https://www.e-verify.gov/about-e-verify/e-verify-data/e-verify-employer-search).

VISA BADGE: INFORMATIONAL ONLY (relevant for STEM OPT). Never used to filter or rank jobs.

Column names vary; any "employer / company / business name" and "doing business as" columns are
read. Rows whose status/termination column says the account is terminated/closed are skipped.
"""

import json
from pathlib import Path

from recrute.badges._csv import read_rows
from recrute.badges.names import best_match, name_variants, normalize_company

_INACTIVE = ("terminat", "closed", "inactive", "suspend")


class EVerifyIndex:
    def __init__(self, names: set[str] | None = None):
        self.names: set[str] = names or set()

    def add_csv(self, source: str | Path | bytes) -> int:
        headers, rows = read_rows(source)
        name_cols = [h for h in headers
                     if ("name" in h or "dba" in h or "doing business" in h or h in (
                         "employer", "company", "business"))
                     and not any(x in h for x in ("city", "state", "site", "contact", "user"))]
        if not name_cols:
            raise ValueError(f"unrecognized E-Verify export columns: {headers}")
        status_cols = [h for h in headers if "status" in h]
        term_cols = [h for h in headers if "termination" in h]
        n = 0
        for row in rows:
            if any(any(w in row.get(c, "").lower() for w in _INACTIVE) for c in status_cols):
                continue
            if any(row.get(c, "").strip() for c in term_cols):
                continue
            for c in name_cols:
                self.names.update(name_variants(row.get(c)))
            n += 1
        return n

    @classmethod
    def from_csv(cls, *sources: str | Path | bytes) -> "EVerifyIndex":
        idx = cls()
        for s in sources:
            idx.add_csv(s)
        return idx

    def lookup(self, name: str | None, *, cutoff: float = 95.0) -> bool | None:
        """True = enrolled, False = not found in the loaded export, None = no data loaded."""
        if not self.names:
            return None
        key = normalize_company(name)
        if not key:
            return None
        if key in self.names:
            return True
        return best_match(key, list(self.names), cutoff=cutoff) is not None

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(self.names)), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "EVerifyIndex":
        return cls(set(json.loads(path.read_text(encoding="utf-8"))))


def load_everify_csv(*sources: str | Path | bytes) -> EVerifyIndex:
    return EVerifyIndex.from_csv(*sources)
