"""Git tests against a temporary bare remote and local clones (fully offline)."""

import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from pnc_bot.gitrepo import (
    BOT_EMAIL,
    GitError,
    PushRejected,
    Repo,
    UndoCandidate,
    commit_message,
    is_bot_content_path,
)
from pnc_bot.notes import NOTES_PATH, append_to_note, insert_note, parse_notes

FIXTURE_NOTES = (Path(__file__).resolve().parent / "fixtures" / "notes.md").read_text(encoding="utf-8")
IMAGE = "src/static/assets/notes/20261121-0123456789.webp"
IMAGE_2 = "src/static/assets/notes/20261121-abcdefabcd.webp"
DRAFT = "src/content/posts/my-draft/index.mdx"


class Sandbox:
    """A bare "GitHub" remote, a seed commit and three clones: bot, other, seed."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.config = base / "gitconfig"
        self.config.write_text(
            "[user]\n\tname = Owner\n\temail = owner@example.com\n"
            "[init]\n\tdefaultBranch = main\n[advice]\n\tdetachedHead = false\n",
            encoding="utf-8",
        )
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update({"GIT_CONFIG_GLOBAL": str(self.config), "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C.UTF-8"})
        self.before_push = None  # hook(argv) -> CompletedProcess | None, see run()
        self.remote = base / "remote.git"
        self.git(base, "init", "-q", "--bare", "-b", "main", str(self.remote))

        seed = self.clone("seed")
        self.write(seed, NOTES_PATH, FIXTURE_NOTES)
        self.write(seed, "README.md", "# blog\n")
        self.write(seed, ".gitignore", "ignored.txt\n")
        self.write(seed, "src/content/posts/hello/index.mdx", "---\ntitle: hi\n---\n")
        self.git(seed, "add", "-A")
        self.git(seed, "commit", "-q", "-m", "initial")
        self.git(seed, "push", "-q", "origin", "HEAD:refs/heads/main")
        self.bot_path = self.clone("bot")
        self.other = self.clone("other")

    # -- helpers ----------------------------------------------------------

    def git(self, cwd: Path, *args: str, input: str | None = None) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, env=self.env, capture_output=True, text=True, input=input, timeout=60
        )
        if proc.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
        return proc.stdout.strip()

    def remote_git(self, *args: str) -> str:
        return self.git(self.base, "--git-dir", str(self.remote), *args)

    def clone(self, name: str) -> Path:
        path = self.base / name
        self.git(self.base, "clone", "-q", str(self.remote), str(path))
        return path

    @staticmethod
    def write(root: Path, relpath: str, text: str) -> None:
        target = root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def run(self, argv, **kwargs):
        """Injected into Repo: isolates git from the user's config, allows a push hook."""
        env = dict(kwargs.pop("env", None) or os.environ)
        env.update({"GIT_CONFIG_GLOBAL": str(self.config), "GIT_CONFIG_NOSYSTEM": "1"})
        if self.before_push is not None and len(argv) > 7 and argv[7] == "push":
            replaced = self.before_push(argv)
            if replaced is not None:
                return replaced
        return subprocess.run(argv, env=env, **kwargs)

    def repo(self, path: Path | None = None) -> Repo:
        return Repo(path or self.bot_path, lock_path=self.base / "bot.lock", run=self.run)

    def remote_head(self) -> str:
        return self.remote_git("rev-parse", "refs/heads/main")

    def remote_file(self, relpath: str) -> str | None:
        proc = subprocess.run(
            ["git", "--git-dir", str(self.remote), "show", f"main:{relpath}"],
            env=self.env, capture_output=True, text=True, timeout=60,
        )
        return proc.stdout if proc.returncode == 0 else None

    def other_commit(self, files: dict[str, str | None], message: str = "owner edit", *, email: str | None = None) -> str:
        """Commit and push from the "other" clone (the owner's laptop)."""
        self.git(self.other, "fetch", "-q", "origin")
        self.git(self.other, "reset", "-q", "--hard", "origin/main")
        for relpath, text in files.items():
            if text is None:
                (self.other / relpath).unlink()
            else:
                self.write(self.other, relpath, text)
        self.git(self.other, "add", "-A")
        identity = ["-c", f"user.email={email}"] if email else []
        self.git(self.other, *identity, "commit", "-q", "-F", "-", input=message)
        self.git(self.other, "push", "-q", "origin", "HEAD:refs/heads/main")
        return self.git(self.other, "rev-parse", "HEAD")

    def status(self, path: Path | None = None) -> str:
        return self.git(path or self.bot_path, "status", "--porcelain", "--untracked-files=all", "--ignored")


