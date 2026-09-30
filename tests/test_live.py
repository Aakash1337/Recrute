import time

import pytest

HX = {"HX-Request": "true"}


class FakePage:
    """A tab: screenshots, mouse and keyboard, recording what was replayed on it."""

    def __init__(self, url="https://x"):
        self.url = url
        self.viewport_size = {"width": 800, "height": 600}
        self.calls = []
        page = self

        class Mouse:
            def click(self, x, y):
                page.calls.append(("click", x, y))

            def wheel(self, dx, dy):
                page.calls.append(("wheel", dy))

        class Keyboard:
            def type(self, text, delay=0):
                page.calls.append(("type", text))

            def press(self, key):
                page.calls.append(("press", key))

        self.mouse, self.keyboard = Mouse(), Keyboard()

    def screenshot(self, **kw):
        return b"\xff\xd8jpg"


def fresh_frame(paths, page=None):
    """What the worker publishes every second while a hand-off is live; returns the tab."""
    from recrute import live

    page = page or FakePage()
    live.publish_frame(paths, page)
    return page


def ev(page, **kw):
    from recrute import live

    return {"session": live.active_session(), "target": live.page_target(page), **kw}


def test_live_queue_roundtrip(paths):
    from recrute import live

    live.start_session(paths)
    page = fresh_frame(paths)
    live.enqueue(paths, ev(page, type="click", x="10", y="20"))
    live.enqueue(paths, ev(page, type="type", text="hello"))
    live.enqueue(paths, ev(page, type="key", key="Enter"))
    live.enqueue(paths, ev(page, type="done"))
    live.enqueue(paths, ev(page, type="type", text="after done"))
    with pytest.raises(ValueError):
        live.enqueue(paths, ev(page, type="key", key="Meta+Q"))
    assert live.apply_inputs(paths, page) is True
    assert page.calls == [("click", 10.0, 20.0), ("type", "hello"), ("press", "Enter")]
    live.clear(paths)


def test_input_never_lands_in_a_tab_you_have_not_seen(paths):
    from recrute import live

    live.start_session(paths)
    login = fresh_frame(paths, FakePage("https://login.example"))
    live.enqueue(paths, ev(login, type="type", text="CANARY-code"))
    popup = FakePage("https://popup.example")  # opened before the input was replayed
    assert live.apply_inputs(paths, popup) is False and popup.calls == []
    # the same tab after a navigation is a different target too
    live.enqueue(paths, ev(login, type="click", x=1, y=1))
    login.url = "https://login.example/next"
    assert live.apply_inputs(paths, login) is False and login.calls == []
    # and input made on the old picture is refused once the new one is published
    fresh_frame(paths, popup)
    with pytest.raises(ValueError, match="page changed"):
        live.enqueue(paths, {"session": live.active_session(), "target": "old", "type": "done"})
    live.clear(paths)


def test_live_open_request_only_http(paths):
    from recrute import live

    with pytest.raises(ValueError):
        live.request_open(paths, "file:///etc/passwd")
    live.request_open(paths, "https://www.linkedin.com/login")
    assert live.take_open_request(paths) == "https://www.linkedin.com/login"
    assert live.take_open_request(paths) is None


def test_live_routes(client):
    assert "Live browser" in client.get("/live").text
    assert client.get("/live/frame").status_code == 404
    assert "No browser" in client.get("/live/status").text
    r = client.post("/live/input", data={"type": "click", "x": "1", "y": "2"}, headers=HX)
    assert r.status_code == 422  # no live session: refused
    assert client.post("/live/input", data={"type": "click", "x": "1", "y": "2"}).status_code \
        == 403  # CSRF: cross-site forms can't drive the browser
    r = client.post("/live/open", data={"url": "javascript:alert(1)"}, headers=HX)
    assert r.status_code == 422


def test_delayed_input_never_crosses_handoffs(paths):
    from recrute import live

    old = live.start_session(paths)
    live.clear(paths)  # hand-off 1 ended
    new = live.start_session(paths)
    page = fresh_frame(paths)
    with pytest.raises(ValueError):
        live.enqueue(paths, {**ev(page, type="type", text="secret"), "session": old})
    # even an event queued for the old session (race) is dropped on replay
    live._QUEUES[new].append({**ev(page, type="type", text="secret"), "session": old,
                              "at": time.time()})
    assert live.apply_inputs(paths, page) is False and page.calls == []
    assert new != old
    live.clear(paths)


def test_screenshots_never_touch_the_disk(paths):
    from recrute import live

    live.start_session(paths)
    fresh_frame(paths)
    assert live.frame_jpeg() is not None
    assert not [f for f in paths.data.rglob("*") if f.is_file() and b"jpg" in f.read_bytes()]
    live.clear(paths)
    assert live.frame_jpeg() is None


def test_frames_only_for_active_session_and_carry_it(client):
    from recrute import live
    from recrute.paths import get_paths

    paths = get_paths()
    sid = live.start_session(paths)
    page = fresh_frame(paths)
    r = client.get("/live/frame")
    assert r.status_code == 200 and r.headers["X-Live-Session"] == sid
    assert r.headers["X-Live-Width"] == "800"
    assert r.headers["X-Live-Target"] == live.page_target(page)
    live.clear(paths)
    live.start_session(paths)  # a new hand-off, no frame of its own yet
    assert client.get("/live/frame").status_code == 404
    live.clear(paths)


