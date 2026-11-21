"""Minimal Telegram Bot API client (urllib through the local proxy).

The proxy is either an HTTP proxy (``http://host:port``, CONNECT tunnel) or a
SOCKS5 proxy (``socks5h://host:port``; the host name is resolved by the
proxy). On ``personal`` the bot talks to NaiveProxy's SOCKS5 listener directly
(one hop less than mihomo). Measured on 2026-11-22, roughly one new tunnel in
seven to ten never finishes its TLS handshake with Telegram, through mihomo or
NaiveProxy alike, while a healthy one opens in about 0.6 s. So opening a
connection (TCP + SOCKS + TLS) has its own short timeout and a stalled attempt
is retried at once on a fresh tunnel: nothing has been sent yet, so the retry
cannot duplicate a message.

The request URL contains the bot token, so no urllib exception, URL or
response object is ever allowed to escape this module: every failure becomes
a ``TelegramError`` built from sanitised text, raised outside the ``except``
block so it carries no ``__context__``.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Sequence

from .util import redact, utf16_len

API_BASE = "https://api.telegram.org"
MESSAGE_LIMIT = 4096
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_RETRY_WAIT = 60
# Only these methods can ever be called: in particular never setWebhook,
# deleteWebhook, logOut or close (the token is shared with the alert chain).
ALLOWED_METHODS = frozenset(
    {
        "getMe",
        "getUpdates",
        "sendMessage",
        "editMessageText",
        "answerCallbackQuery",
        "getFile",
        "setMyCommands",
    }
)
ALLOWED_UPDATES = ["message", "callback_query"]
_FILE_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,255}$")
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")
_NETWORK_ERRORS = (urllib.error.URLError, http.client.HTTPException, OSError)

Buttons = Sequence[Sequence[tuple[str, str]]]

# TCP connect + SOCKS handshake + TLS handshake, together (healthy: ~0.7 s).
CONNECT_TIMEOUT = 2.0
# One request opens at most this many connections (each on a fresh tunnel),
# allowing CONNECT_TIMEOUT times these factors: 2 s, 3 s, 6 s. A stall
# usually costs 2 s; a generally slow network still gets through.
CONNECT_BACKOFF = (1.0, 1.5, 3.0)
CONNECT_ATTEMPTS = len(CONNECT_BACKOFF)
# Per thread: the factor for the connection being opened right now.
_attempt = threading.local()
# Waiting for an answer once the request is out. Telegram answers in well
# under a second; a tunnel can also die after the handshake, and then only
# this timeout ends the wait. A long poll may take its own timeout plus this.
READ_TIMEOUT = 12.0
POLL_GRACE = 7.0
# Methods that can safely be sent again when no answer came back (the second
# copy changes nothing). sendMessage is not one of them: a resend could post
# the message twice, so the callers decide (App retries final results).
REPEATABLE = frozenset({"getMe", "getUpdates", "getFile", "editMessageText", "answerCallbackQuery", "setMyCommands"})


class ProxyError(OSError):
    """The SOCKS5 proxy refused or broke the tunnel (no URL or token inside)."""


class ConnectStageError(OSError):
    """Opening the connection failed before any request byte was sent."""


_SOCKS_REPLIES = {
    1: "general failure",
    2: "not allowed by ruleset",
    3: "network unreachable",
    4: "host unreachable",
    5: "connection refused",
    6: "TTL expired",
    7: "command not supported",
    8: "address type not supported",
}


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ProxyError("SOCKS5 proxy closed the connection")
        data += chunk
    return data


def socks5_connect(sock: socket.socket, host: str, port: int) -> None:
    """Open a CONNECT tunnel to ``host:port`` on a socket connected to a SOCKS5
    proxy (RFC 1928, no authentication, host name resolved by the proxy)."""
    name = host.encode("idna")
    if not 0 < len(name) < 256 or not 0 < port < 65536:
        raise ProxyError("invalid SOCKS5 target")
    sock.sendall(b"\x05\x01\x00")  # version 5, one method: no authentication
    version, method = _recv_exact(sock, 2)
    if version != 5 or method != 0:
        raise ProxyError("SOCKS5 proxy wants authentication or is not SOCKS5")
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + port.to_bytes(2, "big"))
    version, reply, _reserved, atyp = _recv_exact(sock, 4)
    if version != 5:
        raise ProxyError("SOCKS5 proxy sent an invalid reply")
    if reply != 0:
        raise ProxyError(f"SOCKS5 proxy: {_SOCKS_REPLIES.get(reply, f'error {reply}')}")
    if atyp == 1:
        _recv_exact(sock, 4 + 2)
    elif atyp == 4:
        _recv_exact(sock, 16 + 2)
    elif atyp == 3:
        _recv_exact(sock, _recv_exact(sock, 1)[0] + 2)
    else:
        raise ProxyError("SOCKS5 proxy sent an invalid address type")


class _SocksHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS over a SOCKS5 tunnel; opening it is bounded by CONNECT_TIMEOUT."""

    proxy: tuple[str, int] = ("127.0.0.1", 1080)
    connect_timeout: float = CONNECT_TIMEOUT

    def connect(self) -> None:
        failure = ""
        sock = None
        limit = self.connect_timeout * getattr(_attempt, "factor", 1.0)
        try:
            sock = socket.create_connection(self.proxy, timeout=limit)
            sock.settimeout(limit)
            socks5_connect(sock, self.host, self.port)
            tls = self._context.wrap_socket(sock, server_hostname=self.host)
        except (OSError, ssl.SSLError, ValueError) as exc:
            if sock is not None:
                sock.close()
            failure = "timed out" if isinstance(exc, TimeoutError) else f"{type(exc).__name__}: {exc}"
        else:
            tls.settimeout(self.timeout)  # the read timeout for the request
            self.sock = tls
            return
        raise ConnectStageError(f"connect: {failure}")


