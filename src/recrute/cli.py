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
profile_app = typer.Typer(help="Your mega resume -> structured profile.", no_args_is_help=True)
badges_app = typer.Typer(help="Visa badge data (informational only).", no_args_is_help=True)
inbox_app = typer.Typer(help="Inbox tracking (IMAP, read-only).", no_args_is_help=True)
notify_app = typer.Typer(help="Notifications.", no_args_is_help=True)
company_app = typer.Typer(help="Company registry.", no_args_is_help=True)
app.add_typer(browser_app, name="browser")
app.add_typer(llm_app, name="llm")
app.add_typer(profile_app, name="profile")
app.add_typer(badges_app, name="badges")
app.add_typer(inbox_app, name="inbox")
app.add_typer(notify_app, name="notify")
app.add_typer(company_app, name="company")


def _router():
    from sqlmodel import Session

    from recrute.db import get_engine
    from recrute.llm.router import LLMRouter, build_providers

    cfg, paths = get_config(), get_paths()
    return LLMRouter(cfg, build_providers(cfg, paths), lambda: Session(get_engine()))

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


def _setup_logging() -> None:
    import logging

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@app.command()
def serve(host: str | None = None, port: int | None = None,
          worker: Annotated[bool, typer.Option(help="also run the background worker")] = False
          ) -> None:
    """Run the web UI (optionally with the background worker in the same process)."""
    import uvicorn

    _setup_logging()
    init_db()
    if worker:
        from recrute import worker as w

        w.start()
    cfg = get_config().server
    uvicorn.run("recrute.web.app:app", host=host or cfg.host, port=port or cfg.port)


@app.command("worker")
def worker_cmd() -> None:
    """Run the background worker (discovery, triage, packets, drip submissions, inbox)."""
    from recrute import worker as w

    _setup_logging()
    init_db()
    w.run_forever()


@app.command("run")
def run_task_cmd(task: str) -> None:
    """Run one worker task now, e.g. `recrute run discover_boards` / `filter` / `score`."""
    from recrute import worker as w

    _setup_logging()
    init_db()
    tasks = {t.name: t for t in w.default_tasks()}
    if task not in tasks:
        typer.echo(f"unknown task; choose from: {', '.join(tasks)}", err=True)
        raise typer.Exit(1)
    stats = w.run_task(w.build_ctx(), tasks[task])
    typer.echo(json.dumps(stats, indent=2, default=str))
    from recrute.models import TaskRun

    with session_scope() as s:
        run = s.get(TaskRun, task)
        if run is not None and run.last_ok is False:
            typer.echo(f"task failed: {run.last_error}", err=True)
            raise typer.Exit(1)


@app.command()
def token() -> None:
    """Print the access token (LAN login + browser extension)."""
    from recrute.web.security import get_or_create_token

    init_db()
    with session_scope() as s:
        typer.echo(get_or_create_token(s))


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


@profile_app.command("ingest")
def profile_ingest() -> None:
    """Structure resources/resume/* into data/profile.proposed.yaml for review."""
    from recrute.tailor import ingest_resume

    init_db()
    result = ingest_resume(get_paths(), _router(), apply=False)
    typer.echo(f"proposal written from {len(result.sources)} file(s)")
    for f in result.flags:
        typer.echo(f"  [{f.severity}] {f.where}: {f.text} ({f.reason})")
    if result.diff:
        typer.echo(result.diff)
    typer.echo("review it, then: recrute profile accept  (or use the Profile page)")


@profile_app.command("accept")
def profile_accept(force: bool = typer.Option(False, help="accept despite blocking flags")
                   ) -> None:
    """Promote data/profile.proposed.yaml to data/profile.yaml (previous kept as .bak)."""
    from recrute.tailor import BlockingFlagsError, accept_proposed

    try:
        accept_proposed(get_paths(), allow_blocking=force)
    except BlockingFlagsError as e:
        for f in e.flags:
            typer.echo(f"  [{f.severity}] {f.where}: {f.text} ({f.reason})", err=True)
        typer.echo("blocking flags: fix the proposal or pass --force", err=True)
        raise typer.Exit(1) from e
    typer.echo("profile updated")


def _import_badges(kind: str, files: list[Path]) -> None:
    from recrute.badges import EVerifyIndex, H1BIndex, update_company_badges

    init_db()
    out = get_paths().data / "badges"
    out.mkdir(parents=True, exist_ok=True)
    if kind == "h1b":
        index = H1BIndex.from_csv(*files)
        index.save(out / "h1b.json")
        kwargs = {"h1b": index}
    else:
        index = EVerifyIndex.from_csv(*files)
        index.save(out / "everify.json")
        kwargs = {"everify": index}
    from recrute.tasks import refresh_job_badges

    with session_scope() as s:
        n = update_company_badges(s, **kwargs)
        s.commit()
        jobs = refresh_job_badges(s)
    typer.echo(f"imported; {n} companies and {jobs} jobs updated.")


@badges_app.command("import-h1b")
def badges_import_h1b(files: list[Path]) -> None:
    """Import USCIS H-1B Employer Data Hub CSV export(s)."""
    _import_badges("h1b", files)


@badges_app.command("import-everify")
def badges_import_everify(files: list[Path]) -> None:
    """Import an E-Verify employer search CSV export."""
    _import_badges("everify", files)


@inbox_app.command("set-password")
def inbox_set_password(user: str) -> None:
    """Store your IMAP (app) password in the OS keyring. Then enable IMAP in Settings."""
    from recrute.track.mail import set_imap_password

    set_imap_password(user, typer.prompt("IMAP / app password", hide_input=True))
    typer.echo("stored in the OS keyring")


@notify_app.command("set-secret")
def notify_set_secret(kind: str, user: str = typer.Option("", help="SMTP user")) -> None:
    """Store a notification secret in the OS keyring: telegram | smtp | ntfy."""
    from recrute import notify

    secret = typer.prompt(f"{kind} secret", hide_input=True)
    if kind == "telegram":
        notify.set_telegram_token(secret)
    elif kind == "smtp":
        notify.set_smtp_password(user, secret)
    elif kind == "ntfy":
        from recrute.settings import get_setting

        init_db()
        with session_scope() as s:
            url = get_setting(s, "notify")["ntfy_url"]
        if not url:
            typer.echo("set notify.ntfy_url in Settings first", err=True)
            raise typer.Exit(1)
        notify.set_ntfy_token(url, secret)
    else:
        typer.echo("kind must be telegram, smtp or ntfy", err=True)
        raise typer.Exit(1)
    typer.echo("stored in the OS keyring")


@notify_app.command("test")
def notify_test() -> None:
    """Send a test notification through the configured backend."""
    from recrute.tasks import notify

    init_db()
    with session_scope() as s:
        results = notify(s, "Recrute test", "Notifications work.")
    for r in results:
        typer.echo(f"{'✔' if r.ok else '✖'} {r.backend} {r.error or ''}")
    if any(not r.ok for r in results):
        raise typer.Exit(1)


@company_app.command("add")
def company_add(url: str, name: str | None = None) -> None:
    """Add a company by any job/board URL on a supported ATS."""
    from recrute.registry import add_company_from_url

    init_db()
    with session_scope() as s:
        try:
            c = add_company_from_url(s, url, name)
        except ValueError as e:
            typer.echo(f"error: {e}", err=True)
            raise typer.Exit(1) from e
        typer.echo(f"{c.name}: {c.ats}:{c.ats_token}")


if __name__ == "__main__":
    app()
