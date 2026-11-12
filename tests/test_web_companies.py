"""Companies: a paged, searchable list and a page per employer. (Made-up companies only.)"""

from html import unescape

from sqlmodel import Session
from test_web import HX


def _seed(n=3):
    from recrute.db import get_engine
    from recrute.models import Company, Job, JobStatus, Priority

    with Session(get_engine()) as s:
        ids = []
        for i in range(n):
            c = Company(name=f"Acme {i:02d}", ats="greenhouse", ats_token=f"acme{i}",
                        active=(i != 1), poll_error="HTTPError: 503" if i == 2 else None)
            s.add(c)
            s.flush()
            ids.append(c.id)
        s.add(Job(company_id=ids[0], title="Security Engineer", apply_url="https://x/1",
                  canonical_url="https://x/1", status=JobStatus.DISCOVERED,
                  priority=Priority.P1, score=80, badges={}))
        s.add(Job(company_id=ids[0], title="Senior Director", apply_url="https://x/2",
                  canonical_url="https://x/2", status=JobStatus.FILTERED_OUT,
                  filter_reason="title excluded: senior", badges={}))
        s.commit()
        return ids


def test_the_list_is_readable_and_filters(client):
    ids = _seed()
    html = unescape(client.get("/companies").text)
    assert "Showing 1–3 of 3" in html and "Greenhouse ↗" in html
    assert "greenhouse:acme0" not in html and ">seed<" not in html  # (no raw board ids)
    assert "Couldn't reach it" in html and "HTTPError" not in html.split("<table")[1].split(
        "title=")[0]
    only = unescape(client.get("/companies?show=matches").text)
    assert "Acme 00" in only and "Acme 01" not in only
    paused = unescape(client.get("/companies?show=paused").text)
    assert "Acme 01" in paused and "Acme 00" not in paused
    assert "No companies match" in unescape(client.get("/companies?q=zzz").text)
    assert f'/companies/{ids[0]}"' in html


def test_the_list_is_paged(client):
    _seed(n=60)
    first = unescape(client.get("/companies").text)
    assert "Showing 1–50 of 60" in first and "Page 1 of 2" in first and "Acme 59" not in first
    second = unescape(client.get("/companies?p=2").text)
    assert "Showing 51–60 of 60" in second and "Acme 59" in second
    assert "Page 2 of 2" in unescape(client.get("/companies?p=99").text)  # (clamped)


def test_a_companys_page_lists_its_jobs(client):
    ids = _seed()
    html = unescape(client.get(f"/companies/{ids[0]}").text)
    assert "1 job for you right now" in html and "Security Engineer" in html
    assert "Set aside (1)" in html and "Senior Director" in html
    assert "Open their job board" in html and "job-boards.greenhouse.io/acme0" in html
    empty = unescape(client.get(f"/companies/{ids[1]}").text)
    assert "No jobs for you right now" in empty and "Paused" in empty
    assert client.get("/companies/99999").status_code == 404


def test_pausing_relabels_the_button_and_adding_needs_a_link(client):
    ids = _seed()
    assert client.post(f"/companies/{ids[0]}/toggle", headers=HX).text == "Resume checking"
    assert client.post(f"/companies/{ids[0]}/toggle", headers=HX).text == "Pause"
    r = client.post("/companies/add", data={"url": " "}, headers=HX)
    assert r.status_code == 422 and "Paste a link" in r.text
