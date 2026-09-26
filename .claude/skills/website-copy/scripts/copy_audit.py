#!/usr/bin/env python3
"""Read a page the way a visitor does, then flag what makes its copy sound machine-made.

    python3 copy_audit.py extract <input>...        visible copy in reading order, one block per line
    python3 copy_audit.py scan <input>...           likely tells, repeats and sentence rhythm
    python3 copy_audit.py scan --json <input>...    the same, as JSON
    python3 copy_audit.py scan --alt-words 15 ...   the project's alt-text limit, per language
    python3 copy_audit.py locks --locked FILE <input>...   how often each locked line occurs

<input> is an .html file, a directory (every .html under it, skipping dot
directories and node_modules), a JSON message catalog such as next-intl's
messages/nl.json, a .txt or .md file of draft copy, or an http(s) URL.

Stdlib only, so it runs wherever the skill does. Every flag is a reason to
reread a sentence, not an error: plenty of flagged lines are fine, and a page
with no flags can still read like a machine wrote it. Read it aloud either way.

A URL is fetched as raw HTML and no JavaScript runs, so copy a script injects
(a language toggle's dictionary, a menu, form success and error messages) is
not seen. Save the rendered page from a browser and scan that file, or read the
script's strings.

--locked FILE names lines the project keeps word for word (legal, regulatory,
required disclosures): one substring per line, '#' for comments. A .md file is
read for the lines inside its ```locked fences only. A sentence that overlaps a
locked line is still extracted, but never flagged, and it is left out of the
rhythm, the running-copy count, the repeats and the joints (the visible-words
figure still counts it); the rest of its block is checked as usual. A line
that quotes a locked line (an alt text quoting the h1) is exempt too, so keep
headlines out of lock lists. `locks` counts every locked line in the source
of .html, .js and .json files (HTML defaults, script dictionaries, attributes;
a link inside the line is read through), so a count saved before a pass and
compared after it shows drift on any one page.

Text inside elements with the `hidden` attribute (a success message, an error,
a reveal) is extracted with the role "state": checked, never counted.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import urllib.request
from dataclasses import asdict, dataclass
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

__version__ = "2026-09-26"  # bump when a figure changes meaning; add report keys, never rename them

EM_DASH = "\u2014"

HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
# Running copy. Rhythm, repeats and most checks look only at these roles.
PROSE = frozenset({"p", "text", "dd", "li", "caption", "quote", "cell", "summary", "msg"})
# Short display lines: headings, page title, meta description, captions.
DISPLAY = frozenset(HEADINGS + ("title", "meta", "caption", "summary"))
# Interface words a visitor still reads.
INTERFACE = frozenset({"button", "link", "label", "placeholder", "dt"})
# Read, but not counted as the page's words: attributes, hidden states, decoration.
_UNCOUNTED = frozenset({"alt", "aria", "meta", "title", "placeholder", "decor", "kicker", "state"})


@dataclass
class Block:
    """One run of visible text, as a visitor meets it."""

    role: str  # h1-h6, p, li, dd, dt, text, caption, summary, quote, cell,
    #            button, link, label, placeholder, alt, aria, title, meta, msg,
    #            state (inside a `hidden` element), decor, kicker
    text: str
    lang: str = ""  # "nl", "en", or "" when unknown
    splits: int = 0  # <br>s inside a heading: a headline set over several lines
    where: str = ""  # "file:line", or "file#key.path" for a message catalog
    in_main: bool = False  # inside <main>


@dataclass
class Finding:
    check: str
    role: str
    text: str
    where: str
    note: str = ""


# ------------------------------------------------------------------ words and sentences

_TOKEN = re.compile(r"\S*[^\W_]\S*")  # a token with at least one letter or digit
_HAS_LETTER = re.compile(r"[^\W\d_]")


def words(text: str) -> list[str]:
    return _TOKEN.findall(text)


_ABBREVIATIONS = frozenset(
    """o.a. bijv. bv. incl. excl. ca. evt. nr. tel. enz. resp. m.b.t. i.v.m. d.m.v.
    t.a.v. z.s.m. e.g. i.e. etc. vs. no. approx. mr. mrs. ms. dr. st.""".split()
)
# A sentence ends at . ! ? or an ellipsis, plus any closing quote or bracket,
# when whitespace and a capital letter or digit follow.
_BREAK = re.compile(
    r"[.!?\u2026][\"'\u201d\u2019)]*(?=\s+[\"'\u201c\u2018(]?[A-Z0-9\u00c0-\u00d6\u00d8-\u00de])"
)


def sentences(text: str) -> list[str]:
    """Split running text into sentences, keeping abbreviations and decimals whole."""
    text = " ".join(text.split())
    out: list[str] = []
    start = 0
    for m in _BREAK.finditer(text):
        piece = text[start : m.end()]
        last = piece.rsplit(None, 1)[-1].lower()
        if last in _ABBREVIATIONS or re.fullmatch(r"[a-z]\.", last):
            continue
        out.append(piece.strip())
        start = m.end()
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return [s for s in out if s]


# ------------------------------------------------------------------ language

_NL_WORDS = frozenset(
    """de het een en van op te dat die voor met niet zijn je jij jouw u uw wij ons onze
    bij ook als maar om aan er naar uit tot dan nog kunt kun wordt hebben heeft elke
    geen hier daar wat hoe waar wanneer welke wie zo al""".split()
)
_EN_WORDS = frozenset(
    """the a an and to that for with not are you your our us at also if but from by
    or than can be it this every no here there what how where when which who so""".split()
)


def detect_lang(text: str, default: str = "") -> str:
    """'nl' or 'en' from function words, when the block is long enough to tell."""
    tokens = re.findall(r"[a-z\u00e0-\u00ff']+", text.lower())
    if len(tokens) < 4:
        return default
    nl = sum(t in _NL_WORDS for t in tokens)
    en = sum(t in _EN_WORDS for t in tokens)
    if nl >= 2 and nl >= 2 * en:
        return "nl"
    if en >= 2 and en >= 2 * nl:
        return "en"
    return default


def _majority_lang(blocks: list[Block]) -> str:
    counts: dict[str, int] = {}
    for b in blocks:
        lang = detect_lang(b.text)
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    return max(counts, key=counts.get) if counts else ""


def _norm_lang(value: str) -> str:
    return value.strip().lower()[:2] if value else ""


# ------------------------------------------------------------------ HTML

_BLOCK_TAGS = frozenset(
    """address article aside blockquote body button caption dd details dialog div dl dt
    fieldset figcaption figure footer form h1 h2 h3 h4 h5 h6 header hr html label legend
    li main nav ol option p section select summary table tbody td tfoot th thead title
    tr ul""".split()
)
_SKIP_TAGS = frozenset("script style noscript svg template textarea iframe object canvas".split())
_VOID_TAGS = frozenset("area base br col embed hr img input link meta param source track wbr".split())
_ROLE_OF = {
    **{h: h for h in HEADINGS},
    "p": "p",
    "li": "li",
    "dd": "dd",
    "dt": "dt",
    "figcaption": "caption",
    "caption": "caption",
    "summary": "summary",
    "button": "button",
    "label": "label",
    "legend": "label",
    "option": "label",
    "blockquote": "quote",
    "td": "cell",
    "th": "cell",
    "title": "title",
}
_META_KEYS = frozenset(
    {"description", "og:title", "og:description", "og:image:alt", "twitter:title", "twitter:description", "twitter:image:alt"}
)
# The "01", "02" a template puts above each section, alone or fused with its label ("01 de sfeer").
_KICKER = re.compile(r"0\d")
_KICKER_LABEL = re.compile(r"0\d\s+[^\W\d_][\w'-]*(?:\s+[\w'-]+){0,2}")


class _PageParser(HTMLParser):
    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source = source
        self.blocks: list[Block] = []
        self.page_lang = ""
        self.kickers = 0
        self.swapped = 0  # elements a language script rewrites (data-i18n...)
        self.has_main = False
        self._state_at: list[int] = []  # stack depths where a `hidden` element opened
        self._stack: list[tuple[str, str, bool]] = []  # (tag, its lang attribute, aria-hidden)
        self._skip: list[str] = []  # open tags whose content is not shown
        self._buf: list[str] = []
        self._line = 0
        self._all_link = True  # every character so far sat inside an <a>
        self._splits = 0
        self._after_close = False  # the last event closed an inline element

    def _lang(self) -> str:
        for _, lang, _ in reversed(self._stack):
            if lang:
                return lang
        return self.page_lang

    def _emit(self, role: str, text: str, line: int, splits: int = 0, lang: str = "") -> None:
        text = " ".join(text.split())
        if not text:
            return
        if _KICKER.fullmatch(text) or _KICKER_LABEL.fullmatch(text):
            self.kickers += 1
            if not _HAS_LETTER.search(text):
                role = "kicker"
        elif not _HAS_LETTER.search(text) and not re.search(r"\d", text):
            return  # arrows and icons; phone numbers and opening hours stay
        in_main = any(t == "main" for t, _, _ in self._stack)
        self.blocks.append(Block(role, text, lang or self._lang(), splits, f"{self.source}:{line}", in_main))

    def _flush(self) -> None:
        if self._buf:
            role = "text"
            for tag, _, _ in reversed(self._stack):
                if tag in _ROLE_OF:
                    role = _ROLE_OF[tag]
                    break
            if role in ("p", "text") and any(t == "li" for t, _, _ in self._stack):
                role = "li"  # a paragraph inside a list item is still list content
            if self._all_link and role not in HEADINGS and role not in ("button", "title"):
                role = "link"
            if any(hidden for _, _, hidden in self._stack):
                role = "decor"  # aria-hidden: icons, honeypot fields; shown in extract, never checked
            elif self._state_at:
                role = "state"  # hidden until a script shows it: a status line, an error, a reveal
            self._emit(role, "".join(self._buf), self._line, self._splits)
        self._buf, self._all_link, self._splits = [], True, 0

    def handle_starttag(self, tag, attrs):
        if self._skip:
            if tag == self._skip[-1] and tag not in _VOID_TAGS:
                self._skip.append(tag)
            return
        a = {k: (v or "") for k, v in attrs}
        line = self.getpos()[0]
        if tag == "textarea" and a.get("placeholder") and "hidden" not in a:
            self._emit("placeholder", a["placeholder"], line)  # the box itself is skipped, its hint is shown
        if tag in _SKIP_TAGS or ("hidden" in a and tag in _VOID_TAGS):
            if tag not in _VOID_TAGS:
                self._skip.append(tag)
            return
        if any(k.startswith("data-i18n") for k in a):
            self.swapped += 1
        if tag == "main":
            self.has_main = True
        if tag == "html":
            self.page_lang = _norm_lang(a.get("lang", ""))
        if tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key in _META_KEYS and a.get("content"):
                self._emit("alt" if key.endswith(":alt") else "meta", a["content"], line)
            return
        if tag == "br":
            if any(t in HEADINGS for t, _, _ in self._stack):
                self._splits += 1
                self._buf.append(" ")
            else:
                self._flush()
            return
        if tag in _BLOCK_TAGS or (tag == "a" and self._all_link):
            self._flush()  # a link straight after another link is its own block, not "Menu Over ons"
        elif self._after_close and tag not in _VOID_TAGS and "".join(self._buf)[-1:].strip():
            # Sibling inline elements (<span>open</span><span>17:00</span>) are usually set on
            # separate lines by CSS; without a space they would read "open17:00".
            self._buf.append(" ")
        self._after_close = False
        if a.get("alt") and tag in ("img", "area", "input"):
            self._emit("alt", a["alt"], line)
        for key, value in a.items():  # per-language alt text a script swaps in: data-alt-en="..."
            if re.fullmatch(r"data-alt-[a-z]{2}", key) and value and value != a.get("alt"):
                self._emit("alt", value, line, lang=key[-2:])
        if a.get("placeholder"):
            self._emit("placeholder", a["placeholder"], line)
        if a.get("aria-label"):
            self._emit("aria", a["aria-label"], line)
        if tag == "input" and a.get("type", "").lower() in ("submit", "button") and a.get("value"):
            self._emit("button", a["value"], line)
        if tag not in _VOID_TAGS:
            if "hidden" in a:
                self._state_at.append(len(self._stack))
            self._stack.append((tag, _norm_lang(a.get("lang", "")), a.get("aria-hidden", "").lower() == "true"))

    def handle_endtag(self, tag):
        if self._skip:
            if tag == self._skip[-1]:
                self._skip.pop()
            return
        if tag in _BLOCK_TAGS:
            self._flush()
        self._after_close = tag not in _BLOCK_TAGS
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                break
        while self._state_at and len(self._stack) <= self._state_at[-1]:
            self._state_at.pop()

    def handle_data(self, data):
        if self._skip:
            return
        if data.strip():
            if not "".join(self._buf).strip():
                self._line = self.getpos()[0]
            if all(t != "a" for t, _, _ in self._stack):
                self._all_link = False
            self._after_close = False  # "</em>-twist" continues the same word
        self._buf.append(data)

    def close(self):
        super().close()
        self._flush()


def blocks_from_html(html: str, source: str) -> tuple[list[Block], dict]:
    parser = _PageParser(source)
    parser.feed(html)
    parser.close()
    blocks, metas = [], set()
    for b in parser.blocks:
        if b.role == "meta":  # og: and twitter: tags usually repeat the description
            if b.text in metas:
                continue
            metas.add(b.text)
        blocks.append(b)
    page_lang = parser.page_lang or _majority_lang(blocks)
    for b in blocks:
        b.lang = detect_lang(b.text, b.lang or page_lang)
    return blocks, {"lang": page_lang, "kickers": parser.kickers, "swapped": parser.swapped, "has_main": parser.has_main}


# ------------------------------------------------------------------ drafts and catalogs

_MD_INLINE = (
    (re.compile(r"!\[([^\]]*)\]\([^)]*\)"), r"\1"),
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),
    (re.compile(r"[*_`]{1,3}"), ""),
)


def _strip_md(text: str) -> str:
    for rx, repl in _MD_INLINE:
        text = rx.sub(repl, text)
    return " ".join(text.split())


def blocks_from_text(text: str, source: str) -> tuple[list[Block], dict]:
    """A draft in plain text or Markdown: '#' lines are headings, '-' lines list items. A line that
    follows a list item with no blank line between continues that item, as in Markdown."""
    blocks: list[Block] = []
    para: list[str] = []
    role, para_line = "p", 0

    def flush() -> None:
        if para:
            blocks.append(Block(role, _strip_md(" ".join(para)), "", 0, f"{source}:{para_line}"))
            para.clear()

    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        heading = re.match(r"(#{1,6})\s+(.*)", line)
        item = re.match(r"(?:[-*+]|\d+[.)])\s+(.*)", line)
        if not line or heading or item:
            flush()
        if heading:
            blocks.append(Block(f"h{len(heading.group(1))}", _strip_md(heading.group(2)), "", 0, f"{source}:{n}"))
        elif item:
            role, para_line = "li", n
            para.append(item.group(1))
        elif line:
            if not para:
                role, para_line = "p", n
            para.append(line)
    flush()
    blocks = [b for b in blocks if _HAS_LETTER.search(b.text)]
    lang = _majority_lang(blocks)
    for b in blocks:
        b.lang = detect_lang(b.text, lang)
    return blocks, {"lang": lang, "kickers": 0}


def _looks_like_code(text: str) -> bool:
    return " " not in text and bool(re.search(r"[/_.:#=@]|^[a-z]+[A-Z]", text))


def blocks_from_catalog(data, source: str, lang: str = "") -> tuple[list[Block], dict]:
    """Every string in a JSON message catalog, keyed by its path."""
    out: list[Block] = []

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")
        elif isinstance(node, str):
            text = " ".join(re.sub(r"<[^>]*>", "", node).split())
            if _HAS_LETTER.search(text) and not _looks_like_code(text):
                out.append(Block("msg", text, lang, 0, f"{source}#{path}"))

    walk(data, "")
    page_lang = lang or _majority_lang(out)
    for b in out:
        b.lang = detect_lang(b.text, page_lang)
    return out, {"lang": page_lang, "kickers": 0}


def _fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (copy_audit)"})
    with urllib.request.urlopen(request, timeout=20) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def load(target: str) -> list[tuple[str, list[Block], dict]]:
    """(source, blocks, page info) for every page the target names."""
    if re.match(r"https?://", target):
        blocks, info = blocks_from_html(_fetch(target), target)
        return [(target, blocks, info)]
    path = Path(target)
    if path.is_dir():
        pages = []
        for file in sorted(path.rglob("*.htm*")):
            inner = file.relative_to(path).parts[:-1]
            if file.suffix.lower() in (".html", ".htm") and not any(
                part.startswith(".") or part == "node_modules" for part in inner
            ):
                pages.extend(load(str(file)))
        return pages
    if not path.is_file():
        raise FileNotFoundError(f"no such file or directory: {target}")
    suffix = path.suffix.lower()
    if suffix in (".js", ".mjs", ".cjs", ".ts"):
        raise ValueError(f"{target}: a script is code; scan a rendered page or a JSON catalog, or read its strings")
    text = path.read_text(encoding="utf-8", errors="replace")
    if suffix in (".html", ".htm"):
        blocks, info = blocks_from_html(text, str(path))
    elif suffix == ".json":
        lang = path.stem[:2].lower() if re.fullmatch(r"[a-z]{2}([-_][A-Za-z]{2})?", path.stem) else ""
        blocks, info = blocks_from_catalog(json.loads(text), str(path), lang)
    else:
        blocks, info = blocks_from_text(text, str(path))
    return [(str(path), blocks, info)]


# ------------------------------------------------------------------ checks


def _compile(rows):
    return [(re.compile(pattern, re.IGNORECASE), note) for pattern, note in rows]


_STOCK = {
    "en": _compile(
        [
            (r"\bcome for\b.{1,40}\bstay for\b", "stock slogan"),
            (r"\bwhere\b.{1,30}\bmeets?\b", "stock construction"),
            (r"\bmore than just\b", "stock construction"),
            (r"\bnot just\b.{1,60}\bbut\b", "stock construction"),
            (r"\bit'?s not\b.{1,40}\bit'?s\b", "stock construction"),
            (r"\bin the heart of\b|\bnestled\b", "stock location phrase"),
            (r"\b(discover|indulge in|immerse yourself|experience (the|our|a|an))\b", "brochure verb: what does the visitor actually do?"),
            (
                r"\b(journey|elevated?|elevates|curated|seamless(ly)?|unlock|embark|delve|tapestry|vibrant|"
                r"bespoke|unparalleled|world[- ]class|state[- ]of[- ]the[- ]art|cutting[- ]edge|best[- ]in[- ]class|"
                r"next[- ]level|game[- ]chang\w*|revolutionary|transformative|holistic|synerg\w*|empower\w*|leverag\w*)\b",
                "stock marketing word",
            ),
            (r"\b(passion(ate)? (for|about)|we pride ourselves|a testament to|look no further|whether you'?re)\b", "stock phrase"),
            (r"\b(unforgettable|culinary (journey|adventure|experience)|feast for the senses|hidden gem|with feeling)\b", "stock hospitality phrase"),
            (r"\bin today'?s\b.{0,20}\b(world|landscape|market)\b|\bever[- ]evolving\b|\bfast[- ]paced\b", "stock opener"),
            (
                r"\b(thoughtfully|carefully|lovingly|expertly|meticulously) (selected|curated|crafted|designed|chosen|prepared)\b",
                "adverb doing the work a fact should do",
            ),
        ]
    ),
    "nl": _compile(
        [
            (r"\bkom voor\b.{1,40}\bblijf voor\b", "stock slogan"),
            (r"\bwaar\b.{1,40}\b(samenkomen|samenkomt|elkaar ontmoeten)\b", "stock construction"),
            (r"\bmeer dan (alleen |zomaar |gewoon )?een\b", "stock construction"),
            (r"\bin het hart van\b", "stock location phrase"),
            (r"\b(ontdek|beleef|ervaar)\b", "brochure verb: what does the visitor actually do?"),
            (
                r"\b(beleving|culinaire (reis|ervaring|beleving)|smaakbeleving|smaakexplosie|feest voor de zintuigen|onvergetelijk\w*)\b",
                "stock hospitality phrase",
            ),
            (r"\buniek\w* (concept|beleving|ervaring)\b", "stock phrase"),
            (r"\b(met gevoel|met (veel )?passie|passie voor|staat (bij ons )?centraal|wij geloven)\b", "stock phrase"),
            (
                r"\b(naadloos|naadloze|hoogwaardig\w*|toonaangevend\w*|ontzorg\w*|totaalconcept|van a tot z|next level|hotspot)\b",
                "stock marketing word",
            ),
            (r"\b(zorgvuldig|met zorg|met liefde) (geselecteerd|samengesteld|uitgekozen|bereid)\b", "adverb doing the work a fact should do"),
            (r"\breis (langs|door)\b", "stock metaphor"),
            (r"^[^\s,]+,\s+in beeld\.?$", "calque of 'X, in pictures'; say what it is: Foto's"),
        ]
    ),
}

_META_TALK = {
    "en": _compile(
        [
            (r"\b(named|shown|listed|mentioned|published) here\b", ""),
            (r"\bon this (page|site|website)\b", ""),
            (r"\bdespite the name\b", ""),
            (r"\b(photos?|images?|pictures?)\b.{0,20}\b(are|is) real\b", ""),
            (r"\bno \w+ (is|are) (named|shown|listed)\b", ""),
            (r"\bwe (do not|don't) (name|publish|show|list)\b", ""),
        ]
    ),
    "nl": _compile(
        [
            (r"\bop deze (site|website|pagina)\b", ""),
            (r"\b(hier|op deze \w+) (niet )?(genoemd|getoond|vermeld)\b", ""),
            (r"\b(foto[\u2019']?s|beelden|afbeeldingen)\b.{0,20}\b(zijn|is) echt\b", ""),
            (r"\bondanks de naam\b", ""),
            (r"\b(wordt|worden) (hier )?niet (genoemd|getoond|vermeld)\b", ""),
        ]
    ),
}

_STIFF = {
    "en": _compile(
        [
            (r"\butili[sz](e|es|ed|ing|ation)\b", "use"),
            (r"\bin order to\b", "to"),
            (r"\bprior to\b", "before"),
            (r"\bcommenc(e|es|ed|ing)\b", "start"),
            (r"\bfacilitat(e|es|ed|ing)\b", "help, run"),
            (r"\bendeavou?r", "try"),
            (r"\b(please be advised|please note that)\b", "cut it"),
            (r"\bkindly\b", "please"),
            (r"\bshould you wish\b", "if you'd like"),
            (r"\bat your earliest convenience\b", "when you can"),
            (r"\bwe are (pleased|delighted|happy) to\b", "just say it"),
            (r"\bin the event that\b", "if"),
            (r"\ba (wide |broad )?(range|variety|selection|array) of\b", "name them"),
            (r"\bofferings\b", "what you sell"),
            (r"\bstakeholders\b", "say who"),
            (r"\b(with (regard|respect) to|pertaining to|in terms of)\b", "about"),
            (r"\bsubsequently\b", "then"),
            (r"\b(furthermore|moreover|additionally)\b", "also, or nothing"),
            (r"\b(aforementioned|hereby|herein|henceforth)\b", "cut it"),
        ]
    ),
    "nl": _compile(
        [
            (r"\bmiddels\b", "met, via"),
            (r"\bteneinde\b", "om"),
            (r"\breeds\b", "al"),
            (r"\btevens\b", "ook"),
            (r"\bechter\b", "maar"),
            (r"\bindien\b", "als"),
            (r"\balsmede\b", "en"),
            (r"\bdien(t|en) (u |je |jij )?te\b", "moet(en)"),
            (r"\bwoonachtig\b", "wonen"),
            (r"\bgelieve\b", "wilt u, graag"),
            (r"\bten behoeve van\b", "voor"),
            (r"\bmet betrekking tot\b", "over"),
            (r"\bin het kader van\b", "voor, bij"),
            (r"\bdesalniettemin\b", "toch"),
            (r"\bderhalve\b", "daarom"),
            (r"\b(aangaande|inzake|omtrent|betreffende)\b", "over"),
            (r"\bdoch\b", "maar"),
            (r"\bhetgeen\b", "wat"),
            (r"\bdusdanig\b", "zo"),
            (r"\bgaarne\b", "graag"),
            (r"\btracht(en)?\b", "proberen"),
            (r"\bverstrek(t|ken)\b", "geven"),
            (r"\bbovengenoemde\b", "deze"),
            (r"\bzulks\b", "dat"),
            (r"\b(wij verzoeken u|u wordt verzocht)\b", "wilt u"),
            (r"\bzorg dragen voor\b", "zorgen voor"),
            (r"\bvoornamelijk\b", "vooral"),
            (r"(?<!\w)(d\.m\.v\.|i\.v\.m\.|m\.b\.t\.|t\.b\.v\.)", "spell it out: met, door, over, voor"),
        ]
    ),
}

_NUMBER_WORDS = (
    r"twee|drie|vier|vijf|zes|zeven|acht|negen|tien|elf|twaalf|dertien|veertien|vijftien|zestien|"
    r"zeventien|achttien|negentien|twintig|dertig|veertig|vijftig|honderd|two|three|four|five|six|"
    r"seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|twenty|thirty|forty|fifty|hundred"
)
# A figure is a run of digits (2,30 / 22:30 / N90 / 020 123 4567 each count once) or a spelled-out number.
_FIGURE = re.compile(rf"\d+(?:[.,:]\d+|\s\d{{2,}})*|\b(?:{_NUMBER_WORDS})\b", re.IGNORECASE)
# Spelled out only: "36 Unagi" is a dish number on a menu, "Zestien gerechten" is counting.
_COUNTING_HEAD = re.compile(rf"^(?:{_NUMBER_WORDS})\s+[^\W\d_]", re.IGNORECASE)
_ITEM = r"[^\W\d_][\w'\u2019-]*(?:\s+[^\W\d_][\w'\u2019-]*){0,2}"
_TRIAD = re.compile(rf"\b{_ITEM},\s+{_ITEM},?\s+(?:en|and|&)\s+{_ITEM}", re.IGNORECASE)
_SPACED_DASH = re.compile(r"[^\W\d_]\s+[\u2013-]\s+[^\W\d_]")
_COLON = re.compile(r"(?<!\d):(?!\d)")
_NL_FRONTED = re.compile(
    r"^(welke|of|wat|hoe|waar|wanneer|wie|hoeveel|hoelang)\b[^?]*?,\s+"
    r"(ziet|hoort|leest|vindt|kiest|kunt|kun|krijgt|betaalt|bepaalt|weet|zie|hoor|lees|vind|kies|krijg|betaal|bepaal)"
    r"\s+(u|je|jij|jullie|we|wij)\b",
    re.IGNORECASE,
)

_PREPS = frozenset("in op aan bij met voor naar van uit om at on by for with to from".split())
_REPLY = frozenset("ja nee yes no".split())


def _real_triad(s: str) -> bool:
    """'X, Y en Z' as a list, not 'Nee, we ... en je ...' or 'every day, at lunch and at dinner'."""
    for m in _TRIAD.finditer(s):
        items = [x.strip().split() for x in re.split(r",\s*|\s+(?:en|and|&)\s+", m.group(0), flags=re.I)[:3]]
        if len(items) < 3 or not all(items):
            continue
        firsts = [x[0].lower() for x in items]
        if firsts[0] in _REPLY and len(items[0]) == 1:
            continue  # an answer, then a clause: 'Nee, we bezorgen niet en ...' ('No fuss, no frills' is a list)
        if firsts[1] in _PREPS and firsts[2] in _PREPS and firsts[0] not in _PREPS:
            continue  # one thing, then where or when
        return True
    return False


CHECKS = {
    "meta-talk": (
        "talks about the page",
        "Answers a reviewer or a rule, not a visitor. Would a customer miss it? "
        "A required disclosure stays: one calm line, in one place.",
    ),
    "stock": (
        "stock phrase",
        "Would fit any business in the category (swap test). Say the specific true thing, or nothing.",
    ),
    "figures": (
        "dense with figures",
        "Every true number on the page reads like a dossier. Keep the ones that help a visitor decide "
        "or get there; the rest can live in the notes. A heading that counts things rarely needs to.",
    ),
    "triad": (
        "list of three",
        "Keep it if every item is true and specific here; drop it if it is there because three sounds finished.",
    ),
    "staccato": (
        "clipped run",
        "Very short sentences in a row. Once, as a deliberate beat, it works; as a habit it reads clipped. "
        "Would the owner say it like this out loud?",
    ),
    "colon": (
        "colon setup",
        "'X, Y and Z: the point.' One on a page is fine; this page leans on them.",
    ),
    "dash": (
        "dash joint",
        "A dash joining two thoughts. Rewrite the sentence so it needs none; two fragments are not a fix.",
    ),
    "stiff": ("official word", "People say the everyday word."),
    "word-order": (
        "fronted clause (nl)",
        "Clause before the main verb reads translated. Main clause first: 'Als u online reserveert, ziet u ...'.",
    ),
    "title-case": ("Title Case (nl)", "English-style Title Case in Dutch. Dutch headings use sentence case."),
    "alt": (
        "inventory alt text",
        "Say what the picture shows and why it is there, in one plain sentence.",
    ),
}

RHYTHM_NOTES = {
    "clipped": "Short, even sentences. Right for a menu or an opening-hours block; in running prose it reads "
    "clipped. If a cutting pass did this, give each fragment the subject and verb it lost, and let some "
    "sentences run longer. Don't glue fragments together with 'and' or 'so': that is the next tic.",
    "long": "Sentences run long. Split where a reader would take a breath.",
    "even": "Sentence lengths barely vary. Mix a few short ones in with longer ones.",
}


def _tables(table: dict, lang: str) -> list:
    return table[lang] if lang in table else table["en"] + table["nl"]


def _trailing_name(names: list[str]) -> bool:
    """'Route naar de Anna Bijnsgarage': sentence case, then a name of two or more capitalised words.
    It takes two lowercase words before the name, so 'Sushi met Liefde Bereid' is still Title Case."""
    i = len(names)
    while i > 1 and names[i - 1][0].isupper():
        i -= 1
    run, before = names[i:], names[1:i]
    return len(run) >= 2 and len(before) >= 2 and all(w.islower() for w in before)


def check_block(b: Block, alt_words: int = 20) -> list[Finding]:
    """Per-block flags. Each one is a prompt to reread, not a verdict."""
    out: list[Finding] = []
    text, lang = b.text, b.lang
    prose = b.role in PROSE or b.role == "state"  # hidden status lines are checked like prose, never counted
    display, interface = b.role in DISPLAY, b.role in INTERFACE

    def add(check: str, note: str = "") -> None:
        out.append(Finding(check, b.role, text, b.where, note))

    if prose or display or interface or b.role == "alt":
        if EM_DASH in text:
            add("dash", "em dash")
        elif _SPACED_DASH.search(text) and not (
            # A printed list, "Huiswijn wit - Domaine X (Frankrijk - Languedoc)": a name after every dash,
            # up to a bracket, punctuation or a figure ("Sake - Junmai 300 ml").
            # "Fast setup - No code needed" is a feature and a benefit, so it is still a joint.
            b.role in ("li", "cell", "dt", "dd")
            and not any(w[:1].islower() for m in re.finditer(r"\s[\u2013-]\s+([^(),;:\d]+)", text)
                        for w in words(m.group(1)))
        ):
            add("dash", "spaced dash between words")

    if prose or display or interface:
        for check, table in (("meta-talk", _META_TALK), ("stock", _STOCK), ("stiff", _STIFF)):
            if check == "meta-talk" and len(words(text)) < 4:
                continue  # a nav heading like "Op deze site" is not the page talking about itself
            notes = []
            for rx, note in _tables(table, lang):
                m = rx.search(text)
                if m:
                    found = f'"{m.group(0)}"'
                    notes.append(f"{found} -> {note}" if check == "stiff" else f"{found}: {note}" if note else found)
            if notes:
                add(check, "; ".join(notes))

    if prose:
        figures = _FIGURE.findall(text)
        if len(figures) >= 5:
            add("figures", f"{len(figures)} figures")
    elif b.role in ("h1", "h2") and _COUNTING_HEAD.match(text):  # "Four Seasons of Mochi" is a dish
        add("figures", "heading that counts")

    if prose or display:
        parts = sentences(text)
        if len(parts) >= 2 and any(  # "Zin gekregen? Bekijk de kaart" is a question and a label, not clipped
            x.endswith(".") and len(words(x)) <= 5 and len(words(y)) <= 5 for x, y in zip(parts, parts[1:])
        ):
            add("staccato")
        if lang == "nl":
            for s in parts:
                if not s.rstrip().endswith("?") and _NL_FRONTED.search(s):
                    add("word-order")
                    break

    if prose or b.role == "meta":
        for s in sentences(text):
            m = _COLON.search(s)
            if m and len(words(s[: m.start()])) >= 3 and re.match(r"\s+[a-z\u00df-\u00ff]", s[m.end() :]):
                add("colon")
                break

    if display or b.role in ("p", "text", "msg"):  # list items are lists: ingredients, services
        if any(len(words(s)) <= 12 and _real_triad(s) for s in sentences(text)):
            add("triad")

    if lang == "nl" and (b.role in HEADINGS or b.role in ("button", "link")):
        names = re.findall(r"[^\W\d_][\w'\u2019-]*", text)
        rest = [w for w in names[1:] if len(w) >= 3]
        capitals = [w for w in rest if w[0].isupper()]
        if (
            len(names) >= 3
            and any(w.lower() in _NL_WORDS for w in names)
            and len(capitals) >= 2
            and len(capitals) >= 0.6 * len(rest)
            and not _trailing_name(names)
        ):
            add("title-case")

    if b.role == "alt":
        n, commas = len(words(text)), text.count(",")
        if n > alt_words:
            add("alt", f"{n} words, limit {alt_words}")
        elif commas >= 4:
            add("alt", f"{n} words, {commas} commas")

    return out


# ------------------------------------------------------------------ page level


def _is_running(b: Block) -> bool:
    """Sentences that count toward rhythm: prose, not captions, eyebrows or labels."""
    if b.role not in PROSE or b.role == "caption":
        return False
    n = len(words(b.text))
    return (b.text.rstrip().endswith((".", "!", "?")) and n >= 3) or n >= 8


def rhythm(lengths: list[int]) -> dict:
    if not lengths:
        return {"sentences": 0, "note": ""}
    mean = statistics.fmean(lengths)
    sd = statistics.pstdev(lengths) if len(lengths) > 1 else 0.0
    short = sum(n <= 5 for n in lengths) / len(lengths)
    long_ = sum(n >= 30 for n in lengths) / len(lengths)
    note = ""
    if len(lengths) >= 8:
        if mean < 10 and short >= 0.25:
            note = "clipped"
        elif mean > 24:
            note = "long"
        elif sd / mean < 0.35:
            note = "even"
    return {
        "sentences": len(lengths),
        "mean_words": round(mean, 1),
        "sd_words": round(sd, 1),
        "short_share": round(short, 2),
        "long_share": round(long_, 2),
        "note": note,
    }


def repeats(blocks: list[Block]) -> list[dict]:
    """Sentences of five or more words that appear more than once on the page."""
    seen: dict[str, dict] = {}
    for b in blocks:
        if b.role not in PROSE:
            continue
        for s in sentences(b.text):
            if len(words(s)) < 5:
                continue
            key = " ".join(re.sub(r"[^\w\s]", "", s.lower()).split())
            entry = seen.setdefault(key, {"text": s, "count": 0, "where": []})
            entry["count"] += 1
            entry["where"].append(b.where)
    return [e for e in seen.values() if e["count"] >= 2 and not _is_hours_like(e["text"])]


def _is_hours_like(text: str) -> bool:
    """Opening hours and phone lines are utility: repeating them is fine."""
    toks = words(text)
    figures = sum(bool(re.search(r"\d", t)) for t in toks)
    return figures >= 2 and figures * 3 >= len(toks)


_PRICE = re.compile(r"(?:\u20ac|EUR|\$|\u00a3)\s?\d|\d[.,]\d{2}\b")


def _is_figure_line(text: str) -> bool:
    """A price, an hours line or a phone number: at least as many figure tokens as word tokens."""
    toks = words(text)
    digits = sum(bool(re.search(r"\d", t)) for t in toks)
    return digits > 0 and digits >= len(toks) - digits


def _menu_items(blocks: list[Block]) -> set[int]:
    """Indexes of dish lines followed by their price: a menu, not running copy. A dish is a description
    under an h3-h6 dish name, or one list item or table cell holding the name and its gloss."""
    out = set()
    for i in range(1, len(blocks) - 1):
        prev, b, nxt = blocks[i - 1], blocks[i], blocks[i + 1]
        if (b.role in PROSE and (prev.role in HEADINGS[2:] or b.role in ("li", "cell"))
                and _is_figure_line(nxt.text) and _PRICE.search(nxt.text)):
            out.add(i)
    return out


_JOINT = {
    "nl": re.compile(r",\s+(en|maar|dus|want|of)\s|;\s", re.IGNORECASE),
    "en": re.compile(r",\s+(and|but|so|or)\s|;\s", re.IGNORECASE),
}


# Mean words per sentence above which a page is long-form (terms, a privacy policy), where ', and'
# between clauses is ordinary grammar. The ', en' habit shows on pages of short sentences (10 to 13).
_LONG_FORM = 16


def joints(blocks: list[Block], page_lang: str) -> list[dict]:
    """One way of joining two clauses used in many sentences on the page (', en' ... ', en' ...)."""
    lengths = [len(words(s)) for b in blocks if _is_running(b) for s in sentences(b.text)]
    if lengths and statistics.fmean(lengths) > _LONG_FORM:
        return []
    sents, by = 0, {}
    for b in blocks:
        if not _is_running(b):
            continue
        rx = _JOINT.get(b.lang or page_lang)
        for s in sentences(b.text):
            if len(words(s)) < 5:
                continue
            sents += 1
            if not rx:
                continue
            for m in rx.finditer(s):
                q = s.rfind(",", 0, m.start())
                if m.group(1) and q >= 0 and len(words(s[q + 1 : m.start()])) <= 3:
                    continue  # the last item of a list: 'equity, mezzanine, and debt'
                key = ", " + m.group(1).lower() if m.group(1) else ";"
                by.setdefault(key, []).append(s)
                break
    return [{"joint": k, "count": len(v), "of": sents, "sentences": v}
            for k, v in by.items() if len(v) >= 4 and len(v) >= 0.1 * sents]


def _eyebrows(blocks: list[Block]) -> int:
    """Short labels set straight above a heading ("Prijzen" over "Wat kost het?")."""
    visible = [b for b in blocks if b.role not in _UNCOUNTED]
    return sum(
        prev.role in ("p", "text") and len(words(prev.text)) <= 4 and not prev.text.rstrip().endswith(".")
        and not _is_figure_line(prev.text)
        and nxt.role in ("h1", "h2", "h3")
        for prev, nxt in zip(visible, visible[1:])
    )


def _unlock(b: Block, keys: list[str]) -> list[Block]:
    """The block without the sentences a locked line overlaps: [b] when none does, else one block per
    run of free sentences, so two sentences a locked one sits between are never read as neighbours."""
    low = b.text.casefold()
    spans = []
    for k in keys:
        i = low.find(k)
        while i >= 0:
            spans.append((i, i + len(k)))
            i = low.find(k, i + 1)
    if not spans:
        return [b]
    if len(low) != len(b.text):  # casefold changed the length (a German sharp s): lock the whole block
        return []
    runs: list[list[str]] = [[]]
    pos = 0
    for sent in sentences(b.text):
        start = b.text.find(sent, pos)
        end = start + len(sent)
        pos = end
        if start < 0 or not any(a < end and start < z for a, z in spans):
            runs[-1].append(sent)
        elif runs[-1]:
            runs.append([])
    return [Block(b.role, " ".join(r), b.lang, b.splits, b.where, b.in_main) for r in runs if r]


def _visible_words(blocks: list[Block], has_main: bool) -> tuple[int, dict]:
    """Words a visitor reads in <main> (the whole page when there is none), by h2 section."""
    total, by = 0, {"top": 0}
    section = "top"
    for b in blocks:
        if b.role in _UNCOUNTED or (has_main and not b.in_main):
            continue
        if b.role == "h2":
            section = b.text if b.text not in by else f"{b.text} ({sum(k.startswith(b.text) for k in by) + 1})"
            by[section] = 0
        n = len(words(b.text))
        total += n
        by[section] += n
    return total, {k: v for k, v in by.items() if v or k != "top"}


def _lone_short(blocks: list[Block]) -> list[str]:
    """Blocks that are one short sentence on their own: a deliberate beat, or a fragment."""
    out = []
    for b in blocks:
        if b.role not in ("p", "text", "li", "dd", "cell", "msg"):
            continue
        parts = sentences(b.text)
        if (len(parts) == 1 and parts[0].endswith(".") and 2 <= len(words(parts[0])) <= 5
                and not re.search(r"(?:\b[A-Za-z]\.){2,}$", parts[0])):  # 'Hennessy V.S.O.P.', 'Example B.V.'
            out.append(b.text)
    return out if len(out) >= 3 else []


def analyse(source: str, blocks: list[Block], info: dict, locked: tuple[str, ...] = (), alt_words: int = 20) -> dict:
    """The scan report for one page. Keys are added over versions, never renamed."""
    keys = [k.casefold() for k in locked if k.strip()]
    free_of = [_unlock(b, keys) if keys else [b] for b in blocks]  # each block minus its locked sentences
    is_locked = [not (len(f) == 1 and f[0] is b) for f, b in zip(free_of, blocks)]
    items = _menu_items(blocks)
    findings = [f for i, parts in enumerate(free_of) for b in parts
                for f in check_block(b, alt_words) if not (i in items and f.check in ("triad", "staccato", "colon", "figures"))]
    if sum(f.check == "colon" for f in findings) < 2:  # one setup on a page is a sentence, not a habit
        findings = [f for f in findings if f.check != "colon"]
    free = [b for i, parts in enumerate(free_of) if i not in items for b in parts]
    # Whether a block is running prose is judged on the whole block, before its locked sentences come out.
    running = [b for i, parts in enumerate(free_of) if i not in items and _is_running(blocks[i]) for b in parts]
    lengths = [len(words(s)) for b in running for s in sentences(b.text)]
    heads = [b for b in blocks if b.role in ("h1", "h2", "h3")]
    visible, by_section = _visible_words(blocks, info.get("has_main", False))
    return {
        "source": source,
        "lang": info.get("lang", ""),
        "scanner": __version__,
        "body_words": sum(len(words(b.text)) for b in running),
        "visible_words": visible,
        "words_by_section": by_section,
        "rhythm": rhythm([n for n in lengths if n]),
        "locked_blocks": sum(is_locked),
        "locked_words": sum(len(words(b.text)) - sum(len(words(p.text)) for p in parts)
                            for b, parts, hit in zip(blocks, free_of, is_locked) if hit),
        "joints": joints(free, info.get("lang", "")),
        "lone_short": _lone_short([b for i, b in enumerate(blocks) if not is_locked[i] and i not in items]),
        "script_swapped": info.get("swapped", 0),
        "findings": [asdict(f) for f in findings],
        "repeats": repeats(free),
        "headings": {
            "count": len(heads),
            "ending_in_full_stop": [b.text for b in heads if b.text.rstrip().endswith(".")],
            "set_over_lines": [b.text for b in heads if b.splits],
            "numbered_kickers": info.get("kickers", 0),
            "eyebrows": _eyebrows(blocks),
        },
    }


# ------------------------------------------------------------------ output


def _short(where: str) -> str:
    if "#" in where:
        return "#" + where.split("#", 1)[1]
    tail = where.rsplit(":", 1)
    return f"L{tail[1]}" if len(tail) == 2 and tail[1].isdigit() else where


def _clip(text: str, width: int = 150) -> str:
    return text if len(text) <= width else text[: width - 3] + "..."


def render_extract(source: str, blocks: list[Block], info: dict) -> str:
    lines = [f"== {source}  (language {info.get('lang') or '?'}, {len(blocks)} blocks)"]
    for b in blocks:
        extra = f"   [set over {b.splits + 1} lines]" if b.splits else ""
        if b.role == "alt":
            extra += f"   [{len(words(b.text))} words]"
        lines.append(f"{_short(b.where):>8}  {b.role:<11} {b.text}{extra}")
    return "\n".join(lines)


def render_scan(report: dict) -> str:
    r = report["rhythm"]
    sections = ", ".join(f"{k} {v}" for k, v in report.get("words_by_section", {}).items())
    lines = [
        f"== {report['source']}  (copy_audit {report.get('scanner', '?')})",
        f"   language {report['lang'] or '?'} | visible words: {report.get('visible_words', 0)}"
        + (f" ({sections})" if sections else "")
        + f" | running copy: {report['body_words']} words in {r['sentences']} sentences (for the rhythm)",
    ]
    if report.get("script_swapped"):
        lines.append(
            f"   script-swapped: {report['script_swapped']} elements (data-i18n); this scan read the static "
            f"{report['lang'] or 'default'} text. Render the other language and scan the saved page; a JSON catalog "
            "can be scanned directly, a JavaScript dictionary has to be read by hand."
        )
    by_check: dict[str, list[dict]] = {}
    for f in report["findings"]:
        by_check.setdefault(f["check"], []).append(f)
    for check, (label, hint) in CHECKS.items():
        items = by_check.get(check)
        if not items:
            continue
        lines.append(f"\n{label} ({len(items)}): {hint}")
        for f in items:
            note = f"   [{f['note']}]" if f["note"] else ""
            lines.append(f"  {_short(f['where']):>8} {f['role']:<8} {_clip(f['text'])}{note}")

    rep = report["repeats"]
    if rep:
        lines.append(f"\nrepeated ({len(rep)}): Explanations belong in one place; only utility (phone, booking) should repeat.")
        for e in sorted(rep, key=lambda e: -e["count"]):
            lines.append(f"  x{e['count']}  {_clip(e['text'])}")

    for j in report.get("joints", []):
        lines.append(f"\nsame joint ({j['count']} of {j['of']} sentences): '{j['joint']}' joins two clauses again and again. "
                     "Rework enough of them that neighbouring sentences don't share it: two sentences, a relative "
                     "clause, a question and its answer. Dropping the comma changes nothing, and a new 'so' or "
                     "'because' (dus, want, omdat) must be a cause a source gives.")
        for s in j["sentences"][:5]:
            lines.append(f"  {_clip(s)}")
        if len(j["sentences"]) > 5:
            lines.append(f"  ... and {len(j['sentences']) - 5} more (--json lists them all)")

    lone = report.get("lone_short", [])
    if lone:
        lines.append(f"\nshort lines on their own ({len(lone)}): read each aloud. A deliberate beat, a menu line, or a "
                     "fragment? In running copy a fragment gets its subject or verb back; don't glue it to the next sentence.")
        for t in lone:
            lines.append(f"  {_clip(t)}")

    h = report["headings"]
    shape = []
    if len(h["ending_in_full_stop"]) >= 3:
        shape.append(f"{len(h['ending_in_full_stop'])} of {h['count']} headings end in a full stop")
    if len(h["set_over_lines"]) >= 3:
        shape.append(f"{len(h['set_over_lines'])} are set over two or more lines")
    if h["numbered_kickers"] >= 3:
        shape.append(f"{h['numbered_kickers']} numbered section kickers (01, 02, ...)")
    if h["eyebrows"] >= 3:
        shape.append(f"{h['eyebrows']} have an eyebrow label above them")
    if shape:
        lines.append(
            "\nheading shape: " + "; ".join(shape) + ". When every section has the same shape the page "
            "reads like a template. Most headings can simply say what the section is."
        )

    if r["sentences"]:
        lines.append(
            f"\nrhythm: mean {r['mean_words']} words per sentence (sd {r['sd_words']}), "
            f"{r['short_share']:.0%} at five words or fewer, {r['long_share']:.0%} at thirty or more."
        )
        if r["note"]:
            lines.append("  " + RHYTHM_NOTES[r["note"]])

    if report["locked_blocks"]:
        lines.append(f"\nlocked: {report['locked_blocks']} blocks hold a --locked line ({report.get('locked_words', 0)} words); "
                     "those sentences were not checked or counted.")

    if not report["findings"] and not rep and not shape and not r.get("note") and not report.get("joints") and not lone:
        lines.append("\nNo flags. That proves little: read it aloud against the three tests.")
    return "\n".join(lines)


def read_locks(paths: list[str]) -> tuple[str, ...]:
    """Locked lines from each file: one per line, '#' for comments; a .md file's ```locked fences only."""
    out: list[str] = []
    for name in paths:
        text = Path(name).read_text(encoding="utf-8")
        if name.lower().endswith(".md"):
            found = re.findall(r"^```locked[ \t]*\n(.*?)^```", text, re.M | re.S)
            if not found:
                raise ValueError(f"{name}: no ```locked fence")
            text = "\n".join(found)
        lines = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
        if not lines:
            raise ValueError(f"{name}: no locked lines")
        out.extend(lines)
    return tuple(out)


