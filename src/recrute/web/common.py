from datetime import UTC, datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup
from sqlmodel import Session, func, select

from recrute.models import Job, JobStatus

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")

# html=False: raw HTML in job descriptions is escaped (they're untrusted third-party content);
# markdown-it also refuses javascript:/data: links.
_md = MarkdownIt("commonmark", {"html": False, "linkify": False})


def render_md(text: str | None) -> Markup:
    return Markup(_md.render(text or ""))


def ago(dt: datetime | None) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    secs = (datetime.now(UTC) - dt).total_seconds()
    if secs < 3600:
        return f"{max(1, int(secs // 60))}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def safe_url(url: str | None) -> str:
    """Only http(s) links from scraped data are rendered (blocks javascript:/data: URLs)."""
    url = (url or "").strip()
    return url if url.lower().startswith(("http://", "https://")) else "#"


def money(v: int | None) -> str:
    return f"${v / 1000:.0f}k" if v else ""


templates.env.filters["md"] = render_md
templates.env.filters["ago"] = ago
templates.env.filters["money"] = money
templates.env.filters["safe_url"] = safe_url


def nav_counts(session: Session) -> dict[str, int]:
    def count(*conds) -> int:
        return session.exec(select(func.count()).select_from(Job).where(*conds)).one()

    from recrute.review import queue_conditions

    return {
        "queue": count(*queue_conditions()),
        "packets": count(Job.status == JobStatus.PACKET_READY),
        "needs_human": count(Job.status == JobStatus.NEEDS_HUMAN),
    }
