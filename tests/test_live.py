import pytest

HX = {"HX-Request": "true"}


def fresh_frame(paths, sid):
    """What the worker publishes every second while a hand-off is live."""
    import json
    import time

    from recrute import live

    (live.live_dir(paths) / "frame.json").write_text(json.dumps(
        {"url": "https://x", "width": 800, "height": 600, "at": time.time(), "session": sid}),
        encoding="utf-8")


def test_live_queue_roundtrip(paths):
    from recrute import live

    sid = live.start_session(paths)
    fresh_frame(paths, sid)
    live.enqueue(paths, {"type": "click", "x": "10", "y": "20", "session": sid})
    live.enqueue(paths, {"type": "type", "text": "hello", "session": sid})
    live.enqueue(paths, {"type": "key", "key": "Enter", "session": sid})
    live.enqueue(paths, {"type": "done", "session": sid})
    live.enqueue(paths, {"type": "type", "text": "after done", "session": sid})
    with pytest.raises(ValueError):
        live.enqueue(paths, {"type": "key", "key": "Meta+Q", "session": sid})

    class Mouse:
        def __init__(self):
            self.calls = []

        def click(self, x, y):
            self.calls.append(("click", x, y))

        def wheel(self, dx, dy):
            self.calls.append(("wheel", dy))

    class Keyboard:
        def __init__(self, log):
            self.log = log

        def type(self, text, delay=0):
            self.log.append(("type", text))

        def press(self, key):
            self.log.append(("press", key))

    class Page:
        def __init__(self):
            self.mouse = Mouse()
            self.keyboard = Keyboard(self.mouse.calls)

    page = Page()
    assert live.apply_inputs(paths, page) is True
    assert page.mouse.calls == [("click", 10.0, 20.0), ("type", "hello"), ("press", "Enter")]
    assert not list((live.live_dir(paths) / "inputs").glob("*.json"))


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
    with pytest.raises(ValueError):
        live.enqueue(paths, {"type": "type", "text": "secret", "session": old})

    class Page:
        class mouse:  # noqa: N801
            @staticmethod
            def click(x, y):
                raise AssertionError("must not click")

        class keyboard:  # noqa: N801
            @staticmethod
            def type(text, delay=0):
                raise AssertionError("must not type")

    # even an event queued for the old session (race) is dropped on replay
    live._QUEUES[new].append({"type": "type", "text": "secret", "session": old})
    assert live.apply_inputs(paths, Page()) is False
    assert new != old


def test_publish_frame_survives_sharing_violation(paths, monkeypatch):
    from recrute import live

    class Page:
        url = "https://x"
        viewport_size = {"width": 100, "height": 100}

        def screenshot(self, **kw):
            return b"jpg"

    def locked(path, data):
        raise PermissionError(32, "The process cannot access the file")

    monkeypatch.setattr(live, "_atomic_write", locked)
    live.publish_frame(paths, Page())  # must not raise
    live.clear(paths)


def test_frames_only_for_active_session_and_carry_it(client):
    from recrute import live
    from recrute.paths import get_paths

    paths = get_paths()
    sid = live.start_session(paths)

    class Page:
        url = "https://x"
        viewport_size = {"width": 800, "height": 600}

        def screenshot(self, **kw):
            return b"\\xff\\xd8jpg"

    live.publish_frame(paths, Page())
    r = client.get("/live/frame")
    assert r.status_code == 200 and r.headers["X-Live-Session"] == sid
    assert r.headers["X-Live-Width"] == "800"
    live.clear(paths)
    live.start_session(paths)  # a new hand-off, no frame of its own yet
    assert client.get("/live/frame").status_code == 404


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
                                          "X-Live-Height": "1"})
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
        assert page.evaluate("window.__frame && window.__frame.session",
                             isolated_context=False) == "s1"
        browser.close()
    finally:
        pw.stop()


def test_typed_input_never_touches_the_disk(paths):
    from recrute import live

    sid = live.start_session(paths)
    fresh_frame(paths, sid)
    live.enqueue(paths, {"type": "type", "text": "CANARY-p4ssw0rd", "session": sid})
    for f in paths.data.rglob("*"):
        if f.is_file():
            assert b"CANARY" not in f.read_bytes(), f

    typed = []

    class Page:
        class keyboard:  # noqa: N801
            @staticmethod
            def type(text, delay=0):
                typed.append(text)

    assert live.apply_inputs(paths, Page()) is False and typed == ["CANARY-p4ssw0rd"]
    live.clear(paths)


def test_remote_input_without_in_process_worker_is_refused(paths):
    from recrute import live

    sid = live.start_session(paths)
    fresh_frame(paths, sid)
    live._QUEUES.clear()  # as seen from a web server whose worker is another process
    with pytest.raises(ValueError, match="serve --worker"):
        live.enqueue(paths, {"type": "click", "x": 1, "y": 1, "session": sid})
    live.clear(paths)


def test_input_refused_and_dropped_when_screenshots_stop(paths, monkeypatch):
    import time

    from recrute import live

    sid = live.start_session(paths)
    with pytest.raises(ValueError, match="not current"):  # no frame at all yet
        live.enqueue(paths, {"type": "click", "x": 1, "y": 1, "session": sid})
    fresh_frame(paths, sid)
    live.enqueue(paths, {"type": "type", "text": "late", "session": sid})
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 60)  # a minute without screenshots
    with pytest.raises(ValueError, match="not current"):
        live.enqueue(paths, {"type": "click", "x": 1, "y": 1, "session": sid})

    class Page:
        class keyboard:  # noqa: N801
            @staticmethod
            def type(text, delay=0):
                raise AssertionError("a stale event must not be replayed")

    assert live.apply_inputs(paths, Page()) is False
    live.clear(paths)


def test_expired_frame_is_not_served(client, paths):
    import json

    from recrute import live
    from recrute.paths import get_paths

    p = get_paths()
    sid = live.start_session(p)
    (live.live_dir(p) / "frame.jpg").write_bytes(b"jpg")
    (live.live_dir(p) / "frame.json").write_text(json.dumps(
        {"url": "u", "width": 1, "height": 1, "at": 0, "session": sid}), encoding="utf-8")
    assert client.get("/live/frame").status_code == 404
    fresh_frame(p, sid)
    r = client.get("/live/frame")
    assert r.status_code == 200 and float(r.headers["X-Live-Age"]) < 5
    live.clear(p)