def add_note_mutation(title: str, body: str, images: tuple[str, ...] = (IMAGE,)):
    def mutate(workdir: Path) -> str:
        notes_file = workdir / NOTES_PATH
        new_raw, note_id = insert_note(
            notes_file.read_text(encoding="utf-8"), date="2026-11-21", title=title, body=body
        )
        notes_file.write_text(new_raw, encoding="utf-8")
        for image in images:
            target = workdir / image
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"RIFF fake webp " + image.encode())
        return note_id

    return mutate


def note_message(kind: str = "note-add"):
    return lambda note_id: commit_message(f"note: {note_id}", {"Pnc-Bot": kind, "Pnc-Bot-Note": note_id})


class GitTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pnc-bot-git-")
        self.box = Sandbox(Path(self._tmp.name))
        self.repo = self.box.repo()

    def tearDown(self):
        self._tmp.cleanup()

    def assert_clean(self):
        self.assertEqual(self.box.status(), "")
        self.assertFalse((self.box.bot_path / ".git" / "REVERT_HEAD").exists())

    def publish_note(self, title: str = "春天", body: str = "正文", images=(IMAGE,), kind: str = "note-add"):
        return self.repo.publish(add_note_mutation(title, body, images), message=note_message(kind), allowed=is_bot_content_path)


class CommitMessageTests(unittest.TestCase):
    def test_format(self):
        self.assertEqual(
            commit_message("note: 春天", {"Pnc-Bot": "note-add", "Pnc-Bot-Note": "2026-04-11"}),
            "note: 春天\n\nPnc-Bot: note-add\nPnc-Bot-Note: 2026-04-11\n",
        )

    def test_subject_is_one_line_and_short(self):
        message = commit_message("note:  a\nb  " + "字" * 100, {"Pnc-Bot": "note-add"})
        subject = message.split("\n")[0]
        self.assertTrue(subject.startswith("note: a b 字"))
        self.assertEqual(len(subject), 72)
        self.assertTrue(subject.endswith("…"))

    def test_bad_input(self):
        for subject, trailers in (
            ("", {}),
            ("ok", {"Bad Key": "x"}),
            ("ok", {"Pnc-Bot": "a\nb"}),
            ("ok", {"Pnc-Bot": ""}),
        ):
            with self.subTest(subject=subject, trailers=trailers), self.assertRaises(ValueError):
                commit_message(subject, trailers)


class BotPathTests(unittest.TestCase):
    def test_allowed(self):
        for path in (
            NOTES_PATH,
            IMAGE,
            "src/data/link-previews.json",
            "src/content/posts/my-draft/index.mdx",
            "src/content/posts/a/20261121-0123456789.webp",
        ):
            self.assertTrue(is_bot_content_path(path), path)

    def test_rejected(self):
        for path in (
            "README.md",
            "src/content/notes.md.bak",
            "src/data/link-previews.json.bak",
            "src/data/other.json",
            "src/static/assets/notes/evil.webp",
            "src/static/assets/notes/sub/20261121-0123456789.webp",
            "src/content/posts/My-Draft/index.mdx",
            "src/content/posts/-x/index.mdx",
            "src/content/posts/a/index.md",
            "src/content/posts/a/b/index.mdx",
            "src/content/posts/../index.mdx",
            "src/content/posts/a/photo.png",
            "/src/content/notes.md",
            ".github/workflows/deploy.yml",
        ):
            self.assertFalse(is_bot_content_path(path), path)


