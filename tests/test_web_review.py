"""The Review list (/queue: search, filters, sort, counts, rows) and the Filtered page
(/filtered: reasons in plain words, grouped, searchable, paged; moving a job back).
(Made-up companies and postings only.)"""

from datetime import timedelta
from html import unescape

from sqlmodel import Session
from test_web import HX, seed_job

from recrute.web import review_labels as rl

LIST = {**HX, "HX-Target": "list"}


def _text(r) -> str:
    return unescape(r.text)


def _seed_company_job(company: str, **kw) -> int:
    from recrute.db import get_engine
    from recrute.models import Company

    job_id = seed_job(**kw)
    with Session(get_engine()) as s:
        from recrute.models import Job

        c = Company(name=company)
        s.add(c)
        s.flush()
        job = s.get(Job, job_id)
        job.company_id = c.id
        s.add(job)
        s.commit()
    return job_id


def _add_source(job_id: int, source: str) -> None:
    from recrute.db import get_engine
    from recrute.models import JobSource

    with Session(get_engine()) as s:
        s.add(JobSource(job_id=job_id, source=source, url=f"https://x/{source}/{job_id}"))
        s.commit()


def _rows(r) -> list[str]:
    """Row ids in the order they appear."""
    import re

    return re.findall(r'id="row-(\d+)"', r.text)


# --------------------------------------------------------------------------- labels


def test_reasons_fall_into_plain_categories():
    cat = rl.filter_reason_category
    assert cat("score 43 below 75 for P3") == "low_score"
    assert cat("title excluded: sr.") == "senior"
    assert cat("LLM: senior-level role") == "senior"
    assert cat("requires 8+ years") == "senior"
    assert cat("title excluded: internship") == "job_type"
    assert cat("employment type: contract") == "job_type"
    assert cat("title excluded: sales") == "title_word"
    assert cat("location outside the US") == "location"
    assert cat("LLM: location not US-eligible") == "location"
    assert cat("security clearance required") == "clearance"
    assert cat("ITAR / US person required") == "clearance"
    assert cat("title matches no target track") == "not_target"
    assert cat("salary below floor") == "pay"
    assert cat("something new") == "other" and cat(None) == "other"
    assert rl.filter_category_label("senior") == "Too senior"
    assert rl.filter_category_label("made up") == "Other reasons"


def test_reason_sentence_and_fit_band_match_the_job_pane():
    assert "below your minimum of 75" in rl.filter_reason_label("score 43 below 75 for P3")
    assert rl.filter_reason_label(None) == "No reason was recorded."
    assert rl.fit_band(85) == "Strong match" and rl.fit_tone(85) == "ok"
    assert rl.fit_band(40) == "Weak match" and rl.fit_band(None) == ""
    assert rl.fit_tone("n/a") == "neutral"


def test_row_locations_put_us_places_first():
    from types import SimpleNamespace

    job = SimpleNamespace(locations=["Anguilla", "Remote", "United States", "Austin, TX"],
                          remote="remote")
    assert rl.row_locations(job) == ["United States", "Austin, TX", "Anguilla"]
    assert rl.row_locations(SimpleNamespace(locations=["Remote"], remote="remote")) == ["Remote"]
    assert rl.row_locations(SimpleNamespace(locations=None, remote=None)) == []


# --------------------------------------------------------------------------- the review list


def test_queue_explains_itself_and_shows_rows_in_plain_words(client):
    from recrute.models import Priority

    seed_job(title="Detection Engineer", remote="remote", salary_min=120000, salary_max=150000,
             locations=["Toronto, Canada", "Remote", "Austin, TX", "Denver, CO", "Boston, MA"],
             score=85,
             priority=Priority.P0, badges={"sponsorship": "will_sponsor", "h1b": 12,
                                           "e_verify": True, "cap_exempt": True})
    page = _text(client.get("/queue"))
    assert "Nothing is sent to an employer without your approval" in page
    assert "application packet" in page and "CP1" not in page
    assert "Detection Engineer" in page and "Acme" in page
    # US places lead, and "Remote" isn't repeated next to the Remote chip
    assert "Austin, TX · Denver, CO · +2 more" in page
    assert "$120k–$150k" in page
    assert "Strong match: fit score 85 out of 100" in page
    assert '<span class="pill">Remote</span>' in page
    for label in ("Sponsors visas", "H-1B approvals: 12", "E-Verify employer",
                  "Likely H-1B cap-exempt"):
        assert label in page
    assert "<b>1</b> job to review" in page
    assert "next / previous job" in page and "I'll apply myself" in page
    for raw in ("will_sponsor", "🛂", ">P0<", "None"):
        assert raw not in page


