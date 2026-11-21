import contextlib
import hashlib
import io
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from pnc_bot import app as appmod
from pnc_bot import images
from pnc_bot import notes as notesmod
from pnc_bot.app import App, Confirmations, Gate, Interrupts, MediaGroups, parse_command
from pnc_bot.comments import CommentStore
from pnc_bot.deploys import DeployHistory
from pnc_bot.gitrepo import GitError, UndoCandidate
from pnc_bot.ops import DeployPlan, HttpResult, OpsError
from pnc_bot.state import State
from pnc_bot.telegram import TelegramError

from tests.runtime_fakes import OWNER, TOKEN, make_blog, make_comments_db, make_config, write_records

STRANGER = 777
NOW = 1_800_000_000

NOTES_MD = """# 随想

## 2026-11-20 | 旧的

旧内容。
"""


def fake_process_image(data, **kwargs):
    """Stands in for images.process_image (tested in test_images.py)."""
    if not data.startswith(b"IMG:"):
        raise images.ImageRejected("无法识别这张图片")
    return images.ProcessedImage(b"RIFF-webp-" + data, 40, 30, hashlib.sha256(data).hexdigest())


class FakeTelegram:
    def __init__(self):
        self.calls = []
        self.updates = []
        self.next_id = 1000
        self.files = {}
        self.failures = {}  # method name -> errors raised by its next calls
        self.on_edit = None  # hook(message_id, html), called after an edit went through

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    def _maybe_fail(self, name):
        queue = self.failures.get(name)
        if queue:
            raise queue.pop(0)

    def names(self):
        return [c[0] for c in self.calls]

    def texts(self):
        return [c[1][1] if c[0] == "send_message" else c[1][2] for c in self.calls if c[0] in ("send_message", "edit_message")]

    def get_updates(self, offset, timeout):
        self._record("get_updates", offset, timeout)
        item = self.updates.pop(0) if self.updates else []
        if isinstance(item, BaseException):
            raise item
        return item

    def send_message(self, chat_id, html, *, reply_to=None, buttons=None, disable_preview=True):
        self._record("send_message", chat_id, html, reply_to=reply_to, buttons=buttons)
        self._maybe_fail("send_message")
        self.next_id += 1
        return {"message_id": self.next_id}

    def edit_message(self, chat_id, message_id, html, *, buttons=None):
        self._record("edit_message", chat_id, message_id, html)
        self._maybe_fail("edit_message")
        if self.on_edit is not None:
            self.on_edit(message_id, html)

    def history(self, message_id):
        """Every text one bot message was edited to (attempts included), in order."""
        texts = []
        for name, args, kwargs in self.calls:
            if name == "edit_message" and args[1] == message_id:
                texts.append(args[2])
        return texts

    def answer_callback(self, callback_id, text=None):
        self._record("answer_callback", callback_id, text)

    def download_file(self, file_id, max_bytes):
        self._record("download_file", file_id)
        return self.files[file_id]

    def set_my_commands(self, commands, chat_id):
        self._record("set_my_commands", chat_id)


class InlineWorker:
    def __init__(self):
        self.jobs = 0

    def submit(self, fn, *args):
        self.jobs += 1
        fn(*args)

    def pending(self):
        return 0

    def shutdown(self):
        pass


class DeferredWorker(InlineWorker):
    """Queues jobs until run_all(): a git worker that is still busy."""

    def __init__(self):
        super().__init__()
        self.queue = []

    def submit(self, fn, *args):
        self.jobs += 1
        self.queue.append((fn, args))

    def pending(self):
        return len(self.queue)

    def run_all(self):
        while self.queue:
            fn, args = self.queue.pop(0)
            fn(*args)


class FakeTracker:
    def __init__(self):
        self.watches = []
        self.requests = []
        self.unwatched = []

    def watch(self, sha, ok, failed, timeout):
        self.watches.append((sha, ok, failed, timeout))

    def watch_request(self, request_id, ok, failed, timed_out, **kw):
        self.requests.append((request_id, ok, failed, timed_out, kw))

    def unwatch_request(self, request_id):
        self.unwatched.append(request_id)
        return True

    def start(self):
        pass

    def stop(self):
        pass


class FakeContentRepo:
    """A work tree on disk; publish() runs mutate and checks paths like Repo does."""

    def __init__(self, root: Path):
        self.path = root / "work"
        (self.path / "src/content/posts/existing-post").mkdir(parents=True)
        (self.path / "src/static/assets/notes").mkdir(parents=True)
        (self.path / "src/content/notes.md").write_text(NOTES_MD, encoding="utf-8")
        (self.path / "src/content/posts/existing-post/index.mdx").write_text("x")
        self.commits = []
        self.fetches = 0
        self.undo = None
        self.reverted = []
        self.on_revert = None  # hook(candidate): what the revert does to the work tree

    def _snapshot(self):
        return {
            p.relative_to(self.path).as_posix(): p.read_bytes() for p in self.path.rglob("*") if p.is_file()
        }

    def publish(self, mutate, *, message, allowed, attempts=3):
        before = self._snapshot()
        try:
            result = mutate(self.path)
            after = self._snapshot()
            changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
            if not changed:
                raise GitError("没有任何改动可以提交")
            refused = [p for p in changed if not allowed(p)]
            if refused:
                raise GitError(f"不允许修改的路径：{refused[0]}")
        except BaseException:
            shutil.rmtree(self.path)
            for rel, data in before.items():
                (self.path / rel).parent.mkdir(parents=True, exist_ok=True)
                (self.path / rel).write_bytes(data)
            raise
        sha = f"{len(self.commits) + 1:040x}"
        self.commits.append((sha, message(result), changed))
        return sha, result

    def notes(self):
        return (self.path / "src/content/notes.md").read_text(encoding="utf-8")

    def fetch(self):
        self.fetches += 1

    def show(self, path, rev):
        return (self.path / path).read_text(encoding="utf-8")

    def find_undo_candidate(self):
        return self.undo

    def revert(self, candidate):
        self.reverted.append(candidate.sha)
        if self.on_revert is not None:
            self.on_revert(candidate)
        return "f" * 40

    def subject(self, sha):
        return "subject"


class FakeOps:
    def __init__(self):
        self.restarts = []
        self.deploys = []
        self.on_start = None  # hook(plan, request_id) while systemd-run "runs"

    def restart(self, name):
        self.restarts.append(name)
        return f"✅ 已重启 <b>{name}</b>"

    def start_deploy(self, plan, request_id):
        self.deploys.append((plan, request_id))
        if self.on_start is not None:
            self.on_start(plan, request_id)
        return "pnc-bot-deploy-1"

    def unit_finished(self, unit):
        return False

    def unit_journal(self, unit):
        return ""

    def status_report(self, *, queue_size=None, uptime=None):
        return "<b>📊 状态</b>"

    def logs(self, name):
        return f"<pre>{name}</pre>"


def user(uid=OWNER):
    return {"id": uid, "is_bot": False, "first_name": "PNC"}


def private_chat(uid=OWNER):
    return {"id": uid, "type": "private", "first_name": "PNC"}


def message(text=None, *, mid=1, uid=OWNER, chat=None, date=NOW, **extra):
    msg = {"message_id": mid, "date": date, "from": user(uid), "chat": chat or private_chat(uid)}
    if text is not None:
        msg["text"] = text
    msg.update(extra)
    return msg


def photo_message(file_id, *, mid, group=None, caption=None, **extra):
    msg = message(
        mid=mid,
        photo=[
            {"file_id": file_id + "-small", "file_unique_id": "s", "width": 90, "height": 60, "file_size": 1000},
            {"file_id": file_id, "file_unique_id": "b", "width": 1280, "height": 960, "file_size": 90000},
        ],
        **extra,
    )
    if group:
        msg["media_group_id"] = group
    if caption is not None:
        msg["caption"] = caption
    return msg


def callback(data, *, message_id, uid=OWNER, chat=None, cb_id="cb1"):
    return {
        "id": cb_id,
        "from": user(uid),
        "message": {"message_id": message_id, "date": NOW, "chat": chat or private_chat()},
        "chat_instance": "1",
        "data": data,
    }


def reply_to(mid):
    return {"reply_to_message": {"message_id": mid, "date": NOW, "chat": private_chat()}}


def network_error():
    return TelegramError("network error: RemoteDisconnected: Remote end closed connection without response")


def deploy_plan():
    return DeployPlan("deploy", "c" * 40, None, "note: x", "20261121-100000-aaaaaaaaaaaa", redeploy=True)


class AppTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.blog = make_blog(self.root, ["20261121-100000-aaaaaaaaaaaa"], current="20261121-100000-aaaaaaaaaaaa")
        make_comments_db(self.root / "pnc.db", [{"content": "<i>hello</i>"}, {"content": "gone", "status": "hidden"}])
        self.config = make_config(self.root)
        self.tg = FakeTelegram()
        self.state = State(self.config.state_dir)
        self.repo = FakeContentRepo(self.root)
        self.tracker = FakeTracker()
        self.git = InlineWorker()
        self.pool = InlineWorker()
        self.ops = FakeOps()
        self.now = [float(NOW)]
        self.mono = [100.0]
        self.waits = []
        self.http_status = 200
        self.app = self.make_app()

    def make_app(self, **overrides):
        parts = dict(
            tg=self.tg,
            state=self.state,
            repo=self.repo,
            history=DeployHistory(self.blog),
            comments=CommentStore(self.config.comments_db),
            ops=self.ops,
            tracker=self.tracker,
            git_worker=self.git,
            pool=self.pool,
            clock=lambda: self.now[0],
            monotonic=lambda: self.mono[0],
            sleep=lambda s: None,
            http_get=lambda url, **kw: HttpResult(self.http_status, 5),
        )
        parts.update(overrides)
        app = App(self.config, **parts)
        app.stop_event.wait = lambda seconds: self.waits.append(seconds) or False
        return app

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def update(self, uid_counter=[0], **kind):
        uid_counter[0] += 1
        return {"update_id": uid_counter[0], **kind}

    def assert_nothing_happened(self):
        self.assertEqual(self.tg.calls, [])
        self.assertEqual(self.git.jobs, 0)
        self.assertEqual(self.pool.jobs, 0)
        self.assertFalse(self.app.media.pending())
        self.assertEqual(self.repo.commits, [])
        self.assertEqual(len(self.app.confirmations), 0)


class AuthMatrixTest(AppTestBase):
    def unauthorized_updates(self):
        group = {"id": -100123, "type": "group", "title": "alerts"}
        supergroup = {"id": -100456, "type": "supergroup", "title": "alerts"}
        channel = {"id": -100789, "type": "channel", "title": "c"}
        return [
            {"message": message("/status", uid=STRANGER)},
            {"message": message("记一条", uid=STRANGER)},
            {"message": photo_message("p", mid=2, uid=STRANGER, chat=private_chat(STRANGER))},
            {"message": photo_message("p", mid=3, group="g1", uid=STRANGER, chat=private_chat(STRANGER))},
            {"message": message("/deploy", chat=group)},
            {"message": message("/status", chat=supergroup)},
            {"message": photo_message("p", mid=4, group="g2", chat=group)},
            {"message": message("/status", chat={"id": OWNER, "type": "group"})},
            {"message": message("/status", chat={"id": str(OWNER), "type": "private"})},
            {"message": {**message("/status"), "from": {"id": OWNER, "is_bot": True}}},
            {"channel_post": message("/status", chat=channel)},
            {"edited_message": message("/note edited")},
            {"edited_channel_post": message("x", chat=channel)},
            {"callback_query": callback("cf:0123456789abcdef:y", message_id=5, uid=STRANGER)},
            {"callback_query": callback("dc:1", message_id=5, uid=STRANGER)},
            {"callback_query": callback("cf:0123456789abcdef:y", message_id=5, chat=group)},
            {"callback_query": callback("dc:1", message_id=5, chat=supergroup)},
            {"inline_query": {"id": "1", "from": user(), "query": "x", "offset": ""}},
            {"my_chat_member": {"chat": group, "from": user()}},
            {"chat_join_request": {"chat": group, "from": user()}},
            {"message": "not a dict"},
        ]

    def test_dropped_without_any_effect(self):
        for kind in self.unauthorized_updates():
            with self.subTest(kind=next(iter(kind))):
                with self.assertLogs("pnc_bot", level="INFO") as logs:
                    self.app.handle_update(self.update(**kind))
                self.assert_nothing_happened()
                for line in logs.output:
                    self.assertRegex(line, r"ignored [a-z_]+$")
                    self.assertNotIn(str(OWNER), line)
                    self.assertNotIn(str(STRANGER), line)

    def test_dropped_through_the_loop_and_offset_advances(self):
        self.state.set("update_offset", 1)
        batch = [{"update_id": 10 + i, **kind} for i, kind in enumerate(self.unauthorized_updates())]
        self.tg.updates = [batch]
        self.app.poll_once()
        self.assertEqual(self.tg.names(), ["get_updates"])
        self.assertEqual(self.state.get_int("update_offset"), 10 + len(batch))
        self.assertEqual(self.git.jobs + self.pool.jobs, 0)

    def test_stranger_cannot_use_owner_confirmation(self):
        self.app.handle_update(self.update(message=message("/restart pnc-api", mid=7)))
        confirm = self.tg.calls[-1]
        data = confirm[2]["buttons"][0][0][1]
        message_id = self.tg.next_id
        before = len(self.tg.calls)
        self.app.handle_update(self.update(callback_query=callback(data, message_id=message_id, uid=STRANGER)))
        self.assertEqual(len(self.tg.calls), before)
        self.assertEqual(self.ops.restarts, [])
        # the owner's own click still works afterwards
        self.app.handle_update(self.update(callback_query=callback(data, message_id=message_id)))
        self.assertEqual(self.ops.restarts, ["pnc-api"])