class QueryTests(GitTestCase):
    def test_sync_resets_everything_local(self):
        bot = self.box.bot_path
        Sandbox.write(bot, "untracked/dir/file.txt", "x")
        Sandbox.write(bot, "ignored.txt", "x")
        Sandbox.write(bot, "README.md", "changed")
        self.box.git(bot, "commit", "-q", "-am", "local only")
        Sandbox.write(bot, "README.md", "dirty again")
        upstream = self.box.other_commit({"README.md": "from laptop\n"})

        self.assertEqual(self.repo.sync(), upstream)
        self.assertEqual(self.repo.head(), upstream)
        self.assert_clean()
        self.assertEqual((bot / "README.md").read_text(encoding="utf-8"), "from laptop\n")
        self.assertFalse((bot / "untracked").exists())
        self.assertFalse((bot / "ignored.txt").exists())

    def test_resolve_ancestor_subject_show(self):
        first = self.repo.sync()
        second = self.box.other_commit({"README.md": "v2\n"}, "second commit\n\nbody")
        self.repo.fetch()
        self.assertEqual(self.repo.resolve("origin/main"), second)
        self.assertEqual(self.repo.resolve(second[:10]), second)
        self.assertEqual(self.repo.resolve("HEAD"), first)
        self.assertTrue(self.repo.is_ancestor(first, second))
        self.assertTrue(self.repo.is_ancestor(second, second))
        self.assertFalse(self.repo.is_ancestor(second, first))
        self.assertEqual(self.repo.subject(second), "second commit")
        self.assertEqual(self.repo.show("README.md", "origin/main"), "v2\n")
        self.assertEqual(self.repo.show(NOTES_PATH, first), FIXTURE_NOTES)

    def test_bad_revisions_and_paths(self):
        self.repo.sync()
        for rev in ("deadbeef", "no-such-branch", "--upload-pack=touch /tmp/x", "-h", "a..b", "x:y", "", "HEAD\n"):
            with self.subTest(rev=rev), self.assertRaises(GitError):
                self.repo.resolve(rev)
        with self.assertRaises(GitError):
            self.repo.show("missing.txt", "HEAD")
        with self.assertRaises(GitError):
            self.repo.show("/etc/passwd", "HEAD")
        with self.assertRaises(GitError):
            self.repo.is_ancestor("0" * 40, "HEAD")

    def test_changed_paths(self):
        self.repo.sync()
        bot = self.box.bot_path
        Sandbox.write(bot, "README.md", "changed")
        Sandbox.write(bot, "new dir/with space.txt", "x")
        Sandbox.write(bot, "src/static/assets/notes/20261121-0123456789.webp", "x")
        (bot / "src/content/posts/hello/index.mdx").unlink()
        self.assertEqual(
            sorted(self.repo.changed_paths()),
            sorted([
                "README.md",
                "new dir/with space.txt",
                "src/static/assets/notes/20261121-0123456789.webp",
                "src/content/posts/hello/index.mdx",
            ]),
        )
        self.repo.sync()
        self.assertEqual(self.repo.changed_paths(), [])


