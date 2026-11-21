"""pnc-bot: long-polling loop, routing, confirmations and workers.

Threads:
- main: ``getUpdates`` loop; authorizes every update first, routes it,
  buffers media groups and flushes them.
- git worker (1 thread): every repository job, strictly one at a time.
- pool (2 threads): read-only commands and quick ops, so /status never waits
  behind a git job.
- comment poller and deploy tracker (daemon threads).
"""

from __future__ import annotations

import argparse
import logging
import re
import secrets
import signal
import sqlite3
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import auth, gitrepo, images, notes, textfmt
from .comments import (
    Comment,
    CommentPoller,
    CommentStore,
    PostTitles,
    comment_buttons,
    format_comment_confirm,
    format_comment_list,
    format_new_comment,
)
from .config import Config, ConfigError, check_paths, load_config
from .deploys import DeployHistory, DeployTracker, failure_details, record_summary
from .ops import DEPLOY_SH, DEPLOY_UNIT_MAX_SEC, LOG_SOURCES, RESTARTABLE, SHA_ARG_RE, HttpResult, Ops, OpsError, local_get, plan_deploy
from .state import State
from .telegram import FileTooLarge, TelegramClient, TelegramError
from .util import esc, log_exception, redact, shorten

log = logging.getLogger("pnc_bot")

MAX_MESSAGE_AGE = 600
MEDIA_GROUP_DELAY = 2.5
CONFIRM_TTL = 120
MAX_IMAGES = 10
MAX_TOTAL_IMAGE_BYTES = 15 * 1024 * 1024
COMMENT_POLL = 30
LONG_POLL = 50
CONFLICT_SLEEP = 30
MAX_BACKOFF = 60
# Waits between attempts of a Telegram call that failed on the network, with a
# 5xx or a 429 (only on worker, pool and tracker threads, never the poll loop).
RETRY_DELAYS = (2.0, 5.0, 15.0)
SHUTDOWN_NOTICE_WAIT = 5.0

UNSUPPORTED = "暂不支持这种消息"
HEIC_TEXT = images.HEIC_MESSAGE
TOO_OLD = "消息太旧，已忽略"
NOT_A_NOTE_REPLY = (
    "↩️ 回复的消息不对应任何随想，这条没有发布。\n"
    "新随想：直接发送（不要回复），或用 /note 文本\n"
    "追加：回复随想的回执或原消息，或用 /append 随想id 文本"
)
# What the owner is told about unfinished work when the bot stops (SIGTERM).
# Nothing is replayed after a restart.
STOP_QUEUED = "⚠️ 机器人重启了，这条没有处理，请重新发送"
STOP_RUNNING = "⚠️ 机器人重启了，这条可能没处理完（也可能已经提交）：先用 /list 或 /status 看看，再决定要不要重发"
STOP_MEDIA = "⚠️ 机器人重启了，这组图片还没开始处理，请重新发送"
STOP_ACTION_QUEUED = "⚠️ 机器人重启了，这个操作没有执行，需要的话请重新发起"
STOP_ACTION_RUNNING = "⚠️ 机器人重启了，这个操作可能没执行完：用 /status 看看再决定是否重试"
STOP_NO_RESULT = "机器人重启了，没等到部署结果，用 /status 看看"
STOPPING_NO_RESULT = "机器人正在重启，部署结果请用 /status 查看"
STOP_DEPLOY_STARTED = "⏳ 已开始；机器人重启了，没等到结果，用 /status 和 /logs deploy 看看"
STOPPING_DEPLOY_STARTED = "⏳ 已开始；机器人正在重启，结果请用 /status 和 /logs deploy 查看"

COMMAND_RE = re.compile(r"^/([A-Za-z0-9_]{1,32})(?:@[A-Za-z0-9_]{1,64})?(?=\s|$)")
NOTE_ID_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}(?:-\d{1,4})?)(?=\s|$)")
COMMENT_ID_RE = re.compile(r"^#?(\d{1,12})$")
CONFIRM_DATA_RE = re.compile(r"^cf:([0-9a-f]{16}):([yn])$")
HIDE_DATA_RE = re.compile(r"^dc:(\d{1,12})$")

COMMANDS = [
    ("help", "用法"),
    ("note", "记一条随想"),
    ("append", "追加到一条随想"),
    ("draft", "新建文章草稿"),
    ("list", "最近 5 条随想"),
    ("undo", "撤销机器人最近一次提交"),
    ("comments", "最近 10 条评论"),
    ("delcomment", "隐藏一条评论"),
    ("status", "站点状态"),
    ("logs", "查看日志"),
    ("deploy", "部署或回滚"),
    ("restart", "重启服务"),
]
SELFTEST_COMMANDS = {"help", "start", "status", "logs", "list", "comments"}

HELP_HTML = """<b>PNC 博客机器人</b>

<b>写随想</b>
直接发文字和/或图片 → 新随想
第一行 <code># 标题</code> 或 <code>标题 | 正文</code> → 带标题
回复回执或原消息 → 追加到那条随想（回复别的消息不会发布）
/note 文本 — 新随想
/append [id] 文本 — 追加（默认最近一条）
/draft 标题[ | slug] — 文章草稿，后面几行是正文，可带图片
/list — 最近 5 条随想
/undo — 撤销机器人最近一次提交

<b>评论</b>
/comments — 最近 10 条
/delcomment id — 隐藏一条

<b>运维</b>
/status — 服务、健康检查、告警、最近部署
/logs [名称] — 日志
/deploy [sha] — 部署 main，或回到某个提交
/restart 名称 — 重启 pnc-blog / pnc-api / pnc-reactions"""


# How long to wait for a bot-started deploy: the unit's own limit plus slack.
WATCH_DEPLOY_SEC = DEPLOY_UNIT_MAX_SEC + 180


class UserError(Exception):
    """A failure to show the owner as-is (short Chinese text)."""


class Unsupported(Exception):
    def __init__(self, text: str):
        super().__init__(text)
        self.text = text


# --- message parsing ----------------------------------------------------------


@dataclass(frozen=True)
class ImageRef:
    file_id: str
    file_size: int | None


def _int(value: Any) -> int:
    return value if type(value) is int else 0


def parse_command(text: str) -> tuple[str, str] | None:
    """``/cmd@bot args`` → ``("cmd", "args")``; None if not a command."""
    match = COMMAND_RE.match(text or "")
    if not match:
        return None
    return match.group(1).lower(), text[match.end() :].strip()


def message_text(msg: dict) -> tuple[str, list | None]:
    """The text (or caption) of a message with its entities."""
    if isinstance(msg.get("text"), str):
        ents = msg.get("entities")
        return msg["text"], ents if isinstance(ents, list) else None
    if isinstance(msg.get("caption"), str):
        ents = msg.get("caption_entities")
        return msg["caption"], ents if isinstance(ents, list) else None
    return "", None


_UNSUPPORTED_KEYS = (
    "sticker",
    "voice",
    "video",
    "video_note",
    "animation",
    "audio",
    "location",
    "venue",
    "contact",
    "poll",
    "dice",
    "game",
    "story",
    "paid_media",
    "invoice",
)
_HEIC_MIME = {"image/heic", "image/heif", "image/heic-sequence", "image/heif-sequence"}