class ConfirmationTest(AppTestBase):
    def start_restart(self):
        self.app.handle_update(self.update(message=message("/restart pnc-api", mid=7)))
        name, args, kwargs = self.tg.calls[-1]
        self.assertEqual(name, "send_message")
        self.assertIn("pnc-api", args[1])
        yes, no = kwargs["buttons"][0]
        return yes[1], no[1], self.tg.next_id

    def test_confirm_runs_once(self):
        yes, _, message_id = self.start_restart()
        self.assertRegex(yes, r"^cf:[0-9a-f]{16}:y$")
        self.app.handle_update(self.update(callback_query=callback(yes, message_id=message_id)))
        self.assertEqual(self.ops.restarts, ["pnc-api"])
        self.assertIn("answer_callback", self.tg.names())
        # reused nonce (a second tap): answered, but the result stays on the message
        before = len(self.tg.calls)
        self.app.handle_update(self.update(callback_query=callback(yes, message_id=message_id, cb_id="cb2")))
        self.assertEqual(self.ops.restarts, ["pnc-api"])
        self.assertEqual(self.tg.calls[before:], [("answer_callback", ("cb2", "已处理"), {})])
        self.assertEqual(self.tg.history(message_id)[-1], "✅ 已重启 <b>pnc-api</b>")

    def test_double_tap_keeps_the_result(self):
        self.app.handle_update(self.update(message=message("/delcomment 1", mid=8)))
        yes = self.tg.calls[-1][2]["buttons"][0][0][1]
        confirm = self.tg.next_id
        self.app.handle_update(self.update(callback_query=callback(yes, message_id=confirm, cb_id="tap1")))
        self.app.handle_update(self.update(callback_query=callback(yes, message_id=confirm, cb_id="tap2")))
        self.assertEqual(self.tg.history(confirm), ["⏳ 执行中…", "🙈 已隐藏评论 #1"])
        self.assertEqual(self.tg.calls[-1], ("answer_callback", ("tap2", "已处理"), {}))
        self.assertFalse(CommentStore(self.config.comments_db).get(1).visible)

    def test_tap_after_cancel_keeps_the_cancel(self):
        yes, no, message_id = self.start_restart()
        self.app.handle_update(self.update(callback_query=callback(no, message_id=message_id)))
        self.app.handle_update(self.update(callback_query=callback(yes, message_id=message_id, cb_id="late")))
        self.assertEqual(self.tg.history(message_id), ["已取消"])
        self.assertEqual(self.tg.calls[-1], ("answer_callback", ("late", "已处理"), {}))
        self.assertEqual(self.ops.restarts, [])

    def test_cancel(self):
        yes, no, message_id = self.start_restart()
        self.app.handle_update(self.update(callback_query=callback(no, message_id=message_id)))
        self.assertEqual(self.ops.restarts, [])
        self.app.handle_update(self.update(callback_query=callback(yes, message_id=message_id)))
        self.assertEqual(self.ops.restarts, [])

    def test_expired(self):
        yes, _, message_id = self.start_restart()
        self.mono[0] += 121
        self.app.handle_update(self.update(callback_query=callback(yes, message_id=message_id)))
        self.assertEqual(self.ops.restarts, [])
        self.assertIn("已过期", self.tg.texts()[-1])

    def test_forged_nonce(self):
        self.start_restart()
        self.app.handle_update(self.update(callback_query=callback("cf:00000000deadbeef:y", message_id=self.tg.next_id)))
        self.assertEqual(self.ops.restarts, [])
        self.assertIn("已过期", self.tg.texts()[-1])

    def test_malformed_data_only_answered(self):
        self.start_restart()
        before = len(self.tg.calls)
        self.app.handle_update(self.update(callback_query=callback("cf:../../:y", message_id=1)))
        self.assertEqual([c[0] for c in self.tg.calls[before:]], ["answer_callback"])
        self.assertEqual(self.ops.restarts, [])

    def test_nonce_bound_to_its_message(self):
        yes, _, message_id = self.start_restart()
        self.app.handle_update(self.update(callback_query=callback(yes, message_id=message_id - 1)))
        self.assertEqual(self.ops.restarts, [])

    def test_registry_unit(self):
        clock = [0.0]
        reg = Confirmations(ttl=120, clock=lambda: clock[0])
        nonce = reg.open("x", {}, lambda n: 5)
        self.assertRegex(nonce, r"^[0-9a-f]{16}$")
        self.assertIsNone(reg.take(nonce, 6))
        self.assertFalse(reg.used(nonce))  # taken from the wrong message: not "already handled"
        nonce = reg.open("x", {}, lambda n: 5)
        clock[0] = 119.9
        self.assertIsNotNone(reg.take(nonce, 5))
        self.assertIsNone(reg.take(nonce, 5))
        self.assertTrue(reg.used(nonce))
        clock[0] = 119.9 + 120
        self.assertFalse(reg.used(nonce))  # remembered for one TTL only
        reg.open("x", {}, lambda n: 6)
        self.assertEqual(reg._used, {})  # pruned
        self.assertFalse(reg.used("0" * 16))

    def test_delcomment_flow(self):
        self.app.handle_update(self.update(message=message("/delcomment #1", mid=8)))
        name, args, kwargs = self.tg.calls[-1]
        self.assertIn("&lt;i&gt;hello&lt;/i&gt;", args[1])
        yes = kwargs["buttons"][0][0][1]
        self.app.handle_update(self.update(callback_query=callback(yes, message_id=self.tg.next_id)))
        self.assertIn("已隐藏评论 #1", self.tg.texts()[-1])
        self.assertFalse(CommentStore(self.config.comments_db).get(1).visible)

    def test_delcomment_hidden_or_missing(self):
        self.app.handle_update(self.update(message=message("/delcomment 2")))
        self.assertIn("已经是隐藏状态", self.tg.texts()[-1])
        self.app.handle_update(self.update(message=message("/delcomment 99")))
        self.assertIn("没有评论 #99", self.tg.texts()[-1])
        self.app.handle_update(self.update(message=message("/delcomment 1; DROP TABLE comments")))
        self.assertIn("用法", self.tg.texts()[-1])
        self.assertEqual(len(self.app.confirmations), 0)

    def test_hide_button_starts_confirmation(self):
        self.app.handle_update(self.update(callback_query=callback("dc:1", message_id=55)))
        self.assertEqual(self.tg.calls[0][0], "answer_callback")
        self.assertEqual(len(self.app.confirmations), 1)
        self.assertEqual(self.ops.restarts, [])

    def test_deploy_flow(self):
        plan = DeployPlan("deploy", "c" * 40, None, "note: x", "20261121-100000-aaaaaaaaaaaa", redeploy=True)
        with mock.patch.object(appmod, "plan_deploy", return_value=plan) as planner:
            self.app.handle_update(self.update(message=message("/deploy", mid=9)))
        planner.assert_called_once()
        self.assertIsNone(planner.call_args[0][0])
        yes = self.tg.calls[-1][2]["buttons"][0][0][1]
        self.app.handle_update(self.update(callback_query=callback(yes, message_id=self.tg.next_id)))
        self.assertEqual(len(self.ops.deploys), 1)
        self.assertEqual(self.ops.deploys[0][0], plan)
        request_id = self.ops.deploys[0][1]
        self.assertRegex(request_id, r"^[0-9a-f]{16}$")
        self.assertEqual(self.tracker.requests[0][0], request_id)
        self.tracker.requests[0][1]({"result": "success"})
        self.assertIn("✅ 部署完成", self.tg.texts()[-1])

    def test_deploy_bad_sha_never_reaches_git(self):
        with mock.patch.object(appmod, "plan_deploy") as planner:
            for bad in ("/deploy ../x", "/deploy --help", "/deploy abc", "/deploy ABCDEF12", "/deploy 1234567;id"):
                self.app.handle_update(self.update(message=message(bad)))
        planner.assert_not_called()
        self.assertEqual(self.git.jobs, 0)

    def test_deploy_plan_error_reported(self):
        with mock.patch.object(appmod, "plan_deploy", side_effect=appmod.OpsError("这个提交不在 main 上，拒绝部署")):
            self.app.handle_update(self.update(message=message("/deploy abcdef1")))
        self.assertIn("不在 main", self.tg.texts()[-1])
        self.assertEqual(len(self.app.confirmations), 0)

    def test_restart_bad_name(self):
        for bad in ("/restart", "/restart pnc-bot", "/restart pnc-api;id", "/restart ../x"):
            self.app.handle_update(self.update(message=message(bad)))
        self.assertEqual(len(self.app.confirmations), 0)
        self.assertEqual(self.ops.restarts, [])

    def test_undo_flow(self):
        from pnc_bot.gitrepo import UndoCandidate

        self.repo.undo = UndoCandidate("e" * 40, "note: <x>", "note-add", "2026-11-21", ("src/content/notes.md",))
        self.app.handle_update(self.update(message=message("/undo")))
        name, args, kwargs = self.tg.calls[-1]
        self.assertIn("note: &lt;x&gt;", args[1])
        self.assertIn("src/content/notes.md", args[1])
        yes = kwargs["buttons"][0][0][1]
        self.app.handle_update(self.update(callback_query=callback(yes, message_id=self.tg.next_id)))
        self.assertEqual(self.repo.reverted, ["e" * 40])
        self.assertEqual(self.tracker.watches[-1][0], "f" * 40)

    def test_undo_stale_candidate(self):
        from pnc_bot.gitrepo import UndoCandidate

        self.repo.undo = UndoCandidate("e" * 40, "s", "note-add", None, ("src/content/notes.md",))
        self.app.handle_update(self.update(message=message("/undo")))
        yes = self.tg.calls[-1][2]["buttons"][0][0][1]
        self.repo.undo = UndoCandidate("9" * 40, "s", "note-add", None, ("src/content/notes.md",))
        self.app.handle_update(self.update(callback_query=callback(yes, message_id=self.tg.next_id)))
        self.assertEqual(self.repo.reverted, [])
        self.assertIn("情况有变", self.tg.texts()[-1])


