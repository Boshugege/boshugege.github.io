"""Minimal Telegram Bot API client (urllib through the local proxy).

The request URL contains the bot token, so no urllib exception, URL or
response object is ever allowed to escape this module: every failure becomes
a ``TelegramError`` built from sanitised text, raised outside the ``except``
block so it carries no ``__context__``.
"""

from __future__ import annotations

import http.client
import json
import re
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
        timeout: float = 70,
        opener: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{30,}", token or ""):
            raise ValueError("invalid bot token format")
        self._token = token
        self.timeout = timeout
        self._sleep = sleep
        if opener is None:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
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

    def _open(self, request: urllib.request.Request, timeout: float, max_bytes: int) -> tuple[int, bytes]:
        """Perform one HTTP request; returns (status, body) or raises TelegramError."""
        failure = ""
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
            timeout = max(timeout, float(params.get("timeout", 0)) + 20)
        status, payload = self._open(request, timeout, MAX_JSON_BYTES)
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
        status, data = self._open(request, self.timeout, max_bytes)
        if status != 200:
            raise TelegramError(f"download failed (HTTP {status})", status)
        return data
