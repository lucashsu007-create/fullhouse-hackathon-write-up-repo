#!/usr/bin/env python3
"""Read a page the way a visitor does, then flag what makes its copy sound machine-made.

    python3 copy_audit.py extract <input>...        visible copy in reading order, one block per line
    python3 copy_audit.py scan <input>...           likely tells, repeats and sentence rhythm
    python3 copy_audit.py scan --json <input>...    the same, as JSON

<input> is an .html file, a directory (every .html under it, skipping dot
directories and node_modules), a JSON message catalog such as next-intl's
messages/nl.json, a .txt or .md file of draft copy, or an http(s) URL.

Stdlib only, so it runs wherever the skill does. Every flag is a reason to
reread a sentence, not an error: plenty of flagged lines are fine, and a page
with no flags can still read like a machine wrote it. Read it aloud either way.

Copy that JavaScript injects (a language toggle's dictionary, a client-rendered
app) is not in the HTML. Scan the rendered page by URL, or the dictionary.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import urllib.request
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path

EM_DASH = "\u2014"

HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
# Running copy. Rhythm, repeats and most checks look only at these roles.
PROSE = frozenset({"p", "text", "dd", "li", "caption", "quote", "cell", "summary", "msg"})
# Short display lines: headings, page title, meta description, captions.
DISPLAY = frozenset(HEADINGS + ("title", "meta", "caption", "summary"))
# Interface words a visitor still reads.
INTERFACE = frozenset({"button", "link", "label", "placeholder", "dt"})


@dataclass
class Block:
    """One run of visible text, as a visitor meets it."""

    role: str  # h1-h6, p, li, dd, dt, text, caption, summary, quote, cell,
    #            button, link, label, placeholder, alt, aria, title, meta, msg
    text: str
    lang: str = ""  # "nl", "en", or "" when unknown
    splits: int = 0  # <br>s inside a heading: a headline set over several lines
    where: str = ""  # "file:line", or "file#key.path" for a message catalog


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
_META_KEYS = frozenset({"description", "og:title", "og:description", "twitter:title", "twitter:description"})
_KICKER = re.compile(r"0\d")  # the "01", "02" numbers a template puts above each section


class _PageParser(HTMLParser):
    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source = source
        self.blocks: list[Block] = []
        self.page_lang = ""
        self.kickers = 0
        self._stack: list[tuple[str, str]] = []  # (tag, its lang attribute)
        self._skip: list[str] = []  # open tags whose content is not shown
        self._buf: list[str] = []
        self._line = 0
        self._all_link = True  # every character so far sat inside an <a>
        self._splits = 0
        self._after_close = False  # the last event closed an inline element

    def _lang(self) -> str:
        for _, lang in reversed(self._stack):
            if lang:
                return lang
        return self.page_lang

    def _emit(self, role: str, text: str, line: int, splits: int = 0) -> None:
        text = " ".join(text.split())
        if not text:
            return
        if not _HAS_LETTER.search(text):  # arrows, counters, bare numbers
            if _KICKER.fullmatch(text):
                self.kickers += 1
            return
        self.blocks.append(Block(role, text, self._lang(), splits, f"{self.source}:{line}"))

    def _flush(self) -> None:
        if self._buf:
            role = "text"
            for tag, _ in reversed(self._stack):
                if tag in _ROLE_OF:
                    role = _ROLE_OF[tag]
                    break
            if role in ("p", "text") and any(t == "li" for t, _ in self._stack):
                role = "li"  # a paragraph inside a list item is still list content
            if self._all_link and role not in HEADINGS and role not in ("button", "title"):
                role = "link"
            self._emit(role, "".join(self._buf), self._line, self._splits)
        self._buf, self._all_link, self._splits = [], True, 0

    def handle_starttag(self, tag, attrs):
        if self._skip:
            if tag == self._skip[-1] and tag not in _VOID_TAGS:
                self._skip.append(tag)
            return
        a = {k: (v or "") for k, v in attrs}
        line = self.getpos()[0]
        if tag in _SKIP_TAGS or "hidden" in a:
            if tag not in _VOID_TAGS:
                self._skip.append(tag)
            return
        if tag == "html":
            self.page_lang = _norm_lang(a.get("lang", ""))
        if tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key in _META_KEYS and a.get("content"):
                self._emit("meta", a["content"], line)
            return
        if tag == "br":
            if any(t in HEADINGS for t, _ in self._stack):
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
        if a.get("placeholder"):
            self._emit("placeholder", a["placeholder"], line)
        if a.get("aria-label"):
            self._emit("aria", a["aria-label"], line)
        if tag == "input" and a.get("type", "").lower() in ("submit", "button") and a.get("value"):
            self._emit("button", a["value"], line)
        if tag not in _VOID_TAGS:
            self._stack.append((tag, _norm_lang(a.get("lang", ""))))

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

    def handle_data(self, data):
        if self._skip:
            return
        if data.strip():
            if not "".join(self._buf).strip():
                self._line = self.getpos()[0]
            if all(t != "a" for t, _ in self._stack):
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
    return blocks, {"lang": page_lang, "kickers": parser.kickers}


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
    """A draft in plain text or Markdown: '#' lines are headings, '-' lines list items."""
    blocks: list[Block] = []
    para: list[str] = []
    para_line = 0

    def flush() -> None:
        if para:
            blocks.append(Block("p", _strip_md(" ".join(para)), "", 0, f"{source}:{para_line}"))
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
            blocks.append(Block("li", _strip_md(item.group(1)), "", 0, f"{source}:{n}"))
        elif line:
            if not para:
                para_line = n
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
    text = path.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.lower()
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
# A figure is a run of digits (2,30 / 22:30 / N82 / 020 123 4567 each count once) or a spelled-out number.
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
        "'X, Y and Z: the point.' One on a page is fine; one per section is a pattern.",
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
    "clipped": "Short, even sentences. If this came out of a cutting pass it may read clipped: join ideas that "
    "belong together with because, so, but (omdat, dus, maar), and let some sentences run longer.",
    "long": "Sentences run long. Split where a reader would take a breath.",
    "even": "Sentence lengths barely vary. Mix a few short ones in with longer ones.",
}


def _tables(table: dict, lang: str) -> list:
    return table[lang] if lang in table else table["en"] + table["nl"]


def check_block(b: Block) -> list[Finding]:
    """Per-block flags. Each one is a prompt to reread, not a verdict."""
    out: list[Finding] = []
    text, lang = b.text, b.lang
    prose, display, interface = b.role in PROSE, b.role in DISPLAY, b.role in INTERFACE

    def add(check: str, note: str = "") -> None:
        out.append(Finding(check, b.role, text, b.where, note))

    if prose or display or interface or b.role == "alt":
        if EM_DASH in text:
            add("dash", "em dash")
        elif _SPACED_DASH.search(text):
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
    elif b.role in ("h1", "h2", "h3") and _COUNTING_HEAD.match(text):
        add("figures", "heading that counts")

    if prose or display:
        parts = sentences(text)
        if len(parts) >= 2 and any(
            len(words(x)) <= 5 and len(words(y)) <= 5 for x, y in zip(parts, parts[1:])
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

    if (display or (b.role in ("p", "text", "msg") and len(words(text)) <= 14)) and _TRIAD.search(text):
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
        ):
            add("title-case")

    if b.role == "alt":
        n = len(words(text))
        if n > 20 or text.count(",") >= 4:
            add("alt", f"{n} words")

    return out


# ------------------------------------------------------------------ page level


def _is_running(b: Block) -> bool:
    """Sentences that count toward rhythm: prose, but not bare labels."""
    if b.role not in PROSE:
        return False
    n = len(words(b.text))
    ends = b.text.rstrip().endswith((".", "!", "?"))
    return (ends or n >= 8) if b.role == "li" else (ends or n >= 4)


def rhythm(lengths: list[int]) -> dict:
    if not lengths:
        return {"sentences": 0, "note": ""}
    mean = statistics.fmean(lengths)
    sd = statistics.pstdev(lengths) if len(lengths) > 1 else 0.0
    short = sum(n <= 5 for n in lengths) / len(lengths)
    long_ = sum(n >= 30 for n in lengths) / len(lengths)
    note = ""
    if len(lengths) >= 8:
        if mean < 10 and short >= 0.3:
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
    return [e for e in seen.values() if e["count"] >= 2]


def analyse(source: str, blocks: list[Block], info: dict) -> dict:
    findings = [f for b in blocks for f in check_block(b)]
    running = [b for b in blocks if _is_running(b)]
    lengths = [len(words(s)) for b in running for s in sentences(b.text)]
    heads = [b for b in blocks if b.role in ("h1", "h2", "h3")]
    return {
        "source": source,
        "lang": info.get("lang", ""),
        "body_words": sum(len(words(b.text)) for b in running),
        "rhythm": rhythm([n for n in lengths if n]),
        "findings": [asdict(f) for f in findings],
        "repeats": repeats(blocks),
        "headings": {
            "count": len(heads),
            "ending_in_full_stop": [b.text for b in heads if b.text.rstrip().endswith(".")],
            "set_over_lines": [b.text for b in heads if b.splits],
            "numbered_kickers": info.get("kickers", 0),
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
        lines.append(f"{_short(b.where):>8}  {b.role:<11} {b.text}{extra}")
    return "\n".join(lines)


def render_scan(report: dict) -> str:
    r = report["rhythm"]
    lines = [
        f"== {report['source']}",
        f"   language {report['lang'] or '?'} | {report['body_words']} words of running copy | "
        f"{r['sentences']} sentences",
    ]
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

    h = report["headings"]
    shape = []
    if len(h["ending_in_full_stop"]) >= 3:
        shape.append(f"{len(h['ending_in_full_stop'])} of {h['count']} headings end in a full stop")
    if len(h["set_over_lines"]) >= 3:
        shape.append(f"{len(h['set_over_lines'])} are set over two or more lines")
    if h["numbered_kickers"] >= 3:
        shape.append(f"{h['numbered_kickers']} numbered section kickers (01, 02, ...)")
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

    if not report["findings"] and not rep and not shape and not r.get("note"):
        lines.append("\nNo flags. That proves little: read it aloud against the three tests.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="copy_audit.py",
        description=__doc__.split("\n\n", 1)[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("extract", "print the visible copy in reading order, tagged by role"),
        ("scan", "flag likely tells, repeats, heading shapes and sentence rhythm"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("inputs", nargs="+", help="HTML file, directory, URL, JSON catalog, or .txt/.md draft")
        command.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    try:
        pages = [page for target in args.inputs for page in load(target)]
    except (OSError, ValueError) as exc:  # missing file, unreachable URL, malformed JSON
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

    reports = [analyse(*page) for page in pages]
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=1))
    else:
        print("\n\n".join(render_scan(r) for r in reports))
    return 0


if __name__ == "__main__":
    sys.exit(main())
