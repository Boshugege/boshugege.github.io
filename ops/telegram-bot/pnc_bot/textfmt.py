"""Telegram text → Markdown: entity conversion, title parsing, body sanitizing.

Telegram sends message text plus a list of formatting entities whose
``offset``/``length`` count UTF-16 code units. ``entities_to_markdown`` turns
that into Markdown for notes.md. Plain text is passed through untouched (the
owner may type Markdown directly); only the formatting Telegram reports is
translated.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import urlsplit

__all__ = [
    "TITLE_MAX",
    "SAFE_LINK_SCHEMES",
    "EMPHASIS_TAGS",
    "JS_WHITESPACE",
    "entities_to_markdown",
    "split_title",
    "clean_title",
    "sanitize_body",
]

TITLE_MAX = 60
SAFE_LINK_SCHEMES = frozenset({"http", "https", "mailto", "tg"})

# Telegram entity → (Markdown marker, HTML tag used when the marker would not
# work in context; see _Marks).
_EMPHASIS = {"bold": ("**", "strong"), "italic": ("*", "em"), "strikethrough": ("~~", "del")}
EMPHASIS_TAGS = ("strong", "em", "del")
_EMPHASIS_TAG_RE = re.compile(r"</?(?:strong|em|del)>")
# JavaScript's \s (the site's regexes and micromark run in JavaScript).
JS_WHITESPACE = "\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_JS_WHITESPACE_RE = re.compile(f"[{JS_WHITESPACE}]")
_QUOTES = frozenset({"blockquote", "expandable_blockquote"})
_LANGUAGE_RE = re.compile(r"[A-Za-z0-9_+#.-]{1,32}")
# What may precede the text of a line inside an emphasised span: indentation,
# blockquote markers and a list marker. Markers go after it so the line keeps
# its block meaning (``> **x**`` rather than ``**> x**``).
_LINE_PREFIX_RE = re.compile(r"[ \t]*(?:>[ \t]?)*(?:[-+*][ \t]+|\d{1,9}[.)][ \t]+)?")


# --------------------------------------------------------------------------
# entities → Markdown


@dataclass
class _Span:
    start: int  # Python str indices
    end: int
    entity: dict
    children: list[_Span] = field(default_factory=list)

    @property
    def kind(self) -> str:
        value = self.entity.get("type")
        return value if isinstance(value, str) else ""


def _utf16_to_index(text: str) -> list[int]:
    """Map every UTF-16 offset (0..units) to a str index.

    An offset that points between the two halves of a surrogate pair is
    rounded up to the next character, so slicing never splits an emoji.
    """
    mapping: list[int] = []
    for index, char in enumerate(text):
        mapping.append(index)
        if ord(char) > 0xFFFF:
            mapping.append(index + 1)
    mapping.append(len(text))
    return mapping


def _int_field(entity: dict, key: str) -> int | None:
    value = entity.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _build_tree(text: str, entities: list[dict]) -> _Span:
    """Nest entities by containment (sorted by offset asc, length desc)."""
    mapping = _utf16_to_index(text)
    units = len(mapping) - 1
    flat: list[tuple[int, int, int, dict]] = []
    for order, entity in enumerate(entities):
        if not isinstance(entity, dict):
            continue
        offset, length = _int_field(entity, "offset"), _int_field(entity, "length")
        if offset is None or length is None or offset < 0 or length <= 0:
            continue
        start = mapping[min(offset, units)]
        end = mapping[min(offset + length, units)]
        if end > start:
            flat.append((start, -end, order, entity))
    flat.sort()

    root = _Span(0, len(text), {"type": "root"})
    stack = [root]
    for start, negative_end, _order, entity in flat:
        while len(stack) > 1 and start >= stack[-1].end:
            stack.pop()
        parent = stack[-1]
        # Telegram never sends partially overlapping entities; if it did, clip
        # to the enclosing one so the tree stays well formed (no text is lost).
        end = min(-negative_end, parent.end)
        if end <= start:
            continue
        span = _Span(start, end, entity)
        parent.children.append(span)
        stack.append(span)
    return root


def entities_to_markdown(text: str, entities: list[dict] | None) -> str:
    """Render Telegram ``text`` + ``entities`` as Markdown.

    Bold/italic/strikethrough become ``**x**`` / ``*x*`` / ``~~x~~`` where
    those markers work, and ``<strong>`` / ``<em>`` / ``<del>`` where
    CommonMark's flanking rules would leave the markers as literal text
    (``**注意：**今天``: a closing marker after punctuation and before a letter).
    """
    if not text:
        return ""
    if not entities:
        return text
    marks = _Marks(enabled=not any(ord(char) >= _SENTINEL_BASE for char in text))
    rendered = _render_children(text, _build_tree(text, entities), marks, in_link=False)
    return marks.resolve(rendered)


# Emphasis is rendered in two steps: while the tree is rendered every
# emphasised span gets a pair of private-use sentinel characters, and once
# the whole Markdown is known each pair becomes markers or HTML tags,
# depending on the characters around it. Plane 15 private-use characters
# never come from Telegram in practice; if the text has any, markers are
# used directly (the old behaviour).
_SENTINEL_BASE = 0xF0000
_SENTINEL_SPANS = (0xFFFFE - _SENTINEL_BASE) // 2
_ATTENTION_MARKERS = frozenset("*_~")  # micromark's attentionMarkers with GFM


class _Marks:
    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled
        self.kinds: list[str] = []

    def wrap(self, segment: str, kind: str) -> str:
        marker, _tag = _EMPHASIS[kind]
        if not self.enabled or len(self.kinds) >= _SENTINEL_SPANS:
            return _wrap(segment, marker, marker)
        index = len(self.kinds)
        self.kinds.append(kind)
        return _wrap(segment, chr(_SENTINEL_BASE + 2 * index), chr(_SENTINEL_BASE + 2 * index + 1))

    def resolve(self, text: str) -> str:
        if not self.kinds:
            return text
        as_html: set[int] = set()
        while True:
            out, where = self._materialise(text, as_html)
            failing = {
                index
                for index, (opening, closing) in where.items()
                if index not in as_html
                and opening is not None
                and closing is not None
                and not _markers_work(out, opening, closing, _EMPHASIS[self.kinds[index]][0])
            }
            if not failing:
                return out
            as_html |= failing  # only grows, so this ends

    def _materialise(
        self, text: str, as_html: set[int]
    ) -> tuple[str, dict[int, list[tuple[int, int] | None]]]:
        pieces: list[str] = []
        where: dict[int, list[tuple[int, int] | None]] = {}
        length = 0
        limit = _SENTINEL_BASE + 2 * len(self.kinds)
        for char in text:
            code = ord(char)
            if _SENTINEL_BASE <= code < limit:
                index, closing = divmod(code - _SENTINEL_BASE, 2)
                marker, tag = _EMPHASIS[self.kinds[index]]
                if index in as_html:
                    piece = f"</{tag}>" if closing else f"<{tag}>"
                else:
                    piece = marker
                where.setdefault(index, [None, None])[closing] = (length, length + len(piece))
            else:
                piece = char
            pieces.append(piece)
            length += len(piece)
        return "".join(pieces), where


def _char_class(char: str | None) -> int:
    """micromark's classifyCharacter: 1 whitespace, 2 punctuation, 0 other.

    micromark looks at UTF-16 code units, so a character outside the BMP
    (an emoji) is a surrogate half to it: neither whitespace nor punctuation.
    """
    if char is None or char == "\n" or _JS_WHITESPACE_RE.fullmatch(char):
        return 1
    if ord(char) > 0xFFFF:
        return 0
    return 2 if unicodedata.category(char)[0] in "PS" else 0


def _run_around(text: str, start: int, end: int, char: str) -> tuple[int, int]:
    while start > 0 and text[start - 1] == char:
        start -= 1
    while end < len(text) and text[end] == char:
        end += 1
    return start, end


def _markers_work(text: str, opening: tuple[int, int], closing: tuple[int, int], marker: str) -> bool:
    """Whether CommonMark/GFM pairs the markers at ``opening`` and ``closing``.

    Follows micromark's attention and strikethrough tokenizers: the opening
    delimiter run must be able to open and the closing one to close, given
    the characters around each whole run.
    """
    char = marker[0]
    runs = [_run_around(text, *opening, char), _run_around(text, *closing, char)]
    sides = []
    for start, end in runs:
        before = text[start - 1] if start > 0 else None
        after = text[end] if end < len(text) else None
        b, a = _char_class(before), _char_class(after)
        can_open = a == 0 or (a == 2 and b != 0)
        can_close = b == 0 or (b == 2 and a != 0)
        if char == "*":
            can_open = can_open or after in _ATTENTION_MARKERS
            can_close = can_close or before in _ATTENTION_MARKERS
        sides.append((can_open, can_close, end - start))
    (open_opens, open_closes, open_size), (close_opens, close_closes, close_size) = sides
    if not (open_opens and close_closes):
        return False
    if char == "~":
        return open_size == close_size == len(marker)
    # The "rule of 3" for runs that can both open and close.
    if (open_closes or close_opens) and close_size % 3 and (open_size + close_size) % 3 == 0:
        return False
    return True


def _render_children(text: str, span: _Span, marks: _Marks, *, in_link: bool) -> str:
    pieces: list[tuple[str, str | None]] = []
    position = span.start
    for child in span.children:
        if child.start > position:
            pieces.append((_plain(text[position : child.start], in_link), None))
        pieces.append(_render(text, child, marks, in_link=in_link))
        position = child.end
    if position < span.end:
        pieces.append((_plain(text[position : span.end], in_link), None))
    return _join(pieces)


def _plain(segment: str, in_link: bool) -> str:
    if in_link:  # a bare ] would end the link text early
        return segment.replace("[", "\\[").replace("]", "\\]")
    return segment


def _join(pieces: list[tuple[str, str | None]]) -> str:
    """Concatenate rendered pieces, keeping block constructs on their own lines.

    ``kind`` is None for inline text, ``"pre"`` for a fenced code block (must
    start and end on its own line) or ``"quote"`` for a blockquote (must also be
    followed by a blank line, or the next line would continue the quote).
    """
    out = ""
    pending_newlines = 0
    for piece, kind in pieces:
        if not piece:
            continue
        if kind and out and not out.endswith("\n") and not piece.startswith("\n"):
            out += "\n"
        if pending_newlines:
            have = (len(out) - len(out.rstrip("\n"))) + (len(piece) - len(piece.lstrip("\n")))
            out += "\n" * max(0, pending_newlines - have)
            pending_newlines = 0
        out += piece
        if kind == "pre":
            pending_newlines = 1
        elif kind == "quote":
            pending_newlines = 2
    return out


def _render(text: str, span: _Span, marks: _Marks, *, in_link: bool) -> tuple[str, str | None]:
    kind = span.kind
    if kind == "code":
        raw = text[span.start : span.end]
        if "\n" in raw:  # inline code cannot hold line breaks reliably
            return _fenced(raw, ""), "pre"
        return _code_span(raw), None
    if kind == "pre":
        language = span.entity.get("language")
        if not isinstance(language, str) or not _LANGUAGE_RE.fullmatch(language):
            language = ""
        return _fenced(text[span.start : span.end], language), "pre"

    is_link = kind == "text_link"
    url = _safe_url(span.entity.get("url")) if is_link else None
    inner = _render_children(text, span, marks, in_link=in_link or url is not None)
    if kind in _EMPHASIS:
        return _emphasise(inner, kind, marks), None
    if kind in _QUOTES:
        return _blockquote(inner), "quote"
    if url is not None:
        # One link per line, like emphasis: emphasis around a link that
        # spans lines would otherwise cross its brackets.
        closer = f"]({_link_destination(url)})"
        lines = []
        for line in inner.split("\n"):
            prefix = _LINE_PREFIX_RE.match(line).group(0)
            lines.append(prefix + _wrap(line[len(prefix) :], "[", closer))
        return "\n".join(lines), None
    # underline, spoiler, mention, hashtag, cashtag, url, email, phone_number,
    # custom_emoji, text_mention, bot_command, unknown types: plain text.
    return inner, None


def _wrap(segment: str, opener: str, closer: str) -> str:
    """Put markers around ``segment`` with its outer whitespace outside them."""
    core = segment.strip()
    if not core:
        return segment
    lead = segment[: len(segment) - len(segment.lstrip())]
    trail = segment[len(segment.rstrip()) :]
    return f"{lead}{opener}{core}{closer}{trail}"


def _emphasise(inner: str, kind: str, marks: _Marks) -> str:
    """Emphasis per line: Markdown emphasis cannot span paragraphs."""
    lines = []
    for line in inner.split("\n"):
        prefix = _LINE_PREFIX_RE.match(line).group(0)
        lines.append(prefix + marks.wrap(line[len(prefix) :], kind))
    return "\n".join(lines)


def _code_span(raw: str) -> str:
    if not raw:
        return raw
    longest = max((len(run) for run in re.findall(r"`+", raw)), default=0)
    fence = "`" * (longest + 1)
    # A space pads content that starts/ends with a backtick, and protects
    # content that starts and ends with a space (Markdown strips one of each).
    needs_pad = raw[0] == "`" or raw[-1] == "`" or (
        raw[0] == " " and raw[-1] == " " and raw.strip(" ") != ""
    )
    pad = " " if needs_pad else ""
    return f"{fence}{pad}{raw}{pad}{fence}"


def _fenced(raw: str, language: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", raw)), default=0)
    fence = "`" * max(3, longest + 1)
    body = raw if raw.endswith("\n") or not raw else raw + "\n"
    return f"{fence}{language}\n{body}{fence}"


def _blockquote(inner: str) -> str:
    if not inner.strip():
        return inner
    body = inner.strip("\n")
    lead = inner[: len(inner) - len(inner.lstrip("\n"))]
    trail = inner[len(inner.rstrip("\n")) :]
    quoted = "\n".join(f"> {line}" if line.strip() else ">" for line in body.split("\n"))
    return f"{lead}{quoted}{trail}"


def _safe_url(url: object) -> str | None:
    """Return ``url`` if it is an http(s)/mailto/tg link, else None."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url or any(ord(char) < 0x20 or ord(char) == 0x7F for char in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in SAFE_LINK_SCHEMES:
        return None
    if scheme in ("http", "https") and not parts.netloc:
        return None
    return url