class SocksHTTPSHandler(urllib.request.HTTPSHandler):
    """urllib handler sending every https:// request through a SOCKS5 proxy."""

    def __init__(self, proxy_host: str, proxy_port: int, connect_timeout: float = CONNECT_TIMEOUT):
        context = ssl.create_default_context()
        super().__init__(context=context)
        self._context = context
        self._connection = type(
            "SocksHTTPSConnection",
            (_SocksHTTPSConnection,),
            {"proxy": (proxy_host, proxy_port), "connect_timeout": connect_timeout},
        )

    def https_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(self._connection, req, context=self._context)


def build_opener(proxy: str) -> urllib.request.OpenerDirector:
    """An opener for ``proxy``: ``socks5h://`` / ``socks5://`` or ``http(s)://``.

    Environment proxy variables are ignored either way.
    """
    parts = urllib.parse.urlsplit(proxy)
    if parts.scheme in ("socks5", "socks5h"):
        if not parts.hostname or not parts.port:
            raise ValueError("SOCKS5 proxy needs a host and a port")
        return urllib.request.build_opener(urllib.request.ProxyHandler({}), SocksHTTPSHandler(parts.hostname, parts.port))
    return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))


class TelegramError(Exception):
    """A failed Bot API call. The text never contains the token or URL."""

    def __init__(self, description: str, error_code: int | None = None, retry_after: int | None = None):
        super().__init__(description)
        self.description = description
        self.error_code = error_code
        self.retry_after = retry_after


class FileTooLarge(TelegramError):
    """A download was refused because it exceeds the size limit."""


def truncate_html(text: str, limit: int = MESSAGE_LIMIT) -> str:
    """Shorten HTML to ``limit`` UTF-16 units without breaking tags.

    Cuts before any partial tag or entity, appends ``…`` and closes the tags
    still open at the cut.
    """
    if utf16_len(text) <= limit:
        return text
    budget = limit - 200  # room for the ellipsis and closing tags
    used = 0
    cut = 0
    for ch in text:
        used += 2 if ord(ch) > 0xFFFF else 1
        if used > budget:
            break
        cut += 1
    head = text[:cut]
    lt = head.rfind("<")
    if lt > head.rfind(">"):
        head = head[:lt]
    amp = head.rfind("&")
    if amp > head.rfind(";"):
        head = head[:amp]
    stack: list[str] = []
    for match in re.finditer(r"<(/?)([A-Za-z][A-Za-z0-9-]*)[^>]*>", head):
        name = match.group(2).lower()
        if match.group(1):
            if name in stack:
                del stack[len(stack) - 1 - stack[::-1].index(name)]
        else:
            stack.append(name)
    return head + "…" + "".join(f"</{name}>" for name in reversed(stack))


