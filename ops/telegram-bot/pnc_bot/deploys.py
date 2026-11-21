"""Deploy history (written by deploy.sh) and the tracker that reports results.

deploy.sh appends one JSON line per finished attempt to
``<blog_root>/state/deploys.jsonl``. The tracker tails that file and matches
records to whoever is waiting for them: a pushed commit (success when the
deployed sha is that commit or contains it) or a bot-started deploy (matched
by request id). Failures nobody waits for become standalone notifications;
successes nobody waits for are silent.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .gitrepo import GitError
from .util import esc, log_exception, redact, shanghai_time, strip_ansi, tail_text

log = logging.getLogger("pnc_bot.deploys")

RELEASE_RE = re.compile(r"^(\d{8}-\d{6})-([0-9a-f]{12})$")
LOG_NAME_RE = re.compile(r"^deploy-[0-9]{8}-[0-9]{6}-[0-9A-Za-z_-]{1,40}\.log$")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
MAX_READ = 1024 * 1024

Record = dict[str, Any]
Callback = Callable[..., None]


class DeployHistory:
    def __init__(self, blog_root: Path):
        self.root = Path(blog_root)
        self.releases_dir = self.root / "releases"
        self.logs_dir = self.root / "logs"
        self.jsonl = self.root / "state" / "deploys.jsonl"
        self.last_file = self.root / "state" / "last-deploy.json"

    def current_release(self) -> tuple[str, str] | None:
        """``(release name, sha12)`` being served, or None (sha12 "" if unparsable)."""
        try:
            target = os.readlink(self.root / "current")
        except OSError:
            return None
        name = os.path.basename(target.rstrip("/"))
        match = RELEASE_RE.fullmatch(name)
        return (name, match.group(2) if match else "")

    def releases(self) -> list[str]:
        """Kept release directory names, oldest first."""
        try:
            entries = list(os.scandir(self.releases_dir))
        except OSError:
            return []
        names = [e.name for e in entries if RELEASE_RE.fullmatch(e.name) and e.is_dir(follow_symlinks=False)]
        return sorted(names)

    def last(self) -> Record | None:
        """The latest record (``result`` may be ``running``)."""
        try:
            with open(self.last_file, "rb") as fh:
                data = json.loads(fh.read(65536))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def end_offset(self) -> int:
        try:
            return self.jsonl.stat().st_size
        except OSError:
            return 0

    def read_new(self, offset: int) -> tuple[list[Record], int]:
        """Complete records after byte ``offset`` and the offset to continue from.

        A partial last line is left for the next call. If the file shrank
        (truncated or rotated) reading restarts at 0.
        """
        try:
            size = self.jsonl.stat().st_size
        except OSError:
            return [], 0
        if offset > size or offset < 0:
            offset = 0
        if offset == size:
            return [], offset
        with open(self.jsonl, "rb") as fh:
            fh.seek(offset)
            chunk = fh.read(MAX_READ)
        end = chunk.rfind(b"\n")
        if end < 0:
            if len(chunk) >= MAX_READ:  # one absurdly long line: skip it
                return [], offset + len(chunk)
            return [], offset
        records: list[Record] = []
        for line in chunk[: end + 1].splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                log.warning("skipping an unreadable line in deploys.jsonl")
                continue
            if isinstance(record, dict):
                records.append(record)
        return records, offset + end + 1

    def _tail_records(self) -> list[Record]:
        """Complete records in the last ``MAX_READ`` bytes of deploys.jsonl."""
        try:
            with open(self.jsonl, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                start = max(0, fh.tell() - MAX_READ)
                fh.seek(start)
                chunk = fh.read(MAX_READ)
        except OSError:
            return []
        if start > 0:  # began inside a line
            chunk = chunk[chunk.find(b"\n") + 1 :] if b"\n" in chunk else b""
        chunk = chunk[: chunk.rfind(b"\n") + 1]  # a partial last line is still being written
        records = []
        for line in chunk.splitlines():
            try:
                record = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    def release_ok(self, name: str) -> bool:
        """True if the last finished deploy or rollback onto release ``name`` succeeded.

        A failed health check is recorded against the release that failed it
        (deploy.sh switches back), so a kept release that was rejected, or
        one with no record at all, is not known to be good.
        """
        if not isinstance(name, str) or not RELEASE_RE.fullmatch(name):
            return False
        last = None
        for record in self._tail_records():
            if record.get("release") == name and record.get("result") in ("success", "failure"):
                last = record["result"]
        return last == "success"

    def safe_log_path(self, value: object) -> Path | None:
        """A deploy log path from a record, only if it really is one of ours."""
        if not isinstance(value, str) or not value:
            return None
        path = Path(value)
        if not LOG_NAME_RE.fullmatch(path.name):
            return None
        try:
            if path.resolve().parent != self.logs_dir.resolve():
                return None
        except OSError:
            return None
        return path if path.is_file() else None

    def latest_log(self) -> Path | None:
        try:
            names = [e.name for e in os.scandir(self.logs_dir) if LOG_NAME_RE.fullmatch(e.name) and e.is_file()]
        except OSError:
            return None
        return self.logs_dir / max(names) if names else None

    def log_tail(self, path: Path, lines: int = 40) -> str:
        """The last ``lines`` lines of a log file (bounded read)."""
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 128 * 1024))
                data = fh.read()
        except OSError as exc:
            return f"(无法读取日志：{exc.strerror or type(exc).__name__})"
        text = strip_ansi(data.decode("utf-8", errors="replace"))
        return "\n".join(text.splitlines()[-lines:])


def record_summary(record: Record) -> str:
    """One line describing a deploy record (HTML)."""
    result = record.get("result")
    icon = {"success": "✅", "failure": "❌", "running": "⏳"}.get(result, "❔")
    sha = record.get("sha")
    target = f"<code>{esc(sha[:12])}</code>" if isinstance(sha, str) and SHA_RE.fullmatch(sha) else ""
    if record.get("mode") == "rollback" and isinstance(record.get("release"), str):
        target = f"回滚到 <code>{esc(record['release'])}</code>"
    when = record.get("finished_at") or record.get("started_at")
    parts = [f"{icon} {esc(result or '?')}", target, esc(shanghai_time(when)), esc(record.get("trigger") or "?")]
    return " · ".join(p for p in parts if p)


def failure_details(record: Record, history: DeployHistory, *, lines: int = 15) -> str:
    """Error and the last log lines of a failed deploy (HTML, redacted)."""
    out = [record_summary(record)]
    error = record.get("error")
    if isinstance(error, str) and error.strip():
        out.append(f"错误：{esc(redact(error.strip())[:500])}")
    text = record.get("journal") if isinstance(record.get("journal"), str) else None
    if text is None:
        path = history.safe_log_path(record.get("log"))
        text = history.log_tail(path, lines) if path else None
    if text:
        text = redact("\n".join(text.splitlines()[-lines:]))
        # Trimmed after escaping, so the message limit never cuts the newest lines.
        out.append(f"<pre>{tail_text(esc(text), 2500)}</pre>")
    return "\n".join(out)


@dataclass(eq=False)
class _Watch:
    key: str  # "sha" or "request"
    value: str
    on_success: Callback
    on_failure: Callback
    on_timeout: Callback
    deadline: float
    unit_done: Callable[[], bool] | None = None
    unit_tail: Callable[[], str] | None = None
    done_since: float | None = None


class DeployTracker:
    """Background thread that turns deploy records into callbacks."""

    def __init__(
        self,
        history: DeployHistory,
        repo: Any,
        notify: Callable[[str], None],
        *,
        poll: float = 5,
        timeout: float = 1200,
        fallback_grace: float = 60,
        repo_lock: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.history = history
        self.repo = repo
        self.notify = notify
        self.poll = poll
        self.timeout = timeout
        self.fallback_grace = fallback_grace
        self._repo_lock = repo_lock if repo_lock is not None else threading.Lock()
        self._clock = clock
        self._lock = threading.Lock()
        self._watches: list[_Watch] = []
        self._offset: int | None = None
        self._fetched: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- public ----------------------------------------------------------

    def prime(self) -> None:
        """Start from the current end of deploys.jsonl (history is never replayed)."""
        self._offset = self.history.end_offset()

    def start(self) -> None:
        self.prime()
        self._thread = threading.Thread(target=self._run, name="deploy-tracker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def pending(self) -> int:
        with self._lock:
            return len(self._watches)

    def watch(self, sha: str, on_success: Callback, on_failure: Callback, on_timeout: Callback) -> None:
        """Wait for a deploy that contains commit ``sha`` (full sha)."""
        if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
            raise ValueError("invalid sha")
        self._add(_Watch("sha", sha, on_success, on_failure, on_timeout, self._clock() + self.timeout))

    def watch_request(
        self,
        request_id: str,
        on_success: Callback,
        on_failure: Callback,
        on_timeout: Callback,
        *,
        unit_done: Callable[[], bool] | None = None,
        unit_tail: Callable[[], str] | None = None,
        timeout: float | None = None,
    ) -> None:
        """Wait for the record of a bot-started deploy (``request`` field).

        If ``unit_done`` keeps reporting that the transient unit finished and
        still no record arrived ``fallback_grace`` seconds after it first
        did, ``on_failure`` gets a synthetic record whose ``journal`` holds
        ``unit_tail()``. ``timeout`` (default: the tracker's) bounds the wait.
        """
        if not request_id:
            raise ValueError("empty request id")
        self._add(
            _Watch(
                "request",
                request_id,
                on_success,
                on_failure,
                on_timeout,
                self._clock() + (self.timeout if timeout is None else timeout),
                unit_done=unit_done,
                unit_tail=unit_tail,
            )
        )

    def unwatch_request(self, request_id: str) -> bool:
        """Stop waiting for a bot-started deploy (e.g. it could not be started)."""
        with self._lock:
            for w in self._watches:
                if w.key == "request" and w.value == request_id:
                    self._watches.remove(w)
                    return True
        return False

    def poll_once(self) -> None:
        if self._offset is None:
            self.prime()
        records, self._offset = self.history.read_new(self._offset or 0)
        for record in records:
            self._dispatch(record)
        self._check_watches()

    # --- internals -------------------------------------------------------

    def _add(self, watch: _Watch) -> None:
        with self._lock:
            self._watches.append(watch)

    def _run(self) -> None:
        while not self._stop.wait(self.poll):
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 - keep the thread alive
                log_exception(log, "deploy tracker poll failed", exc)

    def _call(self, fn: Callback, *args: Any) -> None:
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001
            log_exception(log, "deploy callback failed", exc)

    def _known(self, sha: str) -> bool:
        try:
            self.repo.resolve(sha)
            return True
        except GitError:
            pass
        if sha in self._fetched:
            return False
        self._fetched.add(sha)
        try:
            self.repo.fetch()
            self.repo.resolve(sha)
            return True
        except GitError:
            return False

    def _contains(self, deployed: str, watched: str) -> bool:
        """True if ``deployed`` is ``watched`` or has it as an ancestor."""
        if deployed == watched:
            return True
        with self._repo_lock:
            if not self._known(deployed):
                return False
            try:
                return bool(self.repo.is_ancestor(watched, deployed))
            except GitError:
                return False

    def _matches(self, watch: _Watch, record: Record) -> bool:
        if watch.key == "request":
            return record.get("request") == watch.value
        sha = record.get("sha")
        if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
            return False
        return self._contains(sha, watch.value)

    def _dispatch(self, record: Record) -> None:
        result = record.get("result")
        if result not in ("success", "failure"):
            return
        with self._lock:
            watches = list(self._watches)
        matched: list[_Watch] = [w for w in watches if self._matches(w, record)]
        with self._lock:
            for w in matched:
                if w in self._watches:
                    self._watches.remove(w)
        for w in matched:
            self._call(w.on_success if result == "success" else w.on_failure, record)
        if not matched and result == "failure":
            self._call(self.notify, "❌ <b>部署失败</b>\n" + failure_details(record, self.history))

    def _check_watches(self) -> None:
        now = self._clock()
        expired: list[_Watch] = []
        orphaned: list[_Watch] = []
        with self._lock:
            watches = list(self._watches)
        for w in watches:
            if now >= w.deadline:
                expired.append(w)
                continue
            if w.unit_done is None:
                continue
            # Asked on every poll: the grace period runs only while every
            # answer says "finished". One wrong answer (a probe that timed
            # out) is undone by the next one that sees the unit running.
            try:
                done = bool(w.unit_done())
            except Exception as exc:  # noqa: BLE001 - unknown: change nothing
                log_exception(log, "unit check failed", exc)
                continue
            if not done:
                w.done_since = None
            elif w.done_since is None:
                w.done_since = now
            elif now - w.done_since >= self.fallback_grace:
                orphaned.append(w)
        with self._lock:
            for w in expired + orphaned:
                if w in self._watches:
                    self._watches.remove(w)
        for w in expired:
            self._call(w.on_timeout)
        for w in orphaned:
            tail = ""
            if w.unit_tail is not None:
                try:
                    tail = w.unit_tail()
                except Exception as exc:  # noqa: BLE001
                    log_exception(log, "unit journal failed", exc)
            self._call(
                w.on_failure,
                {"result": "failure", "error": "部署进程已结束，但没有写入部署记录", "journal": tail},
            )
