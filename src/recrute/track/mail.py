"""Read-only IMAP inbox reader (stdlib imaplib over SSL).

Works with Gmail (IMAP + app password) and any IMAP server. Safety properties:
- The folder is opened with EXAMINE (read-only) and bodies are fetched with BODY.PEEK[], so
  nothing is ever marked as read.
- No STORE / EXPUNGE / COPY / MOVE commands are ever issued: nothing is modified or deleted.

The password is read from the OS keyring (service "recrute", key "imap:<user>"); set it once with
`set_imap_password(user, pw)` (Windows Credential Manager / Linux Secret Service / macOS
Keychain).
"""

import hashlib
import imaplib
import logging
import re
import ssl
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from email import message_from_bytes, policy
from email.message import EmailMessage, Message
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

import keyring

from recrute.capture.htmltext import html_to_text

log = logging.getLogger(__name__)

KEYRING_SERVICE = "recrute"
FETCH_BATCH = 25
MAX_TEXT_CHARS = 200_000
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# --------------------------------------------------------------------------- credentials


def imap_keyring_key(user: str) -> str:
    return f"imap:{user}"


def set_imap_password(user: str, password: str) -> None:
    keyring.set_password(KEYRING_SERVICE, imap_keyring_key(user), password)


def get_imap_password(user: str) -> str | None:
    return keyring.get_password(KEYRING_SERVICE, imap_keyring_key(user))


# --------------------------------------------------------------------------- data


@dataclass
class ImapConfig:
    host: str
    user: str
    port: int = 993
    folder: str = "INBOX"

    @classmethod
    def from_mapping(cls, cfg: "ImapConfig | Mapping[str, Any]") -> "ImapConfig":
        if isinstance(cfg, ImapConfig):
            return cfg
        return cls(host=cfg["host"], user=cfg["user"], port=int(cfg.get("port", 993)),
                   folder=cfg.get("folder", "INBOX"))


@dataclass
class MailMessage:
    message_id: str
    date: datetime  # tz-aware
    sender: str  # bare address, lowercased ("no-reply@us.greenhouse-mail.io")
    subject: str
    text: str  # plain text (text/plain part, or the HTML part converted to text)
    sender_name: str = ""
    html: str | None = None  # raw HTML part if any (alert parsing needs the links)
    uid: int | None = None

    @property
    def sender_domain(self) -> str:
        return self.sender.rpartition("@")[2].lower()


# --------------------------------------------------------------------------- parsing


def _part_text(part: Message) -> str:
    try:
        content = part.get_content()  # type: ignore[attr-defined]
        if isinstance(content, bytes):
            raise LookupError
        return content
    except (LookupError, KeyError, UnicodeError, AssertionError):
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:  # unknown charset name
            return payload.decode("utf-8", errors="replace")


def _bodies(msg: EmailMessage) -> tuple[str | None, str | None]:
    plain: str | None = None
    html: str | None = None
    for part in msg.walk():
        if part.is_multipart():
            continue
        if part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain" and plain is None:
            plain = _part_text(part)
        elif ctype == "text/html" and html is None:
            html = _part_text(part)
    return plain, html


def _parse_date(value: str | None, fallback: datetime | None) -> datetime:
    dt: datetime | None = None
    if value:
        try:
            dt = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            dt = None
    if dt is None:
        dt = fallback or datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def parse_message(raw: bytes, *, uid: int | None = None,
                  internal_date: datetime | None = None) -> MailMessage:
    msg = message_from_bytes(raw, policy=policy.default)
    assert isinstance(msg, EmailMessage)

    def header(name: str) -> str:
        try:
            v = msg.get(name)
        except Exception:  # malformed encoded-word etc.
            v = None
        return re.sub(r"\s+", " ", str(v)).strip() if v is not None else ""

    addrs = getaddresses([header("From")])
    sender_name, sender = addrs[0] if addrs else ("", "")
    subject = header("Subject")
    date_dt = _parse_date(header("Date") or None, internal_date)
    plain, html = _bodies(msg)
    text = plain if plain and plain.strip() else (html_to_text(html) if html else "")
    message_id = header("Message-ID")
    if not message_id:
        digest = hashlib.sha256(
            f"{date_dt.isoformat()}|{sender}|{subject}|{text[:500]}".encode()).hexdigest()[:32]
        message_id = f"<{digest}@recrute.local>"
    return MailMessage(message_id=message_id, date=date_dt, sender=sender.lower(),
                       subject=subject, text=text[:MAX_TEXT_CHARS], sender_name=sender_name,
                       html=html, uid=uid)


# --------------------------------------------------------------------------- IMAP


def imap_date(d: date | datetime) -> str:
    """IMAP SEARCH date (English month names regardless of OS locale)."""
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year:04d}"


_INTERNALDATE_RE = re.compile(
    rb'INTERNALDATE "\s?(\d{1,2})-([A-Za-z]{3})-(\d{4}) '
    rb'(\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2})"')
_UID_RE = re.compile(rb"\bUID (\d+)")


def _parse_internaldate(meta: bytes) -> datetime | None:
    m = _INTERNALDATE_RE.search(meta)
    if not m:
        return None
    day, mon, year, hh, mm, ss, sign, oh, om = m.groups()
    try:
        month = _MONTHS.index(mon.decode().title()) + 1
    except ValueError:
        return None
    try:
        offset = timedelta(hours=int(oh), minutes=int(om)) * (-1 if sign == b"-" else 1)
        return datetime(int(year), month, int(day), int(hh), int(mm), int(ss),
                        tzinfo=timezone(offset))
    except (ValueError, OverflowError):  # an impossible date: the Date header is used instead
        return None