def message_image(msg: dict) -> ImageRef | None:
    """The image a message carries, None if it has none; raises Unsupported."""
    if any(key in msg for key in _UNSUPPORTED_KEYS):
        raise Unsupported(UNSUPPORTED)
    photo = msg.get("photo")
    if isinstance(photo, list) and photo:
        sizes = [p for p in photo if isinstance(p, dict) and isinstance(p.get("file_id"), str)]
        if not sizes:
            raise Unsupported(UNSUPPORTED)
        best = max(sizes, key=lambda p: (_int(p.get("width")) * _int(p.get("height")), _int(p.get("file_size"))))
        return ImageRef(best["file_id"], best.get("file_size") if type(best.get("file_size")) is int else None)
    doc = msg.get("document")
    if isinstance(doc, dict):
        mime = str(doc.get("mime_type") or "").lower()
        name = str(doc.get("file_name") or "").lower()
        if mime in _HEIC_MIME or name.endswith((".heic", ".heif")):
            raise Unsupported(HEIC_TEXT)
        if mime.startswith("image/") and isinstance(doc.get("file_id"), str):
            size = doc.get("file_size")
            return ImageRef(doc["file_id"], size if type(size) is int else None)
        raise Unsupported(UNSUPPORTED)
    return None


def to_markdown(texts: list[tuple[str, list | None]]) -> str:
    """Telegram texts with entities → one Markdown string."""
    parts = []
    for text, entities in texts:
        try:
            parts.append(textfmt.entities_to_markdown(text, entities))
        except (ValueError, TypeError, KeyError, IndexError):
            parts.append(text)  # malformed entities: keep the plain text
    return "\n\n".join(parts).strip()


def after_command(markdown: str, fallback: str) -> str:
    """The Markdown that follows the leading ``/command``."""
    match = COMMAND_RE.match(markdown)
    return markdown[match.end() :].strip() if match else fallback


def commit_subject(text: str) -> str:
    return shorten(" ".join(redact(text).split()), 72)


def unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def note_path_allowed(rel: str) -> bool:
    """Paths a note commit may touch: notes.md and note images (never drafts)."""
    return gitrepo.is_bot_content_path(rel) and not rel.startswith(notes.POSTS_DIR + "/")


# --- small building blocks ------------------------------------------------------


@dataclass
class Pending:
    action: str
    payload: dict
    expires: float
    message_id: int


class Confirmations:
    """Single-use, expiring confirmations bound to the message carrying the buttons.

    Consumed nonces are remembered for ``ttl`` seconds, so a second tap on the
    same button can be told apart from an expired prompt (and must not
    overwrite the result of the first tap).
    """

    def __init__(self, ttl: float = CONFIRM_TTL, clock: Callable[[], float] = time.monotonic):
        self.ttl = ttl
        self._clock = clock
        self._items: dict[str, Pending] = {}
        self._used: dict[str, float] = {}  # consumed nonce -> forget after
        self._lock = threading.RLock()

    def _prune(self, now: float) -> None:
        for nonce in [n for n, p in self._items.items() if p.expires <= now]:
            del self._items[nonce]
        for nonce in [n for n, until in self._used.items() if until <= now]:
            del self._used[nonce]

    def open(self, action: str, payload: dict, send: Callable[[str], int]) -> str:
        """Create a nonce, let ``send(nonce)`` post the buttons, remember the result.

        The lock is held while sending, so a click can never be looked up
        before its message id is known.
        """
        with self._lock:
            now = self._clock()
            self._prune(now)
            nonce = secrets.token_hex(8)
            message_id = send(nonce)
            self._items[nonce] = Pending(action, payload, now + self.ttl, message_id)
            return nonce

    def take(self, nonce: str, message_id: int) -> Pending | None:
        """Consume a confirmation; None if unknown, expired, used or from another message."""
        with self._lock:
            now = self._clock()
            pending = self._items.pop(nonce, None)
            if pending is None or pending.message_id != message_id or now >= pending.expires:
                return None
            self._used[nonce] = now + self.ttl
            return pending

    def used(self, nonce: str) -> bool:
        """True if this nonce was consumed (confirmed or cancelled) recently."""
        with self._lock:
            until = self._used.get(nonce)
            return until is not None and self._clock() < until

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


class MediaGroups:
    """Buffers album items until no new item arrived for ``delay`` seconds."""

    def __init__(self, delay: float = MEDIA_GROUP_DELAY, clock: Callable[[], float] = time.monotonic, max_items: int = 20):
        self.delay = delay
        self._clock = clock
        self.max_items = max_items
        self._groups: dict[str, list[dict]] = {}
        self._last: dict[str, float] = {}

    def add(self, msg: dict) -> None:
        gid = msg["media_group_id"]
        items = self._groups.setdefault(gid, [])
        if len(items) < self.max_items:
            items.append(msg)
        self._last[gid] = self._clock()

    def due(self) -> list[list[dict]]:
        now = self._clock()
        ready = [gid for gid, last in self._last.items() if now - last >= self.delay]
        out = []
        for gid in ready:
            del self._last[gid]
            out.append(self._groups.pop(gid))
        return out

    def pending(self) -> bool:
        return bool(self._groups)

    def drain(self) -> list[list[dict]]:
        """Every buffered group, ready or not (used when the bot stops)."""
        groups = list(self._groups.values())
        self._groups.clear()
        self._last.clear()
        return groups


class Gate:
    """Holds result callbacks back until ``open()``.

    A job registers its deploy watch before it edits its receipt to
    "已提交，部署中…" (a fast deploy must not be missed), so the result can
    arrive while that edit is still in flight. Callbacks that come early wait
    here and run right after the status edit, never before it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._open = False
        self._waiting: list[Callable[[], None]] = []

    def run(self, fn: Callable[[], None]) -> None:
        with self._lock:
            if not self._open:
                self._waiting.append(fn)
                return
        fn()

    def open(self) -> None:
        with self._lock:
            self._open = True
            waiting, self._waiting = self._waiting, []
        for fn in waiting:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                log_exception(log, "deferred callback failed", exc)


class Interrupts:
    """Unfinished work, and what its message should say if the bot stops now.

    Every queued or running job (and every deploy it waits for) has an entry
    ``key -> (message id, html)``. ``close()`` (on shutdown) takes them all;
    from then on ``update()`` returns False, which tells a job that has not
    started yet to stay away: the owner was already told to resend it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[int, tuple[int | None, str]] = {}
        self._next = 0
        self._closed = False

    def add(self, message_id: int | None, html: str) -> int:
        with self._lock:
            self._next += 1
            if not self._closed:
                self._items[self._next] = (message_id, html)
            return self._next

    def update(self, key: int | None, html: str) -> bool:
        """Change the stop notice; False once the bot is stopping (or the entry is gone)."""
        with self._lock:
            if key is None:
                return not self._closed
            if key not in self._items:
                return False
            self._items[key] = (self._items[key][0], html)
            return True

    def finish(self, key: int | None) -> None:
        with self._lock:
            self._items.pop(key, None)  # type: ignore[arg-type]

    def close(self) -> list[tuple[int | None, str]]:
        with self._lock:
            self._closed = True
            items = list(self._items.values())
            self._items.clear()
            return items

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


class Worker:
    """A named thread pool that counts queued jobs and never leaks exception text."""

    def __init__(self, name: str, threads: int):
        self._pool = ThreadPoolExecutor(max_workers=threads, thread_name_prefix=name)
        self._lock = threading.Lock()
        self._pending = 0

    def submit(self, fn: Callable[..., Any], *args: Any) -> None:
        with self._lock:
            self._pending += 1
        self._pool.submit(self._run, fn, args)

    def _run(self, fn: Callable[..., Any], args: tuple) -> None:
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001
            log_exception(log, "job failed", exc)
        finally:
            with self._lock:
                self._pending -= 1

    def pending(self) -> int:
        with self._lock:
            return self._pending

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=True)


