"""Link previews for notes: title, description and thumbnail, fetched once.

A note may carry a link on a line of its own. When it is posted the bot looks
the link up a single time and stores what it found in the blog repo
(``PREVIEWS_PATH``), so the static site can draw a card with no network access
at build time.

The URL comes from a note, and everything behind it (redirects, image URLs,
API answers, page text) comes from the internet, while the bot runs on a host
full of localhost-only services. So every URL that is requested first passes
``check_url``, redirects are walked by hand and re-checked at each hop, every
body has a size limit, and one ``fetch`` has a wall-clock budget. A preview is
a nicety: any failure yields ``None`` and the note is posted without a card.
"""

from __future__ import annotations

import copy
import http.client
import json
import logging
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Callable

from .util import shorten

__all__ = [
    "MAX_LINKS",
    "MAX_HTML_BYTES",
    "MAX_JSON_BYTES",
    "MAX_IMAGE_BYTES",
    "MAX_REDIRECTS",
    "TIMEOUT",
    "BUDGET",
    "PREVIEWS_PATH",
    "USER_AGENT",
    "DIRECT_HOSTS",
    "Preview",
    "UnsafeURL",
    "standalone_links",
    "classify",
    "check_url",
    "Fetcher",
    "entry",
    "merge_previews",
    "validate_previews",
]

log = logging.getLogger(__name__)

MAX_LINKS = 3  # links previewed per note
MAX_HTML_BYTES = 512 * 1024
MAX_JSON_BYTES = 256 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3
TIMEOUT = 8.0  # per request
BUDGET = 20.0  # wall clock for one fetch(): nothing new is started after it
PREVIEWS_PATH = "src/data/link-previews.json"
# Generic on purpose: the request leaves through a shared proxy exit and must
# not name the project or carry a contact address.
USER_AGENT = "Mozilla/5.0 (compatible; LinkPreview/1.0)"

# Hosts asked through the Fetcher's ``direct`` opener (no proxy), when it was
# given one: Bilibili turns the proxy's exit away with HTTP 412. Such a request
# is resolved and sent by this host itself, so "everything leaves from the
# proxy's exit" does not hold for it. That is why this is a closed list of
# exact names of one public site, never a suffix and never anything taken
# from a note or an answer: a redirect from one of them to any other host goes
# through the proxy again, and so do the cover images. Should DNS ever lie
# about one of these names, certificate verification stops the request before
# anything is sent.
DIRECT_HOSTS = frozenset({"api.bilibili.com", "b23.tv", "bilibili.com", "www.bilibili.com", "m.bilibili.com"})

_MAX_URL_CHARS = 2000
_MAX_PREVIEWS_BYTES = 1024 * 1024
_KINDS = ("link", "youtube", "bilibili", "x", "github")
_TEXT_LIMITS = {"title": 120, "description": 200, "site": 40, "author": 60, "language": 30}
_MAX_STARS = 10**9
_ENTRY_KEYS = frozenset({"kind", "url", "image", "stars", "fetched", *_TEXT_LIMITS})
_IMAGE_PATH_RE = re.compile(r"/assets/notes/[0-9]{8}-[0-9a-f]{10}\.webp")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


@dataclass(frozen=True)
class Preview:
    """What was learned about one link. Text fields are already cleaned and cut to length."""

    url: str  # exactly as written in the note
    kind: str  # "link" | "youtube" | "bilibili" | "x" | "github"
    title: str = ""
    description: str = ""
    site: str = ""  # og:site_name or a provider name
    author: str = ""  # channel / uploader / tweet author
    canonical: str = ""  # only when the note URL is a short link that resolved elsewhere
    stars: int | None = None  # GitHub repos
    language: str = ""  # GitHub repos
    image: bytes | None = field(default=None, repr=False)  # raw thumbnail as downloaded; the caller re-encodes


class UnsafeURL(ValueError):
    """A URL the bot must not request. The message never contains the URL."""


class _Failure(ValueError):
    """A lookup that cannot produce a preview; the message is safe to log."""


# --------------------------------------------------------------------------
# text


# Zero-width characters and bidi marks, embeddings, overrides and isolates:
# invisible, and able to reorder or disguise the text around them on the page.
_INVISIBLE = frozenset(
    "\u200b\u200c\u200d\u200e\u200f"  # zero-width space, non-joiner, joiner; the two direction marks
    "\u202a\u202b\u202c\u202d\u202e"  # embeddings and overrides, and their pop
    "\u2060\u2061\u2062\u2063\u2064"  # word joiner, invisible operators
    "\u2066\u2067\u2068\u2069"  # isolates and their pop
    "\ufeff"  # zero-width no-break space (a stray byte order mark)
)