class LoopTest(AppTestBase):
    def test_initial_drain(self):
        self.tg.updates = [[{"update_id": 41, "message": message("old")}, {"update_id": 42, "message": message("old")}]]
        self.app.poll_once()
        self.assertEqual(self.tg.calls, [("get_updates", (-1, 0), {})])
        self.assertEqual(self.state.get_int("update_offset"), 43)
        self.assertEqual(self.repo.commits, [])

    def test_initial_drain_empty(self):
        self.app.poll_once()
        self.assertEqual(self.state.get_int("update_offset"), 0)
        self.app.poll_once()
        self.assertEqual(self.tg.calls[-1], ("get_updates", (0, 50), {}))

    def test_offset_persisted_per_update_even_on_errors(self):
        self.state.set("update_offset", 5)
        self.tg.updates = [[{"update_id": 5, "message": message("/help")}, {"update_id": 6, "message": message("/status")}]]
        with mock.patch.object(self.app, "on_message", side_effect=[RuntimeError("boom"), None]):
            with self.assertLogs("pnc_bot", level="ERROR"):
                self.app.poll_once()
        self.assertEqual(self.state.get_int("update_offset"), 7)

    def test_old_updates_skipped(self):
        self.state.set("update_offset", 10)
        self.tg.updates = [[{"update_id": 9, "message": message("/help")}, {"update_id": "11", "message": message("/help")}]]
        self.app.poll_once()
        self.assertEqual(self.tg.names(), ["get_updates"])
        self.assertEqual(self.state.get_int("update_offset"), 10)

    def test_conflict_waits_30s(self):
        self.state.set("update_offset", 1)
        self.tg.updates = [TelegramError("getUpdates: Conflict", 409)]
        with self.assertLogs("pnc_bot", level="ERROR"):
            self.assertEqual(self.app.loop_step(), 30)

    def test_network_backoff(self):
        self.state.set("update_offset", 1)
        self.tg.updates = [TelegramError("network error: x")] * 8 + [[]]
        waits = []
        with self.assertLogs("pnc_bot", level="WARNING"):
            for _ in range(8):
                waits.append(self.app.loop_step())
        self.assertEqual(waits, [1, 2, 4, 8, 16, 32, 60, 60])
        self.assertEqual(self.app.loop_step(), 0)
        self.tg.updates = [TelegramError("network error: x")]
        with self.assertLogs("pnc_bot", level="WARNING"):
            self.assertEqual(self.app.loop_step(), 1)

    def test_rate_limit_respected(self):
        self.state.set("update_offset", 1)
        self.tg.updates = [TelegramError("getUpdates: Too Many Requests", 429, retry_after=7)]
        with self.assertLogs("pnc_bot", level="WARNING"):
            self.assertEqual(self.app.loop_step(), 7)

    def test_unexpected_errors_do_not_kill_the_loop(self):
        self.state.set("update_offset", 1)
        self.tg.updates = [RuntimeError("bug")]
        with self.assertLogs("pnc_bot", level="ERROR"):
            self.assertEqual(self.app.loop_step(), 5)

    def test_short_poll_while_buffering(self):
        self.state.set("update_offset", 1)
        self.tg.updates = [[{"update_id": 1, "message": photo_message("p1", mid=10, group="g")}], []]
        self.app.poll_once()
        self.app.poll_once()
        self.assertEqual(self.tg.calls[1], ("get_updates", (2, 1), {}))

    def test_startup_sets_chat_scoped_commands(self):
        self.app.startup()
        self.assertEqual(self.tg.calls, [("set_my_commands", (OWNER,), {})])


class ContentTest(AppTestBase):
    def test_text_becomes_note(self):
        self.app.handle_update(self.update(message=message("今天 **很好**", mid=20)))
        self.assertEqual(self.tg.calls[0][0], "send_message")
        self.assertIn("处理中", self.tg.calls[0][1][1])
        receipt = self.tg.next_id
        self.assertEqual(len(self.repo.commits), 1)
        sha, msg, changed = self.repo.commits[0]
        self.assertEqual(changed, ["src/content/notes.md"])
        self.assertTrue(msg.startswith("note: 今天"))
        self.assertIn("Pnc-Bot: note-add", msg)
        self.assertIn("今天 **很好**", self.repo.notes())
        self.assertIn(f"已提交 <code>{sha[:7]}</code>", self.tg.texts()[-1])
        note_id = self.state.last_note_id()
        self.assertEqual(self.state.note_for_message(receipt), note_id)
        self.assertEqual(self.state.note_for_message(20), note_id)
        watched, ok, failed, timeout = self.tracker.watches[0]
        self.assertEqual(watched, sha)
        ok({"result": "success", "sha": sha})
        self.assertIn(f"✅ 已上线 <b>{note_id}</b>", self.tg.texts()[-1])
        self.assertIn(f"https://example.test/notes/{note_id}.html", self.tg.texts()[-1])

    def test_deploy_failure_message(self):
        self.app.handle_update(self.update(message=message("x", mid=20)))
        _, _, failed, _ = self.tracker.watches[0]
        failed({"result": "failure", "sha": "1" * 40, "error": "<boom>"})
        text = self.tg.texts()[-1]
        self.assertIn("❌ <b>部署失败</b>", text)
        self.assertIn("&lt;boom&gt;", text)
        self.assertIn("/undo", text)

    def test_reply_appends(self):
        self.app.handle_update(self.update(message=message("第一条", mid=20)))
        receipt = self.tg.next_id
        note_id = self.state.last_note_id()
        self.app.handle_update(
            self.update(message=message("补充", mid=21, reply_to_message={"message_id": receipt, "date": NOW, "chat": private_chat()}))
        )
        self.assertEqual(len(self.repo.commits), 2)
        self.assertIn("Pnc-Bot: note-append", self.repo.commits[1][1])
        self.assertIn(f"Pnc-Bot-Note: {note_id}", self.repo.commits[1][1])
        notes_md = self.repo.notes()
        self.assertLess(notes_md.index("第一条"), notes_md.index("补充"))
        self.assertLess(notes_md.index("补充"), notes_md.index("## 2026-11-20"))
        self.assertEqual(self.state.note_for_message(21), note_id)

    def test_append_command_default_and_missing_target(self):
        self.app.handle_update(self.update(message=message("/append 没有目标")))
        self.assertIn("没有可追加的随想", self.tg.texts()[-1])
        self.app.handle_update(self.update(message=message("/append 2020-01-01 不存在")))
        self.assertIn("没有找到随想 2020-01-01", self.tg.texts()[-1])
        self.app.handle_update(self.update(message=message("/append 2026-11-20 追加到旧的")))
        self.assertIn("追加到旧的", self.repo.notes())
        self.assertEqual(len(self.repo.commits), 1)

    def test_draft(self):
        self.app.handle_update(self.update(message=message("/draft Hello World | hello-world\n正文第一段")))
        sha, msg, changed = self.repo.commits[0]
        self.assertEqual(changed, ["src/content/posts/hello-world/index.mdx"])
        self.assertIn("draft: hello-world", msg)
        doc = (self.repo.path / "src/content/posts/hello-world/index.mdx").read_text()
        self.assertIn('title: "Hello World"', doc)
        self.assertIn("draft: true", doc)
        _, ok, _, _ = self.tracker.watches[0]
        ok({"result": "success"})
        self.assertIn("草稿已推送", self.tg.texts()[-1])

    def test_draft_existing_slug(self):
        self.app.handle_update(self.update(message=message("/draft 标题 | existing-post")))
        self.assertIn("已经存在", self.tg.texts()[-1])
        self.assertEqual(self.repo.commits, [])

    def test_old_message_ignored(self):
        self.app.handle_update(self.update(message=message("旧消息", date=NOW - 601)))
        self.assertEqual(self.tg.texts(), ["消息太旧，已忽略"])
        self.assertEqual(self.git.jobs, 0)

    def test_unsupported(self):
        self.app.handle_update(self.update(message=message(sticker={"file_id": "s"})))
        self.app.handle_update(self.update(message=message(voice={"file_id": "v"})))
        self.app.handle_update(self.update(message=message(document={"file_id": "d", "mime_type": "application/pdf"})))
        self.assertEqual(self.tg.texts(), ["暂不支持这种消息"] * 3)
        self.assertNotIn("download_file", self.tg.names())

    def test_heic_document_explained_without_download(self):
        self.app.handle_update(
            self.update(message=message(document={"file_id": "d", "mime_type": "image/heic", "file_name": "a.HEIC"}))
        )
        self.assertIn("HEIC", self.tg.texts()[-1])
        self.assertNotIn("download_file", self.tg.names())

    @mock.patch.object(images, "process_image", fake_process_image)
    # The fake "WebP" bytes are not real images; the real-image check on
    # /assets/ references is covered by MergeFollowupTest and the integration tests.
    @mock.patch.object(notesmod, "missing_note_assets", lambda markdown, static_dir: [])
    def test_media_group_buffered_and_flushed(self):
        self.tg.files = {"p1": b"IMG:red", "p2": b"IMG:blue"}
        self.app.handle_update(self.update(message=photo_message("p2", mid=31, group="album")))
        self.app.handle_update(self.update(message=photo_message("p1", mid=30, group="album", caption="# 相册\n两张图")))
        self.assertEqual(self.tg.calls, [])
        self.mono[0] += 2.0
        self.app.flush_media()
        self.assertEqual(self.tg.calls, [])
        self.mono[0] += 0.6
        self.app.flush_media()
        self.assertEqual([c[1][0] for c in self.tg.calls if c[0] == "download_file"], ["p1", "p2"])
        self.assertEqual(len(self.repo.commits), 1)
        _, msg, changed = self.repo.commits[0]
        self.assertEqual(len(changed), 3)
        written = sorted((self.repo.path / "src/static/assets/notes").iterdir())
        self.assertEqual(sorted(p.read_bytes() for p in written), [b"RIFF-webp-IMG:blue", b"RIFF-webp-IMG:red"])
        self.assertTrue(all(appmod.note_path_allowed(p) for p in changed))
        notes_md = self.repo.notes()
        self.assertIn("| 相册", notes_md)
        self.assertEqual(notes_md.count("![相册](/assets/notes/"), 2)
        note_id = self.state.last_note_id()
        self.assertEqual(self.state.note_for_message(30), note_id)
        self.assertEqual(self.state.note_for_message(31), note_id)

    def test_too_many_images(self):
        for i in range(11):
            self.app.handle_update(self.update(message=photo_message(f"p{i}", mid=40 + i, group="big")))
        self.mono[0] += 3
        self.app.flush_media()
        self.assertIn("最多 10 张", self.tg.texts()[-1])
        self.assertNotIn("download_file", self.tg.names())

    @mock.patch.object(images, "process_image", fake_process_image)
    def test_image_rejected(self):
        self.tg.files = {"bad": b"not an image"}
        self.app.handle_update(self.update(message=photo_message("bad", mid=50)))
        self.assertEqual(self.tg.texts()[-1], "❌ 无法识别这张图片")
        self.assertEqual(self.repo.commits, [])

    @mock.patch.object(images, "process_image", fake_process_image)
    def test_processed_images_over_total_limit(self):
        self.tg.files = {f"p{i}": b"IMG:" + bytes([i]) * (6 * 1024 * 1024) for i in range(3)}
        for i in range(3):
            self.app.handle_update(self.update(message=photo_message(f"p{i}", mid=60 + i, group="huge")))
        self.mono[0] += 3
        self.app.flush_media()
        self.assertIn("超过 15 MB", self.tg.texts()[-1])
        self.assertEqual(self.repo.commits, [])

    def test_declared_size_over_limit_not_downloaded(self):
        msg = message(document={"file_id": "big", "mime_type": "image/png", "file_size": 21 * 1024 * 1024})
        self.app.handle_update(self.update(message=msg))
        self.assertIn("20 MB", self.tg.texts()[-1])
        self.assertNotIn("download_file", self.tg.names())

    def test_images_need_a_content_command(self):
        self.app.handle_update(self.update(message=photo_message("p", mid=51, caption="/status")))
        self.assertIn("图片请直接发送", self.tg.texts()[-1])
        self.assertNotIn("download_file", self.tg.names())

    def test_unknown_command(self):
        self.app.handle_update(self.update(message=message("/rm -rf")))
        self.assertIn("/help", self.tg.texts()[-1])

    def test_read_only_commands(self):
        self.app.handle_update(self.update(message=message("/help")))
        self.assertIn("PNC 博客机器人", self.tg.texts()[-1])
        self.app.handle_update(self.update(message=message("/status@pnc_bot")))
        self.assertIn("状态", self.tg.texts()[-1])
        self.app.handle_update(self.update(message=message("/logs")))
        self.assertIn("/logs deploy", self.tg.texts()[-1])
        self.app.handle_update(self.update(message=message("/logs ../../etc")))
        self.assertIn("没有这个日志源", self.tg.texts()[-1])
        self.app.handle_update(self.update(message=message("/comments")))
        self.assertIn("&lt;i&gt;hello&lt;/i&gt;", self.tg.texts()[-1])
        self.app.handle_update(self.update(message=message("/list")))
        self.assertIn("2026-11-20", self.tg.texts()[-1])
        self.assertEqual(self.repo.fetches, 1)

    def test_error_text_escapes(self):
        self.assertEqual(self.app.error_text(ValueError("<b>x</b>")), "&lt;b&gt;x&lt;/b&gt;")
        self.assertNotIn(TOKEN, self.app.error_text(GitError(f"push failed {TOKEN}")))
        with self.assertLogs("pnc_bot", level="ERROR") as logs:
            self.assertIn("内部错误", self.app.error_text(RuntimeError("secret message text")))
        self.assertNotIn("secret message text", "\n".join(logs.output))


