"""Configuration from environment variables (see config.env.example).

Everything is validated up front so a bad config stops the service at start
instead of failing later. The bot token is read from a file and never shown
by ``repr``.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,}$")

DEFAULTS = {
    "PNC_BOT_TOKEN_FILE": "/home/lyy/observability/secrets/telegram_bot_token",
    "PNC_BOT_PROXY": "http://127.0.0.1:7890",
    "PNC_BOT_REPO": "/home/lyy/services/pnc-bot/repo",
    "PNC_BOT_STATE_DIR": "/home/lyy/.local/state/pnc-bot",
    "PNC_BOT_BLOG_ROOT": "/home/lyy/services/pnc-blog",
    "PNC_BOT_COMMENTS_DB": "/home/lyy/services/pnc-api/data/pnc.db",
    "PNC_BOT_SITE_URL": "https://parityncsvt.top",
    "PNC_BOT_LOCAL_SITE": "http://127.0.0.1:46213",
    "PNC_BOT_ALERTMANAGER": "http://127.0.0.1:19093",
}


class ConfigError(Exception):
    """The configuration is invalid; the message never contains secrets."""


@dataclass(frozen=True)
class Config:
    allowed_user_id: int
    token_file: Path
    proxy: str
    repo: Path
    state_dir: Path
    blog_root: Path
    comments_db: Path
    site_url: str
    local_site: str
    alertmanager: str
    token: str = field(default="", repr=False, compare=False)

    def summary(self) -> list[tuple[str, str]]:
        """Non-secret key/value pairs for ``--check-config``."""
        uid = str(self.allowed_user_id)
        return [
            ("allowed user", f"…{uid[-3:]} ({len(uid)} digits)"),
            ("token file", str(self.token_file)),
            ("token", "loaded" if self.token else "not read"),
            ("proxy", mask_url(self.proxy)),
            ("repo", str(self.repo)),
            ("state dir", str(self.state_dir)),
            ("blog root", str(self.blog_root)),
            ("comments db", str(self.comments_db)),
            ("site url", self.site_url),
            ("local site", self.local_site),
            ("alertmanager", self.alertmanager),
        ]


def mask_url(url: str) -> str:
    """Hide credentials embedded in a URL (``http://user:pass@host``)."""
    parts = urlsplit(url)
    if parts.username or parts.password:
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return urlunsplit((parts.scheme, f"***@{host}", parts.path, parts.query, parts.fragment))
    return url


def _get(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if value is None or not value.strip():
        value = DEFAULTS[name]
    return value.strip()


def _path(env: Mapping[str, str], name: str) -> Path:
    value = _get(env, name)
    path = Path(value)
    if not path.is_absolute():
        raise ConfigError(f"{name} must be an absolute path")
    return path


def _url(env: Mapping[str, str], name: str) -> str:
    value = _get(env, name).rstrip("/")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(f"{name} must be an http(s) URL")
    if parts.query or parts.fragment:
        raise ConfigError(f"{name} must not contain a query or fragment")
    return value


# The example id older copies of config.env.example shipped: a copied config
# that was never edited must not hand the bot to that (real) account.
PLACEHOLDER_USER_IDS = frozenset({123456789})


def _user_id(env: Mapping[str, str]) -> int:
    raw = (env.get("PNC_BOT_ALLOWED_USER_ID") or "").strip()
    if not raw:
        raise ConfigError("PNC_BOT_ALLOWED_USER_ID is required")
    if not raw.isascii() or not raw.isdigit():
        raise ConfigError("PNC_BOT_ALLOWED_USER_ID must be a positive integer")
    value = int(raw)
    if value <= 0:
        raise ConfigError("PNC_BOT_ALLOWED_USER_ID must be a positive integer")
    if value in PLACEHOLDER_USER_IDS:
        raise ConfigError("PNC_BOT_ALLOWED_USER_ID is still an example value; set your own Telegram user id")
    return value


def read_token(path: Path) -> str:
    """Read and validate the bot token; errors never include the token."""
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"cannot read token file {path}: {exc.strerror or type(exc).__name__}") from None
    except UnicodeDecodeError:
        raise ConfigError(f"token file {path} is not UTF-8 text") from None
    if not TOKEN_RE.fullmatch(token):
        raise ConfigError(f"token file {path} does not contain a bot token")
    return token


def load_config(env: Mapping[str, str] | None = None, *, read_token_file: bool = True) -> Config:
    """Build a Config from ``env`` (default ``os.environ``).

    ``read_token_file=False`` is used by ``--selftest-command``, which must
    never touch the token.
    """
    env = os.environ if env is None else env
    token_file = _path(env, "PNC_BOT_TOKEN_FILE")
    cfg = Config(
        allowed_user_id=_user_id(env),
        token_file=token_file,
        proxy=_url(env, "PNC_BOT_PROXY"),
        repo=_path(env, "PNC_BOT_REPO"),
        state_dir=_path(env, "PNC_BOT_STATE_DIR"),
        blog_root=_path(env, "PNC_BOT_BLOG_ROOT"),
        comments_db=_path(env, "PNC_BOT_COMMENTS_DB"),
        site_url=_url(env, "PNC_BOT_SITE_URL"),
        local_site=_url(env, "PNC_BOT_LOCAL_SITE"),
        alertmanager=_url(env, "PNC_BOT_ALERTMANAGER"),
        token=read_token(token_file) if read_token_file else "",
    )
    return cfg


def check_paths(cfg: Config, deploy_sh: str) -> list[tuple[bool, str, str]]:
    """Check that every path the bot uses exists and is usable.

    Returns ``(ok, name, detail)`` rows; details never contain secrets.
    """
    rows: list[tuple[bool, str, str]] = []

    try:
        mode = stat.S_IMODE(cfg.token_file.stat().st_mode)
        detail = f"mode {mode:04o}"
        if mode & 0o077:
            detail += " (warning: readable by group/others)"
        rows.append((True, "token file", detail))
    except OSError as exc:
        rows.append((False, "token file", exc.strerror or type(exc).__name__))

    is_repo = (cfg.repo / ".git").exists()
    rows.append((is_repo, "repo", "git work tree" if is_repo else "not a git work tree"))

    state_ok = cfg.state_dir.is_dir() and os.access(cfg.state_dir, os.W_OK)
    if not cfg.state_dir.exists():
        parent = cfg.state_dir.parent
        state_ok = parent.is_dir() and os.access(parent, os.W_OK)
        rows.append((state_ok, "state dir", "will be created" if state_ok else "parent not writable"))
    else:
        rows.append((state_ok, "state dir", "writable" if state_ok else "not writable"))

    current = cfg.blog_root / "current"
    if current.is_symlink():
        rows.append((True, "blog root", f"current -> {os.path.basename(os.readlink(current))}"))
    else:
        rows.append((False, "blog root", "no current symlink"))

    db_ok = cfg.comments_db.is_file() and os.access(cfg.comments_db, os.R_OK)
    rows.append((db_ok, "comments db", "readable" if db_ok else "missing or unreadable"))

    sh_ok = os.path.isfile(deploy_sh) and os.access(deploy_sh, os.X_OK)
    rows.append((sh_ok, "deploy.sh", "executable" if sh_ok else "missing or not executable"))
    return rows