def _unprintable(ch: str) -> bool:
    """Control characters, lone surrogates (they cannot be written as UTF-8) and invisible format characters."""
    return ch in _INVISIBLE or unicodedata.category(ch) in ("Cc", "Cs")


def _clean(value: object, limit: int) -> str:
    """One line of plain text, at most ``limit`` characters; anything that is not a string becomes ``""``."""
    if not isinstance(value, str):
        return ""
    # Whitespace controls (newline, tab) survive this step so that they become a space, not nothing.
    kept = "".join(ch for ch in value if ch.isspace() or not _unprintable(ch))
    return shorten(" ".join(kept.split()), limit)


def _stars(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _MAX_STARS:
        return value
    return None


# --------------------------------------------------------------------------
# links in a note

_FENCE_RE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
_LINE_BREAK_RE = re.compile(r"\r\n|\n|\r")  # Markdown's line endings only (str.splitlines knows many more)
# GFM leaves these out of a bare autolink when they end it, so the rendered
# link would not be the URL on the line.
_TRAILING = ".,;:!?'\"*_~"


def _indent(line: str) -> int:
    """Leading indentation in columns (tab stops of four, as in CommonMark)."""
    columns = 0
    for ch in line:
        if ch == " ":
            columns += 1
        elif ch == "\t":
            columns += 4 - columns % 4
        else:
            break
    return columns


def _line_url(text: str) -> str:
    """The URL when ``text`` is one https URL and nothing else (``<…>`` allowed), else ``""``."""
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1]
    if not text.startswith("https://") or len(text) == len("https://"):
        return ""
    if any(ch.isspace() or ch in "<>" for ch in text):
        return ""
    if text[-1] in _TRAILING or (text[-1] == ")" and text.count(")") > text.count("(")):
        return ""
    return text


def standalone_links(markdown: str) -> list[str]:
    """The https URLs that sit alone on a line, in order, without duplicates.

    Only such a line becomes a card on the site. Lines in fenced or indented
    code are skipped, and so is anything behind a quote or list marker (the
    line then does not start with the URL). Callers slice to ``MAX_LINKS``.
    """
    links: list[str] = []
    fence = ""  # the opening run of ` or ~ while inside a fenced block
    for line in _LINE_BREAK_RE.split(markdown):
        match = _FENCE_RE.fullmatch(line)
        run, rest = match.groups() if match else ("", "")
        if fence:
            # Closed only by the same character, at least as long, with nothing after it.
            if run.startswith(fence) and not rest.strip():
                fence = ""
            continue
        if run and not (run[0] == "`" and "`" in rest):  # a backtick fence has no backtick in its info string
            fence = run
            continue
        if _indent(line) >= 4:
            continue
        url = _line_url(line.strip())
        if url and url not in links:
            links.append(url)
    return links


# --------------------------------------------------------------------------
# what a URL points at

# The site has a twin of classify() in src/lib/link-providers.ts (classifyLink):
# the two follow the same steps in the same order. Change both or neither.
_YT_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")
_BVID_RE = re.compile(r"BV[0-9A-Za-z]{10}")
_AID_RE = re.compile(r"av([0-9]{1,12})")
_SHORT_CODE_RE = re.compile(r"[0-9A-Za-z]{1,16}")
_X_USER_RE = re.compile(r"[A-Za-z0-9_]{1,15}")
_X_ID_RE = re.compile(r"[0-9]{1,20}")
_GH_OWNER_RE = re.compile(r"[A-Za-z0-9-]{1,39}")
_GH_REPO_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")
# First path segments that are GitHub's own pages, not accounts.
_GH_RESERVED = frozenset(
    {
        "features",
        "topics",
        "orgs",
        "settings",
        "marketplace",
        "sponsors",
        "about",
        "pricing",
        "login",
        "join",
        "explore",
        "notifications",
        "search",
        "collections",
        "trending",
        "apps",
        "enterprise",
        "security",
        "site",
        "readme",
        "new",
        "organizations",
        "users",
        "issues",
        "pulls",
    }
)


def _param(query: str, name: str) -> str:
    """The first value of a query parameter (what ``URLSearchParams.get`` returns), ``""`` if absent."""
    for key, value in urllib.parse.parse_qsl(query, keep_blank_values=True):
        if key == name:
            return value
    return ""


def _site(host: str, *prefixes: str) -> str:
    """``host`` without the first of ``prefixes`` it starts with: ``m.youtube.com`` → ``youtube.com``."""
    for prefix in prefixes:
        if host.startswith(prefix + "."):
            return host[len(prefix) + 1 :]
    return host