@dataclass
class ContentJob:
    kind: str  # "note", "append" or "draft"
    source_ids: list[int]
    text: str  # Markdown
    images: list[ImageRef]
    target: str | None = None  # append: note id (None = newest bot note)
    title: str | None = None  # draft
    slug: str | None = None  # draft: explicit slug
    receipt_id: int | None = None
    # Messages the source replies to. They are resolved to a note on the git
    # worker, when every earlier job has recorded its own receipt and sources.
    reply_ids: list[int] = field(default_factory=list)
    key: int | None = None  # entry in App.interrupts


# --- read-only views (shared with --selftest-command) ------------------------------


class Views:
    def __init__(self, config: Config, *, ops: Ops, comments: CommentStore, titles: PostTitles, repo: Any):
        self.config = config
        self.ops = ops
        self.comments = comments
        self.titles = titles
        self.repo = repo

    def note_url(self, note_id: str) -> str:
        return f"{self.config.site_url}/notes/{note_id}.html"

    def help(self) -> str:
        return HELP_HTML

    def status(self, *, queue_size: int | None = None, uptime: float | None = None) -> str:
        return self.ops.status_report(queue_size=queue_size, uptime=uptime)

    def logs(self, name: str | None) -> str:
        names = "\n".join(f"/logs {esc(n)}" for n in LOG_SOURCES)
        if not name:
            return f"<b>日志源</b>\n{names}"
        if name not in LOG_SOURCES:
            return f"没有这个日志源。可用：\n{names}"
        return self.ops.logs(name)

    def list_notes(self) -> str:
        self.repo.fetch()
        raw = self.repo.show(notes.NOTES_PATH, "origin/main")
        items = notes.parse_notes(raw)[:5]
        if not items:
            return "还没有随想。"
        lines = ["<b>最近 5 条随想</b>"]
        for note in items:
            url = self.note_url(note.id)
            lines.append(
                f"\n<code>{esc(note.id)}</code> {esc(notes.note_summary(note))}\n<a href=\"{esc(url)}\">{esc(url)}</a>"
            )
        return "\n".join(lines)

    def comment_list(self) -> str:
        return format_comment_list(self.comments.latest(10), self.config.site_url, self.titles)

    def render(self, name: str, rest: str, *, queue_size: int | None = None, uptime: float | None = None) -> str:
        """Render one read-only command."""
        if name in ("help", "start"):
            return self.help()
        if name == "status":
            return self.status(queue_size=queue_size, uptime=uptime)
        if name == "logs":
            return self.logs(rest.split()[0] if rest.split() else None)
        if name == "list":
            return self.list_notes()
        if name == "comments":
            return self.comment_list()
        raise ValueError("not a read-only command")


# --- the application ----------------------------------------------------------------


