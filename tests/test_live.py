import pytest

HX = {"HX-Request": "true"}


def test_live_queue_roundtrip(paths):
    from recrute import live

    sid = live.start_session(paths)
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

    # even an event file written for the old session (race) is dropped on replay
    (live.live_dir(paths) / "inputs" / "0-stale.json").write_text(
        f'{{"type": "type", "text": "secret", "session": "{old}"}}', encoding="utf-8")
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