class WatchOrderTest(AppTestBase):
    """A deploy watch exists before anything can write the record it waits for."""

    def confirm_deploy(self, app):
        with mock.patch.object(appmod, "plan_deploy", return_value=deploy_plan()):
            app.handle_update(self.update(message=message("/deploy", mid=9)))
        yes = self.tg.calls[-1][2]["buttons"][0][0][1]
        confirm = self.tg.next_id
        app.handle_update(self.update(callback_query=callback(yes, message_id=confirm)))
        return confirm

    def test_watch_registered_before_the_deploy_starts(self):
        seen = []
        self.ops.on_start = lambda plan, request_id: seen.append([r[0] for r in self.tracker.requests])
        self.confirm_deploy(self.app)
        self.assertEqual(seen, [[self.ops.deploys[0][1]]])

    def test_fast_deploy_record_is_not_lost(self):
        """A rollback finishes and the tracker ticks before any later Telegram call returns."""
        app = self.make_app(tracker=None)  # the real DeployTracker
        app.tracker.prime()

        def finish_at_once(plan, request_id):
            write_records(self.blog, {"result": "success", "request": request_id, "sha": plan.sha, "mode": "rollback"})
            app.tracker.poll_once()

        self.ops.on_start = finish_at_once
        confirm = self.confirm_deploy(app)
        history = self.tg.history(confirm)
        self.assertIn("✅ 部署完成：", history[-1], history)
        self.assertEqual(app.tracker.pending(), 0)
        self.assertEqual(len(app.interrupts), 0)

    def test_start_failure_drops_the_watch(self):
        def refuse(plan, request_id):
            raise OpsError("systemd-run 失败：no bus")

        self.ops.on_start = refuse
        confirm = self.confirm_deploy(self.app)
        request_id = self.ops.deploys[0][1]
        self.assertEqual(self.tracker.unwatched, [request_id])
        self.assertEqual(self.tg.history(confirm)[-1], "❌ systemd-run 失败：no bus")
        # A tracker without unwatch_request would still call back: nothing changes.
        _, ok, failed, timed_out, kw = self.tracker.requests[0]
        ok({"result": "success"})
        failed({"result": "failure"})
        timed_out()
        self.assertTrue(kw["unit_done"]())
        self.assertEqual(kw["unit_tail"](), "")
        self.assertEqual(self.tg.history(confirm)[-1], "❌ systemd-run 失败：no bus")
        self.assertEqual(len(self.app.interrupts), 0)

    def test_note_result_during_the_status_edit(self):
        def deployed_meanwhile(message_id, html):
            if html.startswith("📝 已提交") and self.tracker.watches:
                self.tg.on_edit = None
                sha, ok, _, _ = self.tracker.watches[-1]
                ok({"result": "success", "sha": sha})

        self.tg.on_edit = deployed_meanwhile
        receipt = self.tg.next_id + 1
        self.app.handle_update(self.update(message=message("很快上线", mid=20)))
        history = self.tg.history(receipt)
        self.assertTrue(history[0].startswith("📝 已提交"), history)
        self.assertTrue(history[-1].startswith("✅ 已上线"), history)
        self.assertEqual(len(history), 2)

    def test_undo_result_during_the_status_edit(self):
        self.repo.undo = UndoCandidate("e" * 40, "note: x", "note-append", "2026-11-20", ("src/content/notes.md",))

        def deployed_meanwhile(message_id, html):
            if html.startswith("↩️ 已撤销") and self.tracker.watches:
                self.tg.on_edit = None
                sha, ok, _, _ = self.tracker.watches[-1]
                ok({"result": "success", "sha": sha})

        self.app.handle_update(self.update(message=message("/undo")))
        yes = self.tg.calls[-1][2]["buttons"][0][0][1]
        confirm = self.tg.next_id
        self.tg.on_edit = deployed_meanwhile
        self.app.handle_update(self.update(callback_query=callback(yes, message_id=confirm)))
        history = self.tg.history(confirm)
        self.assertTrue(history[-2].startswith("↩️ 已撤销"), history)
        self.assertTrue(history[-1].startswith("✅ 撤销已上线"), history)

    def test_gate(self):
        gate, ran = Gate(), []
        gate.run(lambda: ran.append("early"))
        self.assertEqual(ran, [])
        gate.open()
        self.assertEqual(ran, ["early"])
        gate.run(lambda: ran.append("late"))
        self.assertEqual(ran, ["early", "late"])


