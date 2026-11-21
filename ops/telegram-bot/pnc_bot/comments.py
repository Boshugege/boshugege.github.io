"""Read pnc-api's comments table and hide comments.

Reads use a read-only SQLite connection; the only write is hiding a visible
comment (``status='visible'`` → ``'hidden'``), which is exactly what the
site's own runbook does by hand.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from .util import esc, shanghai_time, shorten

# Same rule as pnc-api's SLUG: comments are keyed by the post's site path.
POST_SLUG_RE = re.compile(r"^/posts/[A-Za-z0-9._-]+\.html$")
CONTENT_LIMIT = 800

_COLUMNS = "id, post_slug, author_name, content, status, created_at"


@dataclass(frozen=True)
class Comment:
    id: int
    post_slug: str
    author_name: str
    content: str
    status: str
    created_at: str

    @property
    def visible(self) -> bool:
        return self.status == "visible"


def _row(row: tuple) -> Comment:
    cid, slug, author, content, status, created = row
    return Comment(int(cid), str(slug or ""), str(author or ""), str(content or ""), str(status or ""), str(created or ""))


class CommentStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    def _read(self) -> sqlite3.Connection:
        uri = f"file:{quote(str(self.db_path))}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.execute("PRAGMA query_only = ON")
        return conn

    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        conn = self._read()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def max_id(self) -> int:
        rows = self._query("SELECT COALESCE(MAX(id), 0) FROM comments")
        return int(rows[0][0])

    def since(self, last_id: int, limit: int = 20) -> list[Comment]:
        rows = self._query(f"SELECT {_COLUMNS} FROM comments WHERE id > ? ORDER BY id LIMIT ?", (last_id, limit))
        return [_row(r) for r in rows]

    def latest(self, limit: int = 10) -> list[Comment]:
        rows = self._query(f"SELECT {_COLUMNS} FROM comments ORDER BY id DESC LIMIT ?", (limit,))
        return [_row(r) for r in rows]

    def get(self, comment_id: int) -> Comment | None:
        rows = self._query(f"SELECT {_COLUMNS} FROM comments WHERE id = ?", (comment_id,))
        return _row(rows[0]) if rows else None

    def hide(self, comment_id: int) -> bool:
        """Hide a visible comment; False if it does not exist or is not visible."""
        conn = sqlite3.connect(self.db_path, timeout=5)
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            with conn:
                cur = conn.execute(
                    "UPDATE comments SET status = 'hidden' WHERE id = ? AND status = 'visible'", (comment_id,)
                )
            return cur.rowcount == 1
        finally:
            conn.close()


class PostTitles:
    """Post titles from the deployed release's index.json, re-read every minute."""

    def __init__(self, blog_root: Path, *, ttl: float = 60, clock=time.monotonic):
        self.path = Path(blog_root) / "current" / "index.json"
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._expires = 0.0
        self._titles: dict[str, str] = {}

    def _load(self) -> dict[str, str]:
        titles: dict[str, str] = {}
        try:
            with open(self.path, "rb") as fh:
                data = json.loads(fh.read(8 * 1024 * 1024))
        except (OSError, ValueError):
            return titles
        if isinstance(data, list):
            for row in data:
                if isinstance(row, dict):
                    slug = "/" + str(row.get("url") or "").lstrip("/")
                    if POST_SLUG_RE.fullmatch(slug):
                        titles[slug] = str(row.get("title") or "")
        return titles

    def get(self, slug: str) -> str | None:
        with self._lock:
            if self._clock() >= self._expires:
                self._titles = self._load()
                self._expires = self._clock() + self._ttl
            return self._titles.get(slug) or None


def post_link(comment: Comment, site_url: str, title: str | None) -> str:
    """HTML for the post a comment belongs to (a link when the slug is valid)."""
    label = esc(title or comment.post_slug or "?")
    if POST_SLUG_RE.fullmatch(comment.post_slug):
        return f'<a href="{esc(site_url.rstrip("/") + comment.post_slug)}">{label}</a>'
    return label


def format_new_comment(comment: Comment, site_url: str, title: str | None) -> str:
    """The notification for a new comment."""
    lines = [
        f"💬 <b>新评论 #{comment.id}</b>",
        f"📄 {post_link(comment, site_url, title)}",
        f"👤 {esc(shorten(comment.author_name, 60))} · {esc(shanghai_time(comment.created_at))}",
    ]
    if not comment.visible:
        lines.append(f"（状态：{esc(comment.status)}）")
    lines += ["", esc(shorten(comment.content, CONTENT_LIMIT))]
    return "\n".join(lines)


def comment_buttons(comment: Comment) -> list[list[tuple[str, str]]] | None:
    """The 隐藏这条 button, which starts the /delcomment confirmation."""
    if not comment.visible:
        return None
    return [[("隐藏这条", f"dc:{comment.id}")]]


def format_comment_list(comments: list[Comment], site_url: str, titles: PostTitles | None) -> str:
    """The reply to /comments (newest first, hidden ones marked)."""
    if not comments:
        return "还没有评论。"
    lines = [f"<b>最近 {len(comments)} 条评论</b>"]
    for c in comments:
        title = titles.get(c.post_slug) if titles else None
        mark = "" if c.visible else " 🙈 已隐藏"
        lines.append("")
        lines.append(
            f"<b>#{c.id}</b>{mark} · {esc(shorten(c.author_name, 40))} · {esc(shanghai_time(c.created_at))}"
        )
        lines.append(f"📄 {post_link(c, site_url, title)}")
        lines.append(esc(shorten(c.content, 200)))
    lines.append("")
    lines.append("隐藏：/delcomment &lt;id&gt;")
    return "\n".join(lines)


def format_comment_confirm(comment: Comment, site_url: str, title: str | None) -> str:
    return "\n".join(
        [
            f"确认隐藏评论 <b>#{comment.id}</b>？",
            f"📄 {post_link(comment, site_url, title)}",
            f"👤 {esc(shorten(comment.author_name, 60))} · {esc(shanghai_time(comment.created_at))}",
            "",
            esc(shorten(comment.content, CONTENT_LIMIT)),
        ]
    )


class CommentPoller:
    """Turns new rows of the comments table into notifications.

    The first run only records the current max id (no backlog). ``notify``
    returns False for a transient failure, so that comment is retried on the
    next run; True (sent, or permanently undeliverable) moves past it.
    """

    KEY = "comments_last_id"

    def __init__(self, store: CommentStore, state: Any, notify: Callable[[Comment], bool], *, batch: int = 20):
        self.store = store
        self.state = state
        self.notify = notify
        self.batch = batch

    def poll_once(self) -> int:
        last = self.state.get_int(self.KEY)
        current = self.store.max_id()
        if last is None or current < last:
            self.state.set(self.KEY, current)
            return 0
        sent = 0
        for comment in self.store.since(last, self.batch):
            if not self.notify(comment):
                break
            self.state.set(self.KEY, comment.id)
            sent += 1
        return sent
