import http.server
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from pnc_bot import ops
from pnc_bot.deploys import DeployHistory
from pnc_bot.ops import DEPLOY_SH, PRE_LIMIT, DeployPlan, HttpResult, Ops, OpsError, PlanError, plan_deploy
from pnc_bot.telegram import truncate_html
from pnc_bot.util import utf16_len

from tests.runtime_fakes import TOKEN, FakeGitRepo, FakeRunner, make_blog, make_config, write_records

HEAD = "c" * 40
OLD = "a" * 40
MID = "b" * 40
OFF_MAIN = "d" * 40
BAD_VALUES = ["pnc-api; rm -rf ~", "--help", "../x", "", "pnc-api ", "PNC-API", "pnc-api\n", "$(id)", None, 1]
REQUEST = "0123456789abcdef"
UNIT = f"pnc-bot-deploy-1700000000-{REQUEST}"
# The runner's limits (SERVER.md §5.3, build-site.yml timeout-minutes: 30).
LIMITS = [
    "--property=NoNewPrivileges=yes",
    "--property=RestrictSUIDSGID=yes",
    "--property=MemoryMax=4G",
    "--property=RuntimeMaxSec=1800",
]
HEAD_ARGV = [
    "systemd-run",
    "--user",
    "--collect",
    "--quiet",
    f"--unit={UNIT}",
    "--description=pnc-bot deploy",
    *LIMITS,
    "--setenv=DEPLOY_TRIGGER=pnc-bot",
    f"--setenv=DEPLOY_REQUEST_ID={REQUEST}",
]


def record(release, result, sha=None, mode="deploy"):
    return {"id": "x", "mode": mode, "sha": sha, "release": release, "result": result, "started_at": "",
            "finished_at": "", "log": "", "error": "", "trigger": "github-actions", "request": ""}


class OpsTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.blog = make_blog(
            self.root,
            [f"20261120-100000-{OLD[:12]}", f"20261121-100000-{MID[:12]}"],
            current=f"20261121-100000-{MID[:12]}",
        )
        self.config = make_config(self.root)
        self.history = DeployHistory(self.blog)
        self.repo = FakeGitRepo(
            HEAD,
            known=[OLD, MID, OFF_MAIN],
            ancestors=[(OLD, HEAD), (MID, HEAD), (OLD, MID)],
            subjects={HEAD: "note: head", OLD: "old <subject>", MID: "mid"},
        )
        self.runner = FakeRunner()
        self.http = []
        self.ops = Ops(
            self.config,
            self.history,
            self.repo,
            runner=self.runner,
            http_get=self.fake_http,
            clock=lambda: 1700000000,
            sleep=lambda s: None,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def fake_http(self, url, **kw):
        self.http.append(url)
        if "alerts" in url:
            body = json.dumps(
                [
                    {"labels": {"alertname": "Watchdog", "severity": "none"}, "annotations": {}},
                    {"labels": {"alertname": "DiskFull", "severity": "critical"}, "annotations": {"summary": "disk <full> & sad"}},
                ]
            ).encode()
            return HttpResult(200, 3, body)
        return HttpResult(200, 12)


class LogsTest(OpsTestBase):
    def test_journal_argv_for_every_unit_source(self):
        for name, (scope, unit) in ops.LOG_SOURCES.items():
            if scope == "file":
                continue
            self.runner.calls.clear()
            self.ops.logs(name)
            expected = ["journalctl"] + (["--user"] if scope == "user" else []) + [
                "-u", unit, "-n", "40", "--no-pager", "-o", "short-iso"
            ]
            self.assertEqual(self.runner.calls, [expected], name)

    def test_exact_known_argv(self):
        self.ops.logs("pnc-api")
        self.ops.logs("cloudflared")
        self.assertEqual(
            self.runner.calls,
            [
                ["journalctl", "--user", "-u", "pnc-api.service", "-n", "40", "--no-pager", "-o", "short-iso"],
                ["journalctl", "-u", "cloudflared.service", "-n", "40", "--no-pager", "-o", "short-iso"],
            ],
        )

    def test_rejects_bad_names_without_running_anything(self):
        for bad in BAD_VALUES:
            with self.assertRaises(OpsError, msg=repr(bad)):
                self.ops.logs(bad)
        self.assertEqual(self.runner.calls, [])

    def test_output_redacted_and_escaped(self):
        self.runner.rules = [(["journalctl"], (0, f"token {TOKEN}\n<script>&\nghp_{'a' * 30}\n", ""))]
        html = self.ops.logs("pnc-bot")
        self.assertNotIn(TOKEN, html)
        self.assertNotIn("ghp_aaaa", html)
        self.assertIn("[REDACTED]", html)
        self.assertIn("&lt;script&gt;&amp;", html)
        self.assertTrue(html.rstrip().endswith("</pre>"))

    def test_long_output_trimmed_to_tail(self):
        lines = "\n".join(f"line {i} " + "x" * 80 for i in range(400))
        self.runner.rules = [(["journalctl"], (0, lines, ""))]
        html = self.ops.logs("pnc-api")
        self.assertLess(len(html), 4000)
        self.assertIn("line 399", html)

    def test_escaped_log_keeps_newest_lines_within_telegram_limit(self):
        # Caddy's journal lines are JSON: escaping turns every " into &quot;.
        lines = "\n".join(
            f'2026-11-21T10:{i:02d}:00+0800 caddy[1]: {{"level":"info","msg":"handled request","n":{i},'
            f'"request":{{"uri":"/notes/{i}.html","method":"GET","headers":{{"User-Agent":["x"]}}}}}}'
            for i in range(40)
        )
        self.runner.rules = [(["journalctl"], (0, lines, ""))]
        html = self.ops.logs("pnc-blog")
        self.assertEqual(truncate_html(html), html)  # nothing is cut off at the end
        self.assertIn("&quot;n&quot;:39,", html)  # the newest line is there
        self.assertTrue(html.endswith("&quot;]}}}</pre>"))
        pre = html.split("<pre>", 1)[1]
        self.assertLessEqual(utf16_len(pre), PRE_LIMIT + len("</pre>"))

    def test_trim_counts_utf16_like_telegram(self):
        lines = "\n".join(f"{i} " + "😀" * 60 for i in range(60))
        self.runner.rules = [(["journalctl"], (0, lines, ""))]
        html = self.ops.logs("pnc-api")
        self.assertEqual(truncate_html(html), html)
        self.assertIn("59 😀", html)

    def test_deploy_log_from_file(self):
        (self.blog / "logs" / "deploy-20261121-100000-aaaaaaaaaaaa.log").write_text("old\n")
        (self.blog / "logs" / "deploy-20261121-110000-rollback.log").write_text("newest run\n")
        html = self.ops.logs("deploy")
        self.assertIn("newest run", html)
        self.assertEqual(self.runner.calls, [])


class RestartTest(OpsTestBase):
    def test_argv_for_every_restartable_unit(self):
        for name, unit in ops.RESTARTABLE.items():
            self.runner.calls.clear()
            html = self.ops.restart(name)
            self.assertEqual(
                self.runner.calls,
                [["systemctl", "--user", "restart", unit], ["systemctl", "--user", "is-active", unit]],
            )
            self.assertIn("✅", html)
        self.assertIn("http://127.0.0.1:46213/", self.http)
        self.assertIn("http://127.0.0.1:46219/healthz", self.http)
        self.assertIn("http://127.0.0.1:46220/reactions/healthz", self.http)

    def test_rejects_bad_names(self):
        for bad in BAD_VALUES + ["pnc-bot", "cloudflared", "runner"]:
            with self.assertRaises(OpsError, msg=repr(bad)):
                self.ops.restart(bad)
        self.assertEqual(self.runner.calls, [])

    def test_failed_restart_reported(self):
        self.runner.rules = [(["systemctl", "--user", "restart"], (1, "", "Job failed <x>"))]
        html = self.ops.restart("pnc-api")
        self.assertIn("❌", html)
        self.assertIn("&lt;x&gt;", html)

    def test_waits_for_health(self):
        clock = [0.0]
        answers = [HttpResult(None, 0, error="ConnectionRefusedError"), HttpResult(502, 1), HttpResult(200, 2)]

        def http(url, **kw):
            return answers.pop(0) if answers else HttpResult(200, 2)

        def sleep(s):
            clock[0] += s

        o = Ops(self.config, self.history, self.repo, runner=self.runner, http_get=http, sleep=sleep, monotonic=lambda: clock[0])
        self.assertIn("200", o.restart("pnc-blog"))
        self.assertEqual(clock[0], 2)

    def test_gives_up_after_wait(self):
        clock = [0.0]

        def sleep(s):
            clock[0] += s

        o = Ops(
            self.config,
            self.history,
            self.repo,
            runner=self.runner,
            http_get=lambda url, **kw: HttpResult(503, 1),
            sleep=sleep,
            monotonic=lambda: clock[0],
        )
        html = o.restart("pnc-api")
        self.assertIn("⚠️", html)
        self.assertLessEqual(clock[0], 21)


class StartDeployTest(OpsTestBase):
    def test_deploy_argv(self):
        plan = DeployPlan("deploy", OLD, None, "s", None)
        unit = self.ops.start_deploy(plan, REQUEST)
        self.assertEqual(unit, UNIT)
        self.assertEqual(self.runner.calls, [HEAD_ARGV + [f"--setenv=DEPLOY_SHA={OLD}", DEPLOY_SH]])

    def test_no_argument_deploy_is_not_pinned_to_the_planned_sha(self):
        # deploy.sh resolves origin/main itself after taking the deploy lock,
        # so a note pushed while the confirmation was open is not undone.
        plan = DeployPlan("deploy", HEAD, None, "s", None, redeploy=True)
        self.ops.start_deploy(plan, REQUEST)
        self.assertEqual(self.runner.calls, [HEAD_ARGV + [DEPLOY_SH]])
        self.assertFalse(any("DEPLOY_SHA" in arg for arg in self.runner.calls[0]))

    def test_runner_limits_on_every_deploy_unit(self):
        release = f"20261120-100000-{OLD[:12]}"
        write_records(self.blog, record(release, "success", OLD))
        for plan in (
            DeployPlan("deploy", OLD, None, "s", None),
            DeployPlan("deploy", HEAD, None, "s", None, redeploy=True),
            DeployPlan("rollback", OLD, release, "s", None),
        ):
            self.runner.calls.clear()
            self.ops.start_deploy(plan, REQUEST)
            argv = self.runner.calls[0]
            self.assertEqual(argv[6:10], LIMITS, plan)
            self.assertLess(argv.index(LIMITS[-1]), argv.index(DEPLOY_SH))

    def test_unit_names_are_unique_within_a_second(self):
        plan = DeployPlan("deploy", OLD, None, "s", None)
        first = self.ops.start_deploy(plan, "0123456789abcdef")
        second = self.ops.start_deploy(plan, "fedcba9876543210")
        self.assertNotEqual(first, second)
        self.assertEqual(second, "pnc-bot-deploy-1700000000-fedcba9876543210")
        self.assertEqual(self.runner.calls[1][4], f"--unit={second}")

    def test_unit_name_chosen_before_start(self):
        unit = self.ops.deploy_unit(REQUEST)
        self.assertEqual(unit, UNIT)
        self.assertEqual(self.ops.start_deploy(DeployPlan("deploy", OLD, None, "s", None), REQUEST, unit=unit), unit)
        self.assertEqual(self.runner.calls[0][4], f"--unit={unit}")
        for bad in ("pnc-api.service", "pnc-bot-deploy-1-fedcba9876543210", "pnc-bot-deploy-1; id", "--help"):
            with self.assertRaises(ValueError, msg=bad):
                self.ops.start_deploy(DeployPlan("deploy", OLD, None, "s", None), REQUEST, unit=bad)
        for bad in ("x; id", "", None):
            with self.assertRaises(ValueError):
                self.ops.deploy_unit(bad)
        self.assertEqual(len(self.runner.calls), 1)

    def test_rollback_argv(self):
        release = f"20261120-100000-{OLD[:12]}"
        write_records(self.blog, record(release, "success", OLD))
        self.ops.start_deploy(DeployPlan("rollback", OLD, release, "s", None), REQUEST)
        self.assertEqual(self.runner.calls, [HEAD_ARGV + [DEPLOY_SH, "--rollback", release]])

    def test_rollback_that_went_bad_after_the_plan_deploys_the_commit(self):
        release = f"20261120-100000-{OLD[:12]}"
        write_records(self.blog, record(release, "success", OLD), record(release, "failure", OLD, "rollback"))
        self.ops.start_deploy(DeployPlan("rollback", OLD, release, "s", None), REQUEST)
        self.assertEqual(self.runner.calls, [HEAD_ARGV + [f"--setenv=DEPLOY_SHA={OLD}", DEPLOY_SH]])

    def test_rollback_to_a_pruned_release_deploys_the_commit(self):
        release = f"20261120-100000-{OLD[:12]}"
        write_records(self.blog, record(release, "success", OLD))
        os.rmdir(self.blog / "releases" / release)
        self.ops.start_deploy(DeployPlan("rollback", OLD, release, "s", None), REQUEST)
        self.assertEqual(self.runner.calls, [HEAD_ARGV + [f"--setenv=DEPLOY_SHA={OLD}", DEPLOY_SH]])

    def test_invalid_plans_never_run(self):
        bad = [
            (DeployPlan("deploy", "abc; rm -rf ~", None, "", None), "0123456789abcdef"),
            (DeployPlan("deploy", HEAD[:12], None, "", None), "0123456789abcdef"),
            (DeployPlan("rollback", OLD, "../../etc", "", None), "0123456789abcdef"),
            (DeployPlan("rollback", OLD, "--help", "", None), "0123456789abcdef"),
            (DeployPlan("rollback", OLD, None, "", None), "0123456789abcdef"),
            (DeployPlan("other", OLD, None, "", None), "0123456789abcdef"),
            (DeployPlan("deploy", HEAD, None, "", None), "x; id"),
            (DeployPlan("deploy", HEAD, None, "", None), ""),
        ]
        for plan, request_id in bad:
            with self.assertRaises(ValueError):
                self.ops.start_deploy(plan, request_id)
        self.assertEqual(self.runner.calls, [])

    def test_systemd_run_failure(self):
        self.runner.rules = [(["systemd-run"], (1, "", f"Failed to start {TOKEN}"))]
        with self.assertRaises(OpsError) as ctx:
            self.ops.start_deploy(DeployPlan("deploy", HEAD, None, "", None), "0123456789abcdef")
        self.assertNotIn(TOKEN, str(ctx.exception))

    def test_unit_helpers(self):
        self.runner.rules = [(["systemctl", "--user", "is-active"], (3, "inactive\n", ""))]
        self.assertTrue(self.ops.unit_finished(UNIT))
        self.ops.unit_journal(UNIT)
        self.assertEqual(
            self.runner.calls,
            [
                ["systemctl", "--user", "is-active", UNIT],
                ["journalctl", "--user", "-u", UNIT, "-n", "15", "--no-pager", "-o", "short-iso"],
            ],
        )
        for bad in ("pnc-api.service", "pnc-bot-deploy-1; id", "--help", f"{UNIT}; id", "pnc-bot-deploy-1-XYZ", None):
            with self.assertRaises(ValueError):
                self.ops.unit_finished(bad)
            with self.assertRaises(ValueError):
                self.ops.unit_journal(bad)

    def test_still_running(self):
        self.runner.rules = [(["systemctl", "--user", "is-active"], (0, "active\n", ""))]
        self.assertFalse(self.ops.unit_finished("pnc-bot-deploy-1"))
        self.assertFalse(self.ops.unit_finished(UNIT))

    def test_only_a_stopped_unit_counts_as_finished(self):
        answers = {
            (3, "inactive\n", ""): True,
            (3, "failed\n", ""): True,
            (0, "active\n", ""): False,
            (3, "activating\n", ""): False,
            (3, "deactivating\n", ""): False,
            (0, "reloading\n", ""): False,
            (124, "", "systemctl timed out after 15s"): False,  # run_command's timeout result
            (1, "", "Failed to connect to bus: No medium found"): False,
            (127, "", "systemctl: No such file or directory"): False,
            (0, "", ""): False,
            (4, "unknown\n", ""): False,
        }
        for answer, finished in answers.items():
            self.runner.rules = [(["systemctl", "--user", "is-active"], answer)]
            self.assertIs(self.ops.unit_finished(UNIT), finished, answer)


class PlanDeployTest(OpsTestBase):
    def test_no_arg_deploys_head(self):
        plan = plan_deploy(None, self.repo, self.history)
        self.assertEqual((plan.mode, plan.sha, plan.release, plan.redeploy), ("deploy", HEAD, None, True))
        self.assertEqual(self.repo.fetches, 1)
        self.assertEqual(plan.serving, f"20261121-100000-{MID[:12]}")

    def test_no_arg_redeploys_when_head_is_serving(self):
        make_blog(self.root, [f"20261121-120000-{HEAD[:12]}"], current=f"20261121-120000-{HEAD[:12]}")
        plan = plan_deploy(None, self.repo, self.history)
        self.assertEqual((plan.mode, plan.redeploy), ("deploy", True))
        self.assertIn("已在运行", plan.describe())

    def test_no_arg_never_plans_a_pinned_rollback(self):
        # HEAD's release is kept (even known good) but not served: a no-argument
        # /deploy still lets deploy.sh pick main when it runs.
        release = f"20261121-120000-{HEAD[:12]}"
        make_blog(self.root, [release], current=f"20261121-100000-{MID[:12]}")
        write_records(self.blog, record(release, "success", HEAD))
        plan = plan_deploy(None, self.repo, self.history)
        self.assertEqual((plan.mode, plan.release, plan.redeploy), ("deploy", None, True))
        text = plan.describe()
        self.assertIn("以真正开始部署时的 main 为准", text)
        self.assertIn(HEAD[:12], text)
        self.assertNotIn("⏪", text)

    def test_rollback_to_kept_release(self):
        release = f"20261120-100000-{OLD[:12]}"
        write_records(self.blog, record(release, "success", OLD))
        plan = plan_deploy(OLD[:7], self.repo, self.history)
        self.assertEqual((plan.mode, plan.sha, plan.release), ("rollback", OLD, release))
        self.assertIn("&lt;subject&gt;", plan.describe())

    def test_never_rolls_back_onto_a_release_that_failed_its_check(self):
        # A deploy of OLD failed deploy.sh's health check; deploy.sh switched
        # back but kept the release directory (prune runs only on success).
        release = f"20261120-100000-{OLD[:12]}"
        write_records(self.blog, record(release, "success", OLD), record(release, "failure", OLD))
        plan = plan_deploy(OLD[:7], self.repo, self.history)
        self.assertEqual((plan.mode, plan.sha, plan.release, plan.reuse), ("deploy", OLD, None, release))
        self.assertNotIn("⏪", plan.describe())
        self.assertIn("自动切回", plan.describe())
        self.ops.start_deploy(plan, REQUEST)
        self.assertEqual(self.runner.calls, [HEAD_ARGV + [f"--setenv=DEPLOY_SHA={OLD}", DEPLOY_SH]])

    def test_kept_release_without_a_record_is_deployed_not_rolled_back(self):
        plan = plan_deploy(OLD[:7], self.repo, self.history)
        self.assertEqual((plan.mode, plan.release, plan.reuse), ("deploy", None, f"20261120-100000-{OLD[:12]}"))

    def test_release_that_recovered_can_be_rolled_back_to(self):
        release = f"20261120-100000-{OLD[:12]}"
        write_records(self.blog, record(release, "failure", OLD), record(release, "success", OLD, "rollback"))
        self.assertEqual(plan_deploy(OLD, self.repo, self.history).mode, "rollback")

    def test_newest_kept_release_wins(self):
        newest = f"20261122-100000-{OLD[:12]}"
        make_blog(self.root, [newest], current=None)
        self.assertEqual(plan_deploy(OLD, self.repo, self.history).reuse, newest)
        write_records(self.blog, record(f"20261120-100000-{OLD[:12]}", "success", OLD), record(newest, "success", OLD))
        plan = plan_deploy(OLD, self.repo, self.history)
        self.assertEqual((plan.mode, plan.release), ("rollback", newest))

    def test_full_deploy_of_older_commit_without_release(self):
        other = "e" * 40
        self.repo.known.add(other)
        self.repo.ancestors.add((other, HEAD))
        plan = plan_deploy(other, self.repo, self.history)
        self.assertEqual((plan.mode, plan.sha, plan.release, plan.redeploy), ("deploy", other, None, False))

    def test_refuses_sha_being_served(self):
        with self.assertRaises(PlanError):
            plan_deploy(MID[:10], self.repo, self.history)

    def test_refuses_non_ancestor(self):
        with self.assertRaises(PlanError) as ctx:
            plan_deploy(OFF_MAIN, self.repo, self.history)
        self.assertIn("不在 main", str(ctx.exception))

    def test_refuses_unknown_sha(self):
        with self.assertRaises(PlanError):
            plan_deploy("f" * 12, self.repo, self.history)

    def test_rejects_bad_sha_before_touching_git(self):
        for bad in ["abc", "ABCDEF1", "abcdefg", "zzzzzzz", "--help", "../x", "pnc-api; rm -rf ~", "a" * 41, "", 7]:
            with self.assertRaises(PlanError, msg=repr(bad)):
                plan_deploy(bad, self.repo, self.history)
        self.assertEqual(self.repo.fetches, 0)


class StatusTest(OpsTestBase):
    def test_report(self):
        (self.blog / "state" / "last-deploy.json").write_text(
            json.dumps({"result": "failure", "sha": MID, "mode": "deploy", "error": "npm <boom>", "trigger": "pnc-bot",
                        "finished_at": "2026-11-21T02:00:00Z"})
        )
        self.runner.rules = [(["systemctl", "is-active", "cloudflared.service"], (3, "inactive\n", ""))]
        html = self.ops.status_report(queue_size=2, uptime=3700)
        self.assertIn(f"20261121-100000-{MID[:12]}", html)
        self.assertIn("mid", html)
        self.assertIn("🔴 cloudflared：inactive", html)
        self.assertIn("🟢 pnc-api：active", html)
        self.assertIn("DiskFull", html)
        self.assertNotIn("Watchdog", html)
        self.assertIn("disk &lt;full&gt; &amp; sad", html)
        self.assertIn("npm &lt;boom&gt;", html)
        self.assertIn("2026-11-21 10:00", html)
        self.assertIn("队列 2", html)
        self.assertIn("1 小时 1 分", html)
        units = [c for c in self.runner.calls if c[0] == "systemctl"]
        self.assertIn(["systemctl", "--user", "is-active", "pnc-github-actions-runner.service"], units)
        self.assertIn(["systemctl", "is-active", "cloudflared.service"], units)
        self.assertTrue(any("api/v2/alerts?active=true&silenced=false&inhibited=false" in u for u in self.http))

    def test_alertmanager_down(self):
        o = Ops(self.config, self.history, self.repo, runner=self.runner, http_get=lambda url, **kw: HttpResult(None, 0, error="URLError"))
        html = o.status_report()
        self.assertIn("无法读取 Alertmanager", html)


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "https://example.com/")
        else:
            self.send_response(200 if self.path == "/" else 404)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


class LocalGetTest(unittest.TestCase):
    def test_local_opener_never_proxies(self):
        for handler in ops._LOCAL_OPENER.handlers:
            self.assertFalse(getattr(handler, "proxies", None), handler)
            self.assertFalse(hasattr(handler, "proxy_open"), handler)

    def test_status_codes_and_no_redirects(self):
        server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with mock.patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9"}):
                self.assertEqual(ops.local_get(base + "/").status, 200)
                self.assertEqual(ops.local_get(base + "/missing").status, 404)
                self.assertEqual(ops.local_get(base + "/redirect").status, 302)
        finally:
            server.shutdown()
            server.server_close()

    def test_unreachable(self):
        result = ops.local_get("http://127.0.0.1:9/", timeout=1)
        self.assertIsNone(result.status)
        self.assertTrue(result.error)


if __name__ == "__main__":
    unittest.main()
