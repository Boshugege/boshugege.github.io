"""Pure functions over notes.md and new-post drafts.

Mirrors the site code: note ids follow ``src/lib/notes.ts``, new notes are
placed like ``scripts/new-note.mjs`` does, drafts look like the output of
``scripts/new-post.mjs``. Nothing here touches git; only
``missing_note_assets`` reads files (under the static dir it is given).
"""

from __future__ import annotations

import html
import html.entities
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .textfmt import _FENCE_OPEN_RE, JS_WHITESPACE, _closes_fence, clean_title

__all__ = [
    "NOTES_PATH",
    "NOTES_ASSET_DIR",
    "POSTS_DIR",
    "HEADING_RE",
    "SITE_HEADING_PATTERN",
    "NOTE_SEPARATOR",
    "SLUG_RE",
    "Note",
    "parse_notes",
    "today_shanghai",
    "insert_note",
    "append_to_note",
    "image_markdown",
    "note_summary",
    "make_slug",
    "parse_draft_command",
    "draft_document",
    "validate_notes",
    "referenced_note_images",
    "escape_mdx",
    "STATIC_DIR",
    "local_asset_path",
    "note_image_assets",
    "missing_note_assets",
]

NOTES_PATH = "src/content/notes.md"
NOTES_ASSET_DIR = "src/static/assets/notes"
POSTS_DIR = "src/content/posts"

STATIC_DIR = "src/static"  # Astro's publicDir: /assets/x is src/static/assets/x

# The note heading pattern of src/lib/notes.ts (flags gm), and HEADING_RE: the
# same pattern spelled out with JavaScript's meaning, where \s includes U+FEFF
# (but not U+001C-U+001F or U+0085), \d is ASCII, and ^ / $ / . treat \r,
# U+2028 and U+2029 as line breaks too. Both must split notes.md the same way.
SITE_HEADING_PATTERN = r"^##\s+(\d{4}-\d{2}-\d{2})(?:\s+\|\s*(.+))?\s*$"
_JS_LINE_BREAKS = "\n\r\u2028\u2029"
HEADING_RE = re.compile(
    rf"(?:^|(?<=[\r\u2028\u2029]))##[{JS_WHITESPACE}]+([0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})"
    rf"(?:[{JS_WHITESPACE}]+\|[{JS_WHITESPACE}]*([^{_JS_LINE_BREAKS}]+))?[{JS_WHITESPACE}]*(?=[{_JS_LINE_BREAKS}]|\Z)",
    re.MULTILINE,
)
# notes.ts trims titles with String.prototype.trim (JavaScript whitespace).
_JS_TRIM = "".join(chr(code) for code in range(0x2000, 0x200B)) + "\t\n\v\f\r \u00a0\u1680\u2028\u2029\u202f\u205f\u3000\ufeff"
# Python's whitespace and JavaScript's, at both ends.
_TRIM_RE = re.compile(rf"^[\s{JS_WHITESPACE}]+|[\s{JS_WHITESPACE}]+$")
# Where scripts/new-note.mjs inserts a new note (before the first match).
_FIRST_HEADING_RE = re.compile(r"(?:^|(?<=[\r\u2028\u2029]))## [0-9]{4}-[0-9]{2}-[0-9]{2}", re.MULTILINE)
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SLUG_MAX = 50
_EXPLICIT_SLUG_MAX = 100
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_SHANGHAI = ZoneInfo("Asia/Shanghai")

# new-post.mjs template pieces.
DRAFT_HINT = "{/* 图片放在同一目录，用 ![说明](./image.png) 引用。 */}"
DRAFT_PLACEHOLDER = "在这里写正文。"


@dataclass(frozen=True)
class Note:
    id: str
    date: str
    title: str | None
    heading_start: int
    body_start: int
    end: int
    body: str


def parse_notes(raw: str) -> list[Note]:
    """Notes in file order (newest first), with the ids notes.ts gives them.

    The oldest note of a day gets the bare date, later ones ``date-2``,
    ``date-3``… counted from the bottom of the file. Offsets index ``raw``.
    """
    matches = list(HEADING_RE.finditer(raw))
    ids = [""] * len(matches)
    seen: Counter[str] = Counter()
    for index in range(len(matches) - 1, -1, -1):
        date = matches[index].group(1)
        seen[date] += 1
        ids[index] = date if seen[date] == 1 else f"{date}-{seen[date]}"

    notes = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        notes.append(
            Note(
                id=ids[index],
                date=match.group(1),
                title=(match.group(2) or "").strip(_JS_TRIM) or None,
                heading_start=match.start(),
                body_start=match.end(),
                end=end,
                body=raw[match.end() : end].strip(_JS_TRIM),
            )
        )
    return notes