def classify(url: str) -> tuple[str, dict[str, str]]:
    """``(kind, ids)`` from the URL text alone, no network; anything unrecognised is ``("link", {})``."""
    plain: tuple[str, dict[str, str]] = ("link", {})
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        _ = parts.port  # a malformed port makes it no URL at all, as it does for the twin
    except (ValueError, AttributeError, TypeError):
        return plain
    if parts.scheme != "https":
        return plain
    segments = [segment for segment in parts.path.split("/") if segment]

    site = _site(host, "www", "m", "music")
    if site in ("youtube.com", "youtu.be"):
        video = ""
        if site == "youtu.be":
            video = segments[0] if len(segments) == 1 else ""
        elif segments == ["watch"]:
            video = _param(parts.query, "v")
        elif len(segments) == 2 and segments[0] in ("shorts", "live", "embed"):
            video = segments[1]
        return ("youtube", {"id": video}) if _YT_ID_RE.fullmatch(video) else plain

    if _site(host, "www", "m") == "bilibili.com":
        if len(segments) == 2 and segments[0] == "video":
            if _BVID_RE.fullmatch(segments[1]):
                return "bilibili", {"bvid": segments[1]}
            match = _AID_RE.fullmatch(segments[1])
            if match:
                return "bilibili", {"aid": match.group(1)}
        return plain
    if host == "b23.tv":  # a short link: only following it tells which video
        if len(segments) == 1 and _SHORT_CODE_RE.fullmatch(segments[0]):
            return "bilibili", {"short": segments[0]}
        return plain

    if _site(host, "www", "mobile", "m") in ("x.com", "twitter.com"):
        # Longer paths (/photo/1, /video/1) still name the same post.
        if len(segments) >= 3 and segments[1] == "status":
            if _X_USER_RE.fullmatch(segments[0]) and _X_ID_RE.fullmatch(segments[2]):
                return "x", {"user": segments[0], "id": segments[2]}
        return plain

    if _site(host, "www") == "github.com":
        if len(segments) == 2 and segments[0].lower() not in _GH_RESERVED:
            owner, repo = segments
            if repo.endswith(".git"):
                repo = repo[: -len(".git")]
            if _GH_OWNER_RE.fullmatch(owner) and _GH_REPO_RE.fullmatch(repo) and repo not in (".", ".."):
                return "github", {"owner": owner, "repo": repo}
    return plain


# --------------------------------------------------------------------------
# which URLs may be requested

_HOST_LABEL = "[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?"
_HOST_RE = re.compile(rf"{_HOST_LABEL}(?:\.{_HOST_LABEL})+")  # at least one dot, no trailing dot
# A last label like this makes the whole name an IPv4 address to the resolver
# (1.2.3.4, 127.1, 0x7f.1, 0177.0.0.1); no real TLD is numeric.
_NUMERIC_LABEL_RE = re.compile(r"[0-9]+|0x[0-9a-f]*")
_INTERNAL_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".lan",
    ".home.arpa",
    ".test",
    ".example",
    ".invalid",
    ".onion",
    ".ts.net",
    ".arpa",
)


def _safe_host(url: object) -> str:
    """The ASCII (IDNA) host name of a URL that may be requested; raises ``UnsafeURL`` otherwise."""
    if not isinstance(url, str) or not url.startswith("https://"):
        raise UnsafeURL("not an https URL")
    if len(url) > _MAX_URL_CHARS:
        raise UnsafeURL("URL too long")
    if any(ch.isspace() or _unprintable(ch) for ch in url):
        raise UnsafeURL("whitespace or control character in URL")
    try:
        netloc = urllib.parse.urlsplit(url).netloc
    except ValueError:
        netloc = ""
    if "@" in netloc:
        raise UnsafeURL("URL with user info")
    if "[" in netloc or "]" in netloc:
        raise UnsafeURL("IP address instead of a host name")
    name, colon, port = netloc.partition(":")
    if colon and port != "443":
        raise UnsafeURL("non-standard port")
    try:
        # IDNA also folds look-alikes (full-width digits, ideographic full stops)
        # into what the resolver will really see, so the checks below run on that.
        host = name.lower().encode("idna").decode("ascii").lower()
    except UnicodeError:
        host = ""
    if len(host) > 253 or not _HOST_RE.fullmatch(host):
        raise UnsafeURL("not a public host name")
    if _NUMERIC_LABEL_RE.fullmatch(host.rsplit(".", 1)[1]):
        raise UnsafeURL("IP address instead of a host name")
    if host == "localhost" or host.endswith(_INTERNAL_SUFFIXES):
        raise UnsafeURL("internal host name")
    return host