# Inline markup a locked line can run through: "See the <a href=...>privacy policy</a>."
_INLINE_TAG = re.compile(
    r"</?(?:a|abbr|b|bdi|bdo|cite|code|dfn|em|i|kbd|mark|q|s|small|span|strong|sub|sup|time|u|var)\b[^<>]*>",
    re.IGNORECASE,
)
_ATTR_VALUE = re.compile(r"""=\s*(?:"([^"]*)"|'([^']*)')""")


_JS_ESCAPE = re.compile(r"\\(?:u([0-9a-fA-F]{4})|x([0-9a-fA-F]{2})|(['\"]))")


def _js_unescape(m) -> str:
    code = m.group(1) or m.group(2)
    return chr(int(code, 16)) if code else m.group(3)


def _plain(text: str) -> str:
    """Source as `locks` reads it: JavaScript string escapes decoded (\\' \\u00a0), inline tags dropped
    (their attribute values kept, each on a line of its own, so data-desc-en="..." still counts),
    entities decoded, word joiners dropped, no-break spaces read as plain spaces, whitespace collapsed."""
    text = _JS_ESCAPE.sub(_js_unescape, text)
    kept: list[str] = []

    def drop(m) -> str:
        kept.extend(a or b for a, b in _ATTR_VALUE.findall(m.group(0)))
        return ""

    pieces = [_INLINE_TAG.sub(drop, text)] + kept
    return "\n".join(" ".join(unescape(p).replace("\u2060", "").split()) for p in pieces)


