"""The web UI in a real browser: what htmx actually renders, which TestClient can't show."""

import threading
import time

import pytest
from sqlmodel import Session


@pytest.fixture
def ui(client):  # (client: an initialized app on a temporary RECRUTE_HOME)
    import uvicorn
    from patchright.sync_api import sync_playwright

    from recrute.web.app import app

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning",
                                           lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        pw = sync_playwright().start()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"playwright unavailable: {e}")
    try:
        browser = pw.chromium.launch(headless=True)
    except Exception as e:  # noqa: BLE001
        pw.stop()
        pytest.skip(f"chromium not installed: {e}")
    page = browser.new_page()
    yield page, f"http://127.0.0.1:{port}"
    browser.close()
    pw.stop()
    server.should_exit = True
    thread.join(timeout=10)


def test_refused_decision_explains_itself_and_keeps_the_row(ui):
    """Audit: htmx drops 4xx bodies by default, so a refused action showed nothing; and the queue
    removed the row anyway, because the request's promise resolves on a refusal too."""
    from test_web import seed_job

    from recrute.db import get_engine
    from recrute.models import Job, JobStatus

    page, base = ui
    job_id = seed_job()
    page.goto(f"{base}/queue")
    page.wait_for_selector(f"#job-pane[data-job-id='{job_id}']")
    with Session(get_engine()) as s:  # decided elsewhere while this page was open
        job = s.get(Job, job_id)
        job.status = JobStatus.REJECTED
        s.add(job)
        s.commit()
    page.keyboard.press("a")
    page.wait_for_selector("#detail .bad")
    assert page.locator("#detail .bad").inner_text().strip()
    assert page.locator(f"#row-{job_id}").count() == 1


def test_accepted_decision_removes_the_row(ui):
    from test_web import seed_job

    page, base = ui
    job_id = seed_job()
    page.goto(f"{base}/queue")
    page.wait_for_selector(f"#job-pane[data-job-id='{job_id}']")
    page.keyboard.press("a")
    page.wait_for_selector(f"#row-{job_id}", state="detached")
    assert "approved" in page.locator("#detail").inner_text()


def test_invalid_setting_shows_its_message(ui):
    page, base = ui
    page.goto(f"{base}/live")
    form = page.locator("form[hx-post='/live/open']")
    form.locator("input[name=url]").fill("not a url")
    form.locator("button").click()
    page.wait_for_selector(".bad")
