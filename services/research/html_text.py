"""Readable text and links from HTML using only the standard library."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

_SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "canvas", "form", "button", "select"}
_BLOCK = {
    "p", "div", "li", "ul", "ol", "section", "article", "main", "header", "footer", "aside", "br", "tr",
    "td", "th", "table", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "dd", "dt", "figcaption",
}
_HEADINGS = {"h1", "h2", "h3"}
_VOID = {"br", "img", "meta", "link", "input", "hr", "source", "wbr", "area", "base", "col", "embed", "track"}
_BOILERPLATE = re.compile(
    r"\b(cookies?|consent|accept all|reject all|privacy preferences|manage preferences|"
    r"enable javascript|javascript is (disabled|required)|your browser)\b", re.I)


@dataclass
class PageText:
    title: str = ""
    description: str = ""
    headings: list[str] = field(default_factory=list)
    text: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)   # (href, anchor text)
    people_text: str = ""   # every line incl. short ones (names, titles on team pages); boilerplate removed


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.in_title = False
        self.heading: list[str] | None = None
        self.link: list | None = None
        self.parts: list[str] = []
        self.out = PageText()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in _SKIP:
            if tag not in _VOID:
                self.skip_depth += 1
            return
        if tag == "title":
            self.in_title = True
        elif tag == "meta":
            name = (a.get("name") or a.get("property") or "").lower()
            if name in ("description", "og:description") and not self.out.description:
                self.out.description = " ".join((a.get("content") or "").split())
        elif tag == "a" and a.get("href"):
            self.link = [a["href"], []]
        if tag in _HEADINGS:
            self.heading = []
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if tag == "title":
            self.in_title = False
        elif tag == "a" and self.link is not None:
            self.out.links.append((self.link[0], " ".join(" ".join(self.link[1]).split())))
            self.link = None
        if tag in _HEADINGS and self.heading is not None:
            h = " ".join(" ".join(self.heading).split())
            if h:
                self.out.headings.append(h)
            self.heading = None
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.skip_depth:
            return
        if self.in_title:
            self.out.title += data
            return
        self.parts.append(data)
        if self.heading is not None:
            self.heading.append(data)
        if self.link is not None:
            self.link[1].append(data)


def extract(html: str, max_chars: int = 8000) -> PageText:
    parser = _Extractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed markup: keep whatever was parsed
        pass
    out = parser.out
    out.title = " ".join(out.title.split())
    lines, seen = [], set()
    all_lines, all_seen = [], set()
    for raw in "".join(parser.parts).split("\n"):
        line = re.sub(r"\s+", " ", raw).strip()
        if line and line.lower() not in all_seen and not (_BOILERPLATE.search(line) and len(line.split()) < 40):
            all_seen.add(line.lower())
            all_lines.append(line)
        # Drop empty lines, repeated nav/footer lines and single-word menu items.
        if not line or line.lower() in seen or (len(line) < 25 and len(line.split()) < 3):
            continue
        if _BOILERPLATE.search(line) and len(line.split()) < 40:   # cookie / consent / JS banners
            continue
        seen.add(line.lower())
        lines.append(line)
    out.text = "\n".join(lines)[:max_chars]
    out.people_text = "\n".join(all_lines)[:20000]
    return out
