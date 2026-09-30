from pathlib import Path

import pytest
from sqlmodel import Session, select

from recrute.badges import compute_badges, update_company_badges
from recrute.badges.cap_exempt import is_cap_exempt
from recrute.badges.everify import EVerifyIndex
from recrute.badges.h1b import H1BIndex, load_h1b_csv
from recrute.badges.names import name_variants, normalize_company
from recrute.models import Company

FIX = Path(__file__).parent / "fixtures" / "badges"

FY2024_TSV = "\t".join([
    "Line by line", "Fiscal Year   ", "Employer (Petitioner) Name", "Tax ID",
    "Industry (NAICS) Code", "Petitioner City", "Petitioner State", "Petitioner Zip Code",
    "New Employment Approval", "New Employment Denial", "Continuation Approval",
    "Continuation Denial", "Change with Same Employer Approval",
    "Change with Same Employer Denial", "New Concurrent Approval", "New Concurrent Denial",
    "Change of Employer Approval", "Change of Employer Denial", "Amended Approval",
    "Amended Denial"]) + "\n" + "\n".join([
        "1\t2024\tACME SECURITY INC\t1234\t54 - Professional\tSAN JOSE\tCA\t95110"
        "\t5\t0\t10\t0\t3\t0\t0\t0\t2\t1\t1\t0",
        "2\t2024\tNEURAL WIDGETS LLC\t2222\t54 - Professional\tSEATTLE\tWA\t98101"
        "\t4\t1\t0\t0\t0\t0\t0\t0\t1\t0\t0\t0",
    ]) + "\n"


# --------------------------------------------------------------------------- names

@pytest.mark.parametrize("raw,expected", [
    ("Acme Widgets, Inc.", "acme widgets"),
    ("ACME WIDGETS INC", "acme widgets"),
    ("Acme Widgets Incorporated", "acme widgets"),
    ("Foo L.L.C.", "foo"),
    ("Bar Corp.", "bar"),
    ("The Johns Hopkins University", "johns hopkins university"),
    ("AT&T Services, Inc.", "at and t services"),
    ("Société Générale S.A.", "societe generale"),
    ("Acme Co., Ltd.", "acme"),
    ("Co", "co"),
    ("Big Holdings LLC DBA Small Brand", "big holdings"),
])
def test_normalize_company(raw, expected):
    assert normalize_company(raw) == expected


def test_name_variants_include_dba():
    assert name_variants("Big Holdings LLC DBA Small Brand") == ["big holdings", "small brand"]


# --------------------------------------------------------------------------- H-1B

def test_h1b_legacy_layout_aggregates_recent_years():
    idx = load_h1b_csv(FIX / "h1b_legacy.csv")
    m = idx.match_company("Acme Security, Inc.")
    assert m is not None and m.score == 100
    # latest FY 2023 -> window 2021..2023, all approval columns, both tax IDs, "1,005" parsed
    assert m.recent_approvals == (10 + 20) + (12 + 25) + (3 + 2) + (1005 + 40)
    # OLDCO's only year (2019) is outside the recent window
    assert idx.recent_approvals("Oldco") == 0
    assert idx.recent_approvals("Johns Hopkins University") == 90


def test_h1b_new_utf16_tab_layout(tmp_path):
    p = tmp_path / "fy2024.csv"
    p.write_bytes(FY2024_TSV.encode("utf-16"))
    idx = H1BIndex.from_csv(p)
    assert idx.recent_approvals("Acme Security") == 5 + 10 + 3 + 0 + 2 + 1
    assert idx.recent_approvals("Neural Widgets, LLC") == 5


def test_h1b_multiple_files_and_window(tmp_path):
    p = tmp_path / "fy2024.tsv"
    p.write_text(FY2024_TSV, encoding="utf-8")
    idx = H1BIndex.from_csv(FIX / "h1b_legacy.csv", p, recent_years=2)
    # window 2023..2024
    assert idx.recent_approvals("ACME SECURITY INC") == (1005 + 40) + 21


def test_h1b_fuzzy_and_miss():
    idx = load_h1b_csv(FIX / "h1b_legacy.csv")
    assert idx.match_company("Acme Securty Inc") is not None  # typo still matches
    assert idx.match_company("Acme") is None  # too different: conservative
    assert idx.recent_approvals("Totally Unknown Startup") == 0
    assert H1BIndex().recent_approvals("Acme") is None  # no data loaded