def test_queue_empty_states(client):
    page = _text(client.get("/queue"))
    assert "You're all caught up" in page and 'id="list"' in page and 'id="detail"' in page
    seed_job()
    page = _text(client.get("/queue?q=zebra"))
    assert "No jobs match these filters" in page and "Clear filters" in page
    assert "Showing <b>0</b> of 1 jobs" in page


def test_queue_search_and_filters(client):
    from recrute.models import Priority

    a = _seed_company_job("Globex", title="Cloud Security Engineer", remote="remote", score=90,
                          priority=Priority.P1, salary_min=140000, salary_max=160000)
    b = seed_job(title="SOC Analyst", remote="hybrid", score=62, priority=Priority.P2)
    c = seed_job(title="100% Remote_ Pentester", remote=None, score=55, priority=Priority.P1,
                 salary_min=50, salary_max=70)
    _add_source(a, "greenhouse")
    _add_source(b, "linkedin")

    def ids(query: str) -> set[int]:
        return {int(x) for x in _rows(client.get(f"/queue?{query}"))}

    assert ids("") == {a, b, c}
    assert ids("q=soc") == {b}
    assert ids("q=globex") == {a}  # the company name is searched too
    assert ids("q=100%25") == {c} and ids("q=remote_") == {c}  # % and _ are literal
    assert ids("track=P1") == {a, c} and ids("track=p2") == {b}
    assert ids("mode=remote") == {a} and ids("mode=hybrid") == {b} and ids("mode=unknown") == {c}
    assert ids("pay=1") == {a, c}
    assert ids("min_fit=60") == {a, b} and ids("min_fit=80") == {a}
    assert ids("source=greenhouse") == {a} and ids("source=linkedin") == {b}
    assert ids("q=engineer&mode=remote&pay=1&min_fit=80&source=greenhouse&track=P1") == {a}
    # stale or malformed values are ignored rather than an error
    assert ids("track=P9&mode=moon&min_fit=abc&sort=sideways&offset=-4") == {a, b, c}
    page = _text(client.get("/queue?mode=remote"))
    assert "Showing <b>1</b> of 3 jobs" in page and "Clear filters" in page
    assert "Remote (1)" in page and "Hybrid (1)" in page and "Pay listed (2)" in page
    assert "Greenhouse (1)" in page


def test_queue_sorts(client):
    from recrute.db import get_engine
    from recrute.models import Job, Priority, utcnow

    low = seed_job(title="Top track low fit", score=60, priority=Priority.P0,
                   salary_min=60, salary_max=80)  # an hourly rate: about $166k a year
    high = seed_job(title="Second track high fit", score=95, priority=Priority.P1,
                    salary_min=100000, salary_max=120000)
    new = seed_job(title="Newest no pay", score=70, priority=Priority.P2)
    with Session(get_engine()) as s:
        for job_id, days in ((low, 9), (high, 5), (new, 1)):
            job = s.get(Job, job_id)
            job.posted_at = utcnow() - timedelta(days=days)
            s.add(job)
        s.commit()

    def order(sort: str) -> list[int]:
        return [int(x) for x in _rows(client.get(f"/queue?sort={sort}"))]

    assert order("best") == [low, high, new]  # your tracks first, as before
    assert order("fit") == [high, new, low]
    assert order("newest") == [new, high, low]
    assert order("pay") == [low, high, new]  # no pay listed goes last
    best = _text(client.get("/queue"))
    assert best.count('class="group-head"') == 3 and "1 job</span>" in best
    assert 'class="group-head"' not in client.get("/queue?sort=fit").text


