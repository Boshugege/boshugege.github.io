import io
import json
import unittest
import urllib.error

from pnc_bot.telegram import (
    CONNECT_ATTEMPTS,
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
        # getUpdates is repeatable: every attempt fails the same way.
        tg, _ = client(*[urllib.error.URLError(reason)] * CONNECT_ATTEMPTS)
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
        tg, opener = client(*[TimeoutError("timed out")] * CONNECT_ATTEMPTS)
        with self.assertRaises(TelegramError) as ctx:
            tg.call("getMe")
        self.assertEqual(len(opener.requests), CONNECT_ATTEMPTS)  # getMe is repeatable
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
        self.assertLess(opener.requests[0][1], 60)  # poll + a short grace, not a minute more
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


class Socks5Test(unittest.TestCase):
    """The SOCKS5 tunnel to NaiveProxy (RFC 1928, no auth, remote DNS)."""

    def serve(self, script):
        """A one-shot fake SOCKS5 proxy on localhost; ``script(conn)`` talks to the client."""
        import socket
        import threading

        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)
        seen = {}

        def run():
            conn, _ = server.accept()
            with conn:
                conn.settimeout(5)
                try:
                    script(conn, seen)
                except OSError:
                    pass

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        client = socket.create_connection(server.getsockname(), timeout=5)
        self.addCleanup(client.close)
        return client, seen, thread

    @staticmethod
    def read(conn, n):
        data = b""
        while len(data) < n:
            data += conn.recv(n - len(data))
        return data

    def test_connect_sends_the_host_name_and_accepts_every_reply_address_type(self):
        from pnc_bot.telegram import socks5_connect

        for atyp, addr in ((1, b"\x01\x02\x03\x04"), (4, b"\x00" * 16), (3, b"\x05proxy")):
            def script(conn, seen, atyp=atyp, addr=addr):
                seen["greeting"] = self.read(conn, 3)
                conn.sendall(b"\x05\x00")
                head = self.read(conn, 5)
                seen["request"] = head + self.read(conn, head[4] + 2)
                conn.sendall(b"\x05\x00\x00" + bytes([atyp]) + addr + b"\x01\xbb")
                conn.sendall(b"after")  # the tunnel's first bytes stay unread

            client, seen, thread = self.serve(script)
            socks5_connect(client, "api.telegram.org", 443)
            thread.join(5)
            self.assertEqual(seen["greeting"], b"\x05\x01\x00")
            self.assertEqual(seen["request"], b"\x05\x01\x00\x03\x10api.telegram.org\x01\xbb")
            self.assertEqual(client.recv(5), b"after")

    def test_refusals_become_proxy_errors(self):
        from pnc_bot.telegram import ProxyError, socks5_connect

        cases = {
            "auth": lambda conn, seen: (self.read(conn, 3), conn.sendall(b"\x05\xff")),
            "not socks": lambda conn, seen: (self.read(conn, 3), conn.sendall(b"HT")),
            "host unreachable": lambda conn, seen: (
                self.read(conn, 3), conn.sendall(b"\x05\x00"), self.read(conn, 23),
                conn.sendall(b"\x05\x04\x00\x01\x00\x00\x00\x00\x00\x00"),
            ),
            "closed": lambda conn, seen: (self.read(conn, 3), conn.close()),
        }
        for name, script in cases.items():
            with self.subTest(name):
                client, _, thread = self.serve(script)
                with self.assertRaises(ProxyError) as caught:
                    socks5_connect(client, "api.telegram.org", 443)
                thread.join(5)
                self.assertNotIn("bot", str(caught.exception))

    def test_a_silent_proxy_times_out_quickly(self):
        import socket
        from pnc_bot.telegram import socks5_connect

        client, _, thread = self.serve(lambda conn, seen: (self.read(conn, 3), __import__("time").sleep(1.5)))
        client.settimeout(0.3)
        with self.assertRaises(socket.timeout):
            socks5_connect(client, "api.telegram.org", 443)

    def test_opener_uses_socks_for_socks_urls_only(self):
        from pnc_bot.telegram import SocksHTTPSHandler, build_opener

        socks = build_opener("socks5h://127.0.0.1:10801")
        self.assertTrue(any(isinstance(h, SocksHTTPSHandler) for h in socks.handlers))
        http_proxy = build_opener("http://127.0.0.1:7890")
        self.assertFalse(any(isinstance(h, SocksHTTPSHandler) for h in http_proxy.handlers))
        handler = next(h for h in socks.handlers if isinstance(h, SocksHTTPSHandler))
        self.assertEqual(handler._connection.proxy, ("127.0.0.1", 10801))

    def test_network_failure_through_socks_is_a_clean_telegram_error(self):
        """A proxy that is not listening fails fast with no token or URL in the text."""
        import socket
        from pnc_bot.telegram import TelegramClient, TelegramError

        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
        free.close()
        client = TelegramClient(TOKEN, f"socks5h://127.0.0.1:{port}", sleep=lambda s: None)
        with self.assertRaises(TelegramError) as caught:
            client.call("getMe")
        text = str(caught.exception)
        self.assertIn("network error", text)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("api.telegram.org", text)