_DESTINATION_ESCAPES = {" ": "%20", "(": "%28", ")": "%29", "<": "%3C", ">": "%3E", "\\": "%5C"}


def _link_destination(url: str) -> str:
    """Percent-encode the characters that would end a Markdown link target."""
    return "".join(_DESTINATION_ESCAPES.get(char, char) for char in url)


# --------------------------------------------------------------------------
# titles

_TABLE_DELIMITER_RE = re.compile(r"[\s|:-]*")
# First lines that are block syntax are never "标题 | 正文".
_BLOCK_START_RE = re.compile(r"(?:#|>|```|~~~|[-+*][ \t]|\d{1,9}[.)][ \t]|[ ]{4}|\t)")


def split_title(markdown: str) -> tuple[str | None, str]:
    """Split an optional title off the first line.

    ``# 标题`` (exactly one ``#`` and a space) on the first line is a title.
    Otherwise ``标题 | 正文…`` is: the first `` | `` with at most 60 characters
    before it, on a first line that is not a table row or other block syntax.
    The text after `` | `` becomes the first body line.
    """
    text = markdown.replace("\r\n", "\n").lstrip("\n")
    first, _, rest = text.partition("\n")

    if first.startswith("# "):
        title = first[2:].strip()
        if title:
            return title, rest.strip("\n")
        return None, text

    index = first.find(" | ")
    if 0 < index <= TITLE_MAX and not first.startswith("|"):
        title = first[:index].strip()
        second = rest.partition("\n")[0]
        looks_like_table = "|" in second and "-" in second and _TABLE_DELIMITER_RE.fullmatch(second)
        if (
            title
            and not _BLOCK_START_RE.match(first)
            and not _inside_markup(first, index)
            and not looks_like_table
        ):
            first_body = first[index + 3 :].lstrip()
            body = f"{first_body}\n{rest}" if rest else first_body
            return title, body.strip("\n")
    return None, text