def check_url(url: str) -> str:
    """Return ``url`` if the bot may request it, else raise ``UnsafeURL``.

    Applied to the note URL, every redirect target and every image URL. Only
    plain public-looking ``https://name[:443]/…`` passes: no user info, no other
    port, no IP literal in any spelling, no single-label or internal name.
    """
    # This is a check of the name only. DNS is resolved by the proxy (socks5h),
    # so the bot never sees the address a name resolves to and cannot refuse a
    # public name that points at a private address. What limits that case is
    # where the request leaves from: the proxy's exit, not this host, so this
    # host's localhost services and private network are not reachable that way.
    # (The one exception, a handful of fixed names, is explained at DIRECT_HOSTS.)
    _safe_host(url)
    return url


# Printable ASCII that already has a meaning in a URL stays as it is ("%" too,
# so existing escapes are not escaped twice); everything else is percent-encoded.
_URL_SAFE = "%/:=&?~+!$,;'@()*[]"


def _wire_url(url: str) -> tuple[str, str]:
    """``(host, URL)`` as actually requested: ASCII, the host as ``check_url`` judged it, no port, no fragment."""
    host = _safe_host(url)
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(parts.path, safe=_URL_SAFE) or "/"
    return host, urllib.parse.urlunsplit(("https", host, path, urllib.parse.quote(parts.query, safe=_URL_SAFE), ""))


def _log_host(url: object) -> str:
    """The host of a URL for the log. Paths and queries of note links never go there."""
    try:
        host = urllib.parse.urlsplit(url).hostname if isinstance(url, str) else None
    except ValueError:
        host = None
    return host[:80] if host and re.fullmatch(r"[A-Za-z0-9._-]+", host) else "?"


# --------------------------------------------------------------------------
# HTML


_META_KEYS = frozenset(
    {
        "og:title",
        "og:description",
        "og:image",
        "og:image:secure_url",
        "og:site_name",
        "twitter:title",
        "twitter:description",
        "twitter:image",
        "description",
    }
)
_MAX_TITLE_CHARS = 2000  # an unclosed <title> must not swallow the page
_CHARSET_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,39}")
_META_CHARSET_RE = re.compile(rb"<meta[^>]{0,200}?charset\s*=\s*[\"']?\s*([A-Za-z0-9_.:-]{1,40})", re.IGNORECASE)


class _MetaParser(HTMLParser):
    """Collects the first ``<title>`` and the first non-empty value of each wanted ``<meta>``."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self._title: list[str] | None = None  # the parts while inside the first <title>
        self._title_seen = False  # later ones are inline SVG titles, not the page's

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "meta":
            found: dict[str, str] = {}
            for name, value in attrs:
                found.setdefault(name, value or "")
            # Open Graph says property=, Twitter cards say name=; sites mix them up freely.
            key = (found.get("property") or found.get("name") or "").strip().lower()
            content = found.get("content", "").strip()
            if key in _META_KEYS and content and key not in self.meta:
                self.meta[key] = content
        elif tag == "title" and not self._title_seen:
            self._title_seen = True
            self._title = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._end_title()

    def handle_data(self, data: str) -> None:
        if self._title is not None:
            self._title.append(data)
            if sum(len(part) for part in self._title) > _MAX_TITLE_CHARS:
                self._end_title()

    def close(self) -> None:
        super().close()
        self._end_title()

    def _end_title(self) -> None:
        if self._title is not None:
            self.title = "".join(self._title)[:_MAX_TITLE_CHARS]
            self._title = None


class _FirstParagraph(HTMLParser):
    """The text of the first ``<p>``: in a tweet's oEmbed blockquote that is the tweet itself."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._inside = False
        self._done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "p" and not self._done:
            self._inside = True
        elif tag == "br" and self._inside:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "p" and self._inside:
            self._inside = False
            self._done = True

    def handle_data(self, data: str) -> None:
        if self._inside:
            self.parts.append(data)


def _first_paragraph(markup: object) -> str:
    if not isinstance(markup, str):
        return ""
    parser = _FirstParagraph()
    parser.feed(markup)
    parser.close()
    return "".join(parser.parts)


def _decode_html(body: bytes, declared: str | None) -> str:
    """Decode a page: the header's charset, else a ``<meta>`` one near the top, else UTF-8."""
    sniffed = _META_CHARSET_RE.search(body[:2048])
    for name in (declared, sniffed.group(1).decode("ascii") if sniffed else None, "utf-8"):
        if not name or not _CHARSET_NAME_RE.fullmatch(name):
            continue
        try:
            return body.decode(name, errors="replace")
        except (LookupError, ValueError):  # unknown name, or a codec that is not a text encoding
            continue
    return body.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# fetching