class ReplyTest(AppTestBase):
    """Replies are resolved on the git worker; a reply to no note is never published."""

    def setUp(self):
        super().setUp()
        self.git = DeferredWorker()  # earlier jobs are still running
        self.app = self.make_app(git_worker=self.git)

    def send(self, text, mid, reply=None):
        extra = reply_to(reply) if reply is not None else {}
        self.app.handle_update(self.update(message=message(text, mid=mid, **extra)))
        return self.tg.next_id

    def test_reply_to_a_pending_receipt_or_source_appends(self):
        receipt = self.send("第一条", 20)
        second = self.send("补一句", 21, reply=receipt)
        self.send("再补一句", 22, reply=20)
        self.assertEqual(self.repo.commits, [])
        self.git.run_all()
        messages = [msg for _, msg, _ in self.repo.commits]
        self.assertEqual(len(messages), 3)
        note_id = self.state.last_note_id()
        self.assertIn("Pnc-Bot: note-add", messages[0])
        for msg in messages[1:]:
            self.assertIn("Pnc-Bot: note-append", msg)
            self.assertIn(f"Pnc-Bot-Note: {note_id}", msg)
        self.assertIn("第一条\n\n补一句\n\n再补一句\n", self.repo.notes())
        self.assertEqual(self.state.recent_note_ids(), [note_id])
        for mid in (20, 21, 22, receipt, second):
            self.assertEqual(self.state.note_for_message(mid), note_id)
        self.assertEqual(self.app._pending_ids, set())

    def test_reply_to_a_bot_message_that_is_no_note(self):
        self.app.handle_update(self.update(message=message("/status", mid=30)))
        status = self.tg.next_id
        before = len(self.tg.calls)
        self.send("关于评论者 #42 的私话", 31, reply=status)
        self.app.handle_update(self.update(message=photo_message("p", mid=32, **reply_to(status))))
        self.assertEqual(
            self.tg.calls[before:],
            [
                ("send_message", (OWNER, appmod.NOT_A_NOTE_REPLY), {"reply_to": 31, "buttons": None}),
                ("send_message", (OWNER, appmod.NOT_A_NOTE_REPLY), {"reply_to": 32, "buttons": None}),
            ],
        )
        self.assertEqual(self.git.jobs, 0)
        self.assertEqual(self.repo.commits, [])

    def test_reply_to_a_draft_receipt_is_not_published(self):
        draft = self.send("/draft 秘密计划 | secret-plan\n正文", 40)
        self.send("还没决定公开的内容", 41, reply=draft)  # the draft job is still queued
        self.git.run_all()
        self.send("更多私密草稿内容", 42, reply=draft)  # and after it ran
        self.assertEqual(self.tg.texts()[-1], appmod.NOT_A_NOTE_REPLY)
        self.assertEqual(self.tg.texts().count(appmod.NOT_A_NOTE_REPLY), 2)
        self.git.run_all()
        self.assertEqual([msg.split("\n")[0] for _, msg, _ in self.repo.commits], ["draft: secret-plan"])

    @mock.patch.object(images, "process_image", fake_process_image)
    def test_reply_to_a_pending_job_that_fails(self):
        self.tg.files = {"bad": b"not an image"}
        self.app.handle_update(self.update(message=photo_message("bad", mid=50)))
        first = self.tg.next_id
        second = self.send("补充", 51, reply=first)
        self.git.run_all()
        self.assertEqual(self.tg.history(first)[-1], "❌ 无法识别这张图片")
        self.assertEqual(self.tg.history(second)[-1], appmod.NOT_A_NOTE_REPLY)
        self.assertEqual(self.repo.commits, [])

    def test_commands_keep_their_meaning_in_a_reply(self):
        older_receipt = self.send("/note 明确的新随想", 60, reply=999)  # /note: new, whatever it replies to
        self.git.run_all()
        older = self.state.last_note_id()
        self.assertEqual(self.state.note_for_message(older_receipt), older)
        newer_receipt = self.send("乙", 61)
        self.send("/append 补甲", 62, reply=older_receipt)  # mapped: the older note, not the newest
        self.send("/append 补乙", 63, reply=newer_receipt)  # still queued when this arrives
        self.git.run_all()
        messages = [msg for _, msg, _ in self.repo.commits]
        newer = self.state.last_note_id()
        self.assertNotEqual(older, newer)
        self.assertIn("Pnc-Bot: note-add", messages[0])
        self.assertIn(f"Pnc-Bot-Note: {older}", messages[2])
        self.assertIn(f"Pnc-Bot-Note: {newer}", messages[3])
        self.assertIn("明确的新随想\n\n补甲", self.repo.notes())
        self.assertIn("乙\n\n补乙", self.repo.notes())


