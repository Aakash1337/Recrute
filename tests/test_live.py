import pytest

HX = {"HX-Request": "true"}


def test_live_queue_roundtrip(paths):
    from recrute import live

    live.enqueue(paths, {"type": "click", "x": "10", "y": "20"})
    live.enqueue(paths, {"type": "type", "text": "hello"})
    live.enqueue(paths, {"type": "key", "key": "Enter"})
    live.enqueue(paths, {"type": "done"})
    with pytest.raises(ValueError):
        live.enqueue(paths, {"type": "key", "key": "Meta+Q"})

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
    assert r.status_code == 200
    assert client.post("/live/input", data={"type": "click", "x": "1", "y": "2"}).status_code \
        == 403  # CSRF: cross-site forms can't drive the browser
    r = client.post("/live/open", data={"url": "javascript:alert(1)"}, headers=HX)
    assert r.status_code == 422
