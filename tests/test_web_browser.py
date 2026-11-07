"""The web UI in a real browser: what htmx actually renders, which TestClient can't show."""

import threading
import time

import pytest
from sqlmodel import Session, select


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


def test_applications_page_says_when_it_is_empty(ui):
    page, base = ui
    page.goto(f"{base}/applications")
    assert "No applications yet" in page.locator("main").inner_text()


def test_long_form_options_do_not_widen_the_packet_page(ui):
    """A live packet's long option text pushed the page past the window and squeezed the labels."""
    from test_integration_web import _seed_packet

    from recrute.db import get_engine
    from recrute.models import Application
    from recrute.schemas import FormAnswer, FormQuestion, Packet

    page, base = ui
    job_id = _seed_packet()
    long_option = "I am not a U.S. person, and I am not a current citizen. " * 8
    with Session(get_engine()) as s:
        app = s.exec(select(Application).where(Application.job_id == job_id)).one()
        packet = Packet.model_validate(app.packet)
        packet.questions += [
            FormQuestion(id="x", label="Which applies?", type="select", options=[long_option]),
            FormQuestion(id="cover", label="Cover Letter", type="file")]
        packet.answers += [FormAnswer(question_id="cover", value=None, source="default")]
        app.packet = packet.model_dump(mode="json")
        s.add(app)
        s.commit()
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{base}/packets/{job_id}")
    page.wait_for_selector("table.answers")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    answers = page.locator("table.answers").inner_text()
    assert "not attached" in answers and "None" not in answers