def test_queue_filter_request_returns_only_rows_and_keeps_the_pane_contract(client):
    job_id = seed_job()
    seed_job(title="Other Role", score=50)
    r = client.get("/queue?q=security&track=&mode=&min_fit=&sort=best", headers=LIST)
    body = _text(r)
    assert "<html" not in body and f'id="row-{job_id}"' in body and "Other Role" not in body
    assert r.headers["HX-Push-Url"] == "/queue?q=security"
    # the first row loads the job pane; the count line is refreshed alongside
    assert 'hx-trigger="click, load"' in body and 'hx-target="#detail"' in body
    assert 'id="queue-status"' in body and 'hx-swap-oob="true"' in body
    assert "Showing <b>1</b> of 2 jobs" in body
    none = _text(client.get("/queue?q=zebra", headers=LIST))
    assert "No jobs match these filters" in none and 'id="detail" hx-swap-oob="true"' in none
    # a normal load, or a restored history entry, gets the whole page
    assert "<html" in client.get("/queue?q=security").text
    assert "<html" in client.get(
        "/queue", headers={**LIST, "HX-History-Restore-Request": "true"}).text
    page = client.get("/queue").text
    for hook in ('id="list"', 'id="detail"', f'data-id="{job_id}"', "recruteDecide",
                 'id="queue-filters"', 'hx-target="#list"'):
        assert hook in page


def test_queue_shows_more_in_pages(client, monkeypatch):
    from recrute.web import views_review

    monkeypatch.setattr(views_review, "QUEUE_PAGE", 2)
    ids = [seed_job(title=f"Role {i}", score=90 - i) for i in range(5)]
    first = client.get("/queue")
    assert [int(x) for x in _rows(first)] == ids[:2]
    assert "Showing <b>2</b> of 5 jobs" in _text(first)
    assert 'hx-get="/queue?offset=2"' in first.text and "3 more" in first.text
    more = client.get("/queue?offset=2", headers={**HX, "HX-Target": "queue-more-2"})
    assert "<html" not in more.text and [int(x) for x in _rows(more)] == ids[2:4]
    assert "load" not in more.text.split('id="queue-status"')[0]  # only the first page autoloads
    assert 'class="group-head"' not in more.text  # same track as the row above: no new header
    assert "Showing <b>4</b> of 5 jobs" in _text(more)
    last = client.get("/queue?offset=4", headers={**HX, "HX-Target": "queue-more-4"})
    assert [int(x) for x in _rows(last)] == ids[4:] and "Show more jobs" not in last.text


def test_queue_mentions_snoozed_jobs_and_marks_unscored_ones(client):
    from recrute.db import get_engine
    from recrute.models import Job, JobStatus, utcnow

    snoozed = seed_job(title="Later Role")
    with Session(get_engine()) as s:
        job = s.get(Job, snoozed)
        job.snoozed_until = utcnow() + timedelta(days=3)
        s.add(job)
        s.commit()
    restored = seed_job(status=JobStatus.FILTERED_OUT, filter_reason="title excluded: senior",
                        title="Senior Role", score=None)
    client.post(f"/jobs/{restored}/restore", headers=HX)
    page = _text(client.get("/queue"))
    assert "1 snoozed job is hidden for now" in page and "Later Role" not in page
    assert "Senior Role" in page and "Not scored yet" in page
    assert "Filtered out (" not in page  # nothing is filtered any more


# --------------------------------------------------------------------------- filtered


def _filtered(title: str, reason: str | None, **kw) -> int:
    from recrute.models import JobStatus

    kw.setdefault("score", None)
    return seed_job(status=JobStatus.FILTERED_OUT, filter_reason=reason, title=title, **kw)