def test_h1b_save_load_roundtrip(tmp_path):
    idx = load_h1b_csv(FIX / "h1b_legacy.csv")
    idx.save(tmp_path / "h1b.json")
    again = H1BIndex.load(tmp_path / "h1b.json")
    assert again.recent_approvals("Acme Security") == idx.recent_approvals("Acme Security")


def test_h1b_bad_columns():
    with pytest.raises(ValueError):
        H1BIndex.from_csv(b"foo,bar\n1,2\n")


def test_h1b_cp1252_bytes():
    data = "Fiscal Year,Employer,Initial Approval\n2023,CAF\xc9 LABS INC,3\n".encode("cp1252")
    idx = H1BIndex.from_csv(data)
    assert idx.recent_approvals("Café Labs") == 3


# --------------------------------------------------------------------------- E-Verify

def test_everify_lookup():
    idx = EVerifyIndex.from_csv(FIX / "everify.csv")
    assert idx.lookup("Acme Security") is True
    assert idx.lookup("Acme Cyber") is True  # DBA name
    assert idx.lookup("Neural Widgets") is True
    assert idx.lookup("Gone Away Corp") is False  # terminated account
    assert idx.lookup("Nobody Inc") is False
    assert EVerifyIndex().lookup("Acme") is None


def test_everify_roundtrip(tmp_path):
    idx = EVerifyIndex.from_csv(FIX / "everify.csv")
    idx.save(tmp_path / "ev.json")
    assert EVerifyIndex.load(tmp_path / "ev.json").names == idx.names


def test_everify_termination_date_column():
    data = b"Company Name,Termination Date\nLive Co Inc,\nDead Co Inc,2020-01-01\n"
    idx = EVerifyIndex.from_csv(data)
    assert idx.lookup("Live Co") is True
    assert idx.lookup("Dead Co") is False


# --------------------------------------------------------------------------- cap-exempt

@pytest.mark.parametrize("name,domain,expected", [
    ("Stanford University", None, True),
    ("Acme", "cs.stanford.edu", True),
    ("Anything", "https://www.mit.edu/careers", True),
    ("Oxford", "ox.ac.uk", True),
    ("Montgomery College", None, True),
    ("Research Foundation for SUNY", None, True),
    ("Lawrence Livermore National Laboratory", None, True),
    ("Jet Propulsion Laboratory", None, True),
    ("Broad Institute", None, True),
    ("Abbott Laboratories", None, None),
    ("Acme Security LLC", "acme.com", False),
    ("Institute Labs LLC", None, None),
    ("Acme", "acme.io", None),
    (None, None, None),
])
def test_cap_exempt(name, domain, expected):
    assert is_cap_exempt(name, domain) is expected


# --------------------------------------------------------------------------- combined

def test_compute_badges():
    h1b = load_h1b_csv(FIX / "h1b_legacy.csv")
    ev = EVerifyIndex.from_csv(FIX / "everify.csv")
    b = compute_badges("We are unable to sponsor visas.", company_name="Acme Security Inc",
                       h1b=h1b, everify=ev)
    assert b["sponsorship"] == "no_sponsorship"
    assert b["sponsorship_quote"] == "We are unable to sponsor visas."
    assert b["h1b"] > 0 and b["e_verify"] is True and b["cap_exempt"] is None
    empty = compute_badges(None, company_name="X")
    assert empty == {"sponsorship": "unknown", "sponsorship_quote": None, "h1b": None,
                     "e_verify": None, "cap_exempt": None}


def test_update_company_badges(engine):
    with Session(engine) as s:
        s.add(Company(name="Acme Security, Inc.", domain="acme.com"))
        s.add(Company(name="Johns Hopkins University", domain="jhu.edu"))
        s.commit()
        n = update_company_badges(s, h1b=load_h1b_csv(FIX / "h1b_legacy.csv"),
                                  everify=EVerifyIndex.from_csv(FIX / "everify.csv"))
        s.commit()
        assert n == 2
        acme = s.exec(select(Company).where(Company.domain == "acme.com")).one()
        assert acme.h1b_recent_approvals > 0 and acme.e_verify is True
        jhu = s.exec(select(Company).where(Company.domain == "jhu.edu")).one()
        assert jhu.cap_exempt is True and jhu.e_verify is False