class App:
    def __init__(
        self,
        config: Config,
        *,
        tg: Any,
        state: State,
        repo: Any,
        history: DeployHistory,
        comments: CommentStore,
        ops: Ops,
        titles: PostTitles | None = None,
        tracker: DeployTracker | None = None,
        git_worker: Any = None,
        pool: Any = None,
        repo_lock: Any = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        http_get: Callable[..., HttpResult] = local_get,
    ):
        self.config = config
        self.chat = config.allowed_user_id
        self.tg = tg
        self.state = state
        self.repo = repo
        self.history = history
        self.comments = comments
        self.ops = ops
        self.titles = titles or PostTitles(config.blog_root)
        self.repo_lock = repo_lock if repo_lock is not None else threading.Lock()
        self.tracker = tracker or DeployTracker(history, repo, self.notify, repo_lock=self.repo_lock)
        self.git = git_worker or Worker("git", 1)
        self.pool = pool or Worker("ro", 2)
        self.clock = clock
        self.monotonic = monotonic
        self.sleep = sleep
        self.http_get = http_get
        self.views = Views(config, ops=ops, comments=comments, titles=self.titles, repo=repo)
        self.confirmations = Confirmations(clock=monotonic)
        self.media = MediaGroups(clock=monotonic)
        self.poller = CommentPoller(comments, state, self.notify_comment)
        self.started = monotonic()
        self.stop_event = threading.Event()
        self.interrupts = Interrupts()
        self._backoff = 0.0
        self._old_groups: OrderedDict[str, None] = OrderedDict()
        # Receipt and source ids of queued note/append jobs: a reply to one of
        # them may only be resolved once that job has run.
        self._pending_ids: set[int] = set()
        self._pending_lock = threading.Lock()
        self._stopped = False

    # --- Telegram helpers (never raise) ------------------------------------

    def _retrying(self, label: str, call: Callable[[], Any], retries: bool) -> Any:
        """``call()``; with ``retries``, network errors, 5xx and 429 are tried again.

        Only for worker, pool and tracker threads: the waits would stall the
        poll loop. Retrying stops as soon as the bot is stopping.
        """
        delays = RETRY_DELAYS if retries else ()
        attempt = 0
        while True:
            try:
                return call()
            except TelegramError as exc:
                code = exc.error_code
                transient = code is None or code == 429 or code >= 500
                if attempt >= len(delays) or not transient or self.stop_event.is_set():
                    raise
                wait = delays[attempt]
                if exc.retry_after:
                    wait = max(wait, float(min(exc.retry_after, MAX_BACKOFF)))
                log.warning("%s failed: %s; retrying in %.0fs", label, exc.description, wait)
                if self.stop_event.wait(wait) or self.stop_event.is_set():
                    raise
                attempt += 1

    def send(self, html: str, *, reply_to: int | None = None, buttons: Any = None, retries: bool = False) -> int | None:
        try:
            result = self._retrying(
                "sendMessage",
                lambda: self.tg.send_message(self.chat, html, reply_to=reply_to, buttons=buttons),
                retries,
            )
        except TelegramError as exc:
            log.warning("sendMessage failed: %s", exc.description)
            return None
        message_id = result.get("message_id") if isinstance(result, dict) else None
        return message_id if type(message_id) is int else None

    def edit(self, message_id: int | None, html: str, *, retries: bool = False) -> None:
        """Edit a bot message; falls back to a new message if that fails."""
        if message_id is None:
            self.send(html, retries=retries)
            return
        try:
            self._retrying("editMessageText", lambda: self.tg.edit_message(self.chat, message_id, html), retries)
        except TelegramError as exc:
            log.warning("editMessageText failed: %s", exc.description)
            if exc.error_code == 400:
                self.send(html, retries=retries)

    def answer(self, callback_id: str, text: str | None = None) -> None:
        try:
            self.tg.answer_callback(callback_id, text)
        except TelegramError as exc:
            log.warning("answerCallbackQuery failed: %s", exc.description)

    def notify(self, html: str) -> None:
        """A standalone message from the deploy tracker's thread."""
        self.send(html, retries=True)

    def notify_comment(self, comment: Comment) -> bool:
        """Send a new-comment notification; False means retry later."""
        html = format_new_comment(comment, self.config.site_url, self.titles.get(comment.post_slug))
        try:
            self.tg.send_message(self.chat, html, buttons=comment_buttons(comment))
            return True
        except TelegramError as exc:
            log.warning("comment notification failed: %s", exc.description)
            return exc.error_code == 400  # a bad request will never succeed: skip it

    def ask(self, action: str, payload: dict, html: str, *, retries: bool = False) -> None:
        """Send a confirmation with ✅/取消 buttons (``retries``: see ``_retrying``)."""

        def send(nonce: str) -> int:
            buttons = [[("✅ 确认", f"cf:{nonce}:y"), ("取消", f"cf:{nonce}:n")]]
            result = self.tg.send_message(self.chat, f"{html}\n\n<i>{CONFIRM_TTL} 秒内有效</i>", buttons=buttons)
            message_id = result.get("message_id") if isinstance(result, dict) else None
            if type(message_id) is not int:
                raise TelegramError("sendMessage: no message id", 0)
            return message_id

        try:
            # One attempt per call: the registry's lock is never held across a wait.
            self._retrying("confirmation", lambda: self.confirmations.open(action, payload, send), retries)
        except TelegramError as exc:
            log.warning("could not send a confirmation: %s", exc.description)

    def error_text(self, exc: BaseException) -> str:
        """User-facing (escaped) text for a job failure."""
        if isinstance(exc, (UserError, images.ImageRejected, OpsError)):
            return esc(str(exc))
        if isinstance(exc, FileTooLarge):
            return "图片太大（单张最多 20 MB）"
        if isinstance(exc, TelegramError):
            return "下载图片失败：" + esc(exc.description)
        if isinstance(exc, gitrepo.PushRejected):
            return "推送一直被拒绝（远端在变），请稍后重试"
        if isinstance(exc, gitrepo.GitError):
            return "提交失败：" + esc(redact(str(exc)))
        if isinstance(exc, ValueError):
            return esc(redact(str(exc)))
        if isinstance(exc, KeyError):
            return "找不到这条随想"
        log_exception(log, "job failed", exc)
        return "内部错误，详见 /logs pnc-bot"

    def _git_job(self, fn: Callable[..., Any], *args: Any) -> None:
        with self.repo_lock:
            fn(*args)

    def _reply_job(self, render: Callable[[], str], reply_to: int | None) -> None:
        try:
            html = render()
        except Exception as exc:  # noqa: BLE001
            html = "❌ " + self.error_text(exc)
        self.send(html, reply_to=reply_to, retries=True)

    # --- the loop -------------------------------------------------------------

    def startup(self) -> None:
        try:
            self.tg.set_my_commands(COMMANDS, self.chat)
        except TelegramError as exc:
            log.warning("setMyCommands failed: %s", exc.description)

    def drain(self) -> None:
        """First start: forget everything queued before the bot existed."""
        updates = self.tg.get_updates(-1, 0)
        ids = [u["update_id"] for u in updates if type(u.get("update_id")) is int]
        self.state.set("update_offset", max(ids) + 1 if ids else 0)
        log.info("drained %d pending update(s)", len(ids))

    def poll_once(self) -> None:
        offset = self.state.get_int("update_offset")
        if offset is None:
            self.drain()
            return
        timeout = 1 if self.media.pending() else LONG_POLL
        updates = self.tg.get_updates(offset, timeout)
        valid = sorted(
            (u for u in updates if type(u.get("update_id")) is int), key=lambda u: u["update_id"]
        )
        for update in valid:
            update_id = update["update_id"]
            if update_id < offset:
                continue
            try:
                self.handle_update(update)
            except Exception as exc:  # noqa: BLE001 - one bad update must not stop the loop
                log_exception(log, "update handling failed", exc)
            offset = update_id + 1
            self.state.set("update_offset", offset)
        self.flush_media()

    def loop_step(self) -> float:
        """One loop iteration with error handling; returns the seconds waited."""
        try:
            self.poll_once()
            self._backoff = 0.0
            return 0.0
        except TelegramError as exc:
            if exc.error_code == 409:
                log.error("getUpdates conflict (409): %s; waiting %ss", exc.description, CONFLICT_SLEEP)
                wait = float(CONFLICT_SLEEP)
            else:
                self._backoff = min(MAX_BACKOFF, self._backoff * 2 if self._backoff else 1.0)
                wait = max(self._backoff, float(min(exc.retry_after or 0, 300)))
                log.warning("getUpdates failed: %s; retrying in %.0fs", exc.description, wait)
        except Exception as exc:  # noqa: BLE001
            log_exception(log, "loop iteration failed", exc)
            wait = 5.0
        self.stop_event.wait(wait)
        return wait

    def run(self) -> None:
        self.startup()
        self.tracker.start()
        threading.Thread(target=self._comment_loop, name="comments", daemon=True).start()
        log.info("pnc-bot started")
        while not self.stop_event.is_set():
            self.loop_step()

    def shutdown(self) -> None:
        """Stop; tell the owner (best effort, a few seconds) about work that will not finish.

        Queued jobs never run, buffered albums are dropped, deploy watches are
        lost and nothing is replayed after a restart, so each of their
        messages says so instead of staying at "处理中/部署中" forever.
        """
        if self._stopped:
            return
        self._stopped = True
        self.stop_event.set()
        self.tracker.stop()
        notices: list[tuple[int, str, bool]] = [
            (message_id, html, False) for message_id, html in self.interrupts.close() if message_id is not None
        ]
        for group in self.media.drain():
            ids = sorted(m["message_id"] for m in group if type(m.get("message_id")) is int)
            if ids:
                notices.append((ids[0], STOP_MEDIA, True))
        if notices:
            sender = threading.Thread(target=self._send_notices, args=(notices,), name="stop-notices", daemon=True)
            sender.start()
            sender.join(SHUTDOWN_NOTICE_WAIT)
            if sender.is_alive():
                log.warning("gave up on the stop notices after %.0fs", SHUTDOWN_NOTICE_WAIT)
        self.git.shutdown()
        self.pool.shutdown()

    def _send_notices(self, notices: list[tuple[int, str, bool]]) -> None:
        for message_id, html, as_reply in notices:
            if as_reply:
                self.send(html, reply_to=message_id)
            else:
                self.edit(message_id, html)

    def _comment_loop(self) -> None:
        while True:
            try:
                self.poller.poll_once()
            except (sqlite3.Error, OSError) as exc:
                log.warning("comment poll failed: %s", type(exc).__name__)
            except Exception as exc:  # noqa: BLE001
                log_exception(log, "comment poll failed", exc)
            if self.stop_event.wait(COMMENT_POLL):
                return

    # --- routing ----------------------------------------------------------------

    def handle_update(self, update: dict) -> None:
        """Authorize first; anything not from the owner's private chat is dropped."""
        kind = auth.classify(update)
        if kind == "message" and auth.authorized_message(update["message"], self.chat):
            self.on_message(update["message"])
            return
        if kind == "callback_query" and auth.authorized_callback(update["callback_query"], self.chat):
            self.on_callback(update["callback_query"])
            return
        log.info("ignored %s", kind)

    def on_message(self, msg: dict) -> None:
        group = msg.get("media_group_id")
        group = group if isinstance(group, str) and group else None
        date = msg.get("date")
        if type(date) is int and self.clock() - date > MAX_MESSAGE_AGE:
            if group is None or group not in self._old_groups:
                if group is not None:
                    self._old_groups[group] = None
                    while len(self._old_groups) > 100:
                        self._old_groups.popitem(last=False)
                self.send(TOO_OLD, reply_to=_int(msg.get("message_id")) or None)
            return
        if group is not None:
            self.media.add(msg)
            return
        self.process([msg])

    def flush_media(self) -> None:
        for group in self.media.due():
            try:
                self.process(group)
            except Exception as exc:  # noqa: BLE001
                log_exception(log, "media group failed", exc)

    @staticmethod
    def reply_ids(msgs: list[dict]) -> list[int]:
        """Ids of the messages these messages reply to (in order, no duplicates)."""
        ids = []
        for msg in msgs:
            reply = msg.get("reply_to_message")
            if isinstance(reply, dict) and type(reply.get("message_id")) is int:
                ids.append(reply["message_id"])
        return list(dict.fromkeys(ids))

    def may_name_a_note(self, message_id: int) -> bool:
        """True if a reply to this message can resolve to a note (now or once queued jobs ran)."""
        # Pending first: a job records its mapping before it leaves the set.
        with self._pending_lock:
            if message_id in self._pending_ids:
                return True
        return self.state.note_for_message(message_id) is not None

    def resolve_reply(self, job: ContentJob) -> bool:
        """Turn ``job.reply_ids`` into a note id (on the git worker, after earlier jobs).

        A plain reply becomes an append; False means it replies to something
        that is no note (any more), so nothing may be published.
        """
        if not job.reply_ids or job.target:
            return True
        target = next((n for n in map(self.state.note_for_message, job.reply_ids) if n), None)
        if job.kind == "note":
            if target is None:
                return False
            job.kind, job.target = "append", target
        elif job.kind == "append" and target is not None:
            job.target = target
        return True

    def _job_ids(self, job: ContentJob) -> list[int]:
        if job.kind not in ("note", "append"):
            return []  # drafts are never mapped to a note
        return [i for i in (job.receipt_id, *job.source_ids) if type(i) is int]

    def _unpend(self, job: ContentJob) -> None:
        with self._pending_lock:
            self._pending_ids.difference_update(self._job_ids(job))

    def process(self, msgs: list[dict]) -> None:
        """Handle one message or one whole media group."""
        msgs = sorted(msgs, key=lambda m: _int(m.get("message_id")))
        source_ids = [m["message_id"] for m in msgs if type(m.get("message_id")) is int]
        first = source_ids[0] if source_ids else None
        try:
            refs = [ref for ref in (message_image(m) for m in msgs) if ref is not None]
        except Unsupported as exc:
            self.send(esc(exc.text), reply_to=first)
            return
        texts = [message_text(m) for m in msgs]
        texts = [(t, e) for t, e in texts if t.strip()]
        raw = "\n\n".join(t for t, _ in texts)
        command = parse_command(raw)
        if command:
            self.command(command[0], command[1], texts, refs, msgs, source_ids)
            return
        if not refs and not raw.strip():
            self.send(UNSUPPORTED, reply_to=first)
            return
        markdown = to_markdown(texts)
        reply_ids = self.reply_ids(msgs)
        if reply_ids and not any(self.may_name_a_note(i) for i in reply_ids):
            # A reply to a comment notification, /status, a draft receipt…:
            # the owner meant something else, so never publish it as a note.
            self.send(NOT_A_NOTE_REPLY, reply_to=first)
            return
        # Note or append is decided when the job runs (resolve_reply).
        self.queue_content(ContentJob("note", source_ids, markdown, refs, reply_ids=reply_ids))

    def command(
        self,
        name: str,
        rest: str,
        texts: list[tuple[str, list | None]],
        refs: list[ImageRef],
        msgs: list[dict],
        source_ids: list[int],
    ) -> None:
        first = source_ids[0] if source_ids else None
        if name in ("note", "append", "draft"):
            body = after_command(to_markdown(texts), rest)
            getattr(self, f"cmd_{name}")(body, refs, msgs, source_ids)
            return
        if refs:
            self.send("图片请直接发送，或配合 /note、/append、/draft 使用", reply_to=first)
            return
        if name in ("help", "start"):
            self.send(self.views.help(), reply_to=first)
        elif name in ("status", "logs", "comments"):
            self.pool.submit(self._reply_job, lambda: self.render(name, rest), first)
        elif name == "list":
            self.git.submit(self._git_job, self._reply_job, lambda: self.render(name, rest), first)
        elif name == "undo":
            self.git.submit(self._git_job, self.prepare_undo, first)
        elif name == "delcomment":
            match = COMMENT_ID_RE.fullmatch(rest)
            if not match:
                self.send("用法：/delcomment 评论编号（/comments 里的 #数字）", reply_to=first)
                return
            self.pool.submit(self.prepare_delcomment, int(match.group(1)))
        elif name == "deploy":
            arg = rest.split()[0] if rest.split() else None
            if arg is not None and not SHA_ARG_RE.fullmatch(arg):
                self.send("用法：/deploy [sha]（sha 是 7–40 位小写十六进制）", reply_to=first)
                return
            self.git.submit(self._git_job, self.prepare_deploy, arg, first)
        elif name == "restart":
            target = rest.split()[0] if rest.split() else ""
            if target not in RESTARTABLE:
                self.send("用法：/restart " + " | ".join(RESTARTABLE), reply_to=first)
                return
            self.ask("restart", {"name": target}, f"确认重启 <b>{esc(target)}</b>？")
        else:
            self.send("未知命令，发送 /help 查看用法", reply_to=first)

    def render(self, name: str, rest: str) -> str:
        return self.views.render(
            name, rest, queue_size=self.git.pending(), uptime=self.monotonic() - self.started
        )

    # --- content commands -------------------------------------------------------

    def cmd_note(self, body: str, refs: list[ImageRef], msgs: list[dict], source_ids: list[int]) -> None:
        if not body and not refs:
            self.send("用法：/note 文本（也可以附图片）", reply_to=source_ids[0] if source_ids else None)
            return
        self.queue_content(ContentJob("note", source_ids, body, refs))

    def cmd_append(self, body: str, refs: list[ImageRef], msgs: list[dict], source_ids: list[int]) -> None:
        target = None
        match = NOTE_ID_RE.match(body)
        if match:
            target = match.group(1)
            body = body[match.end() :].strip()
        if not body and not refs:
            self.send("用法：/append [随想 id] 文本（也可以附图片）", reply_to=source_ids[0] if source_ids else None)
            return
        # Without an explicit id: the replied-to note (resolved when the job
        # runs), else the newest bot note that still exists.
        self.queue_content(ContentJob("append", source_ids, body, refs, target=target, reply_ids=self.reply_ids(msgs)))

    def cmd_draft(self, body: str, refs: list[ImageRef], msgs: list[dict], source_ids: list[int]) -> None:
        first = source_ids[0] if source_ids else None
        if not body:
            self.send("用法：/draft 标题[ | slug]，后面几行是正文（可以附图片）", reply_to=first)
            return
        try:
            title, slug, text = notes.parse_draft_command(body)
            title = textfmt.clean_title(title)
        except ValueError as exc:
            self.send("❌ " + esc(str(exc)), reply_to=first)
            return
        self.queue_content(ContentJob("draft", source_ids, text.strip(), refs, title=title, slug=slug))

    def queue_content(self, job: ContentJob) -> None:
        first = job.source_ids[0] if job.source_ids else None
        if len(job.images) > MAX_IMAGES:
            self.send(f"一次最多 {MAX_IMAGES} 张图片", reply_to=first)
            return
        if any(ref.file_size and ref.file_size > images.MAX_INPUT_BYTES for ref in job.images):
            self.send("图片太大（单张最多 20 MB）", reply_to=first)
            return
        ahead = self.git.pending()
        receipt = "⏳ 处理中…" + (f"（前面还有 {ahead} 个任务）" if ahead else "")
        job.receipt_id = self.send(receipt, reply_to=first)
        job.key = self.interrupts.add(job.receipt_id, STOP_QUEUED)
        with self._pending_lock:
            self._pending_ids.update(self._job_ids(job))
        self.git.submit(self._git_job, self.run_content_job, job)

    def load_images(self, refs: list[ImageRef]) -> list[Any]:
        processed = []
        total = 0
        for ref in refs:
            data = self.tg.download_file(ref.file_id, images.MAX_INPUT_BYTES)
            image = images.process_image(data)
            total += len(image.data)
            if total > MAX_TOTAL_IMAGE_BYTES:
                raise UserError("图片处理后总共超过 15 MB，请少发几张")
            processed.append(image)
        return processed

    @staticmethod
    def _image_files(processed: list[Any], date: str) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        for image in processed:
            files.setdefault(images.image_filename(image, date), image.data)
        return files

    @staticmethod
    def _write_images(folder: Path, files: dict[str, bytes]) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            if not re.fullmatch(images.IMAGE_NAME_RE, name):
                raise ValueError("bad image name")
            (folder / name).write_bytes(data)

    @staticmethod
    def _check_note_images(workdir: Path, markdown: str) -> None:
        folder = workdir / notes.NOTES_ASSET_DIR
        for name in notes.referenced_note_images(markdown):
            if "/" in name or not (folder / name).is_file():
                raise UserError(f"引用的图片不存在：{name}")
        # The site build (src/lib/note-images.ts) fails on any missing or
        # non-image Markdown image under /assets/, not only note images.
        missing = notes.missing_note_assets(markdown, workdir / notes.STATIC_DIR)
        if missing:
            raise UserError(f"引用的图片不存在或不是图片：{missing[0]}")

    def publish_note(self, job: ContentJob, processed: list[Any]) -> tuple[str, str]:
        date = notes.today_shanghai()
        files = self._image_files(processed, date)
        title, body = textfmt.split_title(job.text)
        if title is not None:
            title = textfmt.clean_title(title)
        body = textfmt.sanitize_body(body)
        parts = [body] if body else []
        if files:
            parts.append(notes.image_markdown(list(files), title or "随想配图"))
        full = "\n\n".join(parts)
        if not full.strip():
            raise UserError("内容是空的")

        def mutate(workdir: Path) -> tuple[str, str]:
            path = workdir / notes.NOTES_PATH
            raw = path.read_text(encoding="utf-8")
            new_raw, note_id = notes.insert_note(raw, date=date, title=title, body=full)
            notes.validate_notes(raw, new_raw, expect_added=1)
            self._write_images(workdir / notes.NOTES_ASSET_DIR, files)
            self._check_note_images(workdir, full)
            path.write_text(new_raw, encoding="utf-8")
            note = next((n for n in notes.parse_notes(new_raw) if n.id == note_id), None)
            return note_id, notes.note_summary(note) if note else note_id

        def message(result: tuple[str, str]) -> str:
            note_id, summary = result
            return gitrepo.commit_message(
                commit_subject(f"note: {summary}"), {"Pnc-Bot": "note-add", "Pnc-Bot-Note": note_id}
            )

        sha, (note_id, _) = self.repo.publish(mutate, message=message, allowed=note_path_allowed)
        return sha, note_id

    def publish_append(self, job: ContentJob, processed: list[Any]) -> tuple[str, str]:
        date = notes.today_shanghai()
        files = self._image_files(processed, date)
        text = textfmt.sanitize_body(job.text) if job.text else ""
        candidates = [job.target] if job.target else self.state.recent_note_ids()

        def mutate(workdir: Path) -> str:
            path = workdir / notes.NOTES_PATH
            raw = path.read_text(encoding="utf-8")
            existing = {n.id: n for n in notes.parse_notes(raw)}
            target = next((c for c in candidates if c in existing), None)
            if target is None:
                if job.target:
                    raise UserError(f"没有找到随想 {job.target}")
                raise UserError("没有可追加的随想（机器人建的都不在了）；用 /append 随想id 文本 指定")
            parts = [text] if text else []
            if files:
                parts.append(notes.image_markdown(list(files), existing[target].title or "随想配图"))
            addition = "\n\n".join(parts)
            if not addition.strip():
                raise UserError("内容是空的")
            new_raw = notes.append_to_note(raw, target, addition)
            notes.validate_notes(raw, new_raw, expect_added=0)
            self._write_images(workdir / notes.NOTES_ASSET_DIR, files)
            # The whole updated note: a reference-style image in the addition
            # may use a definition from earlier in the note.
            updated = next(n for n in notes.parse_notes(new_raw) if n.id == target)
            self._check_note_images(workdir, updated.body)
            path.write_text(new_raw, encoding="utf-8")
            return target

        def message(target: str) -> str:
            return gitrepo.commit_message(
                commit_subject(f"note: append to {target}"), {"Pnc-Bot": "note-append", "Pnc-Bot-Note": target}
            )

        return self.repo.publish(mutate, message=message, allowed=note_path_allowed)

    def publish_draft(self, job: ContentJob, processed: list[Any]) -> tuple[str, str]:
        assert job.title
        date = notes.today_shanghai()
        now = datetime.now(timezone.utc)
        files = self._image_files(processed, date)
        chosen: dict[str, str] = {}

        def mutate(workdir: Path) -> str:
            posts = workdir / notes.POSTS_DIR
            existing = {p.name if p.is_dir() else p.stem for p in posts.iterdir()} if posts.is_dir() else set()
            if job.slug:
                if job.slug in existing:
                    raise UserError(f"slug {job.slug} 已经存在")
                slug = job.slug
            else:
                slug = notes.make_slug(job.title, existing, now)
            chosen["slug"] = slug
            folder = posts / slug
            folder.mkdir(parents=True)
            (folder / "index.mdx").write_text(
                notes.draft_document(job.title, date, job.text, list(files)), encoding="utf-8"
            )
            self._write_images(folder, files)
            return slug

        def allowed(rel: str) -> bool:
            slug = chosen.get("slug")
            prefix = f"{notes.POSTS_DIR}/{slug}/"
            if not slug or not rel.startswith(prefix):
                return False
            name = rel[len(prefix) :]
            return name == "index.mdx" or re.fullmatch(images.IMAGE_NAME_RE, name) is not None

        def message(slug: str) -> str:
            return gitrepo.commit_message(commit_subject(f"draft: {slug}"), {"Pnc-Bot": "draft"})

        return self.repo.publish(mutate, message=message, allowed=allowed)

    def run_content_job(self, job: ContentJob) -> None:
        try:
            self._run_content_job(job)
        finally:
            # After the mapping is recorded: a reply always finds one or the other.
            self._unpend(job)

    def _run_content_job(self, job: ContentJob) -> None:
        if not self.interrupts.update(job.key, STOP_RUNNING):
            return  # the bot is stopping; the owner was told to resend this
        try:
            if not self.resolve_reply(job):
                self.interrupts.finish(job.key)
                self.edit(job.receipt_id, NOT_A_NOTE_REPLY, retries=True)
                return
            processed = self.load_images(job.images)
            if job.kind == "note":
                sha, name = self.publish_note(job, processed)
            elif job.kind == "append":
                sha, name = self.publish_append(job, processed)
            else:
                sha, name = self.publish_draft(job, processed)
        except Exception as exc:  # noqa: BLE001
            self.interrupts.finish(job.key)
            self.edit(job.receipt_id, "❌ " + self.error_text(exc), retries=True)
            return
        if job.kind == "note":
            self.state.record_note(name, sha, job.receipt_id, job.source_ids)
        elif job.kind == "append":
            self.state.map_messages(name, [job.receipt_id, *job.source_ids])
        submitted = f"📝 已提交 <code>{esc(sha[:7])}</code>"
        if not self.interrupts.update(job.key, f"{submitted}；{STOP_NO_RESULT}"):
            self.edit(job.receipt_id, f"{submitted}；{STOPPING_NO_RESULT}", retries=True)
            return
        if job.kind == "draft":
            done = lambda: f"✅ 草稿已推送：<code>{esc(notes.POSTS_DIR)}/{esc(name)}/</code>"  # noqa: E731
        else:
            done = lambda: self.note_live(name)  # noqa: E731
        # Watch before the status edit: GitHub may deploy while that edit hangs.
        gate = Gate()
        try:
            self.watch_commit(sha, job.receipt_id, done, key=job.key, gate=gate)
            self.edit(job.receipt_id, f"{submitted}，部署中…", retries=True)
        finally:
            gate.open()

    def note_live(self, note_id: str) -> str:
        url = f"{self.config.site_url}/notes/{note_id}.html"
        status = None
        for attempt in range(3):
            status = self.http_get(f"{self.config.local_site}/notes/{note_id}.html").status
            if status == 200:
                break
            if attempt < 2:
                self.sleep(2)
        html = f'✅ 已上线 <b>{esc(note_id)}</b>\n<a href="{esc(url)}">{esc(url)}</a>'
        if status != 200:
            html += f"\n⚠️ 本地检查返回 {esc(status if status is not None else '无响应')}"
        return html

    def watch_commit(
        self,
        sha: str,
        message_id: int | None,
        success: Callable[[], str],
        *,
        key: int | None = None,
        gate: Gate | None = None,
    ) -> Any:
        """Edit ``message_id`` with the deploy result of commit ``sha``.

        ``gate`` holds the result back until the caller's status edit is
        done; ``key`` is the job's stop notice, dropped once the result is in.
        """

        def final(render: Callable[[], str]) -> None:
            def run() -> None:
                self.interrupts.finish(key)
                self.edit(message_id, render(), retries=True)

            if gate is None:
                run()
            else:
                gate.run(run)

        def ok(record: dict) -> None:
            final(success)

        def failed(record: dict) -> None:
            final(
                lambda: "❌ <b>部署失败</b>\n"
                + failure_details(record, self.history)
                + "\n\n日志：/logs deploy　撤销：/undo"
            )

        def timed_out() -> None:
            final(lambda: f"⌛ 已提交 <code>{esc(sha[:7])}</code>，但 20 分钟内没等到部署结果，用 /status 看看")

        return self.tracker.watch(sha, ok, failed, timed_out)

    # --- confirmations ----------------------------------------------------------

    def on_callback(self, cb: dict) -> None:
        callback_id = cb["id"]
        message_id = cb["message"]["message_id"]
        data = cb.get("data") if isinstance(cb.get("data"), str) else ""
        match = CONFIRM_DATA_RE.fullmatch(data)
        if match:
            pending = self.confirmations.take(match.group(1), message_id)
            if pending is None:
                if self.confirmations.used(match.group(1)):
                    # A second tap: the first one already runs (or ran); its
                    # result must stay on the message.
                    self.answer(callback_id, "已处理")
                    return
                self.answer(callback_id, "已过期")
                self.edit(message_id, "已过期")
                return
            if match.group(2) == "n":
                self.answer(callback_id, "已取消")
                self.edit(message_id, "已取消")
                return
            self.answer(callback_id)
            self.edit(message_id, "⏳ 执行中…")
            self.execute(pending, message_id)
            return
        match = HIDE_DATA_RE.fullmatch(data)
        if match:
            self.answer(callback_id)
            self.pool.submit(self.prepare_delcomment, int(match.group(1)))
            return
        self.answer(callback_id)

    def execute(self, pending: Pending, message_id: int) -> None:
        payload = pending.payload
        key = self.interrupts.add(message_id, STOP_ACTION_QUEUED)
        if pending.action == "undo":
            self.git.submit(self._git_job, self.execute_undo, payload["sha"], message_id, key)
        elif pending.action == "deploy":
            self.pool.submit(self.execute_deploy, payload["plan"], message_id, key)
        elif pending.action == "restart":
            self.pool.submit(self.execute_restart, payload["name"], message_id, key)
        elif pending.action == "delcomment":
            self.pool.submit(self.execute_delcomment, payload["id"], message_id, key)
        else:
            self.interrupts.finish(key)

    def _done(self, key: int | None, message_id: int, html: str) -> None:
        """The final word on an action's message."""
        self.interrupts.finish(key)
        self.edit(message_id, html, retries=True)

    # /undo
    def prepare_undo(self, reply_to: int | None) -> None:
        try:
            candidate = self.repo.find_undo_candidate()
        except Exception as exc:  # noqa: BLE001
            self.send("❌ " + self.error_text(exc), reply_to=reply_to, retries=True)
            return
        if candidate is None:
            self.send("没有可以撤销的机器人提交", reply_to=reply_to, retries=True)
            return
        files = "\n".join(f"• <code>{esc(f)}</code>" for f in list(candidate.files)[:15])
        html = (
            f"确认撤销 <code>{esc(candidate.sha[:7])}</code>？\n{esc(candidate.subject)}\n\n文件：\n{files}"
        )
        self.ask("undo", {"sha": candidate.sha}, html, retries=True)

    def execute_undo(self, sha: str, message_id: int, key: int | None = None) -> None:
        if not self.interrupts.update(key, STOP_ACTION_RUNNING):
            return  # the bot is stopping; the owner was told it did not run
        try:
            candidate = self.repo.find_undo_candidate()
            if candidate is None or candidate.sha != sha:
                self._done(key, message_id, "情况有变（有新的提交或已撤销），请重新 /undo")
                return
            new_sha = self.repo.revert(candidate)
        except Exception as exc:  # noqa: BLE001
            self._done(key, message_id, "❌ " + self.error_text(exc))
            return
        if candidate.kind == "note-add" and candidate.note_id:
            # The id is free again: the next note of that day reuses it, and
            # replies meant for the undone note must not land there.
            try:
                self.state.forget_note(candidate.note_id)
            except sqlite3.Error as exc:
                log.warning("could not forget an undone note: %s", type(exc).__name__)
        reverted = f"↩️ 已撤销 <code>{esc(sha[:7])}</code>（提交 <code>{esc(new_sha[:7])}</code>）"
        if not self.interrupts.update(key, f"{reverted}；{STOP_NO_RESULT}"):
            self.edit(message_id, f"{reverted}；{STOPPING_NO_RESULT}", retries=True)
            return
        gate = Gate()
        try:
            self.watch_commit(
                new_sha,
                message_id,
                lambda: f"✅ 撤销已上线：<code>{esc(sha[:7])}</code> {esc(candidate.subject)}",
                key=key,
                gate=gate,
            )
            self.edit(message_id, f"{reverted}，部署中…", retries=True)
        finally:
            gate.open()

    # /deploy
    def prepare_deploy(self, arg: str | None, reply_to: int | None) -> None:
        try:
            plan = plan_deploy(arg, self.repo, self.history)
        except Exception as exc:  # noqa: BLE001
            self.send("❌ " + self.error_text(exc), reply_to=reply_to, retries=True)
            return
        self.ask("deploy", {"plan": plan}, "确认部署？\n" + plan.describe(), retries=True)

    def execute_deploy(self, plan: Any, message_id: int, key: int | None = None) -> None:
        if not self.interrupts.update(key, STOP_ACTION_RUNNING):
            return  # the bot is stopping: never start a deploy now
        request_id = secrets.token_hex(8)
        described = plan.describe()
        unit: list[str] = []  # filled once systemd-run returned
        cancelled = threading.Event()
        gate = Gate()

        def final(render: Callable[[], str]) -> None:
            def run() -> None:
                if not cancelled.is_set():
                    self._done(key, message_id, render())

            gate.run(run)

        def ok(record: dict) -> None:
            # A no-argument plan deploys whatever main is when deploy.sh
            # starts, so say what actually went live.
            final(lambda: described + "\n\n✅ 部署完成：" + record_summary(record))

        def failed(record: dict) -> None:
            final(lambda: "❌ <b>部署失败</b>\n" + failure_details(record, self.history) + "\n\n日志：/logs deploy")

        def timed_out() -> None:
            final(lambda: described + f"\n\n⌛ {WATCH_DEPLOY_SEC // 60} 分钟没有结果，用 /status 和 /logs deploy 看看")

        def unit_done() -> bool:
            if cancelled.is_set():
                return True  # lets an old tracker drop the watch
            return bool(unit) and bool(self.ops.unit_finished(unit[0]))

        def unit_tail() -> str:
            return self.ops.unit_journal(unit[0]) if unit and not cancelled.is_set() else ""

        # Watch first, start second: a rollback writes its record within a
        # second or two, and the tracker drops records nobody waits for.
        # Name the unit up front (when the ops object can), so the watch
        # knows it even before systemd-run returns.
        name_unit = getattr(self.ops, "deploy_unit", None)
        planned = name_unit(request_id) if callable(name_unit) else None
        self.tracker.watch_request(
            request_id, ok, failed, timed_out, unit_done=unit_done, unit_tail=unit_tail, timeout=WATCH_DEPLOY_SEC
        )
        try:
            try:
                if planned:
                    unit.append(self.ops.start_deploy(plan, request_id, unit=planned))
                else:
                    unit.append(self.ops.start_deploy(plan, request_id))
            except Exception as exc:  # noqa: BLE001
                cancelled.set()
                self._unwatch_request(request_id)
                self._done(key, message_id, "❌ " + self.error_text(exc))
                return
            if self.interrupts.update(key, described + "\n\n" + STOP_DEPLOY_STARTED):
                status = "⏳ 已开始，完成后会更新这条消息"
            else:  # the bot is stopping: nobody will report the result
                status = STOPPING_DEPLOY_STARTED
            self.edit(message_id, described + "\n\n" + status, retries=True)
        finally:
            gate.open()

    def _unwatch_request(self, request_id: str) -> None:
        """Drop the watch of a deploy that never started (if the tracker can)."""
        unwatch = getattr(self.tracker, "unwatch_request", None)
        if callable(unwatch):
            try:
                unwatch(request_id)
            except Exception as exc:  # noqa: BLE001
                log_exception(log, "could not drop a deploy watch", exc)

    # /restart
    def execute_restart(self, name: str, message_id: int, key: int | None = None) -> None:
        if not self.interrupts.update(key, STOP_ACTION_RUNNING):
            return
        try:
            html = self.ops.restart(name)
        except Exception as exc:  # noqa: BLE001
            html = "❌ " + self.error_text(exc)
        self._done(key, message_id, html)

    # /delcomment
    def prepare_delcomment(self, comment_id: int) -> None:
        try:
            comment = self.comments.get(comment_id)
        except (sqlite3.Error, OSError) as exc:
            self.send(f"❌ 读不了评论库（{esc(type(exc).__name__)}）", retries=True)
            return
        if comment is None:
            self.send(f"没有评论 #{comment_id}", retries=True)
            return
        if not comment.visible:
            self.send(f"评论 #{comment_id} 已经是隐藏状态", retries=True)
            return
        html = format_comment_confirm(comment, self.config.site_url, self.titles.get(comment.post_slug))
        self.ask("delcomment", {"id": comment_id}, html, retries=True)

    def execute_delcomment(self, comment_id: int, message_id: int, key: int | None = None) -> None:
        if not self.interrupts.update(key, STOP_ACTION_RUNNING):
            return
        try:
            hidden = self.comments.hide(comment_id)
        except (sqlite3.Error, OSError) as exc:
            self._done(key, message_id, f"❌ 隐藏失败（{esc(type(exc).__name__)}）")
            return
        if hidden:
            self._done(key, message_id, f"🙈 已隐藏评论 #{comment_id}")
        else:
            self._done(key, message_id, f"评论 #{comment_id} 没有变化（不存在或已经隐藏）")