@pytest.mark.browser
def test_live_page_revokes_superseded_frame_urls(client):
    import base64

    from patchright.sync_api import sync_playwright

    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8Dw"
                           "HwAFAAH/q842iQAAAABJRU5ErkJggg==")
    html = client.get("/live").text
    frames = {"n": 0}

    def handle(route):
        path = route.request.url.split("//", 1)[1].split("/", 1)[1].split("?")[0]
        if path == "live":
            return route.fulfill(body=html, content_type="text/html")
        if path == "live/frame":
            frames["n"] += 1
            return route.fulfill(body=png, content_type="image/png",
                                 headers={"X-Live-Session": "s1", "X-Live-Width": "1",
                                          "X-Live-Height": "1", "X-Live-Target": "t1"})
        if path.startswith("static/"):
            r = client.get("/" + path)
            return route.fulfill(body=r.content, content_type=r.headers.get("content-type"))
        return route.fulfill(body="", content_type="text/html")

    try:
        pw = sync_playwright().start()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"playwright unavailable: {e}")
    try:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"chromium not installed: {e}")
        page = browser.new_page()
        page.add_init_script("""(() => {
          window.__urls = {made: 0, revoked: 0};
          const c = URL.createObjectURL.bind(URL), r = URL.revokeObjectURL.bind(URL);
          URL.createObjectURL = b => { window.__urls.made++; return c(b); };
          URL.revokeObjectURL = u => { window.__urls.revoked++; return r(u); };
        })()""")
        page.route("http://recrute.test/**", handle)
        page.goto("http://recrute.test/live")
        page.wait_for_function("window.__urls.made >= 4", timeout=15000)
        counts = page.evaluate("window.__urls", isolated_context=False)
        assert counts["made"] - counts["revoked"] <= 1  # only the frame on screen is alive
        frame = page.evaluate("window.__frame", isolated_context=False)
        assert frame["session"] == "s1" and frame["target"] == "t1"
        browser.close()
    finally:
        pw.stop()


def test_typed_input_never_touches_the_disk(paths):
    from recrute import live

    live.start_session(paths)
    page = fresh_frame(paths)
    live.enqueue(paths, ev(page, type="type", text="CANARY-p4ssw0rd"))
    for f in paths.data.rglob("*"):
        if f.is_file():
            assert b"CANARY" not in f.read_bytes(), f
    assert live.apply_inputs(paths, page) is False
    assert page.calls == [("type", "CANARY-p4ssw0rd")]
    live.clear(paths)


def test_remote_input_without_in_process_worker_is_refused(paths):
    from recrute import live

    live.start_session(paths)
    page = fresh_frame(paths)
    live._QUEUES.clear()  # as seen from a web server whose worker is another process
    with pytest.raises(ValueError, match="serve --worker"):
        live.enqueue(paths, ev(page, type="click", x=1, y=1))
    live.clear(paths)


def test_input_refused_and_dropped_when_screenshots_stop(paths, monkeypatch):
    from recrute import live

    sid = live.start_session(paths)
    with pytest.raises(ValueError, match="not current"):  # no frame at all yet
        live.enqueue(paths, {"type": "click", "x": 1, "y": 1, "session": sid, "target": ""})
    page = fresh_frame(paths)
    live.enqueue(paths, ev(page, type="type", text="late"))
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 60)  # a minute without screenshots
    with pytest.raises(ValueError, match="not current"):
        live.enqueue(paths, ev(page, type="click", x=1, y=1))
    assert live.apply_inputs(paths, page) is False and page.calls == []
    live.clear(paths)


def test_expired_frame_is_not_served(client, monkeypatch):
    from recrute import live
    from recrute.paths import get_paths

    p = get_paths()
    live.start_session(p)
    fresh_frame(p)
    r = client.get("/live/frame")
    assert r.status_code == 200 and float(r.headers["X-Live-Age"]) < 5
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 60)
    assert client.get("/live/frame").status_code == 404
    live.clear(p)


def test_click_that_navigates_stops_the_rest_of_the_batch(paths, monkeypatch):
    from recrute import live

    monkeypatch.setattr(live, "CLICK_SETTLE", 0)
    live.start_session(paths)
    page = fresh_frame(paths, FakePage("https://login.example"))
    real_click = page.mouse.click

    def navigating_click(x, y):
        real_click(x, y)
        page.url = "https://elsewhere.example"  # the click followed a link

    page.mouse.click = navigating_click
    live.enqueue(paths, ev(page, type="click", x=5, y=5))
    live.enqueue(paths, ev(page, type="type", text="CANARY-password"))
    assert live.apply_inputs(paths, page) is False
    assert page.calls == [("click", 5.0, 5.0)]  # the password was never typed on the new page
    live.clear(paths)


def test_same_url_reload_is_a_new_target(paths):
    from recrute import live

    live.start_session(paths)
    page = fresh_frame(paths)
    live.enqueue(paths, ev(page, type="type", text="CANARY"))
    with live._LOCK:  # what the framenavigated listener records on a reload
        live._NAV[id(page)] = live._NAV.get(id(page), 0) + 1
    assert live.apply_inputs(paths, page) is False and page.calls == []
    live.clear(paths)

