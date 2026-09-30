"""Tolerant CSV reading for government data exports (encoding/delimiter/header variants)."""

import csv
import io
import re
from collections.abc import Iterator
from pathlib import Path


def decode_bytes(data: bytes) -> str:
    """USCIS exports have shipped as UTF-8, UTF-8-BOM, UTF-16 (tab separated) and cp1252."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    # UTF-16 without BOM: every other byte is NUL in the ASCII header row.
    head = data[:200]
    if head and head.count(b"\x00") > len(head) // 4:
        return data.decode("utf-16-le" if head[1:2] == b"\x00" else "utf-16-be",
                           errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def norm_header(h: str | None) -> str:
    return re.sub(r"\s+", " ", (h or "").replace("﻿", "")).strip().lower()


def read_rows(source: str | Path | bytes) -> tuple[list[str], Iterator[dict[str, str]]]:
    """Return (normalized headers, iterator of {normalized header: raw value})."""
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    text = decode_bytes(data)
    first = text.split("\n", 1)[0]
    delim = max(",\t;|", key=first.count) if first else ","
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delim)
    try:
        raw_headers = next(reader)
    except StopIteration:
        return [], iter(())
    headers = [norm_header(h) for h in raw_headers]

    def rows() -> Iterator[dict[str, str]]:
        for row in reader:
            if not any(c.strip() for c in row):
                continue
            yield {h: (row[i].strip() if i < len(row) else "") for i, h in enumerate(headers)}

    return headers, rows()


def to_int(v: str | None) -> int:
    if not v:
        return 0
    v = v.replace(",", "").replace(" ", "").strip()
    try:
        return int(float(v))
    except ValueError:
        return 0


def find_col(headers: list[str], *candidates: str, contains: tuple[str, ...] = (),
             exclude: tuple[str, ...] = ()) -> str | None:
    """First header equal to a candidate, else first header containing all `contains` words
    and none of `exclude`."""
    for c in candidates:
        if c in headers:
            return c
    if contains:
        for h in headers:
            if all(w in h for w in contains) and not any(x in h for x in exclude):
                return h
    return None
