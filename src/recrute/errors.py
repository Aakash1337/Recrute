"""Error text safe for logs and the UI: never echoes the data an exception was raised about."""

from __future__ import annotations

import traceback


def safe_error(e: BaseException) -> str:
    """Error text for logs/UI without echoing data. Validation errors (e.g. a malformed profile)
    carry the offending input values, so only their field paths are kept."""
    import yaml
    from pydantic import ValidationError

    if isinstance(e, ValidationError):
        locs = ", ".join(".".join(str(p) for p in err["loc"]) for err in e.errors()[:5])
        return f"ValidationError in {e.title}: invalid field(s) {locs}"
    if isinstance(e, yaml.YAMLError):
        # YAML errors quote the offending source line (e.g. your email): position only
        mark = getattr(e, "problem_mark", None) or getattr(e, "context_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        name = getattr(mark, "name", "") if mark else ""
        src = f" in {name}" if name and not name.startswith("<") else ""
        return f"{e.__class__.__name__}: invalid YAML{src}{where}"
    return f"{e.__class__.__name__}: {str(e)[:200]}"


def safe_traceback(e: BaseException, limit: int = 30) -> str:
    """A traceback for debug logs: stack locations (file:line in function) for the exception
    and its causes, each summarised with safe_error. Frame source lines and raw exception
    messages (which may embed the offending input values) are left out."""
    parts: list[str] = []
    seen: set[int] = set()
    cur: BaseException | None = e
    while cur is not None and id(cur) not in seen and len(seen) < 5:
        seen.add(id(cur))
        frames = traceback.extract_tb(cur.__traceback__, limit=limit)
        stack = "\n".join(f"  {f.filename}:{f.lineno} in {f.name}" for f in frames)
        parts.append(f"{safe_error(cur)}\n{stack}" if stack else safe_error(cur))
        cur = cur.__cause__ or (None if cur.__suppress_context__ else cur.__context__)
    return "\n-- caused by / during --\n".join(parts)