def today_shanghai(now: datetime | None = None) -> str:
    """Today's date in Asia/Shanghai as ``YYYY-MM-DD``."""
    moment = now if now is not None else datetime.now(timezone.utc)
    return moment.astimezone(_SHANGHAI).strftime("%Y-%m-%d")


# A line that renders as nothing. It goes between a heading without a title
# and a body that starts with "|": otherwise the heading pattern's
# "\s+\|\s*(.+)" runs over the blank line and takes the body's first line
# (a table's header row) as the note's title.
NOTE_SEPARATOR = "<!-- -->"


def insert_note(raw: str, *, date: str, title: str | None, body: str) -> tuple[str, str]:
    """Add a note on top, like scripts/new-note.mjs; return (new raw, new note id).

    The new note must parse back (as notes.ts would) with exactly this title
    and body; a title-less body starting with ``|`` gets NOTE_SEPARATOR first.
    """
    if not _DATE_RE.fullmatch(date):
        raise ValueError("日期格式不对")
    body = _TRIM_RE.sub("", body)
    if not body:
        raise ValueError("随想正文不能为空")
    title = _TRIM_RE.sub("", " ".join(title.split())) if title else ""

    if not title and body.startswith("|"):
        body = f"{NOTE_SEPARATOR}\n\n{body}"
    new_raw, added = _insert_entry(raw, date, title, body)
    if added.title != (title or None) or added.body != body:
        raise ValueError("新随想的标题或正文解析出来不对（第一行别用 | 开头，或者加个标题）")
    return new_raw, added.id


def _insert_entry(raw: str, date: str, title: str, body: str) -> tuple[str, Note]:
    heading = f"## {date} | {title}" if title else f"## {date}"
    entry = f"{heading}\n\n{body}\n\n"

    first = _FIRST_HEADING_RE.search(raw)
    if first:
        position = first.start()
        new_raw = raw[:position] + entry + raw[position:]
    else:
        prefix = f"{raw.rstrip()}\n\n"
        position = len(prefix)
        new_raw = prefix + entry

    before, after = parse_notes(raw), parse_notes(new_raw)
    if len(after) != len(before) + 1:
        raise ValueError("正文里有会被当成随想标题的行")
    added = next((note for note in after if note.heading_start == position), None)
    if added is None:  # pragma: no cover - guarded by the count check above
        raise ValueError("新随想没有插入到预期位置")
    return new_raw, added


def append_to_note(raw: str, note_id: str, addition: str) -> str:
    """Append ``addition`` as new paragraph(s) at the end of note ``note_id``.

    The note keeps its title (a title-less, empty note gets NOTE_SEPARATOR
    before an addition starting with ``|``). Raises KeyError if there is no
    such note.
    """
    addition = _TRIM_RE.sub("", addition)
    if not addition:
        raise ValueError("追加的内容不能为空")
    notes = parse_notes(raw)
    note = next((item for item in notes if item.id == note_id), None)
    if note is None:
        raise KeyError(note_id)

    if note.title is None and not note.body and addition.startswith("|"):
        addition = f"{NOTE_SEPARATOR}\n\n{addition}"
    new_raw, appended = _append_entry(raw, note, addition)
    if appended.title != note.title or not appended.body.startswith(note.body):
        raise ValueError("追加后这条随想的标题会变（内容别用 | 开头）")
    return new_raw


def _append_entry(raw: str, note: Note, addition: str) -> tuple[str, Note]:
    head = raw[: note.end].rstrip()
    rest = raw[note.end :]
    new_raw = f"{head}\n\n{addition}\n\n{rest}" if rest else f"{head}\n\n{addition}\n"
    notes = parse_notes(new_raw)
    if len(notes) != len(parse_notes(raw)):
        raise ValueError("追加的内容里有会被当成随想标题的行")
    return new_raw, next(item for item in notes if item.heading_start == note.heading_start)