_ACCEPT_HTML = "text/html,application/xhtml+xml;q=0.9"
_ACCEPT_JSON = "application/json"
_ACCEPT_GITHUB = "application/vnd.github+json"
# What pnc_bot.images can decode comes first: it has no AVIF, which sites serve when a client seems to take anything.
_ACCEPT_IMAGE = "image/jpeg,image/png,image/webp,image/gif;q=0.9,image/*;q=0.5"
_HTML_TYPES = ("text/html", "application/xhtml+xml")
_REDIRECT_CODES = (301, 302, 303, 307, 308)
_READ_CHUNK = 64 * 1024
# Expected ways for a lookup to fail: the network (URLError, HTTPError, timeouts,
# the SOCKS errors are all OSError), a broken HTTP exchange, and bad content
# (UnsafeURL, _Failure, UnicodeError and JSON errors are all ValueError).
_ERRORS = (OSError, ValueError, http.client.HTTPException)
_YT_THUMBNAILS = ("maxresdefault", "mqdefault", "hqdefault")
_PAGE_NUMBER_RE = re.compile(r"[1-9][0-9]{0,3}")
_X_PROFILE_RE = re.compile(r"https://(?:www\.)?(?:twitter|x)\.com/([A-Za-z0-9_]{1,15})/?")


def _reason(exc: BaseException) -> str:
    """Why a lookup failed, for the log: this module's own message or a class name.

    Never the text of a foreign exception, which may hold a URL.
    """
    if isinstance(exc, (_Failure, UnsafeURL)):
        return str(exc)
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, BaseException):
        return type(exc.reason).__name__
    return type(exc).__name__


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


# Handlers of a given opener that the Fetcher's own copy goes without.
# The redirect handler, because redirects are walked by hand (see _own_opener);
# the rest are never needed by an https-only fetcher, and without them even a
# URL that somehow skipped check_url could not read a file or speak plain http.
_UNUSED_HANDLERS = (
    urllib.request.HTTPRedirectHandler,
    urllib.request.HTTPHandler,
    urllib.request.FileHandler,
    urllib.request.FTPHandler,
    urllib.request.DataHandler,
)


