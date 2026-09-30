from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import func, select

from recrute import __version__
from recrute.config import get_config
from recrute.db import init_db, session_scope
from recrute.doctor import run_checks
from recrute.models import Job, LLMCall
from recrute.paths import get_paths
from recrute.settings import APPS_PER_DAY_MAX, APPS_PER_DAY_MIN, all_settings, set_setting

HERE = Path(__file__).parent



@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    get_paths().ensure()
    init_db()
    yield


app = FastAPI(title="Recrute", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": __version__}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with session_scope() as s:
        stats = {
            "jobs": s.exec(select(func.count()).select_from(Job)).one(),
            "llm_calls": s.exec(select(func.count()).select_from(LLMCall)).one(),
        }
        settings = all_settings(s)
    checks = run_checks(get_config(), get_paths())
    return templates.TemplateResponse(request, "dashboard.html", {
        "stats": stats, "settings": settings, "checks": checks,
        "apps_min": APPS_PER_DAY_MIN, "apps_max": APPS_PER_DAY_MAX,
    })


@app.post("/settings/apps-per-day", response_class=HTMLResponse)
def update_apps_per_day(request: Request, value: Annotated[int, Form()]):
    error = None
    with session_scope() as s:
        try:
            set_setting(s, "apps_per_day", value)
        except ValueError as e:
            error = str(e)
        settings = all_settings(s)
    return templates.TemplateResponse(request, "_knob.html", {
        "settings": settings, "error": error, "saved": error is None,
        "apps_min": APPS_PER_DAY_MIN, "apps_max": APPS_PER_DAY_MAX,
    })