class ConnectRetryTest(unittest.TestCase):
    """A tunnel that never finishes opening is retried at once; nothing was sent."""

    class Opener:
        def __init__(self, failures):
            self.failures = list(failures)
            self.opens = 0

        def open(self, request, timeout):
            self.opens += 1
            if self.failures:
                raise self.failures.pop(0)
            body = json.dumps({"ok": True, "result": {"is_bot": True}}).encode()

            class Resp(io.BytesIO):
                status = 200
                headers = {}

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

            return Resp(body)

    def client(self, failures):
        from pnc_bot.telegram import TelegramClient

        opener = self.Opener(failures)
        return TelegramClient(TOKEN, "socks5h://127.0.0.1:10801", opener=opener, sleep=lambda s: None), opener

    def test_stalled_connects_are_retried_until_one_opens(self):
        from pnc_bot.telegram import ConnectStageError

        stalled = urllib.error.URLError(ConnectStageError("connect: timed out"))
        client, opener = self.client([stalled, stalled])
        self.assertEqual(client.call("getMe"), {"is_bot": True})
        self.assertEqual(opener.opens, 3)

    def test_gives_up_after_the_last_attempt(self):
        from pnc_bot.telegram import CONNECT_ATTEMPTS, ConnectStageError, TelegramError

        stalled = urllib.error.URLError(ConnectStageError("connect: timed out"))
        client, opener = self.client([stalled] * CONNECT_ATTEMPTS)
        with self.assertRaises(TelegramError) as caught:
            client.call("getMe")
        self.assertEqual(opener.opens, CONNECT_ATTEMPTS)
        self.assertIn("timed out", str(caught.exception))
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_failures_after_the_request_was_sent_are_not_retried_here(self):
        """A read timeout may mean Telegram already got the message: never resend it blindly."""
        from pnc_bot.telegram import TelegramError

        client, opener = self.client([TimeoutError("read timed out")])
        with self.assertRaises(TelegramError):
            client.call("sendMessage", chat_id=1, text="x")
        self.assertEqual(opener.opens, 1)

    def test_unanswered_repeatable_calls_are_sent_again(self):
        for method, params in (("getMe", {}), ("editMessageText", {"chat_id": 1, "message_id": 2, "text": "x"}),
                               ("getUpdates", {"timeout": 1})):
            with self.subTest(method):
                client, opener = self.client([TimeoutError("read timed out")])
                client.call(method, **params)
                self.assertEqual(opener.opens, 2)

    def test_read_timeouts_are_short(self):
        from pnc_bot import telegram as tgmod

        client, opener = self.client([])
        client.call("sendMessage", chat_id=1, text="x")
        self.assertEqual(client.timeout, tgmod.READ_TIMEOUT)
        self.assertLessEqual(tgmod.READ_TIMEOUT, 15)

    def test_a_real_stalled_tls_handshake_is_cut_after_the_connect_timeout(self):
        """A SOCKS proxy that accepts the tunnel but never speaks TLS."""
        import socket
        import threading
        import time as _time
        from pnc_bot import telegram as tgmod

        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(8)
        self.addCleanup(server.close)

        def run():
            while True:
                try:
                    conn, _ = server.accept()
                except OSError:
                    return
                conn.settimeout(5)
                try:
                    conn.recv(3)
                    conn.sendall(b"\x05\x00")
                    conn.recv(64)
                    conn.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
                    _time.sleep(2)  # the TLS handshake stalls
                except OSError:
                    pass
                finally:
                    conn.close()

        threading.Thread(target=run, daemon=True).start()
        port = server.getsockname()[1]
        handler = tgmod.SocksHTTPSHandler("127.0.0.1", port, connect_timeout=0.3)
        opener = tgmod.urllib.request.build_opener(tgmod.urllib.request.ProxyHandler({}), handler)
        client = tgmod.TelegramClient(TOKEN, f"socks5h://127.0.0.1:{port}", opener=opener, sleep=lambda s: None)
        started = _time.monotonic()
        with self.assertRaises(tgmod.TelegramError) as caught:
            client.call("getMe")
        elapsed = _time.monotonic() - started
        self.assertLess(elapsed, 0.3 * sum(tgmod.CONNECT_BACKOFF) + 1.0)
        self.assertGreater(elapsed, 0.3 * sum(tgmod.CONNECT_BACKOFF) - 0.2)  # each attempt got its own, longer limit
        self.assertIn("timed out", str(caught.exception))