class ImapError(RuntimeError):
    pass


def _default_connect(cfg: ImapConfig) -> imaplib.IMAP4:
    return imaplib.IMAP4_SSL(cfg.host, cfg.port, ssl_context=ssl.create_default_context(),
                             timeout=60)


class ImapInbox:
    """Context manager: `with ImapInbox(cfg) as box: for m in box.fetch_new(after_uid=...)`."""

    def __init__(self, config: "ImapConfig | Mapping[str, Any]", *, password: str | None = None,
                 connect: Callable[[ImapConfig], Any] | None = None):
        self.config = ImapConfig.from_mapping(config)
        self._password = password
        self._connect = connect or _default_connect
        # UIDs that could not be read this sync: the caller keeps its cursor before them
        self.failed_uids: list[int] = []
        self.conn: Any = None
        self.uidvalidity: int | None = None

    def __enter__(self) -> "ImapInbox":
        pw = self._password if self._password is not None else get_imap_password(
            self.config.user)
        if not pw:
            raise ImapError(f"no IMAP password in keyring for {self.config.user!r}; "
                            "set one with set_imap_password()")
        self.conn = self._connect(self.config)
        try:
            try:
                # imaplib raises IMAP4.error on NO/BAD for LOGIN (never returns "NO")
                typ, _ = self.conn.login(self.config.user, pw)
            except imaplib.IMAP4.error as e:
                raise ImapError(f"IMAP login failed: {e}") from None
            if typ != "OK":
                raise ImapError("IMAP login failed")
            # readonly=True -> EXAMINE: the server never sets \Seen on this session.
            try:
                typ, data = self.conn.select(_quote_mailbox(self.config.folder), readonly=True)
            except imaplib.IMAP4.error as e:
                raise ImapError(f"cannot open folder {self.config.folder!r}: {e}") from None
            if typ != "OK":
                raise ImapError(f"cannot open folder {self.config.folder!r}: {data!r}")
            self.uidvalidity = self._read_uidvalidity()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        if self.conn is not None:
            try:
                self.conn.logout()
            except Exception:  # best effort
                pass
            self.conn = None

    def _read_uidvalidity(self) -> int | None:
        # imaplib: response("UIDVALIDITY") -> ("UIDVALIDITY", [b"123"]) after SELECT/EXAMINE,
        # or ("UIDVALIDITY", [None]) if the server didn't send it.
        try:
            code, data = self.conn.response("UIDVALIDITY")
        except Exception:
            return None
        if str(code).upper() != "UIDVALIDITY" or not data or data[-1] is None:
            return None
        try:
            return int(data[-1])
        except (TypeError, ValueError):
            return None

    def search_uids(self, *, since: date | datetime | None = None,
                    after_uid: int | None = None) -> list[int]:
        criteria: list[str] = []
        if after_uid is not None:
            criteria.append(f"UID {after_uid + 1}:*")
        if since is not None:
            criteria.append(f"SINCE {imap_date(since)}")
        if not criteria:
            criteria.append("ALL")
        typ, data = self.conn.uid("SEARCH", *" ".join(criteria).split())
        if typ != "OK":
            raise ImapError(f"UID SEARCH failed: {data!r}")
        uids = sorted({int(x) for x in b" ".join(d for d in data if d).split()})
        if after_uid is not None:  # "n:*" always includes the highest UID, even if < n
            uids = [u for u in uids if u > after_uid]
        return uids

    def fetch(self, uids: list[int], *, batch: int = FETCH_BATCH) -> Iterator[MailMessage]:
        for i in range(0, len(uids), batch):
            chunk = uids[i:i + batch]
            typ, data = self.conn.uid("FETCH", ",".join(map(str, chunk)),
                                      "(UID INTERNALDATE BODY.PEEK[])")
            if typ != "OK":
                raise ImapError(f"UID FETCH failed: {data!r}")
            for item in data:
                if not isinstance(item, tuple) or len(item) < 2:
                    continue
                meta, raw = item[0], item[1]
                m = _UID_RE.search(meta)
                uid = int(m.group(1)) if m else None
                try:
                    msg = parse_message(raw, uid=uid, internal_date=_parse_internaldate(meta))
                except Exception as e:  # one bad message mustn't stop the sync...
                    log.warning("could not read message uid=%s: %s", uid, e.__class__.__name__)
                    if uid is not None:  # ...but it's retried, not skipped for good
                        self.failed_uids.append(uid)
                    continue
                yield msg

    def fetch_new(self, *, since: date | datetime | None = None,
                  after_uid: int | None = None) -> Iterator[MailMessage]:
        yield from self.fetch(self.search_uids(since=since, after_uid=after_uid))


def _quote_mailbox(name: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_.\-/]+", name):
        return name
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def fetch_messages(config: "ImapConfig | Mapping[str, Any]", *,
                   since: date | datetime | None = None, after_uid: int | None = None,
                   password: str | None = None,
                   connect: Callable[[ImapConfig], Any] | None = None) -> Iterator[MailMessage]:
    """Yield messages since a date and/or after the last seen UID (read-only).

    Callers persisting `after_uid` should also persist `ImapInbox.uidvalidity` and restart from
    a date when it changes (UIDs are only meaningful within one UIDVALIDITY).
    """
    with ImapInbox(config, password=password, connect=connect) as box:
        yield from box.fetch_new(since=since, after_uid=after_uid)