def _inside_markup(line: str, index: int) -> bool:
    """Whether ``line[index]`` lies inside Markdown that a split would break.

    That is: a code span, link text or a link destination, an emphasis run
    (``**``, ``*``, ``~~``) or an emphasis tag (``<strong>``…) that opens
    before ``index`` and is not closed before it. Unbalanced plain-text
    ``*`` count as open too, which only means no title is taken.
    """
    brackets = stars = doubles = tildes = 0
    tags: list[str] = []
    position = 0
    while position < index:
        char = line[position]
        if char == "\\":
            position += 2
            continue
        if char == "`":
            run = len(re.match(r"`+", line[position:]).group(0))
            closing = _find_run(line, position + run, "`", run)
            if closing >= index:
                return True
            position = closing + run if closing >= 0 else position + run
            continue
        if char in "*~":
            run = len(re.match(re.escape(char) + "+", line[position:]).group(0))
            if char == "*":
                doubles += run // 2
                stars += run % 2
            else:
                tildes += run // 2
            position += run
            continue
        if char == "[":
            brackets += 1
        elif char == "]":
            brackets = max(0, brackets - 1)
            if line.startswith("(", position + 1):
                end = _link_destination_end(line, position + 1)
                if end < 0 or end >= index:
                    return True
                position = end + 1
                continue
        elif char == "<":
            tag = _EMPHASIS_TAG_RE.match(line, position)
            if tag:
                name = tag.group(0).strip("</>")
                if tag.group(0).startswith("</"):
                    if tags and tags[-1] == name:
                        tags.pop()
                else:
                    tags.append(name)
                position = tag.end()
                continue
        position += 1
    return bool(brackets or stars % 2 or doubles % 2 or tildes % 2 or tags)