class PublishTests(GitTestCase):
    def test_publish_note_with_image(self):
        sha, note_id = self.publish_note("春天", "花开了")
        self.assertEqual(note_id, "2026-11-21")
        self.assertEqual(self.box.remote_head(), sha)
        self.assert_clean()
        log = self.box.remote_git("log", "-1", "--format=%an|%ae|%cn|%ce|%s", sha)
        self.assertEqual(log, f"pnc-bot|{BOT_EMAIL}|pnc-bot|{BOT_EMAIL}|note: 2026-11-21")
        trailers = self.box.remote_git("log", "-1", "--format=%(trailers:only,unfold)", sha)
        self.assertEqual(trailers, "Pnc-Bot: note-add\nPnc-Bot-Note: 2026-11-21")
        notes = parse_notes(self.box.remote_file(NOTES_PATH))
        self.assertEqual((notes[0].id, notes[0].title, notes[0].body), ("2026-11-21", "春天", "花开了"))
        self.assertIsNotNone(self.box.remote_file(IMAGE))
        changed = self.box.remote_git("diff-tree", "--no-commit-id", "--name-only", "-r", sha).split("\n")
        self.assertEqual(sorted(changed), sorted([NOTES_PATH, IMAGE]))

    def test_publish_builds_on_latest_remote(self):
        upstream = self.box.other_commit({"README.md": "laptop\n"})
        sha, _ = self.publish_note()
        self.assertEqual(self.box.remote_git("rev-parse", f"{sha}^"), upstream)

    def test_push_race_is_retried(self):
        calls = []
        mutate = add_note_mutation("赛跑", "正文")

        def racing(workdir: Path) -> str:
            calls.append(workdir)
            result = mutate(workdir)
            if len(calls) == 1:  # someone else pushes while we are working
                self.racer = self.box.other_commit({"README.md": "raced\n"}, "race")
            return result

        sha, note_id = self.repo.publish(racing, message=note_message(), allowed=is_bot_content_path)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.box.remote_head(), sha)
        self.assertEqual(self.box.remote_git("rev-parse", f"{sha}^"), self.racer)
        self.assertEqual(self.box.remote_file("README.md"), "raced\n")
        self.assertEqual(parse_notes(self.box.remote_file(NOTES_PATH))[0].title, "赛跑")
        self.assert_clean()

    def test_push_race_gives_up_after_attempts(self):
        calls = []
        mutate = add_note_mutation("赛跑", "正文")

        def always_racing(workdir: Path) -> str:
            calls.append(1)
            result = mutate(workdir)
            self.box.other_commit({"README.md": f"race {len(calls)}\n"}, "race")
            return result

        with self.assertRaises(PushRejected):
            self.repo.publish(always_racing, message=note_message(), allowed=is_bot_content_path)
        self.assertEqual(len(calls), 3)
        self.assertNotIn(BOT_EMAIL, self.box.remote_git("log", "--format=%ae", "main"))
        self.assert_clean()
        self.assertEqual(self.repo.head(), self.repo.resolve("origin/main"))

    def test_disallowed_path_is_rejected_and_tree_reset(self):
        before = self.box.remote_head()
        mutate = add_note_mutation("坏", "正文")

        def sneaky(workdir: Path) -> str:
            result = mutate(workdir)
            Sandbox.write(workdir, "README.md", "pwned")
            Sandbox.write(workdir, ".github/workflows/x.yml", "on: push")
            return result

        with self.assertRaisesRegex(GitError, "不允许修改的路径"):
            self.repo.publish(sneaky, message=note_message(), allowed=is_bot_content_path)
        self.assertEqual(self.box.remote_head(), before)
        self.assert_clean()
        self.assertEqual((self.box.bot_path / "README.md").read_text(encoding="utf-8"), "# blog\n")
        self.assertFalse((self.box.bot_path / ".github").exists())
        self.assertFalse((self.box.bot_path / IMAGE).exists())

    def test_no_changes(self):
        with self.assertRaisesRegex(GitError, "没有任何改动"):
            self.repo.publish(lambda workdir: None, message=lambda _: "x", allowed=is_bot_content_path)
        self.assert_clean()

    def test_mutate_error_propagates_and_resets(self):
        def failing(workdir: Path) -> None:
            Sandbox.write(workdir, NOTES_PATH, "garbage")
            raise ValueError("validation failed")

        with self.assertRaisesRegex(ValueError, "validation failed"):
            self.repo.publish(failing, message=lambda _: "x", allowed=is_bot_content_path)
        self.assert_clean()
        self.assertEqual((self.box.bot_path / NOTES_PATH).read_text(encoding="utf-8"), FIXTURE_NOTES)

    def test_other_push_failures_are_not_retried(self):
        before = self.box.remote_head()
        pushes = []

        def declined(argv):
            pushes.append(argv)
            return subprocess.CompletedProcess(
                argv, 1, b"", b"remote: error: GH006: Protected branch update failed\n"
                b" ! [remote rejected] HEAD -> main (protected branch hook declined)\n"
                b"error: failed to push some refs to 'git@github.com:x/y.git'\n",
            )

        self.box.before_push = declined
        with self.assertRaises(GitError) as caught:
            self.publish_note()
        self.assertNotIsInstance(caught.exception, PushRejected)
        self.assertIn("protected branch hook declined", str(caught.exception))
        self.assertEqual(len(pushes), 1)
        self.assertEqual(self.box.remote_head(), before)
        self.assertEqual(self.repo.head(), before)  # the unpushed local commit is gone
        self.assert_clean()

    def push_then(self, outcome):
        """A before_push hook: the push really happens, then the client sees ``outcome``."""

        def hook(argv):
            self.box.before_push = None
            try:
                proc = self.box.run(argv, input=b"", capture_output=True, env=self.repo._env(), timeout=60)
            finally:
                self.box.before_push = hook
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return outcome(argv)

        return hook

    @staticmethod
    def hung_up(argv):
        return subprocess.CompletedProcess(argv, 128, b"", b"fatal: the remote end hung up unexpectedly\n")

    @staticmethod
    def timed_out(argv):
        raise subprocess.TimeoutExpired(argv, 120)

    def test_push_failure_after_the_remote_took_the_commit(self):
        for outcome in (self.hung_up, self.timed_out):
            with self.subTest(outcome=outcome.__name__):
                self.box.before_push = self.push_then(outcome)
                with self.assertLogs("pnc_bot.gitrepo", level="WARNING"):
                    sha, note_id = self.publish_note(outcome.__name__, "正文", images=())
                self.box.before_push = None
                self.assertEqual(self.box.remote_head(), sha)
                self.assertEqual(self.repo.head(), sha)
                self.assertEqual(parse_notes(self.box.remote_file(NOTES_PATH))[0].title, outcome.__name__)
                self.assert_clean()

    def test_push_failure_that_did_not_land(self):
        before = self.box.remote_head()
        self.box.before_push = self.hung_up  # nothing reaches the remote
        with self.assertRaises(GitError) as caught:
            self.publish_note()
        self.assertNotIsInstance(caught.exception, PushRejected)
        self.assertIn("hung up", str(caught.exception))
        self.assertEqual(self.box.remote_head(), before)
        self.assertEqual(self.repo.head(), before)
        self.assert_clean()

    def test_revert_push_failure_after_the_remote_took_it(self):
        self.publish_note("要撤销", "x")
        candidate = self.repo.find_undo_candidate()
        self.box.before_push = self.push_then(self.timed_out)
        with self.assertLogs("pnc_bot.gitrepo", level="WARNING"):
            revert_sha = self.repo.revert(candidate)
        self.box.before_push = None
        self.assertEqual(self.box.remote_head(), revert_sha)
        self.assertIsNone(self.box.remote_file(IMAGE))
        self.assert_clean()
        self.assertIsNone(self.repo.find_undo_candidate())

    def test_append_publish(self):
        _, note_id = self.publish_note("一", "第一段")

        def append(workdir: Path) -> str:
            path = workdir / NOTES_PATH
            path.write_text(append_to_note(path.read_text(encoding="utf-8"), note_id, "第二段"), encoding="utf-8")
            return note_id

        sha, result = self.repo.publish(append, message=note_message("note-append"), allowed=is_bot_content_path)
        self.assertEqual(result, note_id)
        self.assertEqual(parse_notes(self.box.remote_file(NOTES_PATH))[0].body, "第一段\n\n第二段")
        self.assertEqual(self.box.remote_head(), sha)


