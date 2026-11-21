import tempfile
import unittest
from pathlib import Path

from pnc_bot.config import ConfigError, load_config

from tests.runtime_fakes import TOKEN

EXAMPLE = Path(__file__).resolve().parent.parent / "config.env.example"


def parse_env_file(path: Path) -> dict[str, str]:
    """KEY=value lines, the way systemd's EnvironmentFile= reads this file."""
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


class ExampleConfigTest(unittest.TestCase):
    def test_unedited_example_does_not_load(self):
        # A copied but unedited example must fail closed, not hand the bot to
        # whoever owns the example's user id.
        env = parse_env_file(EXAMPLE)
        self.assertIn("PNC_BOT_ALLOWED_USER_ID", env)
        with self.assertRaises(ConfigError) as ctx:
            load_config(env, read_token_file=False)
        self.assertIn("PNC_BOT_ALLOWED_USER_ID", str(ctx.exception))

    def test_example_loads_once_the_owner_is_set(self):
        env = parse_env_file(EXAMPLE)
        env["PNC_BOT_ALLOWED_USER_ID"] = "424242"
        config = load_config(env, read_token_file=False)
        self.assertEqual(config.allowed_user_id, 424242)
        self.assertEqual(str(config.blog_root), "/home/lyy/services/pnc-blog")

    def test_example_has_no_secrets(self):
        text = EXAMPLE.read_text(encoding="utf-8")
        self.assertNotRegex(text, r"\d{6,12}:[A-Za-z0-9_-]{30,}")
        self.assertNotIn("ghp_", text)


class UserIdTest(unittest.TestCase):
    def test_old_placeholder_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config({"PNC_BOT_ALLOWED_USER_ID": "123456789"}, read_token_file=False)
        self.assertIn("example", str(ctx.exception))

    def test_invalid_values(self):
        for bad in ("", "  ", "0", "-5", "12a", "１２３", "1e9", "0x10"):
            with self.assertRaises(ConfigError, msg=repr(bad)):
                load_config({"PNC_BOT_ALLOWED_USER_ID": bad}, read_token_file=False)

    def test_valid_value_and_summary_hides_most_digits(self):
        config = load_config({"PNC_BOT_ALLOWED_USER_ID": " 5550001234 "}, read_token_file=False)
        self.assertEqual(config.allowed_user_id, 5550001234)
        summary = dict(config.summary())
        self.assertEqual(summary["allowed user"], "…234 (10 digits)")
        self.assertEqual(summary["token"], "not read")

    def test_token_never_in_repr_or_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token"
            token_file.write_text(TOKEN + "\n")
            config = load_config(
                {"PNC_BOT_ALLOWED_USER_ID": "424242", "PNC_BOT_TOKEN_FILE": str(token_file)}
            )
            self.assertEqual(config.token, TOKEN)
            self.assertNotIn(TOKEN, repr(config))
            token_file.write_text("not-a-token " + TOKEN[:20])
            with self.assertRaises(ConfigError) as ctx:
                load_config({"PNC_BOT_ALLOWED_USER_ID": "424242", "PNC_BOT_TOKEN_FILE": str(token_file)})
            self.assertNotIn(TOKEN[:20], str(ctx.exception))


if __name__ == "__main__":
    unittest.main()


class ProxyConfigTest(unittest.TestCase):
    def load(self, proxy):
        return load_config({"PNC_BOT_ALLOWED_USER_ID": "5550001234", "PNC_BOT_PROXY": proxy}, read_token_file=False)

    def test_default_is_naiveproxy_socks(self):
        config = load_config({"PNC_BOT_ALLOWED_USER_ID": "5550001234"}, read_token_file=False)
        self.assertEqual(config.proxy, "socks5h://127.0.0.1:10801")

    def test_socks_and_http_proxies_accepted(self):
        for value in ("socks5h://127.0.0.1:10801", "socks5://127.0.0.1:1080", "http://127.0.0.1:7890"):
            self.assertEqual(self.load(value).proxy, value)

    def test_bad_socks_urls_refused(self):
        for value in ("socks5h://127.0.0.1", "socks5h://u:p@127.0.0.1:1080", "socks5h://127.0.0.1:1080/x", "socks4://127.0.0.1:1080", "socks5h://127.0.0.1:99999"):
            with self.subTest(value), self.assertRaises(ConfigError):
                self.load(value)
