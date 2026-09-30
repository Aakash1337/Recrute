from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from recrute import __version__
from recrute.db import init_db, session_scope
from recrute.paths import get_paths
from recrute.web.common import HERE
from recrute.web.security import AccessMiddleware, get_or_create_token


@lru_cache
def access_token() -> str:
    with session_scope() as s:
        return get_or_create_token(s)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    get_paths().ensure()
    init_db()
    access_token()
    yield


app = FastAPI(title="Recrute", version=__version__, lifespan=lifespan, docs_url=None,
              redoc_url=None, openapi_url=None)
app.add_middleware(AccessMiddleware, token_provider=access_token)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

from recrute.web import views  # noqa: E402  (routes register on import)

app.include_router(views.router)
