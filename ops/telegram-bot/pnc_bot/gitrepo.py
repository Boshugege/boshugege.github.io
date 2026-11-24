"""Git operations on the bot's own clone of the blog repo.

Every git call is a fixed argv list (no shell), runs with hooks and credential
helpers disabled, never prompts, and has a timeout. Mutations happen under an
exclusive file lock and always leave the work tree clean: on any failure the
clone is reset to the remote branch.
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import subprocess
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from .images import IMAGE_NAME_RE
from .linkpreview import PREVIEWS_PATH
from .notes import NOTES_ASSET_DIR, NOTES_PATH, POSTS_DIR

__all__ = [
    "GitError",
    "PushRejected",
    "BOT_NAME",
    "BOT_EMAIL",
    "TRAILER_KIND",
    "TRAILER_NOTE",
    "TRAILER_REVERTS",
    "UNDOABLE_KINDS",
    "UndoCandidate",
    "commit_message",
    "is_bot_content_path",
    "Repo",
]

log = logging.getLogger(__name__)

BOT_NAME = "pnc-bot"
BOT_EMAIL = "pnc-bot@parityncsvt.top"
TRAILER_KIND = "Pnc-Bot"  # note-add | note-append | draft | undo
TRAILER_NOTE = "Pnc-Bot-Note"
TRAILER_REVERTS = "Pnc-Bot-Reverts"
UNDOABLE_KINDS = frozenset({"note-add", "note-append", "draft"})
SUBJECT_MAX = 72

_IMAGE_NAME = IMAGE_NAME_RE.lstrip("^").rstrip("$")
_BOT_PATH_RE = re.compile(
    rf"(?:{re.escape(NOTES_PATH)}"
    rf"|{re.escape(PREVIEWS_PATH)}"
    rf"|{re.escape(NOTES_ASSET_DIR)}/{_IMAGE_NAME}"
    rf"|{re.escape(POSTS_DIR)}/[a-z0-9][a-z0-9-]*/(?:index\.mdx|{_IMAGE_NAME}))"
)
_REF_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
_REV_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@{}~^-]{0,199}")
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_TRAILER_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")
_SECRET_RES = (
    (re.compile(r"(?<=://)[^/\s@]+@"), "***@"),  # user:password@ in URLs
    (re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}"), "***"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "***"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "***"),
)
# Env vars that would point git at another repository.
_REPO_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_NAMESPACE")

T = TypeVar("T")
Runner = Callable[..., subprocess.CompletedProcess]


class GitError(Exception):
    """A git operation failed; the message is short and safe to show."""


class PushRejected(GitError):
    """The remote branch moved on (non-fast-forward / fetch first)."""


@dataclass(frozen=True)
class UndoCandidate:
    sha: str
    subject: str
    kind: str
    note_id: str | None
    files: tuple[str, ...]


def commit_message(subject: str, trailers: dict[str, str]) -> str:
    """One-line subject (≤ 72 chars), a blank line, then ``Key: value`` trailers."""
    subject = " ".join(subject.split())
    if not subject:
        raise ValueError("empty commit subject")
    if len(subject) > SUBJECT_MAX:
        subject = subject[: SUBJECT_MAX - 1].rstrip() + "…"
    lines = []
    for key, value in trailers.items():
        value = str(value)
        if not _TRAILER_KEY_RE.fullmatch(key) or not value or "\n" in value or "\r" in value:
            raise ValueError(f"bad trailer {key!r}")
        lines.append(f"{key}: {value}")
    if not lines:
        return subject + "\n"
    return f"{subject}\n\n" + "\n".join(lines) + "\n"


def is_bot_content_path(path: str) -> bool:
    """Paths the bot may write: notes.md, note images (link thumbnails are
    note images), the link previews file, draft index.mdx + images."""
    return _BOT_PATH_RE.fullmatch(path) is not None


def _redact(text: str) -> str:
    for pattern, replacement in _SECRET_RES:
        text = pattern.sub(replacement, text)
    return text


def _tail(output: bytes, lines: int = 3, limit: int = 300) -> str:
    text = output.decode("utf-8", "replace")
    kept = [line.strip() for line in text.splitlines() if line.strip()][-lines:]
    joined = " | ".join(kept)
    return _redact(joined[-limit:])


def _check_ref_name(name: str) -> str:
    if not _REF_NAME_RE.fullmatch(name) or ".." in name:
        raise ValueError(f"bad ref name: {name!r}")
    return name


def _check_rev(rev: str) -> str:
    if not isinstance(rev, str) or not _REV_RE.fullmatch(rev) or ".." in rev:
        raise GitError("无效的版本号")
    return rev


class Repo:
    """The bot's clone of the blog repo."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        lock_path: str | os.PathLike[str],
        branch: str = "main",
        remote: str = "origin",
        run: Runner = subprocess.run,
        timeout: float = 120,
    ) -> None:
        self.path = Path(path)
        self.lock_path = Path(lock_path)
        self.branch = _check_ref_name(branch)
        self.remote = _check_ref_name(remote)
        self._run = run
        self.timeout = timeout
        self._held = threading.local()

    @property
    def upstream(self) -> str:
        """``<remote>/<branch>``, the remote-tracking ref."""
        return f"{self.remote}/{self.branch}"

    # -- plumbing ---------------------------------------------------------

    def _env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items() if key not in _REPO_ENV}
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["LC_ALL"] = "C.UTF-8"
        if extra:
            env.update(extra)
        return env

    def _git(
        self,
        *args: str,
        input: bytes | None = None,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        argv = [
            "git",
            "-C",
            str(self.path),
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "credential.helper=",
            *args,
        ]
        name = args[0] if args else "git"
        try:
            proc = self._run(
                argv,
                input=input if input is not None else b"",
                capture_output=True,
                env=self._env(env),
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise GitError(f"git {name} 超时") from None
        except OSError as error:
            raise GitError(f"无法运行 git {name}：{error.strerror or type(error).__name__}") from None
        if check and proc.returncode != 0:
            detail = _tail(proc.stderr or b"") or _tail(proc.stdout or b"") or f"exit {proc.returncode}"
            raise GitError(f"git {name} 失败：{detail}")
        return proc

    def _out(self, *args: str) -> str:
        return (self._git(*args).stdout or b"").decode("utf-8", "replace").strip()

    # -- locking ----------------------------------------------------------

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Exclusive ``flock`` on ``lock_path``; re-entrant within a thread."""
        depth = getattr(self._held, "depth", 0)
        if depth:
            self._held.depth = depth + 1
            try:
                yield
            finally:
                self._held.depth = depth
            return
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._held.depth = 1
            try:
                yield
            finally:
                self._held.depth = 0
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    # -- queries ----------------------------------------------------------

    def fetch(self) -> None:
        """Update ``<remote>/<branch>`` from the remote."""
        refspec = f"+refs/heads/{self.branch}:refs/remotes/{self.remote}/{self.branch}"
        with self.locked():
            self._git("fetch", "--prune", "--no-tags", self.remote, refspec)

    def sync(self) -> str:
        """Fetch, then force the work tree to exactly ``<remote>/<branch>``; return HEAD."""
        with self.locked():
            self.fetch()
            self._reset_to_upstream()
            return self.head()

    def _reset_to_upstream(self) -> None:
        self._git("checkout", "--force", "-B", self.branch, self.upstream)
        self._git("reset", "--hard", self.upstream)
        self._git("clean", "-ffdx")

    def _restore(self) -> None:
        """Best-effort cleanup after a failure; never masks the original error."""
        try:
            self._git("revert", "--abort", check=False)
            self._reset_to_upstream()
        except GitError as error:
            log.warning("could not reset the work tree: %s", error)
            try:
                self._git("reset", "--hard", "HEAD", check=False)
                self._git("clean", "-ffdx", check=False)
            except GitError:
                pass

    def head(self) -> str:
        return self._out("rev-parse", "--verify", "HEAD^{commit}")

    def resolve(self, rev: str) -> str:
        """Full commit sha for ``rev``; GitError if it does not name a commit."""
        _check_rev(rev)
        proc = self._git("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}", check=False)
        sha = (proc.stdout or b"").decode("ascii", "replace").strip()
        if proc.returncode != 0 or not _SHA_RE.fullmatch(sha):
            raise GitError(f"找不到提交 {rev}")
        return sha

    def is_ancestor(self, a: str, b: str) -> bool:
        """True if commit ``a`` is ``b`` or an ancestor of it."""
        proc = self._git("merge-base", "--is-ancestor", _check_rev(a), _check_rev(b), check=False)
        if proc.returncode == 0:
            return True
        if proc.returncode == 1:
            return False
        raise GitError(f"git merge-base 失败：{_tail(proc.stderr or b'') or proc.returncode}")

    def subject(self, sha: str) -> str:
        return self._out("log", "-1", "--format=%s", _check_rev(sha), "--")

    def show(self, path: str, rev: str) -> str:
        """Content of ``path`` at ``rev`` (UTF-8 text)."""
        _check_rev(rev)
        if not path or path.startswith(("/", "-")) or "\0" in path or "\n" in path:
            raise GitError("无效的路径")
        data = self._git("cat-file", "blob", f"{rev}:{path}").stdout or b""
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise GitError(f"{path} 不是 UTF-8 文本") from None

    def changed_paths(self) -> list[str]:
        """Every modified, deleted or untracked path in the work tree."""
        raw = self._git("status", "--porcelain=v1", "-z", "--untracked-files=all").stdout or b""
        entries = raw.split(b"\0")
        paths: list[str] = []
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if len(entry) < 4:
                continue
            status, path = entry[:2], entry[3:]
            paths.append(path.decode("utf-8", "surrogateescape"))
            if b"R" in status or b"C" in status:  # the next entry is the source path
                if index < len(entries) and entries[index]:
                    paths.append(entries[index].decode("utf-8", "surrogateescape"))
                index += 1
        return paths

    # -- mutations --------------------------------------------------------

    def _commit(self, message: str) -> str:
        identity = {
            "GIT_AUTHOR_NAME": BOT_NAME,
            "GIT_AUTHOR_EMAIL": BOT_EMAIL,
            "GIT_COMMITTER_NAME": BOT_NAME,
            "GIT_COMMITTER_EMAIL": BOT_EMAIL,
        }
        self._git(
            "commit",
            "--no-verify",
            "--no-gpg-sign",
            "--cleanup=whitespace",
            "--file=-",
            input=message.encode("utf-8"),
            env=identity,
        )
        return self.head()

    def _push(self) -> None:
        proc = self._git("push", "--no-verify", self.remote, f"HEAD:refs/heads/{self.branch}", check=False)
        if proc.returncode == 0:
            return
        output = (proc.stderr or b"") + b"\n" + (proc.stdout or b"")
        text = output.decode("utf-8", "replace")
        detail = _tail(output) or f"exit {proc.returncode}"
        raced = (
            "[rejected]" in text
            or "non-fast-forward" in text
            or "fetch first" in text
            or "cannot lock ref" in text
        )
        if raced:
            raise PushRejected(f"git push 被拒绝（远端有新提交）：{detail}")
        raise GitError(f"git push 失败：{detail}")

    def _push_commit(self, sha: str) -> None:
        """Push HEAD (== ``sha``); an unclear failure counts as success if the remote has ``sha``.

        A timeout or a dropped connection can come after the remote already
        updated the branch: reporting that as a failure would make the owner
        send the same note again. A rejection is never ambiguous.
        """
        try:
            self._push()
        except PushRejected:
            raise
        except GitError as error:
            if not self._landed(sha):
                raise
            log.warning("git push reported a failure, but the remote has the commit: %s", error)

    def _landed(self, sha: str) -> bool:
        """True if ``<remote>/<branch>`` (fetched now) contains ``sha``."""
        try:
            self.fetch()
            return self.is_ancestor(sha, self.upstream)
        except GitError as error:
            log.warning("could not check whether the push landed: %s", error)
            return False

    def publish(
        self,
        mutate: Callable[[Path], T],
        *,
        message: Callable[[T], str],
        allowed: Callable[[str], bool],
        attempts: int = 3,
    ) -> tuple[str, T]:
        """Sync, let ``mutate`` edit the work tree, commit as pnc-bot and push.

        ``mutate`` runs again from a fresh sync when the push loses a race.
        Every changed path must pass ``allowed``. Returns (commit sha, result).
        """
        if attempts < 1:
            raise ValueError("attempts must be >= 1")
        with self.locked():
            for attempt in range(1, attempts + 1):
                try:
                    self.sync()
                    result = mutate(self.path)
                    paths = self.changed_paths()
                    if not paths:
                        raise GitError("没有任何改动可以提交")
                    refused = [path for path in paths if not allowed(path)]
                    if refused:
                        raise GitError(f"不允许修改的路径：{refused[0]}")
                    self._git("add", "-A", "--", *paths)
                    sha = self._commit(message(result))
                    self._push_commit(sha)
                    return sha, result
                except PushRejected:
                    self._restore()
                    if attempt == attempts:
                        raise
                    log.info("push lost a race, retrying (%d/%d)", attempt + 1, attempts)
                except BaseException:
                    self._restore()
                    raise
        raise AssertionError("unreachable")  # pragma: no cover

    def find_undo_candidate(self, limit: int = 50) -> UndoCandidate | None:
        """Newest bot content commit on ``<remote>/<branch>`` that can be undone."""
        self.fetch()
        raw = self._git(
            "log",
            "-z",
            "--no-merges",
            f"--max-count={int(limit)}",
            "--format=%H%n%ae%n%s%n%(trailers:only=true,unfold=true)",
            self.upstream,
            "--",
        ).stdout or b""

        reverted: set[str] = set()
        for record in raw.split(b"\0"):
            if not record.strip():
                continue
            lines = record.decode("utf-8", "replace").split("\n")
            if len(lines) < 3 or not _SHA_RE.fullmatch(lines[0]):
                continue
            sha, email, subject = lines[0], lines[1], lines[2]
            trailers = _parse_trailers(lines[3:])
            for value in trailers.get(TRAILER_REVERTS, []):
                reverted.add(value.lower())

            kinds = trailers.get(TRAILER_KIND, [])
            if email.lower() != BOT_EMAIL or len(kinds) != 1 or kinds[0] not in UNDOABLE_KINDS:
                continue
            if any(sha.startswith(value) for value in reverted if len(value) >= 7):
                continue
            files = self._commit_files(sha)
            if not files or not all(is_bot_content_path(path) for path in files):
                continue
            notes = trailers.get(TRAILER_NOTE, [])
            return UndoCandidate(
                sha=sha,
                subject=subject,
                kind=kinds[0],
                note_id=notes[0] if notes else None,
                files=tuple(files),
            )
        return None

    def _commit_files(self, sha: str) -> list[str]:
        raw = self._git(
            "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "--no-renames", "--root", sha
        ).stdout or b""
        return [item.decode("utf-8", "surrogateescape") for item in raw.split(b"\0") if item]

    def revert(self, candidate: UndoCandidate, *, attempts: int = 3) -> str:
        """Revert ``candidate`` on top of the remote branch and push; return the new sha."""
        sha = candidate.sha
        if not _SHA_RE.fullmatch(sha):
            raise GitError("无效的提交")
        message = commit_message(
            f'Revert "{candidate.subject}"',
            {TRAILER_KIND: "undo", TRAILER_REVERTS: sha},
        )
        with self.locked():
            for attempt in range(1, attempts + 1):
                try:
                    self.sync()
                    if not self.is_ancestor(sha, "HEAD"):
                        raise GitError("要撤销的提交不在远端分支上")
                    proc = self._git("revert", "--no-commit", sha, check=False)
                    if proc.returncode != 0:
                        raise GitError("撤销会和后来的改动冲突，已放弃；请在电脑上手动处理")
                    changed = self.changed_paths()
                    if not changed:
                        raise GitError("这次提交的改动已经不在了，无需撤销")
                    if not set(changed) <= set(candidate.files):
                        raise GitError("撤销会改动意料之外的文件，已放弃")
                    new_sha = self._commit(message)
                    self._push_commit(new_sha)
                    return new_sha
                except PushRejected:
                    self._restore()
                    if attempt == attempts:
                        raise
                    log.info("push lost a race, retrying (%d/%d)", attempt + 1, attempts)
                except BaseException:
                    self._restore()
                    raise
        raise AssertionError("unreachable")  # pragma: no cover


def _parse_trailers(lines: list[str]) -> dict[str, list[str]]:
    trailers: dict[str, list[str]] = {}
    for line in lines:
        key, separator, value = line.partition(":")
        if separator and _TRAILER_KEY_RE.fullmatch(key.strip()) and value.strip():
            trailers.setdefault(key.strip(), []).append(value.strip())
    return trailers
