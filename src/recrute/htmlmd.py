"""HTML -> Markdown for job descriptions, with non-content nodes removed ENTIRELY.

markdownify's `strip=` drops tags but keeps their text, so inline <script> bootstrap data
(which on logged-in pages can contain session tokens) would end up in stored descriptions and
LLM prompts. These nodes are deleted, contents and all, before conversion.
"""

import re

from bs4 import BeautifulSoup
from markdownify import markdownify

_DROP = ["script", "style", "noscript", "template", "iframe", "object", "embed", "svg",
         "canvas", "form", "input", "button", "select", "textarea", "meta", "link", "head"]


def html_to_markdown(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup.find_all(_DROP):
        tag.decompose()
    md = markdownify(str(soup), heading_style="ATX", strip=["img"])
    return re.sub(r"\n{3,}", "\n\n", md).strip()
