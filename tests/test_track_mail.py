import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from recrute.track import mail
from recrute.track.mail import (
    ImapConfig,
    ImapError,
    ImapInbox,
    fetch_messages,
    imap_date,
    parse_message,
    set_imap_password,
)

FIX = Path(__file__).parent / "fixtures" / "track"


def raw(name: str) -> bytes:
    return (FIX / name).read_bytes()


class FakeKeyring:
    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service, key, value):
        self.store[(service, key)] = value

    def get_password(self, service, key):
        return self.store.get((service, key))


@pytest.fixture
def fake_keyring(monkeypatch):
    kr = FakeKeyring()
    monkeypatch.setattr(mail, "keyring", kr)
    return kr


class FakeIMAP:
    """Minimal imaplib.IMAP4 stand-in that records every command it gets."""

    def __init__(self, messages: dict[int, bytes], *, password="app-pw", uidvalidity=7):
        self.messages = messages
        self.password = password
        self.uidvalidity = uidvalidity
        self.calls: list[tuple] = []

    def login(self, user, pw):
        self.calls.append(("login", user))
        if pw != self.password:
            return "NO", [b"auth failed"]
        return "OK", [b"logged in"]

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        return ("OK", [str(self.uidvalidity).encode()]) if code == "UIDVALIDITY" else ("OK", [None])

    def uid(self, command, *args):
        self.calls.append(("uid", command, *args))
        if command == "SEARCH":
            crit = " ".join(args)
            uids = sorted(self.messages)
            m = re.search(r"UID (\d+):\*", crit)
            if m:
                lo = int(m.group(1))
                # real servers: "n:*" includes the highest UID even when it's < n
                uids = [u for u in uids if u >= lo] or uids[-1:]
            return "OK", [" ".join(map(str, uids)).encode()]
        if command == "FETCH":
            assert "BODY.PEEK[]" in args[1], "must never fetch with BODY[] (marks as read)"
            out = []
            for u in map(int, args[0].split(",")):
                meta = (f'{u} (UID {u} INTERNALDATE "05-Sep-2026 10:00:00 +0200" '
                        f"BODY[] {{{len(self.messages[u])}}}").encode()
                out += [(meta, self.messages[u]), b")"]
            return "OK", out
        raise AssertionError(f"unexpected UID command {command}")

    def logout(self):
        self.calls.append(("logout",))
        return "BYE", [b""]

    def __getattr__(self, name):  # store/expunge/copy/... must never be called
        raise AssertionError(f"forbidden IMAP call: {name}")


CFG = {"host": "imap.example.test", "user": "me@example.test"}


# --------------------------------------------------------------------------- parsing

def test_parse_multipart_with_encoded_subject():
    m = parse_message(raw("confirmation_multipart.eml"), uid=5)
    assert m.message_id == "<conf-001@us.greenhouse-mail.io>"
    assert m.subject == "Thank you for applying to Acme Security – SOC Analyst"
    assert m.sender == "no-reply@us.greenhouse-mail.io"
    assert m.sender_name == "Acme Security Hiring Team"
    assert m.sender_domain == "us.greenhouse-mail.io"
    assert m.date == datetime(2026, 9, 1, 18, 3, 12, tzinfo=UTC)
    assert m.date.tzinfo is not None
    assert "We’ve received your application" in m.text  # QP + utf-8 decoded
    assert m.html and "<b>SOC Analyst</b>" in m.html
    assert m.uid == 5


def test_parse_html_only_latin1():
    m = parse_message(raw("rejection_html_latin1.eml"))
    assert "decided to move forward with other candidates" in m.text
    assert "Café chats" in m.text
    assert "color:red" not in m.text  # <style> stripped
    assert "<p>" not in m.text


def test_parse_missing_date_and_id_unknown_charset():
    internal = datetime(2026, 9, 5, 8, 0, tzinfo=UTC)
    m = parse_message(raw("newsletter_no_date.eml"), internal_date=internal)
    assert m.date == internal
    assert m.message_id.endswith("@recrute.local>")
    assert "Big savings" in m.text
    assert "JVBER" not in m.text  # attachment ignored
    # stable synthetic id
    assert parse_message(raw("newsletter_no_date.eml"),
                         internal_date=internal).message_id == m.message_id


