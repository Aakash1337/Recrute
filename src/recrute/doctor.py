"""Environment checks shared by `recrute doctor` and the web dashboard."""

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from recrute.config import Config
from recrute.paths import Paths


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def _version(command: str) -> str | None:
    path = shutil.which(command)
    if not path:
        return None
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20)
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return "installed (version unknown)"


def find_chrome() -> str | None:
    if sys.platform == "win32":
        candidates = [
            Path(os.environ.get(var, "")) / "Google/Chrome/Application/chrome.exe"
            for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
        ]
        return next((str(c) for c in candidates if c.exists()), None)
    if sys.platform == "darwin":
        mac = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        return str(mac) if mac.exists() else None
    return next(filter(None, (shutil.which(n) for n in ("google-chrome-stable",
                                                        "google-chrome"))), None)


def find_edge() -> str | None:
    if sys.platform == "win32":
        candidates = [
            Path(os.environ.get(var, "")) / "Microsoft/Edge/Application/msedge.exe"
            for var in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")
        ]
        return next((str(c) for c in candidates if c.exists()), None)
    if sys.platform == "darwin":
        mac = Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
        return str(mac) if mac.exists() else None
    return next(filter(None, (shutil.which(n) for n in ("microsoft-edge-stable",
                                                        "microsoft-edge"))), None)


def expected_chromium_revision() -> str | None:
    """The Chromium build the installed patchright version requires."""
    try:
        import patchright

        manifest = Path(patchright.__file__).parent / "driver/package/browsers.json"
        browsers = json.loads(manifest.read_text(encoding="utf-8"))["browsers"]
        return next(b["revision"] for b in browsers if b["name"] == "chromium")
    except (OSError, KeyError, StopIteration, ValueError):
        return None


def bundled_chromium_dir() -> Path | None:
    """The installed bundled Chromium matching patchright's required revision, if any."""
    if env := os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        root = Path(env)
    elif sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    elif sys.platform == "darwin":
        root = Path.home() / "Library/Caches/ms-playwright"
    else:
        root = Path.home() / ".cache/ms-playwright"
    revision = expected_chromium_revision()
    if revision is None:
        return None
    path = root / f"chromium-{revision}"
    return path if path.exists() else None


def run_checks(config: Config, paths: Paths) -> list[Check]:
    checks = [
        Check("config", paths.config_file.exists(),
              str(paths.config_file) if paths.config_file.exists()
              else "no recrute.toml, using defaults (run `recrute init`)", required=False),
        Check("database", paths.db_file.exists(),
              str(paths.db_file) if paths.db_file.exists() else "missing (run `recrute init`)"),
    ]
    from recrute.llm.base import ProviderUnavailableError
    from recrute.llm.router import build_providers

    providers = build_providers(config, paths)
    for name, prov in config.llm.providers.items():
        v = _version(prov.command)
        if v is None:
            checks.append(Check(f"llm:{name}", False, f"{prov.command!r} not on PATH",
                                required=False))
            continue
        try:
            providers[name].ensure_subscription()
            checks.append(Check(f"llm:{name}", True, f"{v}, subscription auth",
                                required=False))
        except ProviderUnavailableError as e:
            checks.append(Check(f"llm:{name}", False, f"{v}: {e}", required=False))
    if not any(c.ok for c in checks if c.name.startswith("llm:")):
        checks.append(Check("llm", False, "no LLM CLI available (need claude or codex)"))

    chrome = find_chrome()
    edge = find_edge()
    chromium = bundled_chromium_dir()
    channel = config.browser.channel
    checks.append(Check("browser:chrome", chrome is not None,
                        chrome or "Google Chrome not found (recommended for best fingerprint)",
                        required=False))
    if channel == "msedge":
        checks.append(Check("browser:edge", edge is not None, edge or "Microsoft Edge not found",
                            required=False))
    checks.append(Check("browser:bundled-chromium", chromium is not None,
                        str(chromium) if chromium
                        else f"revision {expected_chromium_revision()} not installed "
                        "(run `uv run patchright install chromium`)",
                        required=False))
    usable = ((channel == "chrome" and chrome is not None)
              or (channel == "msedge" and edge is not None) or chromium is not None)
    checks.append(Check("browser", usable,
                        "ok" if usable else "no usable browser: install Chrome or bundled "
                        "Chromium"))

    resume_files = [p for p in (paths.resources / "resume").glob("*") if p.is_file()
                    and p.name != ".gitkeep"] if (paths.resources / "resume").exists() else []
    checks.append(Check("resources:resume", bool(resume_files),
                        f"{len(resume_files)} file(s)" if resume_files
                        else "empty (add your mega resume before M3)", required=False))
    return checks