def _find_run(line: str, start: int, char: str, length: int) -> int:
    """Index of the next run of exactly ``length`` ``char``, or -1."""
    for match in re.finditer(re.escape(char) + "+", line[start:]):
        if len(match.group(0)) == length:
            return start + match.start()
    return -1


def _link_destination_end(line: str, start: int) -> int:
    """Index of the ``)`` that closes the ``(`` at ``start``, or -1."""
    depth = 0
    position = start
    while position < len(line):
        char = line[position]
        if char == "\\":
            position += 2
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return position
        position += 1
    return -1


def clean_title(title: str) -> str:
    """Normalise a note/draft title; raise ValueError (Chinese) if unusable.

    Whitespace collapses to single spaces; Markdown decoration at the ends goes
    (leading ``#``, a closing `` ##`` sequence, ``*`` and backticks), but a
    ``#`` that is part of a word (``C#``) stays. Emphasis tags
    (``<strong>``…, see entities_to_markdown) and U+FEFF go everywhere: a
    title is plain text.
    """
    cleaned = " ".join(_EMPHASIS_TAG_RE.sub("", title.replace("\ufeff", "")).split())
    previous = None
    while cleaned != previous:
        previous = cleaned
        cleaned = cleaned.lstrip("#*` ").rstrip("*` ")
        cleaned = re.sub(r"\s+#+$", "", cleaned)
    if not cleaned:
        raise ValueError("标题不能为空")
    if len(cleaned) > TITLE_MAX:
        raise ValueError(f"标题最多 {TITLE_MAX} 个字")
    return cleaned


