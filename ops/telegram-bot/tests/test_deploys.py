import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from pnc_bot import deploys
from pnc_bot.deploys import DeployHistory, DeployTracker, failure_details
from pnc_bot.ops import Ops
from pnc_bot.telegram import truncate_html

from tests.runtime_fakes import TOKEN, FakeGitRepo, make_blog, make_config, write_records

NEW = "1" * 40
CHILD = "2" * 40
OTHER = "3" * 40
REMOTE = "4" * 40


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.blog = make_blog(
            self.root,
            ["20261121-100000-aaaaaaaaaaaa", "20261120-100000-bbbbbbbbbbbb", "not-a-release"],
            current="20261121-100000-aaaaaaaaaaaa",
        )
        (self.blog / "releases" / "20261122-100000-cccccccccccc").write_text("a file, not a release")
        self.history = DeployHistory(self.blog)

    def tearDown(self):
        self.tmp.cleanup()

    def test_current_release(self):
        self.assertEqual(self.history.current_release(), ("20261121-100000-aaaaaaaaaaaa", "aaaaaaaaaaaa"))
        os.unlink(self.blog / "current")
        self.assertIsNone(self.history.current_release())

    def test_releases_sorted_and_filtered(self):
        self.assertEqual(self.history.releases(), ["20261120-100000-bbbbbbbbbbbb", "20261121-100000-aaaaaaaaaaaa"])

    def test_last(self):
        self.assertIsNone(self.history.last())
        (self.blog / "state" / "last-deploy.json").write_text('{"result": "running"}')
        self.assertEqual(self.history.last(), {"result": "running"})
        (self.blog / "state" / "last-deploy.json").write_text("{broken")
        self.assertIsNone(self.history.last())

    def test_read_new_missing_file(self):
        self.assertEqual(self.history.read_new(0), ([], 0))
        self.assertEqual(self.history.read_new(100), ([], 0))

    def test_read_new_complete_lines(self):
        write_records(self.blog, {"id": 1}, {"id": 2})
        records, offset = self.history.read_new(0)
        self.assertEqual([r["id"] for r in records], [1, 2])
        self.assertEqual(offset, self.history.end_offset())
        self.assertEqual(self.history.read_new(offset), ([], offset))

    def test_partial_last_line_waits(self):
        write_records(self.blog, {"id": 1}, partial='{"id": 2, "res')
        records, offset = self.history.read_new(0)
        self.assertEqual([r["id"] for r in records], [1])
        with open(self.blog / "state" / "deploys.jsonl", "a") as fh:
            fh.write('ult": "success"}\n')
        records, offset2 = self.history.read_new(offset)
        self.assertEqual(records, [{"id": 2, "result": "success"}])
        self.assertEqual(offset2, self.history.end_offset())

    def test_only_partial_line(self):
        write_records(self.blog, partial='{"id": 1')
        self.assertEqual(self.history.read_new(0), ([], 0))

    def test_bad_lines_skipped(self):
        path = self.blog / "state" / "deploys.jsonl"
        path.write_bytes(b'not json\n\xff\xfe\n[1, 2]\n{"id": 3}\n')
        with self.assertLogs("pnc_bot.deploys", level="WARNING") as logs:
            records, _ = self.history.read_new(0)
        self.assertEqual(len(logs.output), 2)
        self.assertEqual(records, [{"id": 3}])

    def test_truncation_restarts_from_zero(self):
        write_records(self.blog, {"id": 1}, {"id": 2}, {"id": 3})
        _, offset = self.history.read_new(0)
        (self.blog / "state" / "deploys.jsonl").write_text(json.dumps({"id": 9}) + "\n")
        records, new_offset = self.history.read_new(offset)
        self.assertEqual(records, [{"id": 9}])
        self.assertEqual(new_offset, self.history.end_offset())

    def test_logs(self):
        self.assertIsNone(self.history.latest_log())
        older = self.blog / "logs" / "deploy-20261121-100000-aaaaaaaaaaaa.log"
        newer = self.blog / "logs" / "deploy-20261121-110000-rollback.log"
        older.write_text("old")
        newer.write_text("\n".join(f"line {i}" for i in range(100)))
        (self.blog / "logs" / "other.txt").write_text("x")
        self.assertEqual(self.history.latest_log(), newer)
        tail = self.history.log_tail(newer, 3)
        self.assertEqual(tail, "line 97\nline 98\nline 99")

    def test_release_ok_uses_the_last_record_for_that_release(self):
        a, b = "20261121-100000-aaaaaaaaaaaa", "20261120-100000-bbbbbbbbbbbb"
        self.assertFalse(self.history.release_ok(a))  # no deploys.jsonl
        write_records(self.blog, {"release": a, "result": "success"}, {"release": b, "result": "failure"})
        self.assertTrue(self.history.release_ok(a))
        self.assertFalse(self.history.release_ok(b))
        write_records(self.blog, {"release": a, "result": "running"}, {"release": None, "result": "failure"})
        self.assertTrue(self.history.release_ok(a))  # running / release-less records change nothing
        write_records(self.blog, {"release": a, "mode": "rollback", "result": "failure"})
        self.assertFalse(self.history.release_ok(a))
        write_records(self.blog, {"release": b, "result": "success"}, partial='{"release": "%s", "result": "fail' % b)
        self.assertTrue(self.history.release_ok(b))  # a line still being written is ignored
        for bad in ("", "../x", None, "20261121-100000-AAAAAAAAAAAA"):
            self.assertFalse(self.history.release_ok(bad))

    def test_release_ok_reads_only_the_tail_of_a_big_history(self):
        a = "20261121-100000-aaaaaaaaaaaa"
        path = self.blog / "state" / "deploys.jsonl"
        with open(path, "w") as fh:
            fh.write(json.dumps({"release": a, "result": "failure"}) + "\n")
            filler = json.dumps({"release": None, "result": "success", "pad": "x" * 200}) + "\n"
            while fh.tell() < deploys.MAX_READ + 5000:
                fh.write(filler)
            fh.write(json.dumps({"release": a, "result": "success"}) + "\n")
        self.assertTrue(self.history.release_ok(a))
        with open(path, "a") as fh:
            fh.write("not json\n" + json.dumps({"release": a, "result": "failure"}) + "\n")
        self.assertFalse(self.history.release_ok(a))

    def test_safe_log_path(self):
        good = self.blog / "logs" / "deploy-20261121-100000-aaaaaaaaaaaa.log"
        good.write_text("x")
        self.assertEqual(self.history.safe_log_path(str(good)), good)
        outside = self.root / "deploy-20261121-100000-aaaaaaaaaaaa.log"
        outside.write_text("secret")
        self.assertIsNone(self.history.safe_log_path(str(outside)))
        self.assertIsNone(self.history.safe_log_path("/etc/passwd"))
        self.assertIsNone(self.history.safe_log_path(str(self.blog / "logs" / ".." / "logs" / "missing.log")))
        self.assertIsNone(self.history.safe_log_path(None))


class TrackerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.blog = make_blog(self.root, [], current=None)
        self.history = DeployHistory(self.blog)
        self.repo = FakeGitRepo(CHILD, known=[NEW, OTHER], ancestors=[(NEW, CHILD), (NEW, REMOTE)], remote_only=[REMOTE])
        self.notified = []
        self.events = []
        self.now = [1000.0]
        self.tracker = DeployTracker(
            self.history, self.repo, self.notified.append, timeout=1200, fallback_grace=60, clock=lambda: self.now[0]
        )
        self.tracker.prime()

    def tearDown(self):
        self.tmp.cleanup()

    def callbacks(self, name):
        return (
            lambda record: self.events.append((name, "success", record.get("sha"))),
            lambda record: self.events.append((name, "failure", record.get("error"))),
            lambda: self.events.append((name, "timeout", None)),
        )

    def record(self, **values):
        base = {"id": "x", "mode": "deploy", "sha": None, "release": None, "result": "success",
                "started_at": "2026-11-21T00:00:00Z", "finished_at": "2026-11-21T00:01:00Z",
                "log": "", "error": "", "trigger": "github-actions", "request": ""}
        base.update(values)
        return base

    def test_history_not_replayed(self):
        write_records(self.blog, self.record(sha=OTHER, result="failure", error="old"))
        tracker = DeployTracker(self.history, self.repo, self.notified.append)
        tracker.prime()
        tracker.poll_once()
        self.assertEqual(self.notified, [])

    def test_success_exact_sha(self):
        self.tracker.watch(NEW, *self.callbacks("a"))
        write_records(self.blog, self.record(sha=NEW))
        self.tracker.poll_once()
        self.assertEqual(self.events, [("a", "success", NEW)])
        self.assertEqual(self.tracker.pending(), 0)

    def test_success_via_ancestor(self):
        self.tracker.watch(NEW, *self.callbacks("a"))
        write_records(self.blog, self.record(sha=CHILD))
        self.tracker.poll_once()
        self.assertEqual(self.events, [("a", "success", CHILD)])
        self.assertEqual(self.repo.fetches, 0)

    def test_unknown_sha_fetched_once(self):
        self.tracker.watch(NEW, *self.callbacks("a"))
        write_records(self.blog, self.record(sha=REMOTE))
        self.tracker.poll_once()
        self.assertEqual(self.events, [("a", "success", REMOTE)])
        self.assertEqual(self.repo.fetches, 1)

    def test_unknown_sha_still_unknown_after_fetch(self):
        self.tracker.watch(NEW, *self.callbacks("a"))
        write_records(self.blog, self.record(sha="9" * 40), self.record(sha="9" * 40))
        self.tracker.poll_once()
        self.assertEqual(self.events, [])
        self.assertEqual(self.repo.fetches, 1)
        self.assertEqual(self.tracker.pending(), 1)

    def test_unrelated_success_is_silent_and_keeps_waiting(self):
        self.tracker.watch(NEW, *self.callbacks("a"))
        write_records(self.blog, self.record(sha=OTHER))
        self.tracker.poll_once()
        self.assertEqual(self.events, [])
        self.assertEqual(self.notified, [])
        self.assertEqual(self.tracker.pending(), 1)

    def test_failure_for_watched_commit(self):
        self.tracker.watch(NEW, *self.callbacks("a"))
        write_records(self.blog, self.record(sha=CHILD, result="failure", error="build failed"))
        self.tracker.poll_once()
        self.assertEqual(self.events, [("a", "failure", "build failed")])
        self.assertEqual(self.notified, [])

    def test_unwatched_failure_notifies(self):
        log = self.blog / "logs" / "deploy-20261121-100000-333333333333.log"
        log.write_text("\n".join([f"step {i}" for i in range(30)] + [f"leaked {TOKEN}", "<b>npm ERR</b>"]))
        write_records(self.blog, self.record(sha=OTHER, result="failure", error="health <check> failed", log=str(log)))
        self.tracker.poll_once()
        self.assertEqual(len(self.notified), 1)
        html = self.notified[0]
        self.assertIn("部署失败", html)
        self.assertIn(OTHER[:12], html)
        self.assertIn("health &lt;check&gt; failed", html)
        self.assertIn("&lt;b&gt;npm ERR&lt;/b&gt;", html)
        self.assertNotIn(TOKEN, html)
        self.assertIn("[REDACTED]", html)
        self.assertNotIn("step 10", html)  # only the last 15 lines
        self.assertIn("step 29", html)

    def test_failure_details_keeps_newest_line_after_escaping(self):
        log = self.blog / "logs" / "deploy-20261121-100000-333333333333.log"
        log.write_text("\n".join(f'line {i} ' + '"q" ' * 60 for i in range(15)))
        html = failure_details(self.record(sha=OTHER, result="failure", error="x", log=str(log)), self.history)
        self.assertIn("line 14 ", html)
        pre = html.split("<pre>", 1)[1]
        self.assertLessEqual(len(pre), 2500 + len("…\n</pre>"))
        self.assertEqual(truncate_html(html), html)

    def test_log_outside_logs_dir_not_read(self):
        secret = self.root / "deploy-20261121-100000-333333333333.log"
        secret.write_text("TOP SECRET")
        html = failure_details(self.record(result="failure", log=str(secret)), self.history)
        self.assertNotIn("TOP SECRET", html)

    def test_running_records_ignored(self):
        self.tracker.watch(NEW, *self.callbacks("a"))
        write_records(self.blog, self.record(sha=NEW, result="running"))
        self.tracker.poll_once()
        self.assertEqual(self.events, [])

    def test_timeout(self):
        self.tracker.watch(NEW, *self.callbacks("a"))
        self.now[0] += 1199
        self.tracker.poll_once()
        self.assertEqual(self.events, [])
        self.now[0] += 2
        self.tracker.poll_once()
        self.assertEqual(self.events, [("a", "timeout", None)])
        self.assertEqual(self.tracker.pending(), 0)

    def test_watch_request(self):
        self.tracker.watch_request("abcd1234abcd1234", *self.callbacks("r"))
        write_records(
            self.blog,
            self.record(sha=NEW, request=""),
            self.record(sha=NEW, request="abcd1234abcd1234", result="failure", error="lock wait"),
        )
        self.tracker.poll_once()
        self.assertEqual(self.events, [("r", "failure", "lock wait")])
        self.assertEqual(self.notified, [])

    def test_request_fallback_when_unit_ends_without_record(self):
        done = [False]
        self.tracker.watch_request(
            "abcd1234abcd1234", *self.callbacks("r"), unit_done=lambda: done[0], unit_tail=lambda: "flock: timeout"
        )
        self.tracker.poll_once()
        done[0] = True
        self.tracker.poll_once()
        self.now[0] += 59
        self.tracker.poll_once()
        self.assertEqual(self.events, [])
        self.now[0] += 2
        self.tracker.poll_once()
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0][:2], ("r", "failure"))
        self.assertEqual(self.tracker.pending(), 0)

    def test_one_wrong_finished_probe_does_not_fail_a_running_deploy(self):
        # The first probe answers "finished" by mistake, every later one sees
        # the unit running: no synthetic failure, and the real record wins.
        answers = [True]
        self.tracker.watch_request(
            "abcd1234abcd1234", *self.callbacks("r"), unit_done=lambda: answers.pop(0) if answers else False
        )
        for _ in range(16):  # 80 s of polls, past the 60 s grace
            self.tracker.poll_once()
            self.now[0] += 5
        self.assertEqual(self.events, [])
        self.assertEqual(self.tracker.pending(), 1)
        write_records(self.blog, self.record(sha=NEW, request="abcd1234abcd1234"))
        self.tracker.poll_once()
        self.assertEqual(self.events, [("r", "success", NEW)])

    def test_probe_timeout_with_real_ops_is_not_a_failure(self):
        # The real Ops.unit_finished: one is-active probe times out (run_command's
        # (124, "") result), later ones say "active".
        answers = [(124, "", "systemctl timed out after 15s")]

        def run(argv, timeout=30):
            if list(argv[:3]) == ["systemctl", "--user", "is-active"]:
                return subprocess.CompletedProcess(argv, *(answers.pop(0) if answers else (0, "active\n", "")))
            return subprocess.CompletedProcess(argv, 0, "", "")

        ops = Ops(make_config(Path(self.tmp.name)), self.history, self.repo, runner=run)
        unit = "pnc-bot-deploy-1700000000"  # also valid before units carried the request id
        self.tracker.watch_request(
            "abcd1234abcd1234", *self.callbacks("r"),
            unit_done=lambda: ops.unit_finished(unit), unit_tail=lambda: ops.unit_journal(unit),
        )
        for _ in range(16):
            self.tracker.poll_once()
            self.now[0] += 5
        self.assertEqual(self.events, [])
        write_records(self.blog, self.record(sha=NEW, request="abcd1234abcd1234"))
        self.tracker.poll_once()
        self.assertEqual(self.events, [("r", "success", NEW)])

    def test_grace_restarts_after_a_running_answer(self):
        answers = [True, False, True, True]
        self.tracker.watch_request(
            "abcd1234abcd1234", *self.callbacks("r"), unit_done=lambda: answers.pop(0) if answers else True
        )
        self.tracker.poll_once()  # finished (wrong) at 1000
        self.now[0] += 30
        self.tracker.poll_once()  # running: grace reset
        self.now[0] += 5
        self.tracker.poll_once()  # finished at 1035
        self.now[0] += 55
        self.tracker.poll_once()  # 1090: only 55 s
        self.assertEqual(self.events, [])
        self.now[0] += 5
        self.tracker.poll_once()  # 1095: 60 s
        self.assertEqual([e[:2] for e in self.events], [("r", "failure")])

    def test_probe_errors_neither_start_nor_reset_the_grace(self):
        calls = [0]

        def unit_done():
            calls[0] += 1
            if calls[0] == 2:
                raise RuntimeError("bus gone")
            return True

        self.tracker.watch_request("abcd1234abcd1234", *self.callbacks("r"), unit_done=unit_done)
        self.tracker.poll_once()
        self.now[0] += 30
        with self.assertLogs("pnc_bot.deploys", level="ERROR"):
            self.tracker.poll_once()
        self.now[0] += 31
        self.tracker.poll_once()
        self.assertEqual([e[:2] for e in self.events], [("r", "failure")])

    def test_watch_request_timeout_and_unwatch(self):
        self.tracker.watch_request("abcd1234abcd1234", *self.callbacks("r"), timeout=1900)
        self.tracker.watch_request("ffff0000ffff0000", *self.callbacks("s"))
        self.now[0] += 1201
        self.tracker.poll_once()
        self.assertEqual(self.events, [("s", "timeout", None)])
        self.assertTrue(self.tracker.unwatch_request("abcd1234abcd1234"))
        self.assertFalse(self.tracker.unwatch_request("abcd1234abcd1234"))
        self.assertEqual(self.tracker.pending(), 0)
        write_records(self.blog, self.record(sha=NEW, request="abcd1234abcd1234", result="failure"))
        self.tracker.poll_once()
        self.assertEqual(len(self.events), 1)  # nobody waits any more: standalone notice instead
        self.assertEqual(len(self.notified), 1)

    def test_record_wins_over_fallback(self):
        self.tracker.watch_request("abcd1234abcd1234", *self.callbacks("r"), unit_done=lambda: True)
        self.tracker.poll_once()
        write_records(self.blog, self.record(sha=NEW, request="abcd1234abcd1234"))
        self.now[0] += 10
        self.tracker.poll_once()
        self.assertEqual(self.events, [("r", "success", NEW)])

    def test_callback_errors_do_not_stop_tracker(self):
        def boom(record):
            raise RuntimeError("callback bug")

        self.tracker.watch(NEW, boom, boom, lambda: None)
        self.tracker.watch(NEW, *self.callbacks("b"))
        write_records(self.blog, self.record(sha=NEW))
        with self.assertLogs("pnc_bot.deploys", level="ERROR"):
            self.tracker.poll_once()
        self.assertEqual(self.events, [("b", "success", NEW)])

    def test_rejects_bad_sha(self):
        with self.assertRaises(ValueError):
            self.tracker.watch("not-a-sha", *self.callbacks("a"))


if __name__ == "__main__":
    unittest.main()


class AnsiStripTest(unittest.TestCase):
    def test_log_tail_drops_terminal_colours(self):
        import tempfile
        from pathlib import Path
        from pnc_bot.deploys import DeployHistory
        from pnc_bot.util import strip_ansi
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "deploy-x.log"
            log.write_text("\x1b[2m00:11:57\x1b[22m \x1b[34m[build]\x1b[39m \x1b[32m✓ Completed\x1b[39m\nplain\n", encoding="utf-8")
            tail = DeployHistory(Path(tmp)).log_tail(log, 5)
        self.assertEqual(tail, "00:11:57 [build] ✓ Completed\nplain")
        self.assertNotIn("\x1b", tail)
        self.assertEqual(strip_ansi("a\x1b[1;31mb\x1b[0mc\x1b[K"), "abc")
