from pathlib import Path

from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup, escape
from sqlmodel import Session, func, select

from recrute.models import Job, JobStatus
from recrute.web import humanize
from recrute.web.humanize import ago, friendly_error, money  # noqa: F401  (re-exported)

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")

# html=False: raw HTML in job descriptions is escaped (they're untrusted third-party content);
# markdown-it also refuses javascript:/data: links.
_md = MarkdownIt("commonmark", {"html": False, "linkify": False})


def render_md(text: str | None) -> Markup:
    return Markup(_md.render(text or ""))


def safe_url(url: str | None) -> str:
    """Only http(s) links from scraped data are rendered (blocks javascript:/data: URLs)."""
    url = (url or "").strip()
    return url if url.lower().startswith(("http://", "https://")) else "#"


templates.env.filters["md"] = render_md
templates.env.filters["safe_url"] = safe_url
# the humanize layer: labels for internal values, task/error sentences, money, dates, numbers
templates.env.filters.update(humanize.FILTERS)
# ...and as functions, for calls with several arguments: {{ money(lo, hi, currency) }}
templates.env.globals.update(humanize.FILTERS)
templates.env.globals.update(TASK_ORDER=humanize.TASK_ORDER,
                             STATUS_LABELS=humanize.STATUS_LABELS)


def nav_counts(session: Session) -> dict[str, int]:
    def count(*conds) -> int:
        return session.exec(select(func.count()).select_from(Job).where(*conds)).one()

    from recrute.review import queue_conditions

    counts = {
        "queue": count(*queue_conditions()),
        "packets": count(Job.status == JobStatus.PACKET_READY),
        "needs_human": count(Job.status == JobStatus.NEEDS_HUMAN),
        "live": 0,
    }
    try:  # a browser hand-off waiting for you (in-memory: cheap)
        from recrute import live

        info = live.frame_info()
        counts["live"] = int(bool(info and info.get("fresh")))
    except Exception:  # (the nav must render even if the live view is unavailable)
        pass
    return counts


def page(request, name: str, ctx: dict, session=None):
    """A full page: `nav` (the counts in the top bar) is added when a session is given."""
    if session is not None:
        ctx.setdefault("nav", nav_counts(session))
    return templates.TemplateResponse(request, name, ctx)


# ------------------------------------------------------------------------------ htmx messages
# Small HTML snippets returned by htmx actions. Successes are role=status, errors role=alert;
# errors are worded by humanize.friendly_error, with the raw text in a collapsed disclosure.


def msg_html(text: str, ok: bool = True) -> str:
    if ok:
        return f'<span class="msg msg-ok ok" role="status">{escape(text)}</span>'
    return error_html(text)


def error_html(error, title: str | None = None) -> str:
    """An error snippet. `title` overrides the friendly title (e.g. "Saved, but not approved");
    the friendly title then leads the detail."""
    fe = friendly_error(error) or {"title": None, "detail": "Something went wrong.",
                                   "technical": None}
    if title:
        head = fe["title"]
        fe = {**fe, "title": title,
              "detail": " ".join(p for p in (f"{head}." if head else "", fe["detail"]) if p)}
    return friendly_html(fe)


def friendly_html(friendly: dict) -> str:
    """The alert snippet for a friendly_error dict (the same markup as _ui.html's
    error_block, for responses built in Python)."""
    parts = ['<span class="msg msg-error bad" role="alert">']
    if friendly.get("title"):
        parts.append(f'<strong class="msg-title">{escape(friendly["title"])}</strong> ')
    parts.append(f'<span class="msg-detail">{escape(friendly.get("detail") or "")}</span>')
    if friendly.get("technical"):
        parts.append('<details class="tech"><summary>Technical details</summary>'
                     f'<code>{escape(friendly["technical"])}</code></details>')
    parts.append("</span>")
    return "".join(parts)


def msg_response(text: str, ok: bool = True, status: int = 200) -> HTMLResponse:
    return HTMLResponse(msg_html(text, ok), status_code=status)


def error_response(error, status: int = 409, title: str | None = None) -> HTMLResponse:
    return HTMLResponse(error_html(error, title=title), status_code=status)
