"""Static configuration loaded from recrute.toml.

Runtime-adjustable knobs (applications/day, per-site caps) live in the DB instead; see settings.py.
"""

import tomllib
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from recrute.paths import Paths, get_paths

ProviderName = Literal["claude", "codex"]


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765
    # both set = HTTPS (recommended on a LAN: the live view carries what you type)
    ssl_certfile: str = ""
    ssl_keyfile: str = ""


class BrowserConfig(BaseModel):
    # "chrome" = installed Google Chrome (recommended: best fingerprint), "msedge", or ""
    # for the bundled Chromium that `patchright install chromium` downloads.
    channel: str = "chrome"
    headless: bool = False


class ProviderConfig(BaseModel):
    command: str
    model: str = ""  # empty = CLI default
    extra_args: list[str] = Field(default_factory=list)
    # claude only: cap on the agent loop (--max-turns) for requests that may use web tools
    web_max_turns: int = Field(default=14, ge=1)


class Route(BaseModel):
    provider: ProviderName
    model: str = ""  # overrides the provider's default model for this task

    @classmethod
    def parse(cls, spec: str) -> "Route":
        """"claude" or "claude:sonnet" -> Route."""
        provider, _, model = spec.partition(":")
        return cls.model_validate({"provider": provider, "model": model})


# task -> ordered list of "provider" or "provider:model"; later entries are fallbacks when
# earlier ones fail or hit a usage limit. A [llm.routing] table in recrute.toml is merged over
# these per task, so tasks it doesn't list keep their own default route.
DEFAULT_ROUTING: dict[str, list[str]] = {
    "default": ["claude:sonnet", "codex"],
    "triage": ["codex", "claude:haiku"],
    "tailor": ["claude:sonnet", "codex"],
    "verify": ["claude:sonnet", "codex"],
    "extract": ["claude:sonnet", "codex"],
    "extract_postings": ["codex", "claude:haiku"],
    "answers": ["claude:sonnet", "codex"],
    "classify_email": ["codex", "claude:haiku"],
    "followup": ["claude:sonnet", "codex"],
    "form_map": ["claude:sonnet", "codex"],
    "job_brief": ["claude:haiku", "codex"],
    "company_research": ["claude:sonnet", "codex"],
    "company_fit": ["claude:haiku", "codex"],
}


class LLMConfig(BaseModel):
    timeout_seconds: int = 300
    providers: dict[ProviderName, ProviderConfig] = Field(
        default_factory=lambda: {
            "claude": ProviderConfig(command="claude"),
            "codex": ProviderConfig(command="codex"),
        }
    )
    @field_validator("providers", mode="before")
    @classmethod
    def _merge_default_providers(cls, value):
        """Overriding one provider must not drop the other's defaults."""
        merged = {"claude": {"command": "claude"}, "codex": {"command": "codex"}}
        for name, cfg in (value or {}).items():
            base = merged.get(name, {})
            merged[name] = {**base, **(cfg.model_dump() if isinstance(cfg, BaseModel) else cfg)}
        return merged

    routing: dict[str, list[str]] = Field(
        default_factory=lambda: {task: list(specs) for task, specs in DEFAULT_ROUTING.items()}
    )

    @field_validator("routing", mode="before")
    @classmethod
    def _merge_default_routing(cls, value):
        """A user's [llm.routing] table overrides routes per task; it must not drop the
        defaults of tasks it doesn't mention (they would silently run on "default")."""
        if value is None:
            value = {}
        if not isinstance(value, dict):
            return value  # let pydantic report the type error
        return {**{task: list(specs) for task, specs in DEFAULT_ROUTING.items()}, **value}

    def route(self, task: str) -> list[Route]:
        specs = self.routing.get(task) or self.routing.get("default") or ["claude"]
        return [Route.parse(s) for s in specs]


class Config(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)


def load_config(paths: Paths | None = None) -> Config:
    paths = paths or get_paths()
    if not paths.config_file.exists():
        return Config()
    with paths.config_file.open("rb") as f:
        return Config.model_validate(tomllib.load(f))


@lru_cache
def get_config() -> Config:
    return load_config()