def test_parse_naive_date_becomes_utc():
    data = b"From: a@b.test\nSubject: hi\nDate: Tue, 01 Sep 2026 10:00:00\n\nbody\n"
    m = parse_message(data)
    assert m.date.tzinfo is not None


def test_imap_date_is_locale_independent():
    assert imap_date(date(2026, 3, 7)) == "07-Mar-2026"


# --------------------------------------------------------------------------- keyring

def test_set_and_get_password(fake_keyring):
    set_imap_password("me@example.test", "secret")
    assert fake_keyring.store[("recrute", "imap:me@example.test")] == "secret"
    assert mail.get_imap_password("me@example.test") == "secret"


# --------------------------------------------------------------------------- IMAP

def make_server():
    return FakeIMAP({
        3: raw("confirmation_multipart.eml"),
        4: raw("rejection_html_latin1.eml"),
        9: raw("newsletter_no_date.eml"),
    })


def test_fetch_all_readonly_and_peek(fake_keyring):
    set_imap_password(CFG["user"], "app-pw")
    server = make_server()
    msgs = list(fetch_messages(CFG, connect=lambda cfg: server))
    assert [m.uid for m in msgs] == [3, 4, 9]
    assert ("select", "INBOX", True) in server.calls  # EXAMINE (read-only)
    assert server.calls[-1] == ("logout",)
    assert msgs[2].date == datetime(2026, 9, 5, 8, 0, tzinfo=UTC)  # INTERNALDATE fallback


def test_fetch_after_uid_filters_star_quirk(fake_keyring):
    set_imap_password(CFG["user"], "app-pw")
    server = make_server()
    assert [m.uid for m in fetch_messages(CFG, after_uid=3, connect=lambda c: server)] == [4, 9]
    server = make_server()
    assert list(fetch_messages(CFG, after_uid=9, connect=lambda c: server)) == []


def test_fetch_since_builds_search(fake_keyring):
    server = make_server()
    list(fetch_messages(CFG, since=date(2026, 9, 1), password="app-pw",
                        connect=lambda c: server))
    search = next(c for c in server.calls if c[:2] == ("uid", "SEARCH"))
    assert search[2:] == ("SINCE", "01-Sep-2026")


def test_uidvalidity_exposed(fake_keyring):
    server = make_server()
    with ImapInbox(CFG, password="app-pw", connect=lambda c: server) as box:
        assert box.uidvalidity == 7


def test_missing_password_raises(fake_keyring):
    with pytest.raises(ImapError, match="keyring"):
        list(fetch_messages(CFG, connect=lambda c: make_server()))


def test_login_failure(fake_keyring):
    with pytest.raises(ImapError, match="login"):
        list(fetch_messages(CFG, password="wrong", connect=lambda c: make_server()))


def test_unparseable_message_is_skipped(fake_keyring, monkeypatch):
    server = make_server()
    real = mail.parse_message

    def flaky(data, **kw):
        if kw.get("uid") == 4:
            raise ValueError("boom")
        return real(data, **kw)

    monkeypatch.setattr(mail, "parse_message", flaky)
    msgs = list(fetch_messages(CFG, password="app-pw", connect=lambda c: server))
    assert [m.uid for m in msgs] == [3, 9]


def test_config_mapping_and_folder_quoting():
    cfg = ImapConfig.from_mapping({"host": "h", "user": "u", "port": "1993",
                                   "folder": "[Gmail]/All Mail"})
    assert cfg.port == 1993
    server = FakeIMAP({})
    with ImapInbox(cfg, password="app-pw", connect=lambda c: server):
        pass
    assert ("select", '"[Gmail]/All Mail"', True) in server.calls


def test_internaldate_parse():
    dt = mail._parse_internaldate(b'1 (UID 1 INTERNALDATE " 5-Sep-2026 10:00:00 -0500")')
    assert dt == datetime(2026, 9, 5, 15, 0, tzinfo=UTC)
    assert dt.utcoffset() == timedelta(hours=-5)