def image_markdown(names: list[str], alt: str, prefix: str = "/assets/notes/") -> str:
    """One ``![alt](prefix+name)`` per paragraph."""
    alt = " ".join(alt.split())
    alt = alt.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    for name in names:
        if not name or re.search(r"[\s()<>\\]", name):
            raise ValueError(f"bad image name: {name!r}")
    return "\n\n".join(f"![{alt}]({prefix}{name})" for name in names)


_IMAGE_MD_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_MD_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_LINE_MARKER_RE = re.compile(r"^(?:#{1,6}\s+|>\s?|[-+*]\s+|\d{1,9}[.)]\s+)+")
# HTML the bot itself writes: emphasis tags and NOTE_SEPARATOR.
_BOT_HTML_RE = re.compile(r"</?(?:strong|em|del)>|<!--.*?-->")


def note_summary(note: Note, limit: int = 40) -> str:
    """The note's title, else its first line of text, shortened to ``limit``."""
    if note.title:
        return _shorten(note.title, limit)
    for line in note.body.split("\n"):
        text = line.strip()
        if text.startswith(("```", "~~~")):
            continue
        text = _BOT_HTML_RE.sub("", text)
        text = _LINE_MARKER_RE.sub("", text)
        text = _IMAGE_MD_RE.sub("", text)
        text = _LINK_MD_RE.sub(r"\1", text)
        text = text.replace("**", "").replace("~~", "").replace("`", "").replace("\\", "")
        text = " ".join(text.split())
        if text:
            return _shorten(text, limit)
    return "（图片）" if "![" in note.body else note.id


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def make_slug(title: str, existing: set[str], now: datetime) -> str:
    """A free post slug from the ASCII words of ``title``.

    Falls back to ``post-YYYYMMDD-HHMM`` (Asia/Shanghai) when the title has
    fewer than 3 usable characters; adds ``-2``, ``-3``… on collisions.
    """
    slug = ""
    for word in re.findall(r"[A-Za-z0-9]+", title):
        candidate = f"{slug}-{word.lower()}" if slug else word.lower()
        if len(candidate) > SLUG_MAX:
            if not slug:
                slug = candidate[:SLUG_MAX]
            break
        slug = candidate
    if len(slug) < 3:
        slug = now.astimezone(_SHANGHAI).strftime("post-%Y%m%d-%H%M")

    base, number = slug, 1
    while slug in existing:
        number += 1
        slug = f"{base}-{number}"
    return slug


def parse_draft_command(arg: str) -> tuple[str, str | None, str]:
    """``标题`` or ``标题 | my-slug`` on the first line, body below.

    Returns (clean title, explicit slug or None, body). Raises ValueError with
    a Chinese message for a missing/too long title or an invalid slug.
    """
    text = arg.replace("\r\n", "\n").strip()
    if not text:
        raise ValueError("用法：/draft 标题 [| slug]，正文写在下一行")
    first, _, rest = text.partition("\n")
    title, separator, slug_text = first.rpartition("|")
    if not separator:
        title, slug_text = first, ""
    slug = slug_text.strip() or None
    if slug is not None and (len(slug) > _EXPLICIT_SLUG_MAX or not SLUG_RE.fullmatch(slug)):
        raise ValueError("slug 只能包含小写字母、数字和连字符，并以字母或数字开头")
    return clean_title(title), slug, rest.strip("\n").rstrip()


def draft_document(title: str, date: str, body: str, image_names: list[str]) -> str:
    """``index.mdx`` for a new draft: the new-post.mjs template plus content.

    The body is escaped for MDX (see escape_mdx), so a draft can never break
    the site build. Images follow as ``![](./name)``. Image references in the
    body must load at build time: Astro imports every relative image, and a
    file other than the attached ones (whose names the owner cannot know)
    would be missing, so those raise ValueError (Chinese).
    """
    if not _DATE_RE.fullmatch(date):
        raise ValueError("日期格式不对")
    if not title or "\n" in title:
        raise ValueError("标题不能为空")
    for name in image_names:
        if not name or re.search(r"[\s()<>\\]", name):
            raise ValueError(f"bad image name: {name!r}")
    frontmatter = (
        "---\n"
        f"title: {json.dumps(title, ensure_ascii=False)}\n"
        f'date: "{date}"\n'
        "tags: []\n"
        'excerpt: ""\n'
        "draft: true\n"
        "---\n\n"
    )
    content, outside_code = _escape_mdx(_normalise_newlines(body).strip())
    allowed = {f"./{name}" for name in image_names}
    for url in _image_destinations(outside_code):
        problem = _draft_image_problem(url, allowed)
        if problem:
            raise ValueError(problem)
    parts = [DRAFT_HINT, content or DRAFT_PLACEHOLDER]
    parts.extend(f"![](./{name})" for name in image_names)
    return frontmatter + "\n\n".join(parts) + "\n"