class _Lookup:
    """The requests behind one ``fetch()``; they share a single deadline.

    ``run`` may raise any of ``_ERRORS``; ``Fetcher.fetch`` turns that into ``None``.
    """

    def __init__(self, opener: Any, direct: Any, clock: Callable[[], float]):
        self._opener = opener
        self._direct = direct
        self._clock = clock
        self._deadline = clock() + BUDGET

    def run(self, url: str) -> Preview | None:
        check_url(url)
        kind, ids = classify(url)
        canonical = ""
        preview = None
        if kind != "link":
            providers = {"youtube": self._youtube, "bilibili": self._bilibili, "x": self._x, "github": self._github}
            try:
                if "short" in ids:
                    ids, canonical = self._resolve_short(url)
                preview = _build(url, kind, canonical, providers[kind](ids))
            except _ERRORS as exc:
                log.info("link preview: %s lookup failed (%s), trying the page itself", kind, _reason(exc))
        if preview is None:
            # Also the fallback of every provider. The kind stays what the URL
            # says (the site classifies the URL the same way), only the source differs.
            preview = _build(url, kind, canonical, self._page(url))
        return preview

    # --- transport -------------------------------------------------------

    def _hop(self, url: str, accept: str) -> tuple[Any, str]:
        """One GET that follows nothing: ``(response, "")``, or ``(None, target)`` for a redirect to a checked URL."""
        host, wire = _wire_url(url)  # runs check_url's rules again on exactly what is sent
        opener = self._direct if self._direct is not None and host in DIRECT_HOSTS else self._opener
        request = urllib.request.Request(
            wire,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": accept,
                "Accept-Encoding": "identity",  # no decompression here, so no decompression bombs
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            method="GET",
        )
        self._check_time()
        code, location = 0, None
        try:
            return opener.open(request, timeout=TIMEOUT), ""
        except urllib.error.HTTPError as exc:
            code = exc.code
            location = exc.headers.get("Location") if exc.headers is not None else None
            exc.close()
        if code not in _REDIRECT_CODES or not location:
            raise _Failure(f"HTTP {code}")
        try:
            # http.client reads header bytes as Latin-1, but sites put raw UTF-8 paths in Location.
            location = location.encode("latin-1").decode("utf-8")
        except UnicodeError:
            pass
        target = urllib.parse.urldefrag(urllib.parse.urljoin(url, location.strip())).url
        return None, check_url(target)

    def _get(self, url: str, accept: str, limit: int, *, truncate: bool = False) -> tuple[str, Any, bytes]:
        """GET through at most ``MAX_REDIRECTS`` checked redirects: ``(final URL, headers, body)``.

        A body over ``limit`` is a failure, unless ``truncate`` asks for its first ``limit`` bytes.
        """
        for _ in range(MAX_REDIRECTS + 1):
            response, target = self._hop(url, accept)
            if response is None:
                url = target
                continue
            with response:
                if response.status != 200:
                    raise _Failure(f"HTTP {response.status}")
                if response.headers.get("Content-Encoding", "identity").strip().lower() not in ("", "identity"):
                    raise _Failure("compressed answer to an identity request")
                body = self._read(response, limit + 1)
            if len(body) > limit:
                if not truncate:
                    raise _Failure("answer too large")
                body = body[:limit]
            return url, response.headers, body
        raise _Failure("too many redirects")

    def _read(self, response: Any, limit: int) -> bytes:
        """Up to ``limit`` bytes of the body.

        Read in single ``read1`` steps with the clock checked in between: each
        step is bounded by the socket timeout, so a server that trickles its
        answer cannot hold a note for long. A request that started within the
        budget gets ``TIMEOUT`` past it to deliver its body.
        """
        data = bytearray()
        while len(data) < limit:
            self._check_time(grace=TIMEOUT)
            chunk = response.read1(min(_READ_CHUNK, limit - len(data)))
            if not chunk:
                break
            data += chunk
        return bytes(data)

    def _check_time(self, grace: float = 0.0) -> None:
        if self._clock() >= self._deadline + grace:
            raise _Failure("out of time")

    def _json(self, url: str, accept: str = _ACCEPT_JSON) -> dict:
        _, _, body = self._get(url, accept, MAX_JSON_BYTES)
        try:
            data = json.loads(body)
        except RecursionError:  # absurdly nested; not a ValueError, so made one here
            data = None
        if not isinstance(data, dict):
            raise _Failure("unexpected JSON")
        return data

    def _image(self, url: object, base: str = "") -> bytes | None:
        """A thumbnail's raw bytes, or None: a card without a picture is still a card."""
        if not isinstance(url, str) or not url.strip():
            return None
        try:
            target = urllib.parse.urljoin(base, url.strip()) if base else url.strip()
            _, headers, body = self._get(target, _ACCEPT_IMAGE, MAX_IMAGE_BYTES)
            content_type = headers.get_content_type()
            if not content_type.startswith("image/") or content_type == "image/svg+xml":
                raise _Failure("not a bitmap image")
            if not body:
                raise _Failure("empty image")
        except _ERRORS as exc:
            log.info("link preview: no thumbnail (%s)", _reason(exc))
            return None
        return body

    # --- strategies ------------------------------------------------------

    def _page(self, url: str) -> dict[str, Any]:
        """The generic strategy: Open Graph / Twitter card / plain HTML metadata of the page itself."""
        final, headers, body = self._get(url, _ACCEPT_HTML, MAX_HTML_BYTES, truncate=True)
        if headers.get_content_type() not in _HTML_TYPES:
            raise _Failure("not an HTML page")
        parser = _MetaParser()
        # The whole (truncated) text, not just <head>: some sites put their metas in the body.
        parser.feed(_decode_html(body, headers.get_content_charset()))
        parser.close()
        meta = parser.meta
        host = urllib.parse.urlsplit(final).hostname or ""
        picture = meta.get("og:image:secure_url") or meta.get("og:image") or meta.get("twitter:image")
        return {
            "title": meta.get("og:title") or meta.get("twitter:title") or parser.title,
            "description": meta.get("og:description") or meta.get("twitter:description") or meta.get("description"),
            "site": meta.get("og:site_name") or (host[4:] if host.startswith("www.") else host),
            "image": self._image(picture, base=final),  # relative to where the redirects ended
        }

    def _youtube(self, ids: dict[str, str]) -> dict[str, Any]:
        video = ids["id"]
        fields: dict[str, Any] = {"site": "YouTube"}
        watch = urllib.parse.quote(f"https://www.youtube.com/watch?v={video}", safe="")
        try:
            data = self._json(f"https://www.youtube.com/oembed?url={watch}&format=json")
            fields.update(title=data.get("title"), author=data.get("author_name"))
        except _ERRORS as exc:
            # Videos that forbid embedding answer 401 here but still have thumbnails.
            log.info("link preview: no YouTube oEmbed (%s)", _reason(exc))
        for name in _YT_THUMBNAILS:
            fields["image"] = self._image(f"https://i.ytimg.com/vi/{video}/{name}.jpg")
            if fields["image"]:
                break
        return fields

    def _resolve_short(self, url: str) -> tuple[dict[str, str], str]:
        """Walk a b23.tv link to the video behind it: ``(ids, canonical URL)``."""
        for _ in range(MAX_REDIRECTS):
            response, url = self._hop(url, _ACCEPT_HTML)
            if response is not None:
                response.close()
                break
            kind, ids = classify(url)
            if kind == "bilibili" and "short" not in ids:
                page = _param(urllib.parse.urlsplit(url).query, "p")
                suffix = f"?p={page}" if _PAGE_NUMBER_RE.fullmatch(page) else ""
                return ids, f"https://www.bilibili.com/video/{ids.get('bvid') or 'av' + ids['aid']}{suffix}"
        raise _Failure("short link does not lead to a video")

    def _bilibili(self, ids: dict[str, str]) -> dict[str, Any]:
        query = f"bvid={ids['bvid']}" if "bvid" in ids else f"aid={ids['aid']}"
        data = self._json(f"https://api.bilibili.com/x/web-interface/view?{query}")
        if data.get("code") != 0:
            raise _Failure("bilibili API error")
        video = _as_dict(data.get("data"))
        description = video.get("desc")
        cover = video.get("pic")
        if isinstance(cover, str):
            # The API still hands out http:// (or scheme-less //) covers; the CDN serves them over https too.
            cover = urllib.parse.urljoin("https://api.bilibili.com/", cover.strip())
            if cover.startswith("http://"):
                cover = "https://" + cover[len("http://") :]
        return {
            "title": video.get("title"),
            "description": "" if description == "-" else description,  # "-" is the API's empty description
            "author": _as_dict(video.get("owner")).get("name"),
            "site": "哔哩哔哩",
            "image": self._image(cover),
        }

    def _x(self, ids: dict[str, str]) -> dict[str, Any]:
        tweet = urllib.parse.quote(f"https://twitter.com/{ids['user']}/status/{ids['id']}", safe="")
        data = self._json(f"https://publish.twitter.com/oembed?url={tweet}&omit_script=1&dnt=1")
        name = _clean(data.get("author_name"), _TEXT_LIMITS["author"])
        text = _clean(_first_paragraph(data.get("html")), _TEXT_LIMITS["description"])
        if not name and not text:
            raise _Failure("empty oEmbed answer")
        # The URL may say /i/status/…; the profile URL names the real account.
        author_url = data.get("author_url")
        profile = _X_PROFILE_RE.fullmatch(author_url.strip()) if isinstance(author_url, str) else None
        handle = "@" + (profile.group(1) if profile else ids["user"])
        return {"title": f"{name} ({handle})" if name else handle, "description": text, "author": name, "site": "X"}

    def _github(self, ids: dict[str, str]) -> dict[str, Any]:
        data = self._json(f"https://api.github.com/repos/{ids['owner']}/{ids['repo']}", _ACCEPT_GITHUB)
        return {
            "title": data.get("full_name"),
            "description": data.get("description"),
            "site": "GitHub",
            "stars": data.get("stargazers_count"),
            "language": data.get("language"),
            "image": self._image(_as_dict(data.get("owner")).get("avatar_url")),
        }