class UndoTests(GitTestCase):
    def build_history(self):
        """initial → E (bot draft) → A (bot note) → B (owner) → C (bot, touches README) → D (owner, forged trailer)."""

        def draft(workdir: Path) -> str:
            Sandbox.write(workdir, DRAFT, "---\ntitle: draft\n---\n")
            return "my-draft"

        self.e, _ = self.repo.publish(
            draft,
            message=lambda slug: commit_message(f"draft: {slug}", {"Pnc-Bot": "draft"}),
            allowed=is_bot_content_path,
        )
        self.notes_after_e = self.box.remote_file(NOTES_PATH)
        self.a, self.a_note = self.publish_note("要撤销", "这条会被撤销", images=(IMAGE,))
        self.b = self.box.other_commit({"README.md": "owner\n"}, "docs: owner change")

        def touches_readme(workdir: Path) -> str:
            Sandbox.write(workdir, "README.md", "bot touched\n")
            Sandbox.write(workdir, IMAGE_2, "webp")
            return "2026-11-21-2"

        self.c, _ = self.repo.publish(touches_readme, message=note_message(), allowed=lambda path: True)
        self.d = self.box.other_commit(
            {"README.md": "forged\n"}, "note: forged\n\nPnc-Bot: note-add\nPnc-Bot-Note: 2026-11-21"
        )

    def test_candidate_selection_and_successive_undos(self):
        self.build_history()
        candidate = self.repo.find_undo_candidate()
        self.assertEqual(
            candidate,
            UndoCandidate(
                sha=self.a,
                subject=f"note: {self.a_note}",
                kind="note-add",
                note_id=self.a_note,
                files=tuple(sorted([NOTES_PATH, IMAGE])),
            ),
        )

        revert_sha = self.repo.revert(candidate)
        self.assertEqual(self.box.remote_head(), revert_sha)
        self.assert_clean()
        log = self.box.remote_git("log", "-1", "--format=%ae|%s", revert_sha)
        self.assertEqual(log, f"{BOT_EMAIL}|Revert \"note: {self.a_note}\"")
        trailers = self.box.remote_git("log", "-1", "--format=%(trailers:only,unfold)", revert_sha)
        self.assertEqual(trailers, f"Pnc-Bot: undo\nPnc-Bot-Reverts: {self.a}")
        self.assertIsNone(self.box.remote_file(IMAGE))
        self.assertEqual(self.box.remote_file(NOTES_PATH), self.notes_after_e)
        self.assertIsNotNone(self.box.remote_file(IMAGE_2))  # C stays
        self.assertEqual(self.box.remote_file("README.md"), "forged\n")

        # A is reverted now and the undo commit itself is not undoable: next is the draft.
        candidate = self.repo.find_undo_candidate()
        self.assertEqual((candidate.sha, candidate.kind, candidate.note_id, candidate.files), (self.e, "draft", None, (DRAFT,)))
        self.repo.revert(candidate)
        self.assertIsNone(self.box.remote_file(DRAFT))
        self.assertIsNone(self.repo.find_undo_candidate())

    def test_limit(self):
        self.build_history()
        self.assertIsNone(self.repo.find_undo_candidate(limit=3))  # D, C, B only
        self.assertEqual(self.repo.find_undo_candidate(limit=4).sha, self.a)

    def test_no_bot_commits(self):
        self.box.other_commit({"README.md": "x\n"})
        self.assertIsNone(self.repo.find_undo_candidate())

    def test_sees_remote_commits_without_explicit_sync(self):
        other_bot = self.box.repo(self.box.other)  # e.g. a second clone pushed meanwhile
        sha, _ = other_bot.publish(add_note_mutation("远端", "x"), message=note_message(), allowed=is_bot_content_path)
        self.assertEqual(self.repo.find_undo_candidate().sha, sha)

    def test_revert_conflict(self):
        _, note_id = self.publish_note("会冲突", "原始正文")
        candidate = self.repo.find_undo_candidate()
        raw = self.box.remote_file(NOTES_PATH)
        edited = raw.replace("原始正文", "原始正文，被主人在电脑上改过")
        conflict_head = self.box.other_commit({NOTES_PATH: edited}, "edit the bot note by hand")

        with self.assertRaises(GitError) as caught:
            self.repo.revert(candidate)
        self.assertTrue(str(caught.exception).startswith("撤销会和后来的改动冲突"))
        self.assertEqual(self.box.remote_head(), conflict_head)
        self.assert_clean()
        self.assertEqual(self.repo.head(), conflict_head)

    def test_revert_push_race(self):
        self.publish_note("撤销赛跑", "x")
        candidate = self.repo.find_undo_candidate()
        raced = []

        def race_once(argv):
            if not raced:
                raced.append(self.box.other_commit({"README.md": "raced\n"}, "race"))
            return None

        self.box.before_push = race_once
        revert_sha = self.repo.revert(candidate)
        self.assertEqual(self.box.remote_head(), revert_sha)
        self.assertEqual(self.box.remote_git("rev-parse", f"{revert_sha}^"), raced[0])
        self.assertIsNone(self.box.remote_file(IMAGE))
        self.assert_clean()

    def test_revert_unknown_commit(self):
        candidate = UndoCandidate(sha="1" * 40, subject="x", kind="note-add", note_id=None, files=(NOTES_PATH,))
        with self.assertRaises(GitError):
            self.repo.revert(candidate)
        self.assert_clean()