def _normalise_newlines(text: str) -> str:
    """MDX (micromark) breaks lines at \\r\\n, \\r and \\n."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


# --------------------------------------------------------------------------
# MDX escaping
#
# MDX adds three things to Markdown, and any stray one fails the build: JSX
# (``<``), expressions (``{``/``}``) and ESM (a line starting ``import `` or
# ``export ``). escape_mdx neutralises all of them except inside code and
# math, where they are literal and an escape would show. It only leaves text
# unescaped where MDX is certain to read it as code or math: wherever the
# structure is not certain (fences inside lists or quotes, code spans next to
# links, tables or URLs, unpaired backticks…), it escapes everything. A
# character reference shown inside code is the worst outcome.

# Character references, not backslashes: a GFM autolink literal swallows a
# backslash ("https://x.y/\\<b>" is a link to ".../\\" and then JSX), while
# a reference leaves no "{", "}" or "<" for MDX to start anything with.
_MDX_SPECIAL = {"{": "&#123;", "}": "&#125;", "<": "&lt;"}
# MDX turns off indented code, so a fence may be indented any amount.
_MDX_FENCE_RE = re.compile(r"([ \t]*)(`{3,}|~{3,}|\${2,})(.*)")
_MDX_FENCE_LIKE_RE = re.compile(r"[ \t]*(?:`{3}|~{3}|\${2})")
# Lines that open or continue a container (block quote, list item, footnote).
_MDX_CONTAINER_RE = re.compile(r"[ \t]*(?:>|[-+*](?=[ \t]|$)|[0-9]{1,9}[.)](?=[ \t]|$)|\[\^[^\]]*\]:)[ \t]?")
_MDX_ESM_RE = re.compile(r"(?:import|export)\b")
_MDX_TABLE_DELIMITER_RE = re.compile(r"[ \t]*\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*")
_EMPHASIS_OPEN_RE = re.compile(r"<(strong|em|del)>")
_EMPHASIS_MARKERS = {"strong": "**", "em": "*", "del": "~~"}
# What an emphasis tag pair may enclose to pass through as JSX: no Markdown
# that could start inside the pair and end outside it (or the reverse).
_EMPHASIS_PLAIN_RE = re.compile(r"[^<>{}\[\]*_~`$|\\!\n]+")


def escape_mdx(markdown: str) -> str:
    """Neutralise MDX syntax outside code and math.

    ``{``, ``}`` and ``<`` (also backslash-escaped ones) become character
    references, and so does the ``i``/``e`` of a line starting
    ``import``/``export``. The emphasis tags entities_to_markdown writes
    (``<strong>x</strong>``…) stay JSX when they wrap plain text on one line,
    and turn back into ``**``/``*``/``~~`` otherwise.
    """
    return _escape_mdx(_normalise_newlines(markdown))[0]


def _escape_mdx(markdown: str) -> tuple[str, str]:
    """(escaped Markdown, the same with certain code and math blanked out)."""
    lines = markdown.split("\n")
    kinds = _mdx_line_kinds(lines)
    escaped: list[str] = []
    outside_code: list[str] = []
    index = 0
    while index < len(lines):
        if kinds[index] == "code" or not lines[index].strip(" \t"):
            escaped.append(lines[index])
            outside_code.append("" if kinds[index] == "code" else lines[index])
            index += 1
            continue
        end = index
        while end < len(lines) and kinds[end] != "code" and lines[end].strip(" \t"):
            end += 1
        chunk = [_neutralise_esm(line) for line in lines[index:end]]
        certain = all(kind == "text" for kind in kinds[index:end])
        spans = _mdx_chunk_spans(chunk) if certain else None
        for number, line in enumerate(chunk):
            text, masked = _escape_mdx_inline(line, spans[number] if spans else [])
            escaped.append(text)
            outside_code.append(masked)
        index = end
    return "\n".join(escaped), "\n".join(outside_code)


def _mdx_line_kinds(lines: list[str]) -> list[str]:
    """Each line: "code" (MDX surely reads it as fenced code or math),
    "text", or "unsure" (from the first line whose structure is unclear on)."""
    kinds: list[str] = []
    fence: tuple[str, int] | None = None
    seen_container = False
    unsure = False
    for line in lines:
        if unsure:
            kinds.append("unsure")
            continue
        if fence is not None:
            kinds.append("code")
            char, size = fence
            closing = re.fullmatch(rf"[ \t]*({re.escape(char)}+)[ \t]*", line)
            if closing and len(closing.group(1)) >= size:
                fence = None
            continue
        if _MDX_CONTAINER_RE.match(line):
            seen_container = True
            rest = line
            while prefix := _MDX_CONTAINER_RE.match(rest):
                rest = rest[prefix.end() :]
            if _MDX_FENCE_LIKE_RE.match(rest):  # a fence inside a container
                unsure = True
                kinds.append("unsure")
            else:
                kinds.append("text")
            continue
        opened = _MDX_FENCE_RE.fullmatch(line)
        if opened:
            indent, marker, info = opened.groups()
            char = marker[0]
            if char in "`$" and char in info:
                kinds.append("text")  # not a fence: a paragraph line
                continue
            if "\t" in indent or (indent and seen_container):
                unsure = True  # maybe a fence inside a list item
                kinds.append("unsure")
                continue
            fence = (char, len(marker))
            kinds.append("code")
            continue
        kinds.append("text")
    return kinds


def _neutralise_esm(line: str) -> str:
    if _MDX_ESM_RE.match(line):
        return f"&#{ord(line[0])};{line[1:]}"
    return line


def _mdx_chunk_spans(chunk: list[str]) -> list[list[tuple[int, int]]] | None:
    """Code spans and inline math per line of a paragraph-like chunk, when
    MDX is certain to read them the same way; None when it may not be."""
    if any("|" in line and _MDX_TABLE_DELIMITER_RE.fullmatch(line) for line in chunk):
        return None  # table cells split at | even inside code spans
    spans = []
    for line in chunk:
        found = _mdx_line_spans(line)
        if found is None:
            return None
        outside, position = [], 0
        for start, end in found:
            outside.append(line[position:start])
            position = end
        outside.append(line[position:])
        if any("](" in part or "]:" in part for part in outside):
            return None  # a backtick or $ in a link destination or title is literal
        spans.append(found)
    return spans


def _mdx_line_spans(line: str) -> list[tuple[int, int]] | None:
    """Code spans and inline math on one line, or None if a run is unpaired
    on this line (it may pair with one on another line) or follows a URL
    (a GFM autolink literal may swallow it)."""
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(line):
        char = line[index]
        if char == "\\":
            index += 2
            continue
        if char in "`$":
            run = len(re.match(re.escape(char) + "+", line[index:]).group(0))
            token = re.split(rf"[{JS_WHITESPACE}]", line[:index])[-1].lower()
            if "://" in token or "www." in token:
                return None
            closing = _find_run(line, index + run, char, run)
            if closing < 0:
                return None
            spans.append((index, closing + run))
            index = closing + run
            continue
        index += 1
    return spans


def _find_run(line: str, start: int, char: str, length: int) -> int:
    """Index of the next run of exactly ``length`` ``char``, or -1."""
    for match in re.finditer(re.escape(char) + "+", line[start:]):
        if len(match.group(0)) == length:
            return start + match.start()
    return -1


def _escape_mdx_inline(line: str, spans: list[tuple[int, int]]) -> tuple[str, str]:
    """Escape one line; ``spans`` (code/math) stay as they are.

    Returns (escaped line, escaped line with the spans blanked out).
    """
    out: list[str] = []
    masked: list[str] = []
    starts = {start: end for start, end in spans}
    closing_markers: dict[int, tuple[int, str]] = {}
    index = 0
    while index < len(line):
        if index in starts:
            end = starts[index]
            out.append(line[index:end])
            masked.append(" " * (end - index))
            index = end
            continue
        char = line[index]
        piece = char
        step = 1
        if char == "\\" and index + 1 < len(line):
            following = line[index + 1]
            piece, step = _MDX_SPECIAL.get(following, char + following), 2
        elif index in closing_markers:
            step, piece = closing_markers.pop(index)
        elif char == "<" and (tag := _EMPHASIS_OPEN_RE.match(line, index)):
            name = tag.group(1)
            close = line.find(f"</{name}>", tag.end())
            inner = line[tag.end() : close] if close > tag.end() else ""
            if inner and _EMPHASIS_PLAIN_RE.fullmatch(inner):
                step = close + len(name) + 3 - index
                piece = line[index : index + step]
            elif inner and not any(tag.end() < end and start < close for start, end in spans):
                piece, step = _EMPHASIS_MARKERS[name], tag.end() - index
                closing_markers[close] = (len(name) + 3, piece)
            else:
                piece = _MDX_SPECIAL[char]
        elif char in _MDX_SPECIAL:
            piece = _MDX_SPECIAL[char]
        out.append(piece)
        masked.append(piece)
        index += step
    return "".join(out), "".join(masked)


# --------------------------------------------------------------------------
# image references
#
# What the site build does with a Markdown image: Astro's MDX pipeline
# (remark-collect-images) calls decodeURI on every image URL (a malformed
# %-sequence throws), leaves URLs and /absolute paths alone, and imports any
# other path, which fails when the file is missing. notes.ts sizes images
# under /assets/ (src/lib/note-images.ts) and fails when the file is missing
# or not an image.


def _image_destinations(markdown: str) -> list[str]:
    """URLs of Markdown images, as mdast has them (escapes and character
    references decoded): ``![alt](url)`` anywhere, and every definition
    ``[label]: url`` when the text has an image at all (``![alt][label]``).

    Liberal on purpose: it may report a URL that Markdown would not make an
    image of, never the reverse for the forms above.
    """
    urls: list[str] = []
    for match in re.finditer(r"(?<!\\)(?:\\\\)*!\[", markdown):
        label_end = _label_end(markdown, match.end())
        if label_end < 0 or not markdown.startswith("(", label_end + 1):
            continue
        url = _destination(markdown, label_end + 2)
        if url is not None:
            urls.append(url)
    if "![" in markdown:
        # Anywhere, not just at the start of a line: definitions may sit in
        # block quotes and list items.
        definitions = re.finditer(r"\[(?:[^\]\\\n]|\\.)+\]:[ \t]*(?:\n[ \t>]*)?(<[^<>\n]*>|\S+)", markdown)
        for match in definitions:
            raw = match.group(1)
            urls.append(_decode_markdown(raw[1:-1] if raw.startswith("<") else raw))
    return urls


def _label_end(text: str, start: int) -> int:
    """Index of the ``]`` that closes a link label starting at ``start``."""
    depth = 0
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            if depth == 0:
                return index
            depth -= 1
        index += 1
    return -1


def _destination(text: str, start: int) -> str | None:
    """The link destination after ``(`` at ``start``, decoded, or None."""
    index = start
    while index < len(text) and text[index] in " \t":
        index += 1
    if text.startswith("\n", index):
        index += 1
        while index < len(text) and text[index] in " \t>":  # also a block quote's prefix
            index += 1
    if text.startswith("<", index):
        match = re.match(r"<((?:[^<>\n\\]|\\.)*)>", text[index:])
        return _decode_markdown(match.group(1)) if match else None
    depth = 0
    end = index
    while end < len(text):
        char = text[end]
        if char == "\\" and end + 1 < len(text) and text[end + 1] != "\n":
            end += 2
            continue
        if ord(char) <= 0x20 or ord(char) == 0x7F:
            break
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                break
            depth -= 1
        end += 1
    return _decode_markdown(text[index:end])


_MARKDOWN_ESCAPE_RE = re.compile(
    r"\\([!-/:-@\[-`{-~])|&(#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{0,31});"
)


def _decode_markdown(raw: str) -> str:
    def decode(match: re.Match[str]) -> str:
        if match.group(1) is not None:
            return match.group(1)
        name = match.group(2)
        if name.startswith("#"):
            return html.unescape(match.group(0))
        return html.entities.html5.get(name + ";", match.group(0))

    return _MARKDOWN_ESCAPE_RE.sub(decode, raw)


_URI_RESERVED = frozenset(";/?:@&=+$,#")


def _js_decode_uri(value: str) -> str | None:
    """JavaScript's decodeURI, or None where it throws URIError."""
    out: list[str] = []
    index = 0
    while index < len(value):
        if value[index] != "%":
            out.append(value[index])
            index += 1
            continue
        data = bytearray()
        start = index
        while True:
            pair = value[index + 1 : index + 3]
            if len(pair) != 2 or not all(char in "0123456789abcdefABCDEF" for char in pair):
                return None
            data.append(int(pair, 16))
            index += 3
            first = data[0]
            if first < 0x80:
                break
            expected = 2 if first >> 5 == 0b110 else 3 if first >> 4 == 0b1110 else 4 if first >> 3 == 0b11110 else 0
            if not expected:
                return None
            if len(data) == expected:
                break
            if not value.startswith("%", index):
                return None
        try:
            char = bytes(data).decode("utf-8")
        except UnicodeDecodeError:
            return None
        out.append(value[start:index] if char in _URI_RESERVED else char)
    return "".join(out)


_SPECIAL_SCHEMES = frozenset({"http", "https", "ws", "wss", "ftp"})
_HOST_RE = re.compile(
    r"(?P<host>(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)"
    r"(?::(?P<port>[0-9]{1,5}))?(?:[/?#\\].*)?",
    re.DOTALL,
)


def _is_url(value: str) -> bool:
    """Whether JavaScript's URL.canParse(value) is true, erring towards False."""
    match = re.match(r"([A-Za-z][A-Za-z0-9+.-]*):", value)
    if not match or ord(value[-1]) <= 0x20:
        return False
    scheme, rest = match.group(1).lower(), value[match.end() :]
    if scheme == "file":
        return False
    if scheme not in _SPECIAL_SCHEMES and not rest.startswith("//"):
        return True  # an opaque path: anything goes
    if not rest.startswith("//"):
        return False
    host = _HOST_RE.fullmatch(rest[2:])
    if not host or (host.group("port") and int(host.group("port")) > 65535):
        return False
    labels = host.group("host").lower().split(".")
    if any(label.startswith("xn--") for label in labels):
        return False
    if labels[-1].isdigit() or labels[-1].startswith("0x"):
        # The URL parser reads this host as IPv4 and rejects anything but a
        # plain dotted quad here.
        return len(labels) == 4 and all(
            label.isdigit() and int(label) <= 255 and (label == "0" or not label.startswith("0")) for label in labels
        )
    return True


def _draft_image_problem(url: str, allowed: set[str]) -> str | None:
    decoded = _js_decode_uri(url)
    if decoded is None:
        return f"图片地址里的 % 编码不对：{url}"
    if not decoded or _is_url(decoded) or decoded.startswith("/") or decoded in allowed:
        return None
    return f"草稿引用的图片不存在：{url}（附图会自动放在文末）"


def _normalize_uri(value: str) -> str:
    """micromark-util-sanitize-uri's normalizeUri: the src a hast image gets."""
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "%" and value[index + 1 : index + 3].isascii() and value[index + 1 : index + 3].isalnum() and len(value[index + 1 : index + 3]) == 2:
            out.append(value[index : index + 3])
            index += 3
            continue
        if ord(char) < 128 and re.fullmatch(r"[!#$&-;=?-Z_a-z~]", char):
            out.append(char)
        else:
            out.append("".join(f"%{byte:02X}" for byte in char.encode("utf-8", "surrogatepass")))
        index += 1
    return "".join(out)


def local_asset_path(src: str) -> str | None:
    """src/lib/note-images.ts localAssetPath: the static-dir-relative path an
    image src such as /assets/notes/x.webp points at, or None."""
    if not src.startswith("/assets/"):
        return None
    decoded = _js_decode_uri(re.sub(r"[?#].*", "", src, flags=re.S))
    if decoded is None:
        return None
    segments = decoded[1:].split("/")
    if any(segment in ("", ".", "..") or "\\" in segment for segment in segments):
        return None
    return "/".join(segments)


def note_image_assets(markdown: str) -> list[str]:
    """Static-dir-relative paths of the local images a note shows, in order,
    unique: every Markdown image under /assets/ (what note-images.ts sizes,
    and fails the build on when the file is missing)."""
    paths: list[str] = []
    for url in _image_destinations(markdown):
        path = local_asset_path(_normalize_uri(url))
        if path and path not in paths:
            paths.append(path)
    return paths


_IMAGE_SIGNATURES = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM", b"II*\x00", b"MM\x00*", b"\x00\x00\x01\x00")


def _looks_like_image(head: bytes) -> bool:
    if head.startswith(_IMAGE_SIGNATURES):
        return True
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return True
    if head[4:8] == b"ftyp":  # AVIF / HEIF
        return True
    return b"<svg" in head.lower()


def missing_note_assets(markdown: str, static_dir: Path) -> list[str]:
    """The ``/assets/…`` image srcs in ``markdown`` that would fail the site
    build: no such file under ``static_dir`` (``<workdir>/src/static``), or a
    file that is not an image."""
    root = Path(static_dir).resolve()
    missing: list[str] = []
    for path in note_image_assets(markdown):
        file = (root / path).resolve()
        try:
            inside = file.is_relative_to(root) and file.is_file()
            head = file.read_bytes()[:4096] if inside else b""
        except OSError:
            inside, head = False, b""
        if not inside or not _looks_like_image(head):
            missing.append("/" + path)
    return missing


_DATE_LIKE_HEADING_RE = re.compile(rf"##[\s{JS_WHITESPACE}]+\d{{4}}[-/.年]\d{{1,2}}")


def validate_notes(before: str, after: str, *, expect_added: int) -> None:
    """Check an edited notes.md; raise ValueError (Chinese) if it looks wrong.

    - every new line that looks like a note heading (``## `` + a date-like
      token) must be a real heading that notes.ts recognises, unless it is
      inside a fenced code block (notes.ts never splits there: sanitize_body
      escapes every line it could split on, fenced or not);
    - the number of notes grew by exactly ``expect_added``;
    - every existing note id still names the same note (shared links keep
      working): same title, and a body that is unchanged or only extended.
    """
    old_lines = Counter(before.split("\n"))
    lines = after.split("\n")
    for number, (line, fenced) in enumerate(zip(lines, _fenced_lines(lines)), start=1):
        if old_lines[line] > 0:
            old_lines[line] -= 1
            continue
        if fenced:
            continue
        if _DATE_LIKE_HEADING_RE.match(line) and not HEADING_RE.fullmatch(line):
            raise ValueError(f"第 {number} 行像随想标题，但格式不对（应为 ## YYYY-MM-DD | 标题）")

    old_notes, new_notes = parse_notes(before), parse_notes(after)
    grown = len(new_notes) - len(old_notes)
    if grown != expect_added:
        raise ValueError(f"随想条数应增加 {expect_added} 条，实际变化 {grown:+d} 条")
    # Same id → same note: title unchanged, body unchanged or only extended.
    by_id = {note.id: note for note in new_notes}
    moved = [
        note.id
        for note in old_notes
        if note.id not in by_id
        or by_id[note.id].title != note.title
        or not by_id[note.id].body.startswith(note.body)
    ]
    if moved:
        raise ValueError(f"已有随想的链接会变：{', '.join(moved)}")


def _fenced_lines(lines: list[str]) -> list[bool]:
    """For each line of notes.md: is it inside a fenced code block?

    Fence lines themselves count as fenced. Every note heading ends an open
    fence, because notes.ts renders each note's body on its own.
    """
    flags: list[bool] = []
    fence: tuple[str, int] | None = None
    for line in lines:
        if HEADING_RE.match(line):
            fence = None
            flags.append(False)
        elif fence is not None:
            flags.append(True)
            if _closes_fence(line, fence):
                fence = None
        else:
            opened = _FENCE_OPEN_RE.fullmatch(line)
            if opened and not (opened.group(1)[0] == "`" and "`" in opened.group(2)):
                fence = (opened.group(1)[0], len(opened.group(1)))
            flags.append(fence is not None)
    return flags


_NOTE_IMAGE_RE = re.compile(r"(?<![\w.~/-])/assets/notes/([^\s/()\[\]<>\"'?#\\]+)")


def referenced_note_images(markdown: str) -> list[str]:
    """File names referenced as ``/assets/notes/<name>``, in order, unique."""
    names: list[str] = []
    for match in _NOTE_IMAGE_RE.finditer(markdown):
        if match.group(1) not in names:
            names.append(match.group(1))
    return names