def _build(url: str, kind: str, canonical: str, fields: dict[str, Any]) -> Preview | None:
    """Clean what a strategy found; None when there is nothing to show (no title, description or image)."""
    image = fields.get("image")
    preview = Preview(
        url=url,
        kind=kind,
        canonical=canonical,
        stars=_stars(fields.get("stars")),
        image=image if isinstance(image, bytes) and image else None,
        **{name: _clean(fields.get(name), limit) for name, limit in _TEXT_LIMITS.items()},
    )
    return preview if preview.title or preview.description or preview.image else None


def _own_opener(opener: Any) -> urllib.request.OpenerDirector:
    """A private copy of ``opener`` that follows no redirect and speaks https only."""
    # The opener handed in follows redirects on its own (HTTPRedirectHandler is
    # one of urllib's default handlers), and a redirect target must pass
    # check_url before anything is sent to it. Without that handler nobody
    # claims a 3xx, so urllib's default error handler raises it as an HTTPError
    # that still carries Location, and _Lookup walks the chain by hand.
    # The handlers are copied because add_handler() re-parents them, which
    # would silently rewire the opener the Telegram client keeps using.
    own = urllib.request.OpenerDirector()
    for handler in opener.handlers:
        if not isinstance(handler, _UNUSED_HANDLERS):
            own.add_handler(copy.copy(handler))
    return own


