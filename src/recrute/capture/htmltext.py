"""HTML -> plain text that keeps inline markup inline.

`BeautifulSoup.get_text("\\n")` puts a newline around EVERY tag, so
`<p>Applicants must be <b>U.S. citizens</b>.</p>` becomes three "sentences". Here line breaks
are only inserted around block-level elements and <br>; whitespace from the HTML source
(including newlines) collapses to a single space, as a browser renders it.
"""

import re

from bs4 import BeautifulSoup, NavigableString

BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "body", "caption", "dd", "details", "dialog",
    "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
    "h4", "h5", "h6", "header", "hgroup", "hr", "html", "li", "main", "nav", "ol", "p", "pre",
    "section", "summary", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
})
_DROP = ("script", "style", "head", "title", "noscript", "template")
_BREAK = "\x00"  # placeholder for a real line break
_SPACES = re.compile(r"[^\S\x00]+")


def html_to_text(html: str) -> str:
    soup = BeautifulSoup((html or "").replace("\x00", ""), "lxml")
    for tag in soup(list(_DROP)):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with(NavigableString(_BREAK))
    for tag in soup.find_all(BLOCK_TAGS):
        tag.insert_before(NavigableString(_BREAK))
        tag.append(NavigableString(_BREAK))
    text = _SPACES.sub(" ", soup.get_text("").replace("\xa0", " "))
    out: list[str] = []
    for ln in (x.strip() for x in text.split(_BREAK)):
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out).strip()
