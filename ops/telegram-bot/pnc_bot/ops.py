"""/status, /logs, /deploy and /restart.

Every subprocess is a fixed argv list made of the constants below plus values
that were validated against them (dictionary keys, git-resolved SHAs, release
names from the directory listing). Nothing here ever uses a shell.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
import urllib.error
import urllib.request
import http.client
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from .config import Config
from .deploys import RELEASE_RE, DeployHistory, record_summary
from .gitrepo import GitError
from .util import duration_text, esc, redact, shorten, strip_ansi, tail_text, utf16_len

log = logging.getLogger("pnc_bot.ops")

LOG_SOURCES: dict[str, tuple[str, str | None]] = {
    "pnc-blog": ("user", "pnc-blog.service"),
    "pnc-api": ("user", "pnc-api.service"),
    "pnc-reactions": ("user", "pnc-reactions.service"),
    "pnc-bot": ("user", "pnc-bot.service"),
    "runner": ("user", "pnc-github-actions-runner.service"),
    "cloudflared": ("system", "cloudflared.service"),
    "deploy": ("file", None),
}
RESTARTABLE = {
    "pnc-blog": "pnc-blog.service",
    "pnc-api": "pnc-api.service",
    "pnc-reactions": "pnc-reactions.service",
}
HEALTH = {
    "pnc-blog": "<local_site>/",
    "pnc-api": "http://127.0.0.1:46219/healthz",
    "pnc-reactions": "http://127.0.0.1:46220/reactions/healthz",
}
STATUS_UNITS = [
    ("user", "pnc-blog.service"),
    ("user", "pnc-api.service"),
    ("user", "pnc-reactions.service"),
    ("user", "pnc-bot.service"),
    ("user", "pnc-github-actions-runner.service"),
    ("system", "cloudflared.service"),
]
DEPLOY_SH = "/home/lyy/services/pnc-blog/deploy.sh"
# A bot-started deploy runs the same deploy.sh (npm ci with package lifecycle
# scripts, then the build) as the Actions runner, so its transient unit gets
# the runner's limits: no privilege escalation, no setuid/setgid files, 4G of
# memory, and the job's 30-minute timeout (systemd then sends SIGTERM, which
# deploy.sh records as a failure). Nothing here limits the network or the
# file system: deploy.sh fetches from GitHub, runs ~/.local/bin/npm and
# writes under ~/services/pnc-blog and the node-exporter textfile dir.
DEPLOY_UNIT_MAX_SEC = 1800
DEPLOY_UNIT_PROPERTIES = (
    "--property=NoNewPrivileges=yes",
    "--property=RestrictSUIDSGID=yes",
    "--property=MemoryMax=4G",
    f"--property=RuntimeMaxSec={DEPLOY_UNIT_MAX_SEC}",
)

SHA_ARG_RE = re.compile(r"^[0-9a-f]{7,40}$")
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
REQUEST_ID_RE = re.compile(r"^[0-9a-f]{8,32}$")
# pnc-bot-deploy-<unix time>-<request id>; the id makes it unique even for two
# deploys started in the same second.
DEPLOY_UNIT_RE = re.compile(r"^pnc-bot-deploy-[0-9]{1,20}(-[0-9a-f]{8,32})?$")
LOG_LINES = 40
PRE_LIMIT = 3500

Runner = Callable[[Sequence[str], float], subprocess.CompletedProcess]


class OpsError(Exception):
    """A user-facing failure (message is short Chinese text)."""


def run_command(argv: Sequence[str], timeout: float = 30) -> subprocess.CompletedProcess:
    """Default runner: fixed argv, no shell, captured output, timeout."""
    argv = list(argv)
    if not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError("argv must be a non-empty list of strings")
    try:
        return subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"{argv[0]} timed out after {timeout:.0f}s")
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 127, "", f"{argv[0]}: {exc.strerror or type(exc).__name__}")


# --- local HTTP (never through the proxy) ---------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


@dataclass(frozen=True)
class HttpResult:
    status: int | None
    ms: int
    body: bytes = b""
    error: str | None = None


def local_get(url: str, *, timeout: float = 5, max_bytes: int = 1024 * 1024) -> HttpResult:
    """GET a local URL directly (no proxy, no redirects)."""
    start = time.monotonic()
    error = None
    status: int | None = None
    body = b""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "pnc-bot"})
        with _LOCAL_OPENER.open(request, timeout=timeout) as resp:
            status = resp.status
            body = resp.read(max_bytes)
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", None)
        error = type(reason).__name__ if isinstance(reason, BaseException) else type(exc).__name__
    return HttpResult(status, int((time.monotonic() - start) * 1000), body, error)


def fit_tail(text: str, limit: int) -> str:
    """``tail_text`` measured in UTF-16 units, the way Telegram counts."""
    budget = limit
    while True:
        out = tail_text(text, budget)
        over = utf16_len(out) - limit
        if over <= 0 or budget <= 1:
            return out
        budget = max(1, budget - over)


# --- deploy planning --------------------------------------------------------


@dataclass(frozen=True)
class DeployPlan:
    mode: str  # "deploy" or "rollback"
    sha: str  # full sha (no-argument plan: what origin/main was when planned)
    release: str | None  # kept release to switch to (rollback mode)
    subject: str
    serving: str | None  # release name being served
    # No argument: deploy.sh deploys origin/main as it is when it runs (it
    # resolves main after taking the deploy lock), so a commit pushed between
    # the confirmation and the deploy is never undone. ``sha`` is only shown.
    redeploy: bool = False
    # Deploy mode with a kept release of ``sha`` that is not known to be good
    # (its last recorded deploy or rollback failed, or there is no record):
    # deploy.sh reuses it, checks it and switches back if it is unhealthy.
    reuse: str | None = None

    def describe(self) -> str:
        sha = f"<code>{esc(self.sha[:12])}</code>"
        subject = esc(shorten(self.subject, 80)) if self.subject else ""
        if self.mode == "rollback":
            head = f"⏪ 切换到已保留的版本 <code>{esc(self.release)}</code>（{sha} {subject}），几秒完成"
        elif self.redeploy:
            serving = bool(self.serving) and str(self.serving).endswith("-" + self.sha[:12])
            head = (
                "🚀 部署 main 的最新提交（以真正开始部署时的 main 为准，之后有新提交也会一起上线）\n"
                f"现在 main 是 {sha} {subject}" + ("（已在运行，会重新检查）" if serving else "")
            )
        elif self.reuse:
            head = (
                f"🚀 部署 {sha} {subject}（复用已保留的 <code>{esc(self.reuse)}</code>，"
                "它上次没有通过检查或没有记录；健康检查不通过会自动切回）"
            )
        else:
            head = f"🚀 构建并部署 {sha} {subject}（约 1 分钟）"
        now = f"当前：<code>{esc(self.serving)}</code>" if self.serving else "当前：未知"
        return f"{head}\n{now}"


class PlanError(OpsError):
    pass


def plan_deploy(arg: str | None, repo: Any, history: DeployHistory) -> DeployPlan:
    """Work out what ``/deploy [sha]`` would do (fetches first)."""
    if arg is not None and (not isinstance(arg, str) or not SHA_ARG_RE.fullmatch(arg)):
        raise PlanError("SHA 只能是 7–40 位小写十六进制")
    try:
        repo.fetch()
        head = repo.resolve("origin/main")
    except GitError:
        raise PlanError("无法获取 origin/main") from None
    if arg is None:
        sha = head
    else:
        try:
            sha = repo.resolve(arg)
        except GitError:
            raise PlanError("找不到这个提交") from None
        try:
            on_main = repo.is_ancestor(sha, head)
        except GitError:
            on_main = False
        if not on_main:
            raise PlanError("这个提交不在 main 上，拒绝部署")
    if not isinstance(sha, str) or not FULL_SHA_RE.fullmatch(sha):
        raise PlanError("无法解析提交")
    try:
        subject = repo.subject(sha)
    except GitError:
        subject = ""
    current = history.current_release()
    serving_name = current[0] if current else None
    serving_sha12 = current[1] if current else ""
    is_serving = bool(serving_sha12) and serving_sha12 == sha[:12]
    if arg is None:
        # Never pinned to ``sha``: see DeployPlan.redeploy.
        return DeployPlan("deploy", sha, None, subject, serving_name, redeploy=True)
    if is_serving:
        raise PlanError("现在运行的就是这个版本（要重新部署请直接发 /deploy）")
    kept = [name for name in history.releases() if name.endswith("-" + sha[:12])]
    # An instant switch (deploy.sh --rollback) only onto a release whose last
    # recorded deploy or rollback succeeded. Anything else goes through
    # deploy.sh's deploy path, which reuses the kept release just as fast,
    # checks that the commit is on main and switches back when unhealthy.
    if kept and history.release_ok(kept[-1]):
        return DeployPlan("rollback", sha, kept[-1], subject, serving_name)
    return DeployPlan("deploy", sha, None, subject, serving_name, reuse=kept[-1] if kept else None)


# --- the Ops facade -----------------------------------------------------------


class Ops:
    def __init__(
        self,
        config: Config,
        history: DeployHistory,
        repo: Any,
        *,
        runner: Runner = run_command,
        http_get: Callable[..., HttpResult] = local_get,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.config = config
        self.history = history
        self.repo = repo
        self.run = runner
        self.http_get = http_get
        self.clock = clock
        self.sleep = sleep
        self.monotonic = monotonic

    def health_url(self, name: str) -> str:
        return HEALTH[name].replace("<local_site>", self.config.local_site)

    # --- systemd -----------------------------------------------------

    @staticmethod
    def _systemctl(scope: str) -> list[str]:
        return ["systemctl", "--user"] if scope == "user" else ["systemctl"]

    def unit_state(self, scope: str, unit: str) -> str:
        result = self.run([*self._systemctl(scope), "is-active", unit], 15)
        state = (result.stdout or "").strip().splitlines()
        return state[0] if state else "unknown"

    # --- /status -----------------------------------------------------

    def _alerts(self) -> list[str] | None:
        url = f"{self.config.alertmanager}/api/v2/alerts?active=true&silenced=false&inhibited=false"
        result = self.http_get(url)
        if result.status != 200:
            return None
        try:
            alerts = json.loads(result.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        lines = []
        for alert in alerts if isinstance(alerts, list) else []:
            if not isinstance(alert, dict):
                continue
            labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
            notes = alert.get("annotations") if isinstance(alert.get("annotations"), dict) else {}
            name = str(labels.get("alertname") or "?")
            if name == "Watchdog":
                continue
            severity = str(labels.get("severity") or "")
            summary = str(notes.get("summary") or "")
            text = f"• <b>{esc(name)}</b>"
            if severity:
                text += f" [{esc(severity)}]"
            if summary:
                text += f" {esc(shorten(summary, 120))}"
            lines.append(text)
        return lines

    def status_report(self, *, queue_size: int | None = None, uptime: float | None = None) -> str:
        out = ["<b>📊 状态</b>"]
        current = self.history.current_release()
        if current:
            name, sha12 = current
            subject = ""
            if sha12:
                try:
                    subject = self.repo.subject(sha12)
                except GitError:
                    subject = ""
            line = f"<b>站点</b>：<code>{esc(name)}</code>"
            if subject:
                line += f"\n{esc(shorten(subject, 80))}"
            out.append(line)
        else:
            out.append("<b>站点</b>：未知（没有 current 链接）")

        out.append("\n<b>服务</b>")
        for scope, unit in STATUS_UNITS:
            state = self.unit_state(scope, unit)
            icon = "🟢" if state == "active" else "🔴"
            out.append(f"{icon} {esc(unit.removesuffix('.service'))}：{esc(state)}")

        out.append("\n<b>健康检查</b>")
        for name in HEALTH:
            r = self.http_get(self.health_url(name))
            if r.status is not None:
                icon = "🟢" if r.status == 200 else "🔴"
                out.append(f"{icon} {esc(name)}：{r.status} · {r.ms} ms")
            else:
                out.append(f"🔴 {esc(name)}：{esc(r.error or '无响应')}")

        alerts = self._alerts()
        if alerts is None:
            out.append("\n<b>告警</b>：无法读取 Alertmanager")
        elif alerts:
            out.append(f"\n<b>告警</b>（{len(alerts)}）")
            out.extend(alerts[:10])
        else:
            out.append("\n<b>告警</b>：无")

        last = self.history.last()
        out.append("\n<b>最近部署</b>：" + (record_summary(last) if last else "无记录"))
        if last and last.get("result") == "failure" and isinstance(last.get("error"), str):
            out.append(f"错误：{esc(shorten(redact(last['error']), 200))}")

        if queue_size is not None or uptime is not None:
            parts = []
            if queue_size is not None:
                parts.append(f"队列 {queue_size}")
            if uptime is not None:
                parts.append(f"已运行 {duration_text(uptime)}")
            out.append("\n<b>机器人</b>：" + " · ".join(parts))
        return "\n".join(out)

    # --- /logs -------------------------------------------------------

    @staticmethod
    def log_names() -> list[str]:
        return list(LOG_SOURCES)

    def logs(self, name: str) -> str:
        if not isinstance(name, str) or name not in LOG_SOURCES:
            raise OpsError("没有这个日志源")
        scope, unit = LOG_SOURCES[name]
        if scope == "file":
            path = self.history.latest_log()
            if path is None:
                return f"<b>📜 {esc(name)}</b>\n还没有部署日志。"
            text = self.history.log_tail(path, LOG_LINES)
            title = f"<b>📜 {esc(name)}</b> <code>{esc(path.name)}</code>"
        else:
            assert unit is not None
            argv = ["journalctl"]
            if scope == "user":
                argv.append("--user")
            argv += ["-u", unit, "-n", str(LOG_LINES), "--no-pager", "-o", "short-iso"]
            result = self.run(argv, 20)
            text = result.stdout or ""
            if result.returncode != 0 and result.stderr:
                text = (text + "\n" + result.stderr).strip()
            title = f"<b>📜 {esc(name)}</b>"
        text = redact(strip_ansi(text).rstrip()) or "（没有日志）"
        # Trimmed after escaping: escaping can make the text much longer
        # (Caddy's JSON lines: every " becomes &quot;), and Telegram's message
        # limit would then cut off the newest lines at the end.
        return f"{title}\n<pre>{fit_tail(esc(text), PRE_LIMIT)}</pre>"

    # --- /deploy -----------------------------------------------------

    def deploy_unit(self, request_id: str) -> str:
        """The transient unit name for one deploy request (unique per request).

        Callers that need the name before the deploy starts (to watch it) pass
        it on to ``start_deploy(..., unit=...)``.
        """
        if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
            raise ValueError("invalid request id")
        return f"pnc-bot-deploy-{int(self.clock())}-{request_id}"

    def start_deploy(self, plan: DeployPlan, request_id: str, *, unit: str | None = None) -> str:
        """Start deploy.sh in a transient user unit; returns the unit name."""
        if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
            raise ValueError("invalid request id")
        if not isinstance(plan.sha, str) or not FULL_SHA_RE.fullmatch(plan.sha):
            raise ValueError("invalid sha in plan")
        if unit is None:
            unit = self.deploy_unit(request_id)
        elif not isinstance(unit, str) or not DEPLOY_UNIT_RE.fullmatch(unit) or not unit.endswith("-" + request_id):
            raise ValueError("invalid unit")
        argv = [
            "systemd-run",
            "--user",
            "--collect",
            "--quiet",
            f"--unit={unit}",
            "--description=pnc-bot deploy",
            *DEPLOY_UNIT_PROPERTIES,
            "--setenv=DEPLOY_TRIGGER=pnc-bot",
            f"--setenv=DEPLOY_REQUEST_ID={request_id}",
        ]
        if plan.mode == "rollback":
            if not isinstance(plan.release, str) or not RELEASE_RE.fullmatch(plan.release):
                raise ValueError("invalid release in plan")
            if not self._rollback_still_good(plan.release):
                # Pruned, or it failed a check since the confirmation: deploy
                # the commit instead (reuses or rebuilds, switches back if bad).
                log.info("kept release is gone or no longer known good; deploying its commit instead")
                argv += [f"--setenv=DEPLOY_SHA={plan.sha}", DEPLOY_SH]
            else:
                argv += [DEPLOY_SH, "--rollback", plan.release]
        elif plan.mode == "deploy":
            if not plan.redeploy:
                argv.append(f"--setenv=DEPLOY_SHA={plan.sha}")
            argv.append(DEPLOY_SH)
        else:
            raise ValueError("invalid plan mode")
        result = self.run(argv, 30)
        if result.returncode != 0:
            detail = redact((result.stderr or result.stdout or "").strip())
            raise OpsError(f"systemd-run 失败：{shorten(detail, 300)}")
        return unit

    def _rollback_still_good(self, release: str) -> bool:
        return release in self.history.releases() and self.history.release_ok(release)

    def unit_finished(self, unit: str) -> bool:
        """True only when systemd says the unit is stopped.

        ``systemctl is-active`` answers ``inactive`` (also for a unit that was
        already garbage-collected) or ``failed`` with exit status 3. A timeout,
        an unreachable user bus or any other answer means "don't know", which
        must not count as finished.
        """
        if not isinstance(unit, str) or not DEPLOY_UNIT_RE.fullmatch(unit):
            raise ValueError("invalid unit")
        result = self.run(["systemctl", "--user", "is-active", unit], 15)
        lines = (result.stdout or "").strip().splitlines()
        state = lines[0].strip() if lines else ""
        return result.returncode == 3 and state in ("inactive", "failed")

    def unit_journal(self, unit: str, lines: int = 15) -> str:
        if not isinstance(unit, str) or not DEPLOY_UNIT_RE.fullmatch(unit):
            raise ValueError("invalid unit")
        argv = ["journalctl", "--user", "-u", unit, "-n", str(int(lines)), "--no-pager", "-o", "short-iso"]
        return redact(self.run(argv, 20).stdout or "")

    # --- /restart ----------------------------------------------------

    def restart(self, name: str, *, wait: float = 20) -> str:
        if not isinstance(name, str) or name not in RESTARTABLE:
            raise OpsError("这个服务不能重启")
        unit = RESTARTABLE[name]
        result = self.run(["systemctl", "--user", "restart", unit], 60)
        if result.returncode != 0:
            detail = redact((result.stderr or "").strip())
            return f"❌ 重启 {esc(name)} 失败\n<pre>{esc(shorten(detail, 1000))}</pre>"
        deadline = self.monotonic() + wait
        health = self.http_get(self.health_url(name))
        while health.status != 200 and self.monotonic() < deadline:
            self.sleep(1)
            health = self.http_get(self.health_url(name))
        state = self.unit_state("user", unit)
        ok = state == "active" and health.status == 200
        check = f"{health.status} · {health.ms} ms" if health.status is not None else (health.error or "无响应")
        icon = "✅" if ok else "⚠️"
        return f"{icon} 已重启 <b>{esc(name)}</b>：{esc(state)}，健康检查 {esc(check)}"