# --------------------------------------------------------------------------
# body

# notes.ts splits notes.md at /^##\s+(\d{4}-\d{2}-\d{2})…/m. Its \s is
# JavaScript's (it includes U+FEFF; Python's does not) and also matches
# newlines, so "##" alone followed by a date line splits too. Python's \s and
# \d are added on top: escaping a line notes.ts would not split on is harmless.
_NOTE_START_RE = re.compile(rf"##[\s{JS_WHITESPACE}]+\d{{4}}-\d{{2}}-\d{{2}}")
_BLANK_RE = re.compile(rf"[\s{JS_WHITESPACE}]*")
_LEADING_SPACE_RE = re.compile(rf"^[\s{JS_WHITESPACE}]+")
_FENCE_OPEN_RE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
# An ATX heading: up to 3 spaces, 1-6 #, then whitespace or the end of the line.
_ATX_HEADING_RE = re.compile(r" {0,3}(#{1,6})(?=[ \t]|$)[ \t]*(.*)")
_ATX_CLOSING_RE = re.compile(r"(?:^|[ \t]+)#+$")


def sanitize_body(body: str) -> str:
    """Make Markdown safe to embed as one note in notes.md.

    - line endings become ``\\n`` (also lone ``\\r`` and U+2028/2029, which
      JavaScript treats as line breaks); U+FEFF is removed;
    - a line that would start a new note (``## 2026-01-01…``) gets a leading
      backslash, even inside code blocks, because notes.ts does not know
      about code blocks;
    - other ``#`` / ``##`` headings become ``###`` and headings without text
      become blank lines (outside code blocks);
    - trailing spaces are removed, 3+ blank lines collapse to 2, and the
      result is stripped (the start before the heading rules: notes.ts trims
      each body before rendering it, so an indented first line is no code).
    """
    text = body.replace("\r\n", "\n").replace("\ufeff", "")
    for separator in ("\r", "\u2028", "\u2029"):
        text = text.replace(separator, "\n")
    # notes.ts trims each note's body before rendering it, so the first line
    # loses its indentation ("    # x" is a heading there, not code).
    lines = _LEADING_SPACE_RE.sub("", text).split("\n")

    out: list[str] = []
    fence: tuple[str, int] | None = None
    for index, line in enumerate(lines):
        line = line.rstrip(" \t")
        if line.startswith("##") and _starts_note(lines, index):
            out.append("\\" + line)
            continue
        if fence is None:
            opened = _FENCE_OPEN_RE.fullmatch(line)
            heading = _ATX_HEADING_RE.match(line)
            if opened and not (opened.group(1)[0] == "`" and "`" in opened.group(2)):
                fence = (opened.group(1)[0], len(opened.group(1)))
            elif heading:
                if not _ATX_CLOSING_RE.sub("", heading.group(2)).strip():
                    line = ""  # "#", "## ##": an empty heading
                elif len(heading.group(1)) <= 2:
                    line = "### " + heading.group(2)
        elif _closes_fence(line, fence):
            fence = None
        out.append(line)

    collapsed: list[str] = []
    blank_run = 0
    for line in out:
        blank_run = blank_run + 1 if line == "" else 0
        if blank_run <= 2:
            collapsed.append(line)
    return "\n".join(collapsed).strip()


def _starts_note(lines: list[str], index: int) -> bool:
    line = lines[index]
    if _NOTE_START_RE.match(line):
        return True
    if not _BLANK_RE.fullmatch(line[2:]):
        return False
    # "##" + only whitespace: \s+ may run over blank lines into a date on a
    # later line.
    following = [line]
    for later in lines[index + 1 :]:
        following.append(later)
        if not _BLANK_RE.fullmatch(later):
            break
    return _NOTE_START_RE.match("\n".join(following)) is not None


def _closes_fence(line: str, fence: tuple[str, int]) -> bool:
    char, length = fence
    stripped = line.strip()
    indent = len(line) - len(line.lstrip(" "))
    return (
        indent <= 3
        and len(stripped) >= length
        and stripped == char * len(stripped)
    )