def test_filtered_page_groups_reasons_and_hides_off_target_titles_by_default(client):
    near = _filtered("Senior Security Engineer", "title excluded: senior")
    low = _filtered("Security Analyst", "score 43 below 75 for P3", score=43)
    _filtered("Account Executive", "title matches no target track")
    page = _text(client.get("/filtered"))
    assert "set aside for you automatically" in page and "move it back to Review" in page
    assert "Senior Security Engineer" in page and "Security Analyst" in page
    assert "Account Executive" not in page  # not a target role: one click away
    for chip in ("Near misses <b>2</b>", "Too senior <b>1</b>", "Low fit score <b>1</b>",
                 "Not a target role <b>1</b>", "Everything <b>3</b>"):
        assert chip in page
    assert "The title contains “senior”, which you chose to leave out." in page
    assert "Its fit score of 43 is below your minimum of 75" in page
    for raw in ("title excluded", "score 43 below", "filtered_out", "for P3"):
        assert raw not in page
    # near misses lead with the best fit; each job opens in Recrute, the posting is a side link
    assert page.index("Security Analyst") < page.index("Senior Security Engineer")
    assert f'<a href="/jobs/{near}"' in page and f'<a href="/jobs/{low}"' in page
    assert f'hx-post="/jobs/{near}/restore"' in page and "Move back to Review" in page
    assert "Showing <b>1–2</b> of 2 (3 filtered out in all)" in page
    only = _text(client.get("/filtered?reason=not_target"))
    assert "Account Executive" in only and "Security Analyst" not in only
    everything = _text(client.get("/filtered?reason=all"))
    assert all(t in everything for t in ("Account Executive", "Security Analyst"))
    assert 'aria-current="true"' in everything


def test_filtered_search_looks_through_everything(client):
    _filtered("Senior Security Engineer", "title excluded: senior")
    _filtered("Account Executive", "title matches no target track")
    page = _text(client.get("/filtered?q=account"))
    assert "Account Executive" in page and "Senior Security Engineer" not in page
    assert "of 1 matching “account”" in page and "Clear search" in page
    assert "Acme" in _text(client.get("/filtered?q=acme"))  # by company
    # the dashboard's "why jobs were filtered" rows link here with the stored reason
    by_reason = _text(client.get("/filtered?q=title%20excluded"))
    assert "Senior Security Engineer" in by_reason and "Account Executive" not in by_reason
    none = _text(client.get("/filtered?q=zebra"))
    assert "No filtered jobs match “zebra”" in none and "Nothing has been filtered" not in none
    scoped = _text(client.get("/filtered?q=account&reason=senior"))
    assert "No filtered jobs match “account” in Too senior" in scoped
    assert "Search everything" in scoped


def test_filtered_is_paged(client, monkeypatch):
    from recrute.web import views_review

    monkeypatch.setattr(views_review, "FILTERED_PAGE", 2)
    for i in range(5):
        _filtered(f"Senior Role {i}", "title excluded: senior")
    first = _text(client.get("/filtered"))
    assert "Showing <b>1–2</b> of 5" in first and "Page 1 of 3" in first
    assert 'href="/filtered?reason=near&sort=fit&page=2"' in client.get("/filtered").text.replace(
        "&amp;", "&")
    assert first.count('hx-post="/jobs/') == 2
    last = _text(client.get("/filtered?page=3"))
    assert "Showing <b>5–5</b> of 5" in last and last.count('hx-post="/jobs/') == 1
    assert "Page 3 of 3" in _text(client.get("/filtered?page=99"))  # past the end: the last page
    assert "Page 1 of 3" in _text(client.get("/filtered?page=abc"))


def test_filtered_empty_state(client):
    page = _text(client.get("/filtered"))
    assert "Nothing has been filtered out yet" in page


def test_restore_says_what_happened(client):
    from recrute.db import get_engine
    from recrute.models import Job, JobStatus

    job_id = _filtered("Senior Security Engineer", "title excluded: senior")
    r = client.post(f"/jobs/{job_id}/restore", headers=HX)
    assert r.status_code == 200 and "restored" in r.text and f'href="/jobs/{job_id}"' in r.text
    with Session(get_engine()) as s:
        assert s.get(Job, job_id).status == JobStatus.DISCOVERED
    again = client.post(f"/jobs/{job_id}/restore", headers=HX)  # a second click changes nothing
    assert again.status_code == 409 and "already in your review list" in _text(again)
    assert "restored" not in again.text
    gone = client.post("/jobs/987654/restore", headers=HX)
    assert gone.status_code == 404 and "isn't in Recrute any more" in _text(gone)
    assert "Senior Security Engineer" not in client.get("/filtered").text
