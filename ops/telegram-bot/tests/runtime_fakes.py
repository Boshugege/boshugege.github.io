"""Fakes and fixtures shared by the runtime tests (not a test module itself)."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

from pnc_bot.config import Config
from pnc_bot.gitrepo import GitError

OWNER = 424242
TOKEN = "123456789:AAH" + "x" * 32

# pnc-api's schema (SERVER.md §3.2), comments table only.
COMMENTS_SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS comments (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  post_slug       TEXT NOT NULL,
  author_name     TEXT NOT NULL,
  content         TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'visible',
  browser_name    TEXT,
  browser_version TEXT,
  os_name         TEXT,
  os_version      TEXT,
  country_code    TEXT,
  referer         TEXT,
  ip_hash         TEXT,
  user_agent_hash TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS comments_by_post ON comments (post_slug, status, created_at);
"""


def make_config(root: Path, **overrides) -> Config:
    values = dict(
        allowed_user_id=OWNER,
        token_file=root / "token",
        proxy="http://127.0.0.1:7890",
        repo=root / "repo",
        state_dir=root / "state",
        blog_root=root / "blog",
        comments_db=root / "pnc.db",
        site_url="https://example.test",
        local_site="http://127.0.0.1:46213",
        alertmanager="http://127.0.0.1:19093",
        token="",
    )
    values.update(overrides)
    return Config(**values)


def make_blog(root: Path, releases: list[str], current: str | None) -> Path:
    blog = root / "blog"
    (blog / "releases").mkdir(parents=True, exist_ok=True)
    (blog / "logs").mkdir(exist_ok=True)
    (blog / "state").mkdir(exist_ok=True)
    for name in releases:
        (blog / "releases" / name).mkdir(exist_ok=True)
    if current:
        link = blog / "current"
        if link.is_symlink():
            link.unlink()
        os.symlink(blog / "releases" / current, link)
    return blog


def write_records(blog: Path, *records: dict, partial: str = "") -> None:
    with open(blog / "state" / "deploys.jsonl", "a", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
        fh.write(partial)


def make_comments_db(path: Path, rows: list[dict]) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(COMMENTS_SCHEMA)
    for row in rows:
        conn.execute(
            "INSERT INTO comments (post_slug, author_name, content, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                row.get("post_slug", "/posts/hello.html"),
                row.get("author_name", "读者"),
                row.get("content", "写得好"),
                row.get("status", "visible"),
                row.get("created_at", "2026-11-21T14:03:00.000Z"),
            ),
        )
    conn.commit()
    conn.close()


class FakeRunner:
    """Records argv lists; answers from a list of (prefix, CompletedProcess) rules."""

    def __init__(self, rules=None):
        self.calls: list[list[str]] = []
        self.rules = rules or []

    def __call__(self, argv, timeout=30):
        argv = list(argv)
        assert all(isinstance(a, str) for a in argv), argv
        self.calls.append(argv)
        for prefix, result in self.rules:
            if argv[: len(prefix)] == list(prefix):
                return subprocess.CompletedProcess(argv, *result)
        return subprocess.CompletedProcess(argv, 0, "active\n", "")


class FakeGitRepo:
    """Enough of gitrepo.Repo for planning and tracking."""

    def __init__(self, head: str, *, known=(), ancestors=(), subjects=None, remote_only=()):
        self.head_sha = head
        self.known = set(known) | {head}
        self.remote_only = set(remote_only)  # become known after fetch()
        self.ancestors = set(ancestors)  # (a, b) pairs: a is an ancestor of b
        self.subjects = subjects or {}
        self.fetches = 0

    def fetch(self):
        self.fetches += 1
        self.known |= self.remote_only

    def resolve(self, rev):
        if rev == "origin/main":
            return self.head_sha
        matches = [sha for sha in self.known if sha.startswith(rev)]
        if len(matches) != 1:
            raise GitError(f"找不到提交 {rev}")
        return matches[0]

    def is_ancestor(self, a, b):
        if a not in self.known or b not in self.known:
            raise GitError("unknown")
        return a == b or (a, b) in self.ancestors

    def subject(self, sha):
        for full, subject in self.subjects.items():
            if full.startswith(sha):
                return subject
        raise GitError("unknown")