def inline_keyboard(buttons: Buttons) -> dict:
    rows = []
    for row in buttons:
        out = []
        for text, data in row:
            if len(data.encode("utf-8")) > 64:
                raise ValueError("callback data longer than 64 bytes")
            out.append({"text": text, "callback_data": data})
        rows.append(out)
    return {"inline_keyboard": rows}


class TelegramClient:
    def __init__(
        self,
        token: str,
        proxy: str,
        *,
        timeout: float = READ_TIMEOUT,
        opener: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{30,}", token or ""):
            raise ValueError("invalid bot token format")
        self._token = token
        self.timeout = timeout
        self._sleep = sleep
        if opener is None:
            opener = build_opener(proxy)
        self._opener = opener

    def __repr__(self) -> str:
        return "TelegramClient(<token hidden>)"

    # --- transport -------------------------------------------------------

    def _clean(self, text: str) -> str:
        text = text.replace(self._token, "<token>")
        text = _URL_RE.sub("<url>", redact(text))
        return " ".join(text.split())[:300]

    def _describe(self, exc: BaseException) -> str:
        if isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError):
            reason = exc.reason
            if isinstance(reason, BaseException):
                text = f"{type(reason).__name__}: {reason}"
            else:
                text = str(reason)
        elif isinstance(exc, TimeoutError):
            text = "timed out"
        else:
            text = f"{type(exc).__name__}: {exc}"
        return self._clean(text)

    @staticmethod
    def _read_limited(resp: Any, max_bytes: int) -> bytes:
        length = resp.headers.get("Content-Length") if getattr(resp, "headers", None) is not None else None
        if length and length.isdigit() and int(length) > max_bytes:
            raise FileTooLarge("文件太大")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise FileTooLarge("文件太大")
            chunks.append(chunk)
        return b"".join(chunks)

    def _open(
        self, request: urllib.request.Request, timeout: float, max_bytes: int, *, repeatable: bool = False
    ) -> tuple[int, bytes]:
        """Perform one HTTP request; returns (status, body) or raises TelegramError.

        A connection that could not be opened (stalled tunnel or handshake) is
        retried at once on a fresh one, up to CONNECT_ATTEMPTS: the request
        had not been sent, so this never repeats it. A request that got no
        answer is retried too, but only when ``repeatable``.
        """
        failure = ""
        for attempt in range(1, CONNECT_ATTEMPTS + 1):
            _attempt.factor = CONNECT_BACKOFF[attempt - 1]
            try:
                with self._opener.open(request, timeout=timeout) as resp:
                    return int(getattr(resp, "status", 200)), self._read_limited(resp, max_bytes)
            except urllib.error.HTTPError as exc:
                status = exc.code
                try:
                    body = exc.read(MAX_JSON_BYTES) or b""
                except Exception:  # noqa: BLE001 - the body is optional
                    body = b""
                finally:
                    exc.close()
                return status, body
            except _NETWORK_ERRORS as exc:
                failure = self._describe(exc)
                stage = exc.reason if isinstance(exc, urllib.error.URLError) else exc
                if attempt < CONNECT_ATTEMPTS and (isinstance(stage, ConnectStageError) or repeatable):
                    continue
                break
        # Raised outside the except block: no __context__ holding the URL.
        raise TelegramError(f"network error: {failure}")

    def _call_once(self, method: str, params: dict) -> Any:
        body = json.dumps(params, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{API_BASE}/bot{self._token}/{method}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        timeout = self.timeout
        if method == "getUpdates":
            timeout = float(params.get("timeout", 0)) + POLL_GRACE
        status, payload = self._open(request, timeout, MAX_JSON_BYTES, repeatable=method in REPEATABLE)
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            data = None
        if not isinstance(data, dict):
            raise TelegramError(f"{method}: invalid response (HTTP {status})", status)
        if data.get("ok") is True:
            return data.get("result")
        code = data.get("error_code")
        code = code if type(code) is int else status
        description = self._clean(str(data.get("description") or "unknown error"))
        parameters = data.get("parameters")
        retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
        retry_after = retry_after if type(retry_after) is int else None
        raise TelegramError(f"{method}: {description}", code, retry_after)

    def call(self, method: str, **params: Any) -> Any:
        """Call a Bot API method and return its ``result``.

        A 429 answer is retried once after ``retry_after`` seconds (if that is
        at most a minute); everything else raises TelegramError.
        """
        if method not in ALLOWED_METHODS:
            raise ValueError(f"Bot API method not allowed: {method}")
        try:
            return self._call_once(method, params)
        except TelegramError as exc:
            wait = exc.retry_after
            if exc.error_code != 429 or wait is None or not 0 <= wait <= MAX_RETRY_WAIT:
                raise
        self._sleep(wait)
        return self._call_once(method, params)

    # --- helpers ---------------------------------------------------------

    def get_updates(self, offset: int | None, timeout: int) -> list[dict]:
        params: dict[str, Any] = {"timeout": int(timeout), "allowed_updates": ALLOWED_UPDATES}
        if offset:
            params["offset"] = int(offset)
        result = self.call("getUpdates", **params)
        if not isinstance(result, list):
            raise TelegramError("getUpdates: result is not a list")
        return [u for u in result if isinstance(u, dict)]

    def send_message(
        self,
        chat_id: int,
        html: str,
        *,
        reply_to: int | None = None,
        buttons: Buttons | None = None,
        disable_preview: bool = True,
    ) -> dict:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": truncate_html(html),
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": bool(disable_preview)},
        }
        if reply_to is not None:
            params["reply_parameters"] = {"message_id": int(reply_to), "allow_sending_without_reply": True}
        if buttons:
            params["reply_markup"] = inline_keyboard(buttons)
        result = self.call("sendMessage", **params)
        return result if isinstance(result, dict) else {}

    def edit_message(self, chat_id: int, message_id: int, html: str, *, buttons: Buttons | None = None) -> None:
        """Replace a message's text; without ``buttons`` the keyboard is removed."""
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": int(message_id),
            "text": truncate_html(html),
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if buttons:
            params["reply_markup"] = inline_keyboard(buttons)
        try:
            self.call("editMessageText", **params)
        except TelegramError as exc:
            if "message is not modified" not in exc.description:
                raise

    def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        params: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            params["text"] = text
        self.call("answerCallbackQuery", **params)

    def set_my_commands(self, commands: Sequence[tuple[str, str]], chat_id: int) -> None:
        self.call(
            "setMyCommands",
            commands=[{"command": name, "description": desc} for name, desc in commands],
            scope={"type": "chat", "chat_id": chat_id},
        )

    def download_file(self, file_id: str, max_bytes: int) -> bytes:
        """Download a file sent to the bot, refusing anything over ``max_bytes``."""
        info = self.call("getFile", file_id=file_id)
        if not isinstance(info, dict):
            raise TelegramError("getFile: invalid result")
        size = info.get("file_size")
        if type(size) is int and size > max_bytes:
            raise FileTooLarge("文件太大")
        path = info.get("file_path")
        if not isinstance(path, str) or not _FILE_PATH_RE.fullmatch(path) or ".." in path.split("/"):
            raise TelegramError("getFile: unusable file path")
        request = urllib.request.Request(
            f"{API_BASE}/file/bot{self._token}/{urllib.parse.quote(path)}", method="GET"
        )
        status, data = self._open(request, self.timeout, max_bytes, repeatable=True)
        if status != 200:
            raise TelegramError(f"download failed (HTTP {status})", status)
        return data
