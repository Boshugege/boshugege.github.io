import io
import json
import unittest
import urllib.error

from pnc_bot.telegram import (
    FileTooLarge,
    TelegramClient,
    TelegramError,
    truncate_html,
)
from pnc_bot.util import utf16_len

TOKEN = "123456789:AAH" + "x" * 32


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status = status
        self._body = io.BytesIO(body)
        self.headers = headers or {}

    def read(self, n=-1):
        return self._body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Returns queued responses; an exception in the queue is raised."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def json(self, index=-1):
        return json.loads(self.requests[index][0].data.decode())

    def method(self, index=-1):
        return self.requests[index][0].full_url.rsplit("/", 1)[1]


def ok(result):
    return FakeResponse(200, json.dumps({"ok": True, "result": result}).encode())


def http_error(code, payload):
    body = io.BytesIO(json.dumps(payload).encode())
    return urllib.error.HTTPError(f"https://api.telegram.org/bot{TOKEN}/x", code, "err", {}, body)


def client(*responses, sleeps=None):
    opener = FakeOpener(*responses)
    sleeps = sleeps if sleeps is not None else []
    return TelegramClient(TOKEN, "http://127.0.0.1:7890", opener=opener, sleep=sleeps.append), opener


class CallTest(unittest.TestCase):
    def test_returns_result_and_posts_json(self):
        tg, opener = client(ok({"id": 1}))
        self.assertEqual(tg.call("getMe"), {"id": 1})
        request = opener.requests[0][0]
        self.assertEqual(request.get_method(), "POST")
        self.assertTrue(request.full_url.endswith("/getMe"))
        self.assertEqual(request.headers["Content-type"], "application/json")

    def test_forbidden_methods_never_sent(self):
        tg, opener = client()
        for method in ("setWebhook", "deleteWebhook", "logOut", "close", "sendDocument"):
            with self.assertRaises(ValueError):
                tg.call(method)
        self.assertEqual(opener.requests, [])

    def test_api_error(self):
        tg, _ = client(http_error(400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}))
        with self.assertRaises(TelegramError) as ctx:
            tg.call("sendMessage", chat_id=1, text="x")
        self.assertEqual(ctx.exception.error_code, 400)
        self.assertIn("chat not found", str(ctx.exception))

    def test_429_retried_once(self):
        sleeps = []
        tg, opener = client(
            http_error(429, {"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 3}}),
            ok(True),
            sleeps=sleeps,
        )
        self.assertTrue(tg.call("sendMessage", chat_id=1, text="x"))
        self.assertEqual(sleeps, [3])
        self.assertEqual(len(opener.requests), 2)

    def test_429_twice_raises(self):
        too_many = {"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 1}}
        tg, _ = client(http_error(429, too_many), http_error(429, too_many))
        with self.assertRaises(TelegramError) as ctx:
            tg.call("sendMessage", chat_id=1, text="x")
        self.assertEqual(ctx.exception.retry_after, 1)

    def test_429_with_long_wait_not_slept(self):
        sleeps = []
        too_many = {"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 3600}}
        tg, _ = client(http_error(429, too_many), sleeps=sleeps)
        with self.assertRaises(TelegramError):
            tg.call("sendMessage", chat_id=1, text="x")
        self.assertEqual(sleeps, [])

    def test_network_error_never_leaks_token_or_url(self):
        reason = OSError(f"failed to reach https://api.telegram.org/bot{TOKEN}/getUpdates")
        tg, _ = client(urllib.error.URLError(reason))
        with self.assertRaises(TelegramError) as ctx:
            tg.call("getUpdates", timeout=0)
        exc = ctx.exception
        text = f"{exc} {exc.args} {exc.description!r}"
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("api.telegram.org", text)
        self.assertIsNone(exc.__context__)
        self.assertIsNone(exc.__cause__)
        self.assertIsNone(exc.error_code)

    def test_timeout_is_a_network_error(self):
        tg, _ = client(TimeoutError("timed out"))
        with self.assertRaises(TelegramError) as ctx:
            tg.call("getMe")
        self.assertIn("network error", str(ctx.exception))

    def test_description_with_token_is_cleaned(self):
        tg, _ = client(http_error(401, {"ok": False, "error_code": 401, "description": f"Unauthorized {TOKEN}"}))
        with self.assertRaises(TelegramError) as ctx:
            tg.call("getMe")
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertIsNone(ctx.exception.__context__)

    def test_invalid_json(self):
        tg, _ = client(FakeResponse(502, b"<html>bad gateway</html>"))
        with self.assertRaises(TelegramError) as ctx:
            tg.call("getMe")
        self.assertEqual(ctx.exception.error_code, 502)

    def test_repr_hides_token(self):
        tg, _ = client()
        self.assertNotIn(TOKEN, repr(tg))
        self.assertNotIn(TOKEN.split(":")[1], repr(tg))

    def test_rejects_malformed_token(self):
        with self.assertRaises(ValueError) as ctx:
            TelegramClient("not-a-token", "http://127.0.0.1:7890")
        self.assertNotIn("not-a-token", str(ctx.exception))


class HelperTest(unittest.TestCase):
    def test_get_updates_params(self):
        tg, opener = client(ok([{"update_id": 5}]), ok([]))
        self.assertEqual(tg.get_updates(17, 50), [{"update_id": 5}])
        body = opener.json(0)
        self.assertEqual(body, {"timeout": 50, "allowed_updates": ["message", "callback_query"], "offset": 17})
        self.assertGreater(opener.requests[0][1], 50)
        tg.get_updates(0, 1)
        self.assertNotIn("offset", opener.json(1))

    def test_send_message(self):
        tg, opener = client(ok({"message_id": 9}))
        result = tg.send_message(42, "<b>hi</b>", reply_to=3, buttons=[[("OK", "cf:0123456789abcdef:y")]])
        self.assertEqual(result["message_id"], 9)
        body = opener.json()
        self.assertEqual(body["parse_mode"], "HTML")
        self.assertEqual(body["reply_parameters"], {"message_id": 3, "allow_sending_without_reply": True})
        self.assertEqual(body["reply_markup"], {"inline_keyboard": [[{"text": "OK", "callback_data": "cf:0123456789abcdef:y"}]]})
        self.assertEqual(body["link_preview_options"], {"is_disabled": True})

    def test_send_message_truncates(self):
        tg, opener = client(ok({"message_id": 1}))
        tg.send_message(1, "a" * 5000)
        text = opener.json()["text"]
        self.assertLessEqual(utf16_len(text), 4096)
        self.assertTrue(text.endswith("…"))

    def test_callback_data_limit(self):
        tg, opener = client()
        with self.assertRaises(ValueError):
            tg.send_message(1, "x", buttons=[[("x", "d" * 65)]])
        self.assertEqual(opener.requests, [])

    def test_edit_ignores_not_modified(self):
        tg, _ = client(http_error(400, {"ok": False, "error_code": 400, "description": "Bad Request: message is not modified"}))
        tg.edit_message(1, 2, "same")  # no exception

    def test_edit_other_errors_raise(self):
        tg, _ = client(http_error(400, {"ok": False, "error_code": 400, "description": "Bad Request: message to edit not found"}))
        with self.assertRaises(TelegramError):
            tg.edit_message(1, 2, "x")

    def test_set_my_commands_chat_scope(self):
        tg, opener = client(ok(True))
        tg.set_my_commands([("help", "用法")], 42)
        body = opener.json()
        self.assertEqual(body["scope"], {"type": "chat", "chat_id": 42})
        self.assertEqual(body["commands"], [{"command": "help", "description": "用法"}])

    def test_answer_callback(self):
        tg, opener = client(ok(True))
        tg.answer_callback("abc", "已过期")
        self.assertEqual(opener.json(), {"callback_query_id": "abc", "text": "已过期"})


class DownloadTest(unittest.TestCase):
    def test_download(self):
        tg, opener = client(ok({"file_id": "f", "file_size": 3, "file_path": "photos/file_1.jpg"}), FakeResponse(200, b"abc"))
        self.assertEqual(tg.download_file("f", 10), b"abc")
        request = opener.requests[1][0]
        self.assertEqual(request.get_method(), "GET")
        self.assertTrue(request.full_url.endswith("/file/bot" + TOKEN + "/photos/file_1.jpg"))

    def test_declared_size_too_large(self):
        tg, opener = client(ok({"file_id": "f", "file_size": 11, "file_path": "photos/a.jpg"}))
        with self.assertRaises(FileTooLarge):
            tg.download_file("f", 10)
        self.assertEqual(len(opener.requests), 1)  # never downloaded

    def test_streamed_body_too_large(self):
        tg, _ = client(ok({"file_id": "f", "file_path": "photos/a.jpg"}), FakeResponse(200, b"x" * 11))
        with self.assertRaises(FileTooLarge):
            tg.download_file("f", 10)

    def test_content_length_too_large(self):
        tg, _ = client(ok({"file_id": "f", "file_path": "a.jpg"}), FakeResponse(200, b"", {"Content-Length": "99"}))
        with self.assertRaises(FileTooLarge):
            tg.download_file("f", 10)

    def test_unsafe_file_path(self):
        for path in ("../secret", "/etc/passwd", "a/../../b", "a b", ""):
            tg, opener = client(ok({"file_id": "f", "file_path": path}))
            with self.assertRaises(TelegramError):
                tg.download_file("f", 10)
            self.assertEqual(len(opener.requests), 1)

    def test_http_error_on_download(self):
        err = urllib.error.HTTPError("u", 404, "nf", {}, io.BytesIO(b"not found"))
        tg, _ = client(ok({"file_id": "f", "file_path": "a.jpg"}), err)
        with self.assertRaises(TelegramError) as ctx:
            tg.download_file("f", 10)
        self.assertEqual(ctx.exception.error_code, 404)


class TruncateTest(unittest.TestCase):
    def test_short_text_untouched(self):
        self.assertEqual(truncate_html("<b>x</b>"), "<b>x</b>")

    def test_closes_open_tags(self):
        html = "<b>title</b>\n<pre>" + "line &amp; more\n" * 500 + "</pre>"
        out = truncate_html(html, 1000)
        self.assertLessEqual(utf16_len(out), 1000)
        self.assertTrue(out.endswith("…</pre>"))
        self.assertEqual(out.count("<pre>"), out.count("</pre>"))

    def test_no_partial_entity_or_tag(self):
        html = "x" * 795 + "&amp;" + "<a href='y'>link</a>" * 50
        out = truncate_html(html, 1000)
        self.assertNotRegex(out, r"&[a-z]*…")
        self.assertNotRegex(out, r"<[^>]*…")

    def test_astral_characters_count_twice(self):
        out = truncate_html("😀" * 3000, 1000)
        self.assertLessEqual(utf16_len(out), 1000)


if __name__ == "__main__":
    unittest.main()