class LockTests(GitTestCase):
    def test_lock_serialises_instances(self):
        second = self.box.repo()
        holding, release, acquired = threading.Event(), threading.Event(), threading.Event()

        def first_holder():
            with self.repo.locked():
                holding.set()
                release.wait(10)

        def second_waiter():
            with second.locked():
                acquired.set()

        threads = [threading.Thread(target=first_holder), threading.Thread(target=second_waiter)]
        threads[0].start()
        self.assertTrue(holding.wait(10))
        threads[1].start()
        time.sleep(0.3)
        self.assertFalse(acquired.is_set())
        release.set()
        self.assertTrue(acquired.wait(10))
        for thread in threads:
            thread.join(10)

    def test_lock_is_reentrant_in_one_thread(self):
        done = threading.Event()

        def nested():
            with self.repo.locked(), self.repo.locked():
                self.repo.sync()
            done.set()

        thread = threading.Thread(target=nested, daemon=True)
        thread.start()
        self.assertTrue(done.wait(30), "nested locked() deadlocked")
        self.assertEqual(oct((self.box.base / "bot.lock").stat().st_mode & 0o777), oct(0o600))


class FakeRunner:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return subprocess.CompletedProcess(argv, *result)


class RunnerContractTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.lock = Path(self._tmp.name) / "lock"

    def tearDown(self):
        self._tmp.cleanup()

    def test_argv_env_and_timeout(self):
        fake = FakeRunner((0, b"a" * 40 + b"\n", b""))
        repo = Repo("/srv/repo", lock_path=self.lock, run=fake, timeout=33)
        self.assertEqual(repo.head(), "a" * 40)
        argv, kwargs = fake.calls[0]
        self.assertEqual(argv[:7], ["git", "-C", "/srv/repo", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper="])
        self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(kwargs["env"]["LC_ALL"], "C.UTF-8")
        self.assertEqual(kwargs["timeout"], 33)
        self.assertTrue(kwargs["capture_output"])
        self.assertNotIn("shell", kwargs)
        self.assertTrue(all(isinstance(arg, str) for arg in argv))

    def test_timeout_and_missing_git(self):
        repo = Repo("/srv/repo", lock_path=self.lock, run=FakeRunner(subprocess.TimeoutExpired(["git"], 1)))
        with self.assertRaisesRegex(GitError, "超时"):
            repo.head()
        repo = Repo("/srv/repo", lock_path=self.lock, run=FakeRunner(FileNotFoundError(2, "No such file")))
        with self.assertRaisesRegex(GitError, "无法运行 git"):
            repo.head()

    def test_error_message_is_short_and_redacted(self):
        token = "123456789:" + "A" * 35
        stderr = ("noise\n" * 50 + f"fatal: could not read from https://user:{token}@example.com/x\n").encode()
        repo = Repo("/srv/repo", lock_path=self.lock, run=FakeRunner((128, b"", stderr)))
        with self.assertRaises(GitError) as caught:
            repo.head()
        message = str(caught.exception)
        self.assertNotIn(token, message)
        self.assertNotIn("user:", message)
        self.assertNotIn("GIT_TERMINAL_PROMPT", message)
        self.assertLess(len(message), 400)

    def test_bad_constructor_arguments(self):
        with self.assertRaises(ValueError):
            Repo("/srv/repo", lock_path=self.lock, branch="--force")
        with self.assertRaises(ValueError):
            Repo("/srv/repo", lock_path=self.lock, remote="a b")


if __name__ == "__main__":
    unittest.main()