def lock_counts(locked: tuple[str, ...], targets: list[str]) -> dict[str, int]:
    """How often each locked line occurs in the raw source (HTML defaults, script dictionaries and
    meta tags alike). Save the counts before a pass and compare after: every count must match."""
    files: list[Path] = []
    for target in targets:
        if re.match(r"https?://", target):
            raise ValueError(f"{target}: locks reads source files, not URLs")
        path = Path(target)
        if path.is_dir():
            files += [
                f for f in sorted(path.rglob("*"))
                if f.suffix.lower() in (".html", ".htm", ".js", ".json")
                and not any(part.startswith(".") or part == "node_modules" for part in f.relative_to(path).parts[:-1])
            ]
        elif path.is_file():
            files.append(path)
        else:
            raise FileNotFoundError(f"no such file or directory: {target}")
    corpus = [_plain(f.read_text(encoding="utf-8", errors="replace")) for f in files]
    return {line: sum(c.count(_plain(line)) for c in corpus) for line in dict.fromkeys(locked)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="copy_audit.py",
        description=__doc__.split("\n\n", 1)[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"copy_audit {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("extract", "print the visible copy in reading order, tagged by role"),
        ("scan", "flag likely tells, repeats, heading shapes and sentence rhythm"),
        ("locks", "count each --locked line in the raw source; exit 1 if one is found nowhere"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("inputs", nargs="+", help="site directory, or .html/.js/.json source files" if name == "locks"
                             else "HTML file, directory, URL, JSON catalog, or .txt/.md draft")
        command.add_argument("--json", action="store_true", help="machine-readable output")
        command.add_argument(
            "--locked", action="append", default=[], metavar="FILE", required=name == "locks",
            help="lines kept word for word, one per line (a .md file: its ```locked fences); "
            "sentences that hold one are not flagged or counted",
        )
        command.add_argument(
            "--alt-words", type=int, default=20, metavar="N",
            help="flag alt text over N words, each language on its own (take N from the project's rules)",
        )
    args = parser.parse_args(argv)

    try:
        locked = read_locks(args.locked)
        short = [line for line in locked if len(words(line)) < 4]
        if short and args.command == "scan":
            print(f"copy_audit: {len(short)} --locked line(s) under four words (first: {short[0]!r}); each exempts "
                  "every sentence that contains it, so use longer, unique substrings where you can", file=sys.stderr)
        if args.command == "locks":
            counts = lock_counts(locked, args.inputs)
            if args.json:
                print(json.dumps(counts, ensure_ascii=False, indent=1))
            else:
                print("\n".join(f"{n:>4}  {line}" for line, n in counts.items()))
            missing = [line for line, n in counts.items() if not n]
            if missing:
                print(f"copy_audit: {len(missing)} locked line(s) found nowhere", file=sys.stderr)
            return 1 if missing else 0
        pages = [page for target in args.inputs for page in load(target)]
    except (OSError, ValueError) as exc:  # missing file, unreachable URL, malformed JSON, empty lock file
        print(f"copy_audit: {exc}", file=sys.stderr)
        return 2
    if not pages:
        print("copy_audit: no pages found", file=sys.stderr)
        return 2

    if args.command == "extract":
        if args.json:
            payload = [{"source": s, "lang": i.get("lang", ""), "blocks": [asdict(b) for b in bl]} for s, bl, i in pages]
            print(json.dumps(payload, ensure_ascii=False, indent=1))
        else:
            print("\n\n".join(render_extract(*page) for page in pages))
        return 0

    reports = [analyse(*page, locked=locked, alt_words=args.alt_words) for page in pages]
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=1))
    else:
        print("\n\n".join(render_scan(r) for r in reports))
    return 0


if __name__ == "__main__":
    sys.exit(main())