# --- entry points -----------------------------------------------------------------------


def _repo(config: Config) -> Any:
    return gitrepo.Repo(config.repo, lock_path=config.state_dir / "git.lock")


def build_app(config: Config) -> App:
    tg = TelegramClient(config.token, config.proxy)
    state = State(config.state_dir)
    repo = _repo(config)
    history = DeployHistory(config.blog_root)
    ops = Ops(config, history, repo)
    return App(
        config,
        tg=tg,
        state=state,
        repo=repo,
        history=history,
        comments=CommentStore(config.comments_db),
        ops=ops,
    )


def check_config_main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    print("config loaded")
    for key, value in config.summary():
        print(f"  {key}: {value}")
    rows = check_paths(config, DEPLOY_SH)
    for ok, name, detail in rows:
        print(f"  [{'ok' if ok else 'FAIL'}] {name}: {detail}")
    return 0 if all(ok for ok, _, _ in rows) else 1


def selftest_main(command: str) -> int:
    """Run one read-only command against the real system and print its HTML.

    Never reads the token and never talks to Telegram.
    """
    parsed = parse_command((command or "").strip())
    if parsed is None or parsed[0] not in SELFTEST_COMMANDS:
        print("refused: only /help, /status, /logs [name], /list and /comments can be self-tested", file=sys.stderr)
        return 2
    try:
        config = load_config(read_token_file=False)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    history = DeployHistory(config.blog_root)
    if parsed[0] == "list":
        # /list fetches under the git lock, which lives in the state dir.
        config.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    repo = _repo(config)
    views = Views(
        config,
        ops=Ops(config, history, repo),
        comments=CommentStore(config.comments_db),
        titles=PostTitles(config.blog_root),
        repo=repo,
    )
    try:
        html = views.render(parsed[0], parsed[1])
    except Exception as exc:  # noqa: BLE001
        print(f"failed: {type(exc).__name__}: {redact(str(exc))}", file=sys.stderr)
        return 1
    print(html)
    return 0


def serve() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        log.error("config error: %s", exc)
        return 1
    app = build_app(config)

    def on_term(signum: int, frame: Any) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, on_term)
    try:
        app.run()
    except (SystemExit, KeyboardInterrupt):
        log.info("stopping")
    finally:
        app.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pnc-bot", description="Private Telegram bot for PNC's Blog")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-config", action="store_true", help="validate config and paths, print a summary")
    mode.add_argument("--selftest-command", metavar="CMD", help="run one read-only command and print its HTML")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(threadName)s %(name)s: %(message)s"
    )
    if args.check_config:
        return check_config_main()
    if args.selftest_command is not None:
        return selftest_main(args.selftest_command)
    return serve()
