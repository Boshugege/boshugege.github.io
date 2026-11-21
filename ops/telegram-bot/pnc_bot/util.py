"""Small helpers shared by the runtime modules: escaping, redaction, time."""

from __future__ import annotations

import html
import logging
import re
import traceback
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")

# Anything that looks like a credential is removed from text that leaves the
# server (Telegram replies, notifications).
_SECRET_PATTERNS = (
    re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}"),  # Telegram bot token
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub tokens
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),  # GitHub fine-grained PAT
)


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")


def strip_ansi(text: str) -> str:
    """Remove terminal colour/control sequences (deploy logs carry npm/Astro colours)."""
    return _ANSI.sub("", text)


def redact(text: str) -> str:
    """Replace anything that looks like a token with ``[REDACTED]``."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def esc(value: object) -> str:
    """HTML-escape any value for Telegram's HTML parse mode."""
    return html.escape(str(value), quote=True)


def utf16_len(text: str) -> int:
    """Length in UTF-16 code units, which is how Telegram counts."""
    return len(text.encode("utf-16-le")) // 2


def shorten(text: str, limit: int) -> str:
    """Cut ``text`` to at most ``limit`` characters, ending with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def tail_text(text: str, limit: int) -> str:
    """Keep the last ``limit`` characters, starting at a line boundary."""
    if len(text) <= limit:
        return text
    cut = text[-limit:]
    newline = cut.find("\n")
    if 0 <= newline < len(cut) - 1:
        cut = cut[newline + 1 :]
    return "…\n" + cut


def parse_time(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp; naive values are taken as UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def shanghai_time(value: object) -> str:
    """``2026-11-21T14:03:00Z`` → ``2026-11-21 22:03`` (Asia/Shanghai)."""
    parsed = parse_time(value)
    if parsed is None:
        return "?"
    return parsed.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M")


def duration_text(seconds: float) -> str:
    """Human-readable duration in Chinese, e.g. ``3 小时 5 分``."""
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days} 天 {hours} 小时"
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def log_exception(logger: logging.Logger, what: str, exc: BaseException) -> None:
    """Log an exception by type and stack only.

    Exception messages can carry message text or file names, which must never
    reach the logs, so only the class name and the frames are written.
    """
    frames = "".join(traceback.format_tb(exc.__traceback__)).rstrip()
    logger.error("%s: %s\n%s", what, type(exc).__name__, frames)