class UndoForgetsTest(AppTestBase):
    """After /undo of a note, its id names nothing the bot made."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(notesmod, "today_shanghai", return_value="2026-11-21")
        patcher.start()
        self.addCleanup(patcher.stop)

    def undo(self, kind, note_id, restore=None):
        self.repo.undo = UndoCandidate("e" * 40, "note: x", kind, note_id, ("src/content/notes.md",))
        if restore is not None:
            self.repo.on_revert = lambda c: (self.repo.path / "src/content/notes.md").write_text(restore, encoding="utf-8")
        self.app.handle_update(self.update(message=message("/undo")))
        yes = self.tg.calls[-1][2]["buttons"][0][0][1]
        self.app.handle_update(self.update(callback_query=callback(yes, message_id=self.tg.next_id)))
        self.assertEqual(self.repo.reverted[-1], "e" * 40)

    def test_replies_to_an_undone_note_do_not_reach_the_next_one(self):
        before = self.repo.notes()
        self.app.handle_update(self.update(message=message("笔记A", mid=20)))
        receipt_a = self.tg.next_id
        note_id = self.state.last_note_id()
        self.undo("note-add", note_id, restore=before)
        self.assertIsNone(self.state.note_for_message(receipt_a))
        self.assertIsNone(self.state.note_for_message(20))
        self.assertEqual(self.state.recent_note_ids(), [])

        self.app.handle_update(self.update(message=message("笔记B", mid=22)))
        self.assertEqual(self.state.last_note_id(), note_id)  # B reuses A's id
        self.app.handle_update(self.update(message=message("回复A", mid=23, **reply_to(receipt_a))))
        self.assertEqual(self.tg.texts()[-1], appmod.NOT_A_NOTE_REPLY)
        self.assertNotIn("回复A", self.repo.notes())
        self.assertEqual(len(self.repo.commits), 2)

    def test_append_without_id_skips_an_undone_note(self):
        before = self.repo.notes()
        self.app.handle_update(self.update(message=message("笔记A", mid=20)))
        note_id = self.state.last_note_id()
        self.undo("note-add", note_id, restore=before)
        # The laptop writes a note of the same day: it gets the undone note's id.
        laptop, laptop_id = notesmod.insert_note(before, date="2026-11-21", title="笔记本", body="电脑上写的")
        self.assertEqual(laptop_id, note_id)
        (self.repo.path / "src/content/notes.md").write_text(laptop, encoding="utf-8")
        self.app.handle_update(self.update(message=message("/append 追加", mid=25)))
        self.assertIn("没有可追加的随想", self.tg.texts()[-1])
        self.assertEqual(self.repo.notes(), laptop)

    def test_undo_of_an_append_keeps_the_note(self):
        self.app.handle_update(self.update(message=message("笔记A", mid=20)))
        receipt_a = self.tg.next_id
        note_id = self.state.last_note_id()
        self.app.handle_update(self.update(message=message("补充", mid=21, **reply_to(receipt_a))))
        self.undo("note-append", note_id)
        self.assertEqual(self.state.note_for_message(receipt_a), note_id)
        self.assertEqual(self.state.recent_note_ids(), [note_id])


class RetryTest(AppTestBase):
    """Final edits and notices survive a flaky proxy; the poll thread never waits."""

    def note_watch(self):
        receipt = self.tg.next_id + 1
        self.app.handle_update(self.update(message=message("上线", mid=20)))
        sha, ok, failed, timed_out = self.tracker.watches[0]
        return receipt, sha, ok

    def attempts(self, receipt, prefix):
        return [t for t in self.tg.history(receipt) if t.startswith(prefix)]

    def test_final_edit_retried(self):
        receipt, sha, ok = self.note_watch()
        self.tg.failures["edit_message"] = [network_error(), TelegramError("editMessageText: invalid response (HTTP 502)", 502)]
        with self.assertLogs("pnc_bot", level="WARNING"):
            ok({"result": "success", "sha": sha})
        self.assertEqual(len(self.attempts(receipt, "✅ 已上线")), 3)
        self.assertEqual(self.waits, [2.0, 5.0])

    def test_gives_up_after_three_retries(self):
        receipt, sha, ok = self.note_watch()
        self.tg.failures["edit_message"] = [network_error() for _ in range(5)]
        with self.assertLogs("pnc_bot", level="WARNING"):
            ok({"result": "success", "sha": sha})
        self.assertEqual(len(self.attempts(receipt, "✅ 已上线")), 4)
        self.assertEqual(self.waits, [2.0, 5.0, 15.0])
        self.assertEqual(self.tg.calls[-1][0], "edit_message")  # no new message as a fallback

    def test_rate_limit_waits_as_asked(self):
        receipt, sha, ok = self.note_watch()
        self.tg.failures["edit_message"] = [TelegramError("editMessageText: Too Many Requests", 429, retry_after=30)]
        with self.assertLogs("pnc_bot", level="WARNING"):
            ok({"result": "success", "sha": sha})
        self.assertEqual(self.waits, [30.0])
        self.assertEqual(len(self.attempts(receipt, "✅ 已上线")), 2)

    def test_bad_request_falls_back_without_retrying(self):
        receipt, sha, ok = self.note_watch()
        self.tg.failures["edit_message"] = [TelegramError("editMessageText: message to edit not found", 400)]
        with self.assertLogs("pnc_bot", level="WARNING"):
            ok({"result": "success", "sha": sha})
        self.assertEqual(self.waits, [])
        self.assertEqual(self.tg.calls[-1][0], "send_message")
        self.assertTrue(self.tg.calls[-1][1][1].startswith("✅ 已上线"))

    def test_forbidden_is_not_retried(self):
        receipt, sha, ok = self.note_watch()
        self.tg.failures["edit_message"] = [TelegramError("editMessageText: Forbidden", 403)]
        with self.assertLogs("pnc_bot", level="WARNING"):
            ok({"result": "success", "sha": sha})
        self.assertEqual(self.waits, [])
        self.assertEqual(len(self.attempts(receipt, "✅ 已上线")), 1)

    def test_deploy_failure_notice_retried(self):
        self.tg.failures["send_message"] = [network_error()]
        with self.assertLogs("pnc_bot", level="WARNING"):
            self.app.notify("❌ <b>部署失败</b>")
        self.assertEqual([c[1][1] for c in self.tg.calls], ["❌ <b>部署失败</b>"] * 2)
        self.assertEqual(self.waits, [2.0])

    def test_action_result_retried(self):
        self.app.handle_update(self.update(message=message("/restart pnc-api", mid=7)))
        yes = self.tg.calls[-1][2]["buttons"][0][0][1]
        confirm = self.tg.next_id
        original = self.ops.restart

        def restart(name):  # the proxy drops the result edit once
            self.tg.failures["edit_message"] = [network_error()]
            return original(name)

        self.ops.restart = restart
        with self.assertLogs("pnc_bot", level="WARNING"):
            self.app.handle_update(self.update(callback_query=callback(yes, message_id=confirm)))
        self.assertEqual(self.tg.history(confirm), ["⏳ 执行中…", "✅ 已重启 <b>pnc-api</b>", "✅ 已重启 <b>pnc-api</b>"])
        self.assertEqual(self.waits, [2.0])

    def test_confirmation_from_a_worker_retried(self):
        self.tg.failures["send_message"] = [network_error()]
        with self.assertLogs("pnc_bot", level="WARNING"):
            self.app.handle_update(self.update(message=message("/delcomment 1", mid=8)))
        self.assertEqual(len(self.app.confirmations), 1)
        self.assertEqual(self.waits, [2.0])

    def test_poll_thread_never_waits(self):
        self.tg.failures["send_message"] = [network_error()]
        with self.assertLogs("pnc_bot", level="WARNING"):
            self.app.handle_update(self.update(message=message("/restart pnc-api", mid=7)))
        self.assertEqual(len(self.app.confirmations), 0)
        self.app.handle_update(self.update(message=message("/restart pnc-api", mid=8)))
        _, no = self.tg.calls[-1][2]["buttons"][0]
        self.tg.failures["edit_message"] = [network_error()]
        with self.assertLogs("pnc_bot", level="WARNING"):
            self.app.handle_update(self.update(callback_query=callback(no[1], message_id=self.tg.next_id)))
        self.assertEqual(self.waits, [])

    def test_no_retries_once_stopping(self):
        receipt, sha, ok = self.note_watch()
        self.app.stop_event.set()
        self.tg.failures["edit_message"] = [network_error()]
        with self.assertLogs("pnc_bot", level="WARNING"):
            ok({"result": "success", "sha": sha})
        self.assertEqual(self.waits, [])


class ShutdownTest(AppTestBase):
    """On SIGTERM, every unfinished message says so; nothing queued runs any more."""

    def setUp(self):
        super().setUp()
        self.git = DeferredWorker()
        self.pool = DeferredWorker()
        self.app = self.make_app(git_worker=self.git, pool=self.pool)

    def test_queued_work_is_reported_and_never_runs(self):
        self.app.handle_update(self.update(message=message("第一条", mid=20)))
        first = self.tg.next_id
        self.app.handle_update(self.update(message=message("第二条", mid=21)))
        second = self.tg.next_id
        self.app.handle_update(self.update(message=photo_message("p2", mid=31, group="album")))
        self.app.handle_update(self.update(message=photo_message("p1", mid=30, group="album")))
        self.app.handle_update(self.update(message=message("/restart pnc-api", mid=40)))
        yes = self.tg.calls[-1][2]["buttons"][0][0][1]
        confirm = self.tg.next_id
        self.app.handle_update(self.update(callback_query=callback(yes, message_id=confirm)))

        self.app.shutdown()
        self.assertEqual(self.tg.history(first)[-1], appmod.STOP_QUEUED)
        self.assertEqual(self.tg.history(second)[-1], appmod.STOP_QUEUED)
        self.assertEqual(self.tg.history(confirm)[-1], appmod.STOP_ACTION_QUEUED)
        self.assertIn(("send_message", (OWNER, appmod.STOP_MEDIA), {"reply_to": 30, "buttons": None}), self.tg.calls)
        self.assertFalse(self.app.media.pending())

        calls = len(self.tg.calls)
        self.git.run_all()  # the executor cancels these; one that slips through must not act
        self.pool.run_all()
        self.assertEqual(self.repo.commits, [])
        self.assertEqual(self.ops.restarts, [])
        self.assertEqual(len(self.tg.calls), calls)

    def test_job_running_while_stopping(self):
        self.app.handle_update(self.update(message=message("推送中", mid=20)))
        receipt = self.tg.next_id
        publish = self.repo.publish

        def stop_during_publish(*args, **kwargs):
            self.app.shutdown()
            return publish(*args, **kwargs)

        self.repo.publish = stop_during_publish
        self.git.run_all()
        [(sha, _, _)] = self.repo.commits
        self.assertEqual(
            self.tg.history(receipt),
            [appmod.STOP_RUNNING, f"📝 已提交 <code>{sha[:7]}</code>；{appmod.STOPPING_NO_RESULT}"],
        )
        self.assertEqual(self.tracker.watches, [])
        self.assertEqual(self.state.note_for_message(20), self.state.last_note_id())  # it was published

    def test_waiting_for_a_deploy_is_reported(self):
        self.app.handle_update(self.update(message=message("已推送", mid=20)))
        receipt = self.tg.next_id
        self.git.run_all()
        sha = self.tracker.watches[0][0]
        plan = deploy_plan()
        with mock.patch.object(appmod, "plan_deploy", return_value=plan):
            self.app.handle_update(self.update(message=message("/deploy", mid=21)))
            self.git.run_all()
        yes = self.tg.calls[-1][2]["buttons"][0][0][1]
        confirm = self.tg.next_id
        self.app.handle_update(self.update(callback_query=callback(yes, message_id=confirm)))
        self.pool.run_all()

        self.app.shutdown()
        self.assertEqual(self.tg.history(receipt)[-1], f"📝 已提交 <code>{sha[:7]}</code>；{appmod.STOP_NO_RESULT}")
        self.assertEqual(self.tg.history(confirm)[-1], plan.describe() + "\n\n" + appmod.STOP_DEPLOY_STARTED)

    def test_deploy_starting_while_stopping(self):
        plan = deploy_plan()
        with mock.patch.object(appmod, "plan_deploy", return_value=plan):
            self.app.handle_update(self.update(message=message("/deploy", mid=21)))
            self.git.run_all()
        yes = self.tg.calls[-1][2]["buttons"][0][0][1]
        confirm = self.tg.next_id
        self.app.handle_update(self.update(callback_query=callback(yes, message_id=confirm)))
        self.ops.on_start = lambda plan, request_id: self.app.shutdown()  # SIGTERM during systemd-run
        self.pool.run_all()
        self.assertEqual(
            self.tg.history(confirm),
            ["⏳ 执行中…", appmod.STOP_ACTION_RUNNING, plan.describe() + "\n\n" + appmod.STOPPING_DEPLOY_STARTED],
        )

    def test_queued_deploy_never_starts_after_stopping(self):
        with mock.patch.object(appmod, "plan_deploy", return_value=deploy_plan()):
            self.app.handle_update(self.update(message=message("/deploy", mid=21)))
            self.git.run_all()
        yes = self.tg.calls[-1][2]["buttons"][0][0][1]
        confirm = self.tg.next_id
        self.app.handle_update(self.update(callback_query=callback(yes, message_id=confirm)))
        self.app.shutdown()
        self.pool.run_all()
        self.assertEqual(self.ops.deploys, [])
        self.assertEqual(self.tracker.requests, [])
        self.assertEqual(self.tg.history(confirm)[-1], appmod.STOP_ACTION_QUEUED)

    def test_finished_work_is_left_alone(self):
        self.app.handle_update(self.update(message=message("已上线", mid=20)))
        self.git.run_all()
        sha, ok, _, _ = self.tracker.watches[0]
        ok({"result": "success", "sha": sha})
        calls = len(self.tg.calls)
        self.app.shutdown()
        self.app.shutdown()  # idempotent
        self.assertEqual(len(self.tg.calls), calls)
        self.assertEqual(len(self.app.interrupts), 0)

    def test_stop_notices_are_bounded(self):
        self.app.handle_update(self.update(message=message("第一条", mid=20)))
        release = threading.Event()
        self.addCleanup(release.set)
        self.tg.on_edit = lambda message_id, html: release.wait(10)  # the proxy hangs
        with mock.patch.object(appmod, "SHUTDOWN_NOTICE_WAIT", 0.2), self.assertLogs("pnc_bot", level="WARNING"):
            started = time.monotonic()
            self.app.shutdown()
            self.assertLess(time.monotonic() - started, 5)

    def test_interrupts_unit(self):
        reg = Interrupts()
        a = reg.add(1, "queued")
        self.assertTrue(reg.update(a, "running"))
        b = reg.add(None, "no message")
        reg.finish(b)
        self.assertTrue(reg.update(None, "x"))
        self.assertEqual(reg.close(), [(1, "running")])
        self.assertFalse(reg.update(a, "later"))
        self.assertFalse(reg.update(None, "x"))
        reg.add(2, "after close")
        self.assertEqual(len(reg), 0)


class ParseTest(unittest.TestCase):
    def test_parse_command(self):
        self.assertEqual(parse_command("/note hi"), ("note", "hi"))
        self.assertEqual(parse_command("/Note@pnc_bot  hi\nthere"), ("note", "hi\nthere"))
        self.assertEqual(parse_command("/list"), ("list", ""))
        self.assertIsNone(parse_command("note"))
        self.assertIsNone(parse_command("/note-x"))
        self.assertIsNone(parse_command(" /note"))

    def test_media_groups_unit(self):
        clock = [0.0]
        groups = MediaGroups(delay=2.5, clock=lambda: clock[0])
        groups.add({"media_group_id": "a", "message_id": 1})
        clock[0] = 2
        groups.add({"media_group_id": "a", "message_id": 2})
        clock[0] = 4
        self.assertEqual(groups.due(), [])
        clock[0] = 4.5
        self.assertEqual([m["message_id"] for m in groups.due()[0]], [1, 2])
        self.assertFalse(groups.pending())


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        make_blog(self.root, ["20261121-100000-aaaaaaaaaaaa"], current="20261121-100000-aaaaaaaaaaaa")
        make_comments_db(self.root / "pnc.db", [{"content": "hi <there>"}])
        self.env = {
            "PNC_BOT_ALLOWED_USER_ID": str(OWNER),
            "PNC_BOT_TOKEN_FILE": str(self.root / "missing-token"),
            "PNC_BOT_REPO": str(self.root / "repo"),
            "PNC_BOT_STATE_DIR": str(self.root / "state"),
            "PNC_BOT_BLOG_ROOT": str(self.root / "blog"),
            "PNC_BOT_COMMENTS_DB": str(self.root / "pnc.db"),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=False), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with mock.patch.object(appmod, "TelegramClient", side_effect=AssertionError("must not contact Telegram")), \
                    mock.patch.object(appmod, "DEPLOY_SH", str(self.root / "deploy.sh")):
                code = appmod.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_selftest_refuses_changing_commands(self):
        for command in ("/deploy", "/restart pnc-api", "/undo", "/delcomment 1", "/note hi", "status", "/rm"):
            code, out, _ = self.run_main("--selftest-command", command)
            self.assertEqual(code, 2, command)
            self.assertEqual(out, "")

    def test_selftest_never_reads_token(self):
        code, out, _ = self.run_main("--selftest-command", "/help")
        self.assertEqual(code, 0)
        self.assertIn("PNC 博客机器人", out)
        code, out, _ = self.run_main("--selftest-command", "/comments")
        self.assertEqual(code, 0)
        self.assertIn("hi &lt;there&gt;", out)
        self.assertFalse((self.root / "state").exists())

    def test_check_config_never_prints_token(self):
        token_file = self.root / "token"
        token_file.write_text(TOKEN + "\n")
        os.chmod(token_file, 0o600)
        self.env["PNC_BOT_TOKEN_FILE"] = str(token_file)
        code, out, err = self.run_main("--check-config")
        self.assertNotIn(TOKEN, out + err)
        self.assertNotIn(TOKEN.split(":")[1], out + err)
        self.assertNotIn(str(OWNER), out + err)
        self.assertIn("token: loaded", out)
        self.assertEqual(code, 1)  # repo and deploy.sh are missing here
        self.assertIn("[FAIL] repo", out)

    def test_check_config_bad_token(self):
        token_file = self.root / "token"
        token_file.write_text("garbage-secret")
        self.env["PNC_BOT_TOKEN_FILE"] = str(token_file)
        code, out, err = self.run_main("--check-config")
        self.assertEqual(code, 1)
        self.assertNotIn("garbage-secret", out + err)

    def test_missing_user_id(self):
        self.env["PNC_BOT_ALLOWED_USER_ID"] = "-5"
        code, _, err = self.run_main("--selftest-command", "/help")
        self.assertEqual(code, 1)
        self.assertIn("PNC_BOT_ALLOWED_USER_ID", err)


if __name__ == "__main__":
    unittest.main()


class MergeFollowupTest(AppTestBase):
    """Cross-group follow-ups applied at merge time."""

    confirm_deploy = WatchOrderTest.confirm_deploy

    def test_unit_is_named_before_start_and_watched_as_long_as_it_may_run(self):
        class NamedOps(FakeOps):
            def deploy_unit(self, request_id):
                return f"pnc-bot-deploy-1-{request_id}"

            def start_deploy(self, plan, request_id, *, unit=None):
                self.unit = unit
                return super().start_deploy(plan, request_id)

        self.ops = NamedOps()
        app = self.make_app()
        self.confirm_deploy(app)
        request_id = self.ops.deploys[0][1]
        self.assertEqual(self.ops.unit, f"pnc-bot-deploy-1-{request_id}")
        kw = self.tracker.requests[0][4]
        self.assertEqual(kw["timeout"], appmod.WATCH_DEPLOY_SEC)
        self.assertGreater(appmod.WATCH_DEPLOY_SEC, appmod.DEPLOY_UNIT_MAX_SEC)

    def test_success_says_which_commit_went_live(self):
        confirm = self.confirm_deploy(self.app)
        _, ok, _, _, _ = self.tracker.requests[0]
        ok({"result": "success", "mode": "deploy", "sha": "b" * 40, "trigger": "pnc-bot",
            "finished_at": "2026-11-21T16:00:00Z"})
        last = self.tg.history(confirm)[-1]
        self.assertIn("✅ 部署完成：", last)
        self.assertIn("b" * 12, last)

    def test_any_missing_site_asset_is_refused_before_commit(self):
        work = self.root / "asset-check"
        (work / notesmod.NOTES_ASSET_DIR).mkdir(parents=True)
        with self.assertRaises(appmod.UserError) as caught:
            App._check_note_images(work, "看图 ![x](/assets/img/missing.png)")
        self.assertIn("/assets/img/missing.png", str(caught.exception))
        # An existing real image is fine.
        from PIL import Image
        (work / "src/static/assets/img").mkdir(parents=True)
        Image.new("RGB", (4, 3)).save(work / "src/static/assets/img/ok.png")
        App._check_note_images(work, "![x](/assets/img/ok.png)")
