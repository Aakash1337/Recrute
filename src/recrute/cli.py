import json
import shutil
from pathlib import Path
from typing import Annotated

import typer

from recrute.config import get_config
from recrute.db import init_db, session_scope
from recrute.doctor import run_checks
from recrute.paths import get_paths
from recrute.settings import all_settings, set_setting

app = typer.Typer(help="Recrute: human-in-the-loop job discovery and applications.",
                  no_args_is_help=True)
browser_app = typer.Typer(help="Dedicated browser profile.", no_args_is_help=True)
llm_app = typer.Typer(help="LLM providers (subscription CLIs).", no_args_is_help=True)
app.add_typer(browser_app, name="browser")
app.add_typer(llm_app, name="llm")

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@app.command()
def init() -> None:
    """Create data/resources folders, recrute.toml and the database."""
    paths = get_paths()
    paths.ensure()
    if not paths.config_file.exists():
        example = PROJECT_ROOT / "config.example.toml"
        if example.exists():
            shutil.copy(example, paths.config_file)
            typer.echo(f"created {paths.config_file}")
    init_db()
    typer.echo(f"database ready at {paths.db_file}")
    typer.echo("next: `recrute doctor`, then `recrute browser login`")


@app.command()
def doctor() -> None:
    """Check that everything needed is installed and configured."""
    checks = run_checks(get_config(), get_paths())
    for c in checks:
        mark = "✔" if c.ok else ("✖" if c.required else "!")
        typer.echo(f"{mark} {c.name:26} {c.detail}")
    if any(c.required and not c.ok for c in checks):
        raise typer.Exit(1)


@app.command()
def serve(host: str | None = None, port: int | None = None) -> None:
    """Run the web UI."""
    import uvicorn

    cfg = get_config().server
    uvicorn.run("recrute.web.app:app", host=host or cfg.host, port=port or cfg.port)


@app.command("settings")
def show_settings() -> None:
    """Show runtime settings."""
    init_db()
    with session_scope() as s:
        typer.echo(json.dumps(all_settings(s), indent=2))


@app.command("set")
def set_cmd(key: str, value: str) -> None:
    """Set a runtime setting, e.g. `recrute set apps_per_day 25`.
    JSON values are accepted: `recrute set site_caps '{"linkedin_easy_apply": 10}'`."""
    init_db()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = value
    with session_scope() as s:
        try:
            set_setting(s, key, parsed)
        except (KeyError, ValueError, TypeError) as e:
            typer.echo(f"error: {e}", err=True)
            raise typer.Exit(1) from e
    typer.echo(f"{key} = {json.dumps(parsed)}")


@browser_app.command("login")
def browser_login(urls: Annotated[list[str] | None, typer.Argument()] = None) -> None:
    """Open the dedicated profile so you can log into sites. Close the window when done."""
    from recrute.browser.runtime import open_context

    urls = urls or ["https://www.linkedin.com/login", "https://accounts.google.com/"]
    with open_context(get_config().browser, get_paths(), headless=False) as ctx:
        first = ctx.pages[0] if ctx.pages else ctx.new_page()
        first.goto(urls[0])
        for url in urls[1:]:
            ctx.new_page().goto(url)
        typer.echo("Log in to your accounts, then close the browser window.")
        ctx.wait_for_event("close", timeout=0)
    typer.echo("sessions saved to the dedicated profile")


@browser_app.command("probe")
def browser_probe(headless: bool = False) -> None:
    """Visit a bot-detection page and report what websites can see."""
    from recrute.browser.probe import probe
    from recrute.browser.runtime import open_context

    paths = get_paths()
    with open_context(get_config().browser, paths, headless=headless) as ctx:
        report = probe(ctx, paths.data / "probe")
    typer.echo(json.dumps(report, indent=2))
    if report["suspicious"]:
        raise typer.Exit(2)


@llm_app.command("test")
def llm_test(provider: str | None = None) -> None:
    """Round-trip a tiny structured prompt through each provider (or one)."""
    from recrute.llm import LLMError, LLMRequest, build_providers

    paths = get_paths()
    providers = build_providers(get_config(), paths)
    names = [provider] if provider else list(providers)
    schema = {"type": "object", "properties": {"answer": {"type": "integer"}},
              "required": ["answer"], "additionalProperties": False}
    failed = False
    for name in names:
        p = providers[name]
        if not p.available():
            typer.echo(f"! {name}: not installed")
            continue
        try:
            r = p.complete(LLMRequest(prompt="What is 17 + 25? Reply with the answer only.",
                                      schema=schema))
            ok = r.output == {"answer": 42}
            failed |= not ok
            typer.echo(f"{'✔' if ok else '✖'} {name}: {r.output} ({r.duration_ms} ms)")
        except LLMError as e:
            failed = True
            typer.echo(f"✖ {name}: {e}")
    if failed:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
