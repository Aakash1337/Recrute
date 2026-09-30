import json

import httpx
import pytest
from pydantic import ValidationError

from recrute.notify import NotifyConfig, backends, send, set_smtp_password, set_telegram_token


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def set_password(self, service, key, value):
        self.store[(service, key)] = value

    def get_password(self, service, key):
        return self.store.get((service, key))


@pytest.fixture
def kr(monkeypatch):
    k = FakeKeyring()
    monkeypatch.setattr(backends, "keyring", k)
    return k


class Recorder:
    def __init__(self, status=200, body=None):
        self.requests: list[httpx.Request] = []
        self.status = status
        self.body = body if body is not None else {"ok": True}

    def client(self):
        def handler(req: httpx.Request):
            self.requests.append(req)
            return httpx.Response(self.status, json=self.body)
        return httpx.Client(transport=httpx.MockTransport(handler))


def test_ui_backend_is_noop(kr):
    assert [(r.backend, r.ok) for r in send("t", "b", config={"backends": ["ui"]})] == \
        [("ui", True)]


def test_ntfy_json_publish(kr):
    rec = Recorder()
    cfg = NotifyConfig(backends=["ntfy"], ntfy_url="https://ntfy.example/recrute-abc")
    res = send("Nouvel emploi ✓", "body", config=cfg, priority="high",
               click_url="http://laptop:8765/jobs/1", client=rec.client())
    assert res[0].ok
    req = rec.requests[0]
    assert str(req.url) == "https://ntfy.example/"
    payload = json.loads(req.content)
    assert payload == {"topic": "recrute-abc", "title": "Nouvel emploi ✓", "message": "body",
                       "priority": 4, "click": "http://laptop:8765/jobs/1"}
    assert "authorization" not in req.headers


def test_ntfy_token_from_keyring(kr):
    backends.set_ntfy_token("https://ntfy.example/topic", "tk_123")
    rec = Recorder()
    send("t", "b", config={"backends": ["ntfy"], "ntfy_url": "https://ntfy.example/topic"},
         client=rec.client())
    assert rec.requests[0].headers["authorization"] == "Bearer tk_123"


def test_ntfy_missing_topic(kr):
    res = send("t", "b", config={"backends": ["ntfy"], "ntfy_url": "https://ntfy.example/"})
    assert not res[0].ok and "topic" in res[0].error


def test_telegram(kr):
    set_telegram_token("123:ABC")
    rec = Recorder()
    cfg = {"backends": ["telegram"], "telegram_chat_id": "42"}
    res = send("Title", "x" * 5000, config=cfg, client=rec.client())
    assert res[0].ok
    req = rec.requests[0]
    assert str(req.url) == "https://api.telegram.org/bot123:ABC/sendMessage"
    body = json.loads(req.content)
    assert body["chat_id"] == "42" and len(body["text"]) == 4096
    assert body["text"].startswith("Title\n\n")


def test_telegram_errors_do_not_leak_token(kr):
    set_telegram_token("123:SECRET")
    rec = Recorder(status=401, body={"ok": False, "description": "Unauthorized"})
    res = send("t", "b", config={"backends": ["telegram"], "telegram_chat_id": "1"},
               client=rec.client())
    assert not res[0].ok and "SECRET" not in res[0].error

    def boom(req):
        raise httpx.ConnectError("failed for https://api.telegram.org/bot123:SECRET/x")

    client = httpx.Client(transport=httpx.MockTransport(boom))
    res = send("t", "b", config={"backends": ["telegram"], "telegram_chat_id": "1"},
               client=client)
    assert not res[0].ok and "SECRET" not in res[0].error


def test_telegram_missing_token(kr):
    res = send("t", "b", config={"backends": ["telegram"], "telegram_chat_id": "1"})
    assert not res[0].ok and "keyring" in res[0].error


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, cfg):
        self.cfg = cfg
        self.calls = []
        self.sent = []
        FakeSMTP.instances.append(self)

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, pw):
        self.calls.append(("login", user, pw))

    def send_message(self, msg):
        self.sent.append(msg)

    def quit(self):
        self.calls.append("quit")


def test_email_backend(kr):
    set_smtp_password("me@example.test", "pw")
    cfg = NotifyConfig(backends=["email"], smtp_host="smtp.example.test",
                       smtp_user="me@example.test", smtp_to=["me@example.test"])
    FakeSMTP.instances.clear()
    res = send("Digest", "Hello", config=cfg, smtp_factory=FakeSMTP)
    assert res[0].ok, res[0].error
    smtp = FakeSMTP.instances[0]
    assert smtp.calls == ["starttls", ("login", "me@example.test", "pw"), "quit"]
    msg = smtp.sent[0]
    assert msg["Subject"] == "Digest" and msg["To"] == "me@example.test"
    assert msg.get_content().strip() == "Hello"


def test_email_missing_password(kr):
    cfg = NotifyConfig(backends=["email"], smtp_host="h", smtp_user="u", smtp_to=["x@y"])
    res = send("t", "b", config=cfg, smtp_factory=FakeSMTP)
    assert not res[0].ok and "keyring" in res[0].error


def test_one_failure_does_not_block_others(kr):
    rec = Recorder()
    cfg = {"backends": ["telegram", "ntfy", "ui"], "ntfy_url": "https://ntfy.example/t"}
    res = send("t", "b", config=cfg, client=rec.client())
    assert [(r.backend, r.ok) for r in res] == [("telegram", False), ("ntfy", True),
                                               ("ui", True)]


def test_unknown_backend_rejected_by_config():
    with pytest.raises(ValidationError):
        NotifyConfig(backends=["bogus"])
