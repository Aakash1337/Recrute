"""Filesystem layout. Everything is relative to RECRUTE_HOME (default: current directory).

All paths go through pathlib so the same code runs on Linux and Windows.
"""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    home: Path

    @property
    def config_file(self) -> Path:
        return self.home / "recrute.toml"

    @property
    def data(self) -> Path:
        return self.home / "data"

    @property
    def db_file(self) -> Path:
        return self.data / "recrute.db"

    @property
    def browser_profile(self) -> Path:
        return self.data / "browser-profile"

    @property
    def receipts(self) -> Path:
        return self.data / "receipts"

    @property
    def llm_workdir(self) -> Path:
        # Neutral cwd for LLM CLIs so they don't pick up project instructions (CLAUDE.md etc.)
        return self.data / "llm-workdir"

    @property
    def resources(self) -> Path:
        return self.home / "resources"

    def ensure(self) -> None:
        for d in (self.data, self.browser_profile, self.receipts, self.llm_workdir,
                  self.resources / "resume", self.resources / "cover_letters",
                  self.resources / "writing"):
            d.mkdir(parents=True, exist_ok=True)


def get_paths() -> Paths:
    return Paths(Path(os.environ.get("RECRUTE_HOME", Path.cwd())).resolve())
