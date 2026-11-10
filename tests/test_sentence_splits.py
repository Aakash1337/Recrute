"""A summary or letter is never cut after an abbreviation: "... captain. M.S." was a resume's
whole second sentence. (Made-up people and places only.)"""

import pytest


@pytest.mark.parametrize("text,sentences", [
    ("Defended networks as a blue team captain. M.S. in Cybersecurity from Northfield.",
     ["Defended networks as a blue team captain.", "M.S. in Cybersecurity from Northfield."]),
    ("Built tools, e.g. scanners. Then shipped them!",
     ["Built tools, e.g. scanners.", "Then shipped them!"]),
    ("Works with Acme Inc. on triage. Leads the SOC.",
     ["Works with Acme Inc. on triage.", "Leads the SOC."]),
    ("One. Two? Three.", ["One.", "Two?", "Three."]),
    ("Engineer. B.Tech. in IT from Riverside. Then more.",
     ["Engineer.", "B.Tech. in IT from Riverside.", "Then more."]),
    ("Researcher. Ph.D. in CS. Done.", ["Researcher.", "Ph.D. in CS.", "Done."]),
    ("I built tools at Acme Inc. I secured networks.",
     ["I built tools at Acme Inc.", "I secured networks."]),
    ("Works in the U.S. He leads triage.", ["Works in the U.S.", "He leads triage."]),
    ("We met Dr. Smith. Then left.", ["We met Dr. Smith.", "Then left."]),
    ("No period at the end", ["No period at the end"])])
def test_split_sentences(text, sentences):
    from recrute.tailor.common import split_sentences

    assert split_sentences(text) == sentences


def test_a_summary_keeps_its_degree_sentence_whole():
    from test_tailor_support import SELECT_OUT, make_profile

    from recrute.tailor.select import validate_selection

    raw = {**SELECT_OUT, "summary": "Security engineer and blue team captain. M.S. in "
                                    "Cybersecurity from Northfield. Third sentence."}
    sel = validate_selection(make_profile(), raw)
    assert sel.summary == ("Security engineer and blue team captain. M.S. in Cybersecurity "
                           "from Northfield.")


def test_a_letter_is_trimmed_by_whole_sentences():
    from recrute.tailor.cover_letter import limit_words

    paras = ["Dear team,", "I led triage for two years. I earned an M.S. in Security from "
             "Northfield State University in the year it opened.", "Thanks."]
    out = limit_words(paras, 15)
    assert not any(p.rstrip().endswith("M.S.") for p in out)


def test_a_letter_keeps_a_whole_sentence_ending_in_an_abbreviation():
    """Re-audit: 'I built tools at Acme Inc. I secured networks.' was one 'sentence', so
    trimming it dropped both."""
    from recrute.tailor.cover_letter import limit_words

    assert limit_words(["I built tools at Acme Inc. I secured networks."], 6) == [
        "I built tools at Acme Inc."]