class Fetcher:
    """Fetches link previews through ``opener``; ``fetch`` never raises.

    ``opener`` is in production the SOCKS5 opener of ``pnc_bot.telegram``. Only
    https goes through the proxy there, which is one reason plain http is
    never requested. ``direct``, if given, is an opener without a proxy; it is
    used for ``DIRECT_HOSTS`` and for nothing else.
    """

    def __init__(
        self,
        opener: urllib.request.OpenerDirector,
        *,
        direct: urllib.request.OpenerDirector | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._opener = _own_opener(opener)
        self._direct = _own_opener(direct) if direct is not None else None
        self._clock = clock

    def fetch(self, url: str) -> Preview | None:
        """The preview for ``url``, or None when there is none to be had within ``BUDGET`` seconds."""
        host = _log_host(url)
        try:
            preview = _Lookup(self._opener, self._direct, self._clock).run(url)
        except _ERRORS as exc:
            log.info("link preview: nothing for %s (%s)", host, _reason(exc))
            return None
        except Exception as exc:  # a bug here may cost a card, never the note
            log.warning("link preview: unexpected %s for %s", type(exc).__name__, host)
            return None
        if preview is None:
            log.info("link preview: nothing to show for %s", host)
        return preview


# --------------------------------------------------------------------------
# the stored file


def entry(preview: Preview, image_path: str | None, fetched: str) -> dict:
    """The JSON object stored for one link; empty values are left out.

    ``image_path`` is the site path of the re-encoded thumbnail (if any) and
    ``fetched`` a ``YYYY-MM-DD`` date.
    """
    values = {
        "kind": preview.kind,
        "title": preview.title,
        "description": preview.description,
        "site": preview.site,
        "author": preview.author,
        "url": preview.canonical,
        "image": image_path,
        "stars": preview.stars,
        "language": preview.language,
        "fetched": fetched,
    }
    return {key: value for key, value in values.items() if value is not None and value != ""}


def _validate_entry(item: object) -> None:
    if not isinstance(item, dict) or not set(item) <= _ENTRY_KEYS:
        raise ValueError("link previews: an entry is not an object of known keys")
    if not isinstance(item.get("kind"), str) or item["kind"] not in _KINDS:
        raise ValueError("link previews: unknown kind")
    if "fetched" in item and not (isinstance(item["fetched"], str) and _DATE_RE.fullmatch(item["fetched"])):
        raise ValueError("link previews: bad fetched date")
    for name, limit in _TEXT_LIMITS.items():
        # One more than the limit, for a truncation that adds its ellipsis after the cut.
        if name in item and not (isinstance(item[name], str) and len(item[name]) <= limit + 1):
            raise ValueError(f"link previews: bad {name}")
    if "image" in item and not (isinstance(item["image"], str) and _IMAGE_PATH_RE.fullmatch(item["image"])):
        raise ValueError("link previews: bad image path")
    if "url" in item:
        check_url(item["url"])
    if "stars" in item and _stars(item["stars"]) is None:
        raise ValueError("link previews: bad stars")


def validate_previews(raw: str) -> None:
    """Raise ``ValueError`` unless ``raw`` is a well-formed previews file.

    The site build trusts this file, so the bot refuses to write (or build on)
    anything that is not exactly the shape it produces itself.
    """
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_PREVIEWS_BYTES:
        raise ValueError("link previews: not text or too large")
    try:
        data = json.loads(raw)
    except RecursionError:
        data = None
    if not isinstance(data, dict) or set(data) != {"version", "links"}:
        raise ValueError("link previews: not a {version, links} object")
    if type(data["version"]) is not int or data["version"] != 1:
        raise ValueError("link previews: unknown version")
    if not isinstance(data["links"], dict):
        raise ValueError("link previews: links is not an object")
    for url, item in data["links"].items():
        check_url(url)
        _validate_entry(item)


def merge_previews(raw: str | None, entries: dict[str, dict]) -> str:
    """The new text of the previews file: the links in ``raw`` plus ``entries`` (which win).

    ``raw`` is the current text, or None / ``""`` when there is no file yet.
    The result is deterministic (sorted keys), so the same input always gives
    the same bytes. Raises ``ValueError`` when ``raw`` or the result is not valid.
    """
    links: dict[str, dict] = {}
    if raw:
        validate_previews(raw)
        links.update(json.loads(raw)["links"])
    links.update(entries)
    text = json.dumps({"version": 1, "links": links}, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    validate_previews(text)  # never write a file the next run (or the site) would reject
    return text
