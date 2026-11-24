import email.message
import http.client
import io
import json
import unittest
import urllib.error
import urllib.request
import urllib.response

from pnc_bot.linkpreview import (
    BUDGET,
    DIRECT_HOSTS,
    MAX_HTML_BYTES,
    MAX_IMAGE_BYTES,
    MAX_JSON_BYTES,
    MAX_REDIRECTS,
    TIMEOUT,
    USER_AGENT,
    Fetcher,
    Preview,
    UnsafeURL,
    check_url,
    classify,
    entry,
    merge_previews,
    standalone_links,
    validate_previews,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PAGE = "https://example.com/post"
VIDEO = "dQw4w9WgXcQ"
YT_OEMBED = f"https://www.youtube.com/oembed?url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3D{VIDEO}&format=json"
YT_THUMB = f"https://i.ytimg.com/vi/{VIDEO}/%s.jpg"
BVID = "BV1xx411c7mD"
BILI_API = "https://api.bilibili.com/x/web-interface/view?"
X_OEMBED = "https://publish.twitter.com/oembed?url=https%3A%2F%2Ftwitter.com%2F{}%2Fstatus%2F{}&omit_script=1&dnt=1"
GH_REPO = "https://github.com/python/cpython"
GH_API = "https://api.github.com/repos/python/cpython"
LOGGER = "pnc_bot.linkpreview"


# --------------------------------------------------------------------------
# a fake network


class Reply:
    """A canned HTTP answer."""

    def __init__(self, body=b"", ctype="text/html; charset=utf-8", status=200, headers=None):
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.status = status
        self.headers = dict(headers or {})
        if ctype:
            self.headers["Content-Type"] = ctype


def page(head="", body="", ctype="text/html; charset=utf-8"):
    return Reply(f"<!doctype html><html><head>{head}</head><body>{body}</body></html>", ctype)


def og(**tags):
    return page("".join(f'<meta property="og:{key}" content="{value}">' for key, value in tags.items()))


def js(payload):
    return Reply(json.dumps(payload), "application/json; charset=utf-8")


def image(data=PNG, ctype="image/png"):
    return Reply(data, ctype)


def redirect(location, status=302):
    return Reply(status=status, headers={"Location": location})


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeHTTPS(urllib.request.HTTPSHandler):
    """Answers https requests from a table of URL → Reply (or an exception to raise).

    It takes the place of the real HTTPSHandler in a real OpenerDirector, so
    nothing reaches the network and urllib's own error and redirect machinery
    is what the Fetcher gets to deal with. Unknown URLs answer 404.
    """

    def __init__(self, routes, clock=None, cost=0.0):
        super().__init__()
        self.routes = routes
        self.requests = []
        self.clock = clock
        self.cost = cost  # seconds each request takes on the fake clock

    def https_open(self, req):
        self.requests.append(req)
        if self.clock is not None:
            self.clock.now += self.cost
        answer = self.routes.get(req.full_url, Reply(status=404))
        if isinstance(answer, BaseException):
            raise answer
        headers = email.message.Message()
        for name, value in answer.headers.items():
            headers[name] = value
        response = urllib.response.addinfourl(io.BytesIO(answer.body), headers, req.full_url, answer.status)
        response.msg = "fake"
        return response


class Always(dict):
    """A route table that gives every URL the same answer."""

    def __init__(self, answer):
        super().__init__()
        self.answer = answer

    def get(self, key, default=None):
        return self.answer


def make(routes, clock=None, cost=0.0):
    """A Fetcher over a fake network, and the list of requests it sends."""
    clock = clock or Clock()
    handler = FakeHTTPS(routes, clock, cost)
    return Fetcher(urllib.request.build_opener(handler), clock=clock), handler.requests


def fetch(url, routes):
    fetcher, requests = make(routes)
    return fetcher.fetch(url), [request.full_url for request in requests]


# --------------------------------------------------------------------------


class StandaloneLinksTest(unittest.TestCase):
    def test_only_a_line_that_is_nothing_but_the_url(self):
        text = "看看这个\nhttps://example.com/a\n\nsee https://example.com/b for more\nhttps://example.com/c 好看"
        self.assertEqual(standalone_links(text), ["https://example.com/a"])

    def test_order_duplicates_and_line_endings(self):
        text = "https://b.example.org/\r\nhttps://a.example.org/?q=1#frag\rhttps://b.example.org/\n  https://c.example.org  \n"
        self.assertEqual(
            standalone_links(text),
            ["https://b.example.org/", "https://a.example.org/?q=1#frag", "https://c.example.org"],
        )

    def test_angle_brackets(self):
        self.assertEqual(standalone_links("<https://example.com/a>"), ["https://example.com/a"])
        for line in ("<https://example.com/a", "https://example.com/a>", "<https://example.com/a b>", "<<https://example.com/a>>"):
            self.assertEqual(standalone_links(line), [], line)

    def test_only_https(self):
        for line in ("http://example.com/", "HTTPS://example.com/", "ftp://example.com/", "example.com", "https://", "<https://>"):
            self.assertEqual(standalone_links(line), [], line)

    def test_not_a_bare_line(self):
        for line in (
            "https://example.com/a https://example.com/b",
            "[link](https://example.com/a)",
            "**https://example.com/a**",
            "`https://example.com/a`",
            "> https://example.com/a",
            ">https://example.com/a",
            "- https://example.com/a",
            "* https://example.com/a",
            "+ https://example.com/a",
            "1. https://example.com/a",
            "# https://example.com/a",
        ):
            self.assertEqual(standalone_links(line), [], line)

    def test_indented_code(self):
        self.assertEqual(standalone_links("   https://example.com/a"), ["https://example.com/a"])
        for line in ("    https://example.com/a", "\thttps://example.com/a", " \thttps://example.com/a", "      https://example.com/a"):
            self.assertEqual(standalone_links(line), [], repr(line))

    def test_fenced_code(self):
        text = "```\nhttps://example.com/in\n```\nhttps://example.com/out\n~~~sh\nhttps://example.com/in2\n~~~\n"
        self.assertEqual(standalone_links(text), ["https://example.com/out"])

    def test_fence_closes_only_on_same_char_and_length(self):
        text = "````\nhttps://example.com/1\n```\nhttps://example.com/2\n~~~~\nhttps://example.com/3\n```` \nhttps://example.com/4"
        self.assertEqual(standalone_links(text), ["https://example.com/4"])
        text = "~~~\nhttps://example.com/1\n```\nhttps://example.com/2\n~~~~~\nhttps://example.com/3"
        self.assertEqual(standalone_links(text), ["https://example.com/3"])

    def test_fence_details(self):
        # a closing fence carries no text; an unclosed fence runs to the end
        self.assertEqual(standalone_links("```\nhttps://example.com/1\n``` end\nhttps://example.com/2"), [])
        # up to three spaces of indentation still open and close a fence
        self.assertEqual(standalone_links("   ```py\nhttps://example.com/1\n  ```\nhttps://example.com/2"), ["https://example.com/2"])
        # a backtick in the info string means it is not a fence at all
        self.assertEqual(standalone_links("``` a`b\nhttps://example.com/1"), ["https://example.com/1"])
        # two backticks are no fence either
        self.assertEqual(standalone_links("``\nhttps://example.com/1\n``"), ["https://example.com/1"])

    def test_trailing_punctuation(self):
        for last in ".,;:!?'\"*_~":
            self.assertEqual(standalone_links("https://example.com/a" + last), [], last)
            self.assertEqual(standalone_links(f"<https://example.com/a{last}>"), [], last)
        self.assertEqual(standalone_links("https://example.com/a)"), [])
        self.assertEqual(standalone_links("https://example.com/(a))"), [])
        wiki = "https://en.wikipedia.org/wiki/Rust_(programming_language)"
        self.assertEqual(standalone_links(wiki), [wiki])
        self.assertEqual(standalone_links("https://example.com/a/"), ["https://example.com/a/"])

    def test_returns_everything_for_the_caller_to_slice(self):
        text = "\n".join(f"https://example.com/{n}" for n in range(6))
        self.assertEqual(len(standalone_links(text)), 6)
        self.assertEqual(standalone_links(""), [])


class ClassifyTest(unittest.TestCase):
    def test_youtube(self):
        for url in (
            f"https://www.youtube.com/watch?v={VIDEO}",
            f"https://youtube.com/watch?v={VIDEO}&t=42s",
            f"https://m.youtube.com/watch?feature=share&v={VIDEO}",
            f"https://music.youtube.com/watch?v={VIDEO}&list=RDAMVM",
            f"https://youtu.be/{VIDEO}",
            f"https://youtu.be/{VIDEO}?si=abc",
            f"https://www.youtube.com/shorts/{VIDEO}",
            f"https://www.youtube.com/live/{VIDEO}?feature=share",
            f"https://www.youtube.com/embed/{VIDEO}/",
            f"https://WWW.YouTube.com/watch?v={VIDEO}#t=1",
            # as loose as the site's classifyLink: empty path segments and these host prefixes do not matter
            f"https://www.youtube.com/watch/?v={VIDEO}",
            f"https://www.youtube.com//shorts//{VIDEO}",
            f"https://music.youtube.com/shorts/{VIDEO}",
            f"https://www.youtu.be/{VIDEO}",
            f"https://www.youtube.com:443/watch?v={VIDEO}",
        ):
            self.assertEqual(classify(url), ("youtube", {"id": VIDEO}), url)

    def test_youtube_near_misses(self):
        for url in (
            "https://www.youtube.com/watch?v=short",
            f"https://www.youtube.com/watch?v={VIDEO}x",
            f"https://www.youtube.com/watch?vv={VIDEO}",
            f"https://www.youtube.com/watch/x?v={VIDEO}",
            f"https://www.youtube.com/watch?v=&v={VIDEO}",
            "https://www.youtube.com/watch",
            "https://www.youtube.com/@channel",
            "https://www.youtube.com/playlist?list=PL123",
            f"https://www.youtube.com/v/{VIDEO}",
            f"https://www.youtube.com/shorts/{VIDEO}/extra",
            f"https://youtu.be/{VIDEO}/extra",
            f"https://www.m.youtube.com/watch?v={VIDEO}",
            f"https://gaming.youtube.com/watch?v={VIDEO}",
            f"https://www.youtube.com:port/watch?v={VIDEO}",
            "https://youtu.be/",
            f"https://youtube.com.evil.example/watch?v={VIDEO}",
            f"https://notyoutube.com/watch?v={VIDEO}",
            f"http://www.youtube.com/watch?v={VIDEO}",
        ):
            self.assertEqual(classify(url), ("link", {}), url)

    def test_bilibili(self):
        self.assertEqual(classify(f"https://www.bilibili.com/video/{BVID}"), ("bilibili", {"bvid": BVID}))
        self.assertEqual(classify(f"https://www.bilibili.com/video/{BVID}/?spm_id_from=333.1007&p=2"), ("bilibili", {"bvid": BVID}))
        self.assertEqual(classify(f"https://m.bilibili.com/video/{BVID}"), ("bilibili", {"bvid": BVID}))
        self.assertEqual(classify(f"https://bilibili.com/video/{BVID}"), ("bilibili", {"bvid": BVID}))
        self.assertEqual(classify("https://www.bilibili.com/video/av170001"), ("bilibili", {"aid": "170001"}))
        self.assertEqual(classify("https://www.bilibili.com/video/av170001/"), ("bilibili", {"aid": "170001"}))
        self.assertEqual(classify("https://b23.tv/AbC123x"), ("bilibili", {"short": "AbC123x"}))
        self.assertEqual(classify("https://b23.tv/AbC123x/?share=1"), ("bilibili", {"short": "AbC123x"}))
        self.assertEqual(classify(f"https://b23.tv/{BVID}"), ("bilibili", {"short": BVID}))
        self.assertEqual(classify("https://www.bilibili.com/video/av123456789012")[1], {"aid": "123456789012"})
        self.assertEqual(classify("https://b23.tv/" + "a" * 16)[1], {"short": "a" * 16})

    def test_bilibili_near_misses(self):
        for url in (
            "https://www.bilibili.com/video/BV1xx411c7m",  # nine characters
            "https://www.bilibili.com/video/BV1xx411c7mDD",
            "https://www.bilibili.com/video/bv1xx411c7mD",
            "https://www.bilibili.com/video/av",
            "https://www.bilibili.com/video/av1234567890123",  # thirteen digits
            "https://b23.tv/" + "a" * 17,
            "https://www.bilibili.com/video/AV170001",
            "https://www.bilibili.com/video/av１２３",
            f"https://www.bilibili.com/video/{BVID}/extra",
            "https://www.bilibili.com/read/cv123",
            "https://live.bilibili.com/123",
            "https://space.bilibili.com/1",
            "https://b23.tv/",
            "https://b23.tv/a/b",
            "https://b23.tv/a-b",
            "https://www.b23.tv/AbC123x",
        ):
            self.assertEqual(classify(url), ("link", {}), url)

    def test_x(self):
        expected = ("x", {"user": "jack", "id": "20"})
        for url in (
            "https://twitter.com/jack/status/20",
            "https://x.com/jack/status/20",
            "https://www.x.com/jack/status/20/",
            "https://mobile.twitter.com/jack/status/20?s=20&t=abc",
            "https://mobile.x.com/jack/status/20",
            "https://X.com/jack/status/20#m",
            "https://m.twitter.com/jack/status/20",
            "https://x.com/jack/status/20/photo/1",
            "https://x.com/jack/status/20/video/1?s=20",
        ):
            self.assertEqual(classify(url), expected, url)
        self.assertEqual(classify("https://x.com/a_B_9/status/1850000000000000000")[1]["user"], "a_B_9")

    def test_x_near_misses(self):
        for url in (
            "https://x.com/jack",
            "https://x.com/jack/status/",
            "https://x.com/jack/status/abc",
            "https://x.com/jack/status/" + "1" * 21,
            "https://x.com/jack/statuses/20",
            "https://x.com/sixteen_chars_xx/status/20",
            "https://x.com/ja-ck/status/20",
            "https://x.com/jack/status/２０",
            "https://api.x.com/jack/status/20",
            "https://nitter.net/jack/status/20",
        ):
            self.assertEqual(classify(url), ("link", {}), url)

    def test_github(self):
        expected = ("github", {"owner": "python", "repo": "cpython"})
        for url in (
            "https://github.com/python/cpython",
            "https://github.com/python/cpython/",
            "https://github.com/python/cpython.git",
            "https://github.com/python/cpython.git/",
            "https://www.github.com/python/cpython",
            "https://GitHub.com/python/cpython?tab=readme-ov-file#readme",
            "https://github.com/python/cpython//",
            "https://github.com//python/cpython",
        ):
            self.assertEqual(classify(url), expected, url)
        self.assertEqual(classify("https://github.com/Boshugege/boshugege.github.io")[1]["repo"], "boshugege.github.io")
        self.assertEqual(classify("https://github.com/a-b/c_d.e-f")[1], {"owner": "a-b", "repo": "c_d.e-f"})
        self.assertEqual(classify("https://github.com/a/" + "r" * 100 + ".git")[1], {"owner": "a", "repo": "r" * 100})

    def test_github_near_misses(self):
        for url in (
            "https://github.com/python",
            "https://github.com/python/",
            "https://github.com/python/cpython/issues",
            "https://github.com/python/cpython/tree/main",
            "https://github.com/python/.git",
            "https://github.com/python/" + "r" * 101,
            "https://github.com/" + "o" * 40 + "/cpython",
            "https://github.com/python/..",
            "https://github.com/py_thon/cpython",
            "https://github.com/python/c%20python",
            "https://github.com/features/actions",
            "https://github.com/topics/python",
            "https://github.com/orgs/python",
            "https://github.com/Settings/profile",
            "https://github.com/marketplace/actions",
            "https://github.com/sponsors/someone",
            "https://github.com/search/advanced",
            "https://github.com/users/someone",
            "https://github.com/issues/assigned",
            "https://github.com/pulls/mentioned",
            "https://gist.github.com/python/cpython",
            "https://raw.githubusercontent.com/python/cpython",
        ):
            self.assertEqual(classify(url), ("link", {}), url)

    def test_everything_else_is_a_link(self):
        for url in ("https://example.com/", "https://example.com/watch?v=" + VIDEO, "", "not a url", "https://[bad", "mailto:a@b.c"):
            self.assertEqual(classify(url), ("link", {}), url)

    def test_host_tricks_do_not_fool_it(self):
        for url in (
            f"https://youtube.com@evil.example.org/watch?v={VIDEO}",
            "https://evil.example.org/github.com/python/cpython",
            "https://evil.example.org/?x=https://x.com/jack/status/20",
            "https://github.com.evil.example.org/python/cpython",
        ):
            self.assertEqual(classify(url), ("link", {}), url)


class CheckUrlTest(unittest.TestCase):
    def test_accepts_public_https(self):
        for url in (
            "https://example.com",
            "https://example.com/",
            "https://example.com:443/a/b?c=d&e=f#g",
            "https://EXAMPLE.com/Path",
            "https://sub.domain.example.co.uk/x",
            "https://zh.wikipedia.org/wiki/中文",
            "https://例え.jp/",
            "https://xn--r8jz45g.jp/",
            "https://my_host.example.com/",
            "https://i0.hdslb.com/bfs/archive/a.jpg@672w_378h",
            "https://1e100.net/",
        ):
            self.assertEqual(check_url(url), url)

    def assert_unsafe(self, url):
        with self.assertRaises(UnsafeURL, msg=repr(url)) as caught:
            check_url(url)
        if isinstance(url, str) and len(url) > 12:
            self.assertNotIn(url, str(caught.exception))

    def test_scheme(self):
        for url in (
            "http://example.com/",
            "HTTPS://example.com/",
            "ftp://example.com/",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "data:text/html,hi",
            "//example.com/",
            "example.com",
            "https:example.com",
            "https:/example.com",
            "",
            " https://example.com/",
            None,
            b"https://example.com/",
        ):
            self.assert_unsafe(url)

    def test_userinfo_and_port(self):
        for url in (
            "https://user@example.com/",
            "https://user:secret@example.com/",
            "https://example.com@evil.example.org/",
            "https://example.com:8443/",
            "https://example.com:80/",
            "https://example.com:0443/",
            "https://example.com:/",
            "https://example.com:443:443/",
            "https://example.com:abc/",
        ):
            self.assert_unsafe(url)

    def test_ip_literals_in_any_spelling(self):
        for url in (
            "https://127.0.0.1/",
            "https://127.0.0.1:443/",
            "https://10.0.0.8/",
            "https://169.254.169.254/latest/meta-data/",
            "https://8.8.8.8/",
            "https://[::1]/",
            "https://[::ffff:127.0.0.1]/",
            "https://[fe80::1%25eth0]/",
            "https://::1/",
            "https://2130706433/",
            "https://0x7f000001/",
            "https://0x7f.1/",
            "https://0X7F.0.0.1/",
            "https://127.1/",
            "https://0177.0.0.1/",
            "https://example.0x10/",
            "https://example.com.123/",
            "https://１２７.０.０.１/",
            "https://127。0。0。1/",
        ):
            self.assert_unsafe(url)

    def test_host_names(self):
        for url in (
            "https://localhost/",
            "https://LOCALHOST/",
            "https://intranet/",
            "https://app.localhost/",
            "https://printer.local/",
            "https://vault.internal/",
            "https://metadata.google.internal/",
            "https://router.lan/",
            "https://nas.home.arpa/",
            "https://a.test/",
            "https://a.example/",
            "https://a.invalid/",
            "https://abcdefghij234567.onion/",
            "https://box.tail1234.ts.net/",
            "https://1.0.0.127.in-addr.arpa/",
            "https://example.com./",
            "https://localhost./",
            "https://.example.com/",
            "https://a..example.com/",
            "https://-a.example.com/",
            "https://exa mple.com/",
            "https://exa%6dple.com/",
            "https://example.com\\@evil.example.org/",
            "https://evil.example.org\\.example.com/",
            "https:///path",
            "https://?x=1",
            "https://" + "a" * 64 + ".com/",
            "https://" + ".".join(["a" * 60] * 5) + ".com/",
        ):
            self.assert_unsafe(url)

    def test_length_and_characters(self):
        self.assertEqual(len(check_url("https://example.com/" + "a" * 1980)), 2000)
        for url in (
            "https://example.com/" + "a" * 1981,
            "https://example.com/a b",
            "https://example.com/a\tb",
            "https://example.com/a\nb",
            "https://example.com/a\r\nHost: evil",
            "https://example.com/\x00",
            "https://example.com/\x7f",
            "https://example.com/\x85",
            "https://example.com/\u3000",
            "https://example.com/\u202egnp.exe",
            "https://example.com/\u200b",
            "https://example.com/\ud800",
            "https://exam\u200bple.com/",
        ):
            self.assert_unsafe(url)

    def test_is_a_value_error(self):
        self.assertTrue(issubclass(UnsafeURL, ValueError))


class GenericTest(unittest.TestCase):
    def test_open_graph(self):
        head = (
            "<title>Plain title</title>"
            '<meta property="og:title" content="OG title">'
            '<meta name="og:description" content="OG description">'
            '<meta property="og:site_name" content="Example Site">'
            '<meta property="og:image" content="https://cdn.example.com/cover.png">'
            '<meta name="twitter:title" content="Twitter title">'
            '<meta name="description" content="Plain description">'
        )
        preview, urls = fetch(PAGE, {PAGE: page(head), "https://cdn.example.com/cover.png": image()})
        self.assertEqual(
            preview,
            Preview(url=PAGE, kind="link", title="OG title", description="OG description", site="Example Site", image=PNG),
        )
        self.assertEqual(urls, [PAGE, "https://cdn.example.com/cover.png"])
        self.assertNotIn("PNG", repr(preview))

    def test_request_headers(self):
        fetcher, requests = make({PAGE: og(title="T", image="/i.png"), "https://example.com/i.png": image()})
        self.assertIsNotNone(fetcher.fetch(PAGE))
        self.assertEqual(len(requests), 2)
        for request in requests:
            headers = {name.lower(): value for name, value in request.header_items()}
            self.assertEqual(headers["user-agent"], USER_AGENT)
            self.assertEqual(headers["accept-encoding"], "identity")
            self.assertEqual(headers["accept-language"], "zh-CN,zh;q=0.9,en;q=0.8")
            # no Cookie, no Referer, nothing else (Host is urllib's own)
            self.assertEqual(set(headers) - {"host"}, {"user-agent", "accept", "accept-encoding", "accept-language"})
            self.assertEqual(request.get_method(), "GET")
            self.assertIsNone(request.data)
            self.assertEqual(request.timeout, TIMEOUT)
        self.assertIn("text/html", requests[0].get_header("Accept"))
        self.assertIn("image/", requests[1].get_header("Accept"))

    def test_falls_back_to_twitter_card_then_plain_html(self):
        head = (
            '<meta name="twitter:title" content="Card title">'
            '<meta property="twitter:description" content="Card description">'
            '<meta name="twitter:image" content="https://example.com/card.jpg">'
            "<title>Plain title</title>"
            '<meta name="description" content="Plain description">'
        )
        url = "https://www.example.com/post"
        preview, _ = fetch(url, {url: page(head), "https://example.com/card.jpg": image(JPEG, "image/jpeg")})
        self.assertEqual((preview.title, preview.description), ("Card title", "Card description"))
        self.assertEqual((preview.site, preview.image), ("example.com", JPEG))

        head = '<TITLE>\n  Plain &amp; simple\n</TITLE><META NAME="Description" CONTENT="Plain description">'
        preview, urls = fetch(PAGE, {PAGE: page(head)})
        self.assertEqual((preview.title, preview.description), ("Plain & simple", "Plain description"))
        self.assertEqual((preview.site, preview.image), ("example.com", None))
        self.assertEqual(urls, [PAGE])

    def test_first_non_empty_value_wins(self):
        head = (
            '<meta property="og:title" content="  ">'
            '<meta property="og:title" content="First">'
            '<meta property="og:title" content="Second">'
            '<meta property="og:description">'
            '<meta content="Reversed attributes" property="og:description">'
        )
        preview, _ = fetch(PAGE, {PAGE: page(head)})
        self.assertEqual((preview.title, preview.description), ("First", "Reversed attributes"))

    def test_entities_and_metas_in_body(self):
        body = '<meta property="og:title" content="Tom &amp; Jerry &#x4E2D;&lt;b&gt;"><svg><title>icon</title></svg>'
        preview, _ = fetch(PAGE, {PAGE: page("<title>Head title</title>", body)})
        self.assertEqual(preview.title, "Tom & Jerry 中<b>")
        preview, _ = fetch(PAGE, {PAGE: page("<title>Head title</title>", "<svg><title>icon</title></svg>")})
        self.assertEqual(preview.title, "Head title")

    def test_script_content_is_not_markup(self):
        head = "<script>var s = '<meta property=\"og:title\" content=\"from script\">';</script><title>Real</title>"
        preview, _ = fetch(PAGE, {PAGE: page(head)})
        self.assertEqual(preview.title, "Real")

    def test_charset_from_header(self):
        body = '<html><head><meta property="og:title" content="中文标题"></head></html>'.encode("gbk")
        preview, _ = fetch(PAGE, {PAGE: Reply(body, "text/html; charset=GBK")})
        self.assertEqual(preview.title, "中文标题")

    def test_charset_from_meta(self):
        for declaration in (
            '<meta charset="gb18030">',
            "<meta charset=gb18030>",
            '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=gb18030">',
        ):
            body = f"<html><head>{declaration}<title>中文标题</title></head></html>".encode("gb18030")
            preview, _ = fetch(PAGE, {PAGE: Reply(body, "text/html")})
            self.assertEqual(preview.title, "中文标题", declaration)

    def test_charset_defaults_to_utf8_and_survives_nonsense(self):
        body = "<title>中文标题</title>".encode("utf-8")
        for ctype in ("text/html", "text/html; charset=no-such-charset", "text/html; charset=zip", "text/html; charset=../../x"):
            preview, _ = fetch(PAGE, {PAGE: Reply(body, ctype)})
            self.assertEqual(preview.title, "中文标题", ctype)
        preview, _ = fetch(PAGE, {PAGE: Reply(b"<title>ok \xff\xfe bytes</title>", "text/html")})
        self.assertEqual(preview.title, "ok \ufffd\ufffd bytes")

    def test_header_charset_beats_meta(self):
        body = '<meta charset="gbk"><title>中文标题</title>'.encode("utf-8")
        preview, _ = fetch(PAGE, {PAGE: Reply(body, "text/html; charset=utf-8")})
        self.assertEqual(preview.title, "中文标题")

    def test_xhtml(self):
        preview, _ = fetch(PAGE, {PAGE: Reply("<html><head><title>X</title></head></html>", "application/xhtml+xml")})
        self.assertEqual(preview.title, "X")

    def test_not_html_is_none(self):
        for ctype in ("application/pdf", "application/json", "image/png", "text/plain", "application/octet-stream", None):
            preview, urls = fetch(PAGE, {PAGE: Reply("<title>T</title>", ctype)})
            self.assertIsNone(preview, ctype)
            self.assertEqual(urls, [PAGE])

    def test_nothing_to_show_is_none(self):
        for reply in (page(), page("<title>  </title>"), page('<meta property="og:site_name" content="Only a site">'), Reply(b"")):
            self.assertIsNone(fetch(PAGE, {PAGE: reply})[0])

    def test_image_alone_is_enough(self):
        preview, _ = fetch(PAGE, {PAGE: og(image="https://example.com/i.png"), "https://example.com/i.png": image()})
        self.assertEqual((preview.title, preview.description, preview.image), ("", "", PNG))

    def test_http_errors_are_none(self):
        for status in (204, 400, 403, 404, 429, 500, 503):
            self.assertIsNone(fetch(PAGE, {PAGE: Reply("<title>T</title>", status=status)})[0], status)

    def test_compressed_answer_is_none(self):
        reply = Reply(b"\x1f\x8b\x08\x00", headers={"Content-Encoding": "gzip"})
        self.assertIsNone(fetch(PAGE, {PAGE: reply})[0])

    def test_relative_image_url(self):
        cases = {
            "/img/a.png": "https://example.com/img/a.png",
            "a.png": "https://example.com/blog/a.png",
            "../a.png": "https://example.com/a.png",
            "//cdn.example.com/a.png": "https://cdn.example.com/a.png",
            "  https://cdn.example.com/b.png  ": "https://cdn.example.com/b.png",
        }
        for written, resolved in cases.items():
            url = "https://example.com/blog/post"
            preview, urls = fetch(url, {url: og(title="T", image=written), resolved: image()})
            self.assertEqual(preview.image, PNG, written)
            self.assertEqual(urls, [url, resolved])

    def test_image_resolves_against_the_final_url(self):
        routes = {
            "https://short.example.org/x": redirect("https://www.example.com/a/post"),
            "https://www.example.com/a/post": og(title="T", image="cover.png"),
            "https://www.example.com/a/cover.png": image(),
        }
        preview, urls = fetch("https://short.example.org/x", routes)
        self.assertEqual((preview.url, preview.image), ("https://short.example.org/x", PNG))
        self.assertEqual((preview.site, preview.canonical), ("example.com", ""))
        self.assertEqual(urls[-1], "https://www.example.com/a/cover.png")

    def test_secure_url_preferred(self):
        head = (
            '<meta property="og:image" content="https://example.com/plain.png">'
            '<meta property="og:image:secure_url" content="https://example.com/secure.png">'
            '<meta name="twitter:image" content="https://example.com/card.png">'
        )
        _, urls = fetch(PAGE, {PAGE: page(head)})
        self.assertEqual(urls, [PAGE, "https://example.com/secure.png"])

    def test_image_failure_is_not_fatal(self):
        pic = "https://example.com/i.png"
        for reply in (
            Reply(status=404),
            Reply(status=500),
            image(b"<svg/>", "image/svg+xml"),
            image(b"<svg/>", "IMAGE/SVG+XML; charset=utf-8"),
            image(PNG, "text/html"),
            image(PNG, "application/octet-stream"),
            image(PNG, None),
            image(b""),
            image(b"x" * (MAX_IMAGE_BYTES + 1)),
            OSError("boom"),
            http.client.IncompleteRead(b""),
            redirect("https://127.0.0.1/i.png"),
        ):
            preview, urls = fetch(PAGE, {PAGE: og(title="T", image=pic), pic: reply})
            self.assertEqual((preview.title, preview.image), ("T", None))
            self.assertEqual(urls, [PAGE, pic])

    def test_unsafe_image_url_is_never_requested(self):
        for pic in (
            "http://example.com/i.png",
            "https://127.0.0.1/i.png",
            "https://localhost/i.png",
            "https://nas.local/i.png",
            "https://example.com:8080/i.png",
            "https://user@example.com/i.png",
            "data:image/png;base64,iVBORw0KGgo=",
            "file:///etc/passwd",
            "https://[bad",
        ):
            preview, urls = fetch(PAGE, {PAGE: og(title="T", image=pic)})
            self.assertEqual((preview.title, preview.image), ("T", None), pic)
            self.assertEqual(urls, [PAGE], pic)

    def test_image_at_the_size_limit(self):
        pic = "https://example.com/i.png"
        data = b"x" * MAX_IMAGE_BYTES
        preview, _ = fetch(PAGE, {PAGE: og(title="T", image=pic), pic: image(data)})
        self.assertEqual(preview.image, data)

    def test_long_page_is_truncated_not_refused(self):
        late = '<meta property="og:description" content="too late">'
        body = "<html><head><title>Early</title></head><body>" + "x" * MAX_HTML_BYTES + late + "</body></html>"
        preview, _ = fetch(PAGE, {PAGE: Reply(body)})
        self.assertEqual((preview.title, preview.description), ("Early", ""))

    def test_unclosed_title_does_not_swallow_the_page(self):
        preview, _ = fetch(PAGE, {PAGE: Reply("<title>Never closed " + "word " * 5000)})
        self.assertTrue(preview.title.startswith("Never closed word"))
        self.assertEqual(len(preview.title), 120)

    def test_url_is_sent_in_ascii_without_fragment(self):
        url = "https://example.com/文章/a b".replace(" ", "%20") + "?q=搜索&x=1|2#frag"
        wire = "https://example.com/%E6%96%87%E7%AB%A0/a%20b?q=%E6%90%9C%E7%B4%A2&x=1%7C2"
        preview, urls = fetch(url, {wire: og(title="T")})
        self.assertEqual(urls, [wire])
        self.assertEqual(preview.url, url)

    def test_idn_host_is_sent_as_punycode(self):
        preview, urls = fetch("https://例え.jp:443", {"https://xn--r8jz45g.jp/": og(title="T")})
        self.assertEqual(urls, ["https://xn--r8jz45g.jp/"])
        self.assertEqual(preview.url, "https://例え.jp:443")


class RedirectTest(unittest.TestCase):
    def test_follows_checked_redirects(self):
        routes = {
            "https://a.example.org/1": redirect("https://b.example.org/2", 301),
            "https://b.example.org/2": redirect("/3?x=1#frag", 302),
            "https://b.example.org/3?x=1": redirect("4", 307),
            "https://b.example.org/4": og(title="Arrived"),
        }
        preview, urls = fetch("https://a.example.org/1", routes)
        self.assertEqual(preview.title, "Arrived")
        self.assertEqual(preview.url, "https://a.example.org/1")
        self.assertEqual(preview.site, "b.example.org")
        self.assertEqual(
            urls,
            ["https://a.example.org/1", "https://b.example.org/2", "https://b.example.org/3?x=1", "https://b.example.org/4"],
        )
        self.assertEqual(len(urls), MAX_REDIRECTS + 1)

    def test_every_redirect_status(self):
        for status in (301, 302, 303, 307, 308):
            routes = {PAGE: redirect("https://example.com/final", status), "https://example.com/final": og(title="T")}
            self.assertEqual(fetch(PAGE, routes)[0].title, "T", status)
        for status in (300, 304, 305):
            routes = {PAGE: redirect("https://example.com/final", status), "https://example.com/final": og(title="T")}
            preview, urls = fetch(PAGE, routes)
            self.assertIsNone(preview, status)
            self.assertEqual(urls, [PAGE])

    def test_redirect_to_unsafe_target_is_none_and_not_requested(self):
        for target in (
            "http://example.com/final",
            "https://127.0.0.1/admin",
            "https://localhost/admin",
            "https://[::1]/admin",
            "https://2130706433/",
            "https://grafana.internal/",
            "https://example.com:8443/",
            "https://user:pw@example.com/",
            "//127.0.0.1/admin",
            "file:///etc/passwd",
            "ftp://example.com/x",
            "javascript:alert(1)",
        ):
            routes = {PAGE: redirect(target), "https://127.0.0.1/admin": og(title="secret"), "https://localhost/admin": og(title="secret")}
            preview, urls = fetch(PAGE, routes)
            self.assertIsNone(preview, target)
            self.assertEqual(urls, [PAGE], target)

    def test_unsafe_target_after_a_safe_hop(self):
        routes = {PAGE: redirect("https://b.example.org/"), "https://b.example.org/": redirect("https://10.0.0.1/")}
        preview, urls = fetch(PAGE, routes)
        self.assertIsNone(preview)
        self.assertEqual(urls, [PAGE, "https://b.example.org/"])

    def test_too_many_redirects(self):
        routes = {f"https://example.com/{n}": redirect(f"https://example.com/{n + 1}") for n in range(MAX_REDIRECTS + 1)}
        routes[f"https://example.com/{MAX_REDIRECTS + 1}"] = og(title="Too far")
        preview, urls = fetch("https://example.com/0", routes)
        self.assertIsNone(preview)
        self.assertEqual(len(urls), MAX_REDIRECTS + 1)
        # one hop fewer is fine
        self.assertEqual(fetch("https://example.com/1", routes)[0].title, "Too far")

    def test_redirect_loop_ends(self):
        preview, urls = fetch(PAGE, {PAGE: redirect(PAGE)})
        self.assertIsNone(preview)
        self.assertEqual(len(urls), MAX_REDIRECTS + 1)

    def test_redirect_without_location(self):
        self.assertIsNone(fetch(PAGE, {PAGE: Reply(status=302)})[0])
        self.assertIsNone(fetch(PAGE, {PAGE: redirect("   ")})[0])

    def test_raw_utf8_location(self):
        # http.client hands header bytes over as Latin-1
        location = "/文章".encode("utf-8").decode("latin-1")
        routes = {PAGE: redirect(location), "https://example.com/%E6%96%87%E7%AB%A0": og(title="T")}
        self.assertEqual(fetch(PAGE, routes)[0].title, "T")

    def test_image_redirects_are_checked_too(self):
        pic = "https://example.com/i.png"
        routes = {PAGE: og(title="T", image=pic), pic: redirect("https://cdn.example.com/i.png"), "https://cdn.example.com/i.png": image()}
        self.assertEqual(fetch(PAGE, routes)[0].image, PNG)
        routes[pic] = redirect("https://localhost/i.png")
        routes["https://localhost/i.png"] = image()
        preview, urls = fetch(PAGE, routes)
        self.assertIsNone(preview.image)
        self.assertEqual(urls, [PAGE, pic])

    def test_the_given_opener_is_left_alone(self):
        handler = FakeHTTPS({PAGE: redirect("https://example.com/final"), "https://example.com/final": og(title="T")})
        opener = urllib.request.build_opener(handler)
        before = list(opener.handlers)
        fetcher = Fetcher(opener)
        self.assertEqual(opener.handlers, before)
        self.assertTrue(all(h.parent is opener for h in opener.handlers))
        self.assertTrue(any(isinstance(h, urllib.request.HTTPRedirectHandler) for h in opener.handlers))
        # the original still follows redirects by itself; the Fetcher's copy does not
        with opener.open(PAGE) as response:
            self.assertEqual(response.url, "https://example.com/final")
        self.assertEqual(fetcher.fetch(PAGE).title, "T")
        self.assertEqual(len(handler.requests), 4)

    def test_own_opener_speaks_https_only(self):
        fetcher, _ = make({})
        kinds = {type(h) for h in fetcher._opener.handlers}
        for unwanted in ("HTTPHandler", "FileHandler", "FTPHandler", "DataHandler", "HTTPRedirectHandler"):
            self.assertNotIn(getattr(urllib.request, unwanted), kinds)
        for url in ("http://example.com/", "file:///etc/hosts", "data:text/plain,hi"):
            with self.assertRaises(urllib.error.URLError):
                fetcher._opener.open(url, timeout=1)


class YouTubeTest(unittest.TestCase):
    URL = f"https://youtu.be/{VIDEO}?si=abc"

    def test_oembed_and_best_thumbnail(self):
        routes = {
            YT_OEMBED: js({"title": "Never Gonna Give You Up", "author_name": "Rick Astley", "thumbnail_url": "https://i.ytimg.com/x.jpg"}),
            YT_THUMB % "maxresdefault": image(JPEG, "image/jpeg"),
        }
        preview, urls = fetch(self.URL, routes)
        self.assertEqual(
            preview,
            Preview(url=self.URL, kind="youtube", title="Never Gonna Give You Up", site="YouTube", author="Rick Astley", image=JPEG),
        )
        self.assertEqual(urls, [YT_OEMBED, YT_THUMB % "maxresdefault"])

    def test_thumbnail_fallback_order(self):
        routes = {YT_OEMBED: js({"title": "T"}), YT_THUMB % "hqdefault": image(JPEG, "image/jpeg")}
        preview, urls = fetch(f"https://www.youtube.com/watch?v={VIDEO}", routes)
        self.assertEqual(preview.image, JPEG)
        self.assertEqual(urls[1:], [YT_THUMB % "maxresdefault", YT_THUMB % "mqdefault", YT_THUMB % "hqdefault"])
        routes[YT_THUMB % "mqdefault"] = image(PNG)
        preview, urls = fetch(f"https://www.youtube.com/shorts/{VIDEO}", routes)
        self.assertEqual(preview.image, PNG)
        self.assertEqual(len(urls), 3)

    def test_title_without_thumbnail(self):
        preview, _ = fetch(self.URL, {YT_OEMBED: js({"title": "T", "author_name": "A"})})
        self.assertEqual((preview.title, preview.author, preview.image), ("T", "A", None))

    def test_thumbnail_without_oembed(self):
        for broken in (Reply(status=401), Reply("not json", "application/json"), js(["a", "list"]), OSError("down")):
            preview, urls = fetch(self.URL, {YT_OEMBED: broken, YT_THUMB % "mqdefault": image(JPEG, "image/jpeg")})
            self.assertEqual(preview, Preview(url=self.URL, kind="youtube", site="YouTube", image=JPEG))
            self.assertNotIn(self.URL, urls)  # no need for the page itself

    def test_falls_back_to_the_page(self):
        page_url = f"https://www.youtube.com/watch?v={VIDEO}"
        cover = "https://i.ytimg.com/vi/x/og.jpg"
        routes = {page_url: og(title="From the page", site_name="YouTube", image=cover), cover: image()}
        preview, urls = fetch(page_url, routes)
        self.assertEqual((preview.kind, preview.title, preview.site), ("youtube", "From the page", "YouTube"))
        self.assertEqual((preview.image, preview.author), (PNG, ""))
        self.assertEqual(urls[0], YT_OEMBED)
        self.assertEqual(urls[-2:], [page_url, cover])

    def test_everything_fails(self):
        preview, urls = fetch(self.URL, {})
        self.assertIsNone(preview)
        self.assertEqual(len(urls), 5)  # oEmbed, three thumbnails, the page


class BilibiliTest(unittest.TestCase):
    URL = f"https://www.bilibili.com/video/{BVID}/?spm_id_from=333.1007"
    COVER = "https://i0.hdslb.com/bfs/archive/cover.jpg"

    def answer(self, **data):
        video = {
            "title": "【教程】标题",
            "desc": "视频简介",
            "pic": "http://i0.hdslb.com/bfs/archive/cover.jpg",
            "owner": {"mid": 1, "name": "UP主"},
        }
        video.update(data)
        return js({"code": 0, "message": "0", "data": video})

    def test_bvid(self):
        routes = {BILI_API + f"bvid={BVID}": self.answer(), self.COVER: image(JPEG, "image/jpeg")}
        preview, urls = fetch(self.URL, routes)
        self.assertEqual(
            preview,
            Preview(
                url=self.URL,
                kind="bilibili",
                title="【教程】标题",
                description="视频简介",
                site="哔哩哔哩",
                author="UP主",
                image=JPEG,
            ),
        )
        self.assertEqual(urls, [BILI_API + f"bvid={BVID}", self.COVER])  # the http:// cover was asked for over https

    def test_aid(self):
        routes = {BILI_API + "aid=170001": self.answer(pic="//i0.hdslb.com/bfs/archive/cover.jpg"), self.COVER: image()}
        preview, urls = fetch("https://www.bilibili.com/video/av170001", routes)
        self.assertEqual((preview.kind, preview.title, preview.image, preview.canonical), ("bilibili", "【教程】标题", PNG, ""))
        self.assertEqual(urls[0], BILI_API + "aid=170001")

    def test_odd_answers(self):
        api = BILI_API + f"bvid={BVID}"
        preview, urls = fetch(self.URL, {api: self.answer(desc="-", pic=None, owner="nobody")})
        self.assertEqual((preview.title, preview.description, preview.author, preview.image), ("【教程】标题", "", "", None))
        self.assertEqual(urls, [api])
        preview, urls = fetch(self.URL, {api: self.answer(pic="http://127.0.0.1/cover.jpg")})
        self.assertIsNone(preview.image)
        self.assertEqual(urls, [api])

    def test_short_link(self):
        short = "https://b23.tv/AbC123x"
        api = BILI_API + f"bvid={BVID}"
        routes = {
            short: redirect(f"https://www.bilibili.com/video/{BVID}?p=2&share_source=copy_web&vd_source=abcdef"),
            api: self.answer(),
            self.COVER: image(),
        }
        preview, urls = fetch(short, routes)
        self.assertEqual((preview.url, preview.kind, preview.title, preview.image), (short, "bilibili", "【教程】标题", PNG))
        self.assertEqual(preview.canonical, f"https://www.bilibili.com/video/{BVID}?p=2")
        self.assertEqual(urls, [short, api, self.COVER])

    def test_short_link_canonical_forms(self):
        short = "https://b23.tv/AbC123x"
        cases = {
            f"https://m.bilibili.com/video/{BVID}?share_source=copy": (f"bvid={BVID}", f"https://www.bilibili.com/video/{BVID}"),
            f"https://www.bilibili.com/video/{BVID}/?p=0": (f"bvid={BVID}", f"https://www.bilibili.com/video/{BVID}"),
            f"https://www.bilibili.com/video/{BVID}?p=1%202": (f"bvid={BVID}", f"https://www.bilibili.com/video/{BVID}"),
            "https://www.bilibili.com/video/av170001?p=12": ("aid=170001", "https://www.bilibili.com/video/av170001?p=12"),
        }
        for target, (query, canonical) in cases.items():
            preview, _ = fetch(short, {short: redirect(target), BILI_API + query: self.answer(pic="")})
            self.assertEqual(preview.canonical, canonical, target)

    def test_short_link_through_several_hops(self):
        short = "https://b23.tv/AbC123x"
        routes = {
            short: redirect("https://b23.tv/Other99"),
            "https://b23.tv/Other99": redirect(f"https://www.bilibili.com/video/{BVID}"),
            BILI_API + f"bvid={BVID}": self.answer(pic=""),
        }
        self.assertEqual(fetch(short, routes)[0].canonical, f"https://www.bilibili.com/video/{BVID}")

    def test_short_link_to_something_else_falls_back_to_the_page(self):
        short = "https://b23.tv/AbC123x"
        live = "https://live.bilibili.com/123"
        preview, urls = fetch(short, {short: redirect(live), live: og(title="直播间", site_name="哔哩哔哩直播")})
        self.assertEqual((preview.kind, preview.title, preview.site), ("bilibili", "直播间", "哔哩哔哩直播"))
        self.assertEqual(preview.canonical, "")
        self.assertEqual(urls, [short, live, short, live])  # once to resolve, once more for the page

    def test_short_link_that_does_not_redirect(self):
        short = "https://b23.tv/AbC123x"
        preview, urls = fetch(short, {short: og(title="页面")})
        self.assertEqual((preview.kind, preview.title, preview.canonical), ("bilibili", "页面", ""))
        self.assertEqual(urls, [short, short])

    def test_short_link_to_unsafe_target(self):
        short = "https://b23.tv/AbC123x"
        preview, urls = fetch(short, {short: redirect("https://127.0.0.1/video/" + BVID)})
        self.assertIsNone(preview)
        self.assertEqual(urls, [short, short])

    def test_api_failure_falls_back_to_the_page(self):
        api = BILI_API + f"bvid={BVID}"
        wire = f"https://www.bilibili.com/video/{BVID}/?spm_id_from=333.1007"
        for broken in (
            js({"code": -404, "message": "啥都木有"}),
            js({"code": 0}),
            js({"data": {"title": "no code"}}),
            Reply(status=412),
            js([]),
        ):
            preview, urls = fetch(self.URL, {api: broken, wire: og(title="页面标题", site_name="哔哩哔哩")})
            self.assertEqual((preview.kind, preview.title, preview.site, preview.author), ("bilibili", "页面标题", "哔哩哔哩", ""))
            self.assertEqual(urls, [api, wire])

    def test_api_failure_after_resolving_keeps_canonical(self):
        short = "https://b23.tv/AbC123x"
        video = f"https://www.bilibili.com/video/{BVID}?p=3"
        preview, _ = fetch(short, {short: redirect(video), video: og(title="页面标题")})
        self.assertEqual((preview.title, preview.canonical), ("页面标题", video))

    def test_everything_fails(self):
        self.assertIsNone(fetch(self.URL, {})[0])


class DirectTest(unittest.TestCase):
    """With a ``direct`` opener, Bilibili's own hosts go around the proxy; nothing else ever does."""

    SHORT = "https://b23.tv/AbC123x"
    API = BILI_API + f"bvid={BVID}"
    COVER = "https://i0.hdslb.com/bfs/archive/cover.jpg"
    VIDEO_PAGE = f"https://www.bilibili.com/video/{BVID}"

    def fetch(self, url, routes):
        proxy, direct = FakeHTTPS(routes), FakeHTTPS(routes)
        fetcher = Fetcher(urllib.request.build_opener(proxy), direct=urllib.request.build_opener(direct), clock=Clock())
        preview = fetcher.fetch(url)
        return preview, [r.full_url for r in proxy.requests], [r.full_url for r in direct.requests]

    def test_short_link_and_api_go_direct_the_cover_does_not(self):
        answer = js({"code": 0, "data": {"title": "标题", "pic": "http://i0.hdslb.com/bfs/archive/cover.jpg"}})
        routes = {self.SHORT: redirect(self.VIDEO_PAGE), self.API: answer, self.COVER: image()}
        preview, proxied, direct = self.fetch(self.SHORT, routes)
        self.assertEqual((preview.title, preview.image, preview.canonical), ("标题", PNG, self.VIDEO_PAGE))
        self.assertEqual(direct, [self.SHORT, self.API])
        self.assertEqual(proxied, [self.COVER])

    def test_page_fallback_goes_direct(self):
        for host in ("www.bilibili.com", "m.bilibili.com", "bilibili.com"):
            url = f"https://{host}/video/{BVID}"
            preview, proxied, direct = self.fetch(url, {self.API: Reply(status=412), url: og(title="页面标题")})
            self.assertEqual(preview.title, "页面标题")
            self.assertEqual(direct, [self.API, url])
            self.assertEqual(proxied, [])

    def test_redirect_to_another_host_returns_to_the_proxy(self):
        elsewhere = "https://example.com/landing"
        routes = {self.SHORT: redirect(elsewhere), elsewhere: og(title="Elsewhere", image="/i.png"), "https://example.com/i.png": image()}
        preview, proxied, direct = self.fetch(self.SHORT, routes)
        self.assertEqual((preview.title, preview.image), ("Elsewhere", PNG))
        self.assertEqual(set(direct), {self.SHORT})
        self.assertEqual(set(proxied), {elsewhere, "https://example.com/i.png"})

    def test_direct_redirects_are_checked_too(self):
        routes = {self.SHORT: redirect("https://127.0.0.1/admin"), "https://127.0.0.1/admin": og(title="secret")}
        preview, proxied, direct = self.fetch(self.SHORT, routes)
        self.assertIsNone(preview)
        self.assertEqual((proxied, set(direct)), ([], {self.SHORT}))

    def test_nothing_else_goes_direct(self):
        routes = Always(og(title="T"))
        for url in (
            PAGE,
            GH_REPO,
            f"https://youtu.be/{VIDEO}",
            "https://x.com/jack/status/20",
            "https://live.bilibili.com/123",
            "https://space.bilibili.com/1",
            "https://i0.hdslb.com/bfs/archive/cover.jpg",
            "https://api.bilibili.com.example.org/x/web-interface/view",
            "https://b23.tv.example.org/AbC123x",
            "https://notb23.tv/AbC123x",
        ):
            preview, proxied, direct = self.fetch(url, routes)
            self.assertEqual(direct, [], url)
            self.assertGreaterEqual(len(proxied), 1, url)

    def test_the_list_is_closed_and_safe(self):
        self.assertEqual(DIRECT_HOSTS, {"api.bilibili.com", "b23.tv", "bilibili.com", "www.bilibili.com", "m.bilibili.com"})
        for host in DIRECT_HOSTS:
            check_url(f"https://{host}/")

    def test_without_a_direct_opener_everything_is_proxied(self):
        answer = js({"code": 0, "data": {"title": "标题"}})
        preview, urls = fetch(self.SHORT, {self.SHORT: redirect(self.VIDEO_PAGE), self.API: answer})
        self.assertEqual(preview.title, "标题")
        self.assertEqual(urls, [self.SHORT, self.API])

    def test_the_given_direct_opener_is_left_alone(self):
        handler = FakeHTTPS({})
        direct = urllib.request.build_opener(handler)
        before = list(direct.handlers)
        fetcher = Fetcher(urllib.request.build_opener(FakeHTTPS({})), direct=direct)
        self.assertEqual(direct.handlers, before)
        self.assertTrue(all(h.parent is direct for h in direct.handlers))
        kinds = {type(h) for h in fetcher._direct.handlers}
        self.assertNotIn(urllib.request.HTTPRedirectHandler, kinds)
        self.assertNotIn(urllib.request.HTTPHandler, kinds)


class XTest(unittest.TestCase):
    URL = "https://x.com/jack/status/20?s=20"
    HTML = (
        '<blockquote class="twitter-tweet" data-dnt="true"><p lang="en" dir="ltr">just setting up<br>my &lt;twttr&gt; &amp; more '
        '<a href="https://t.co/abc">pic.twitter.com/abc</a></p>&mdash; jack (@jack) '
        '<a href="https://twitter.com/jack/status/20?ref_src=twsrc%5Etfw">March 21, 2006</a></blockquote>\n'
    )

    def answer(self, **changes):
        data = {"author_name": "jack", "author_url": "https://twitter.com/jack", "html": self.HTML, "type": "rich"}
        data.update(changes)
        return js(data)

    def test_oembed(self):
        api = X_OEMBED.format("jack", "20")
        preview, urls = fetch(self.URL, {api: self.answer()})
        self.assertEqual(
            preview,
            Preview(
                url=self.URL,
                kind="x",
                title="jack (@jack)",
                description="just setting up my <twttr> & more pic.twitter.com/abc",
                site="X",
                author="jack",
            ),
        )
        self.assertEqual(urls, [api])  # and never an image

    def test_twitter_host_and_display_name(self):
        api = X_OEMBED.format("NASA", "1850000000000000000")
        answer = self.answer(author_name="NASA 🚀", author_url="https://twitter.com/NASA")
        preview, _ = fetch("https://twitter.com/NASA/status/1850000000000000000", {api: answer})
        self.assertEqual((preview.title, preview.author), ("NASA 🚀 (@NASA)", "NASA 🚀"))

    def test_handle_comes_from_the_profile_url(self):
        api = X_OEMBED.format("i", "20")
        preview, _ = fetch("https://x.com/i/status/20", {api: self.answer(author_name="Jack D", author_url="https://x.com/jack/")})
        self.assertEqual(preview.title, "Jack D (@jack)")
        for odd in (None, 5, "", "https://evil.example.org/jack", "https://twitter.com/jack/status/20", "javascript:alert(1)"):
            preview, _ = fetch("https://x.com/i/status/20", {api: self.answer(author_name="Jack D", author_url=odd)})
            self.assertEqual(preview.title, "Jack D (@i)", odd)

    def test_partial_answers(self):
        api = X_OEMBED.format("jack", "20")
        preview, _ = fetch(self.URL, {api: self.answer(html="<blockquote>no paragraph</blockquote>")})
        self.assertEqual((preview.title, preview.description), ("jack (@jack)", ""))
        preview, _ = fetch(self.URL, {api: self.answer(author_name=None)})
        self.assertEqual((preview.title, preview.author), ("@jack", ""))
        self.assertTrue(preview.description.startswith("just setting up"))
        preview, _ = fetch(self.URL, {api: self.answer(html="<p>first</p><p>second</p>")})
        self.assertEqual(preview.description, "first")
        preview, _ = fetch(self.URL, {api: self.answer(html=["not", "text"])})
        self.assertEqual(preview.description, "")

    def test_falls_back_to_the_page(self):
        api = X_OEMBED.format("jack", "20")
        for broken in (Reply(status=404), js({"author_name": "", "html": ""}), js("text"), ValueError("bad")):
            preview, urls = fetch(self.URL, {api: broken, self.URL: og(title="jack on X", description="my twttr")})
            self.assertEqual((preview.kind, preview.title, preview.description), ("x", "jack on X", "my twttr"))
            self.assertEqual((preview.site, preview.author), ("x.com", ""))
            self.assertEqual(urls, [api, self.URL])

    def test_everything_fails(self):
        self.assertIsNone(fetch(self.URL, {})[0])


class GitHubTest(unittest.TestCase):
    URL = GH_REPO
    AVATAR = "https://avatars.githubusercontent.com/u/1525981?v=4"

    def answer(self, **changes):
        data = {
            "full_name": "python/cpython",
            "description": "The Python programming language",
            "stargazers_count": 65000,
            "language": "Python",
            "owner": {"login": "python", "avatar_url": self.AVATAR},
            "private": False,
        }
        data.update(changes)
        return js(data)

    def test_api(self):
        fetcher, requests = make({GH_API: self.answer(), self.AVATAR: image()})
        preview = fetcher.fetch(self.URL + ".git")
        self.assertEqual(
            preview,
            Preview(
                url=self.URL + ".git",
                kind="github",
                title="python/cpython",
                description="The Python programming language",
                site="GitHub",
                stars=65000,
                language="Python",
                image=PNG,
            ),
        )
        self.assertEqual([r.full_url for r in requests], [GH_API, self.AVATAR])
        self.assertEqual(requests[0].get_header("Accept"), "application/vnd.github+json")

    def test_sparse_repo(self):
        preview, urls = fetch(self.URL, {GH_API: self.answer(description=None, language=None, stargazers_count=0, owner=None)})
        self.assertEqual((preview.title, preview.description, preview.language), ("python/cpython", "", ""))
        self.assertEqual((preview.stars, preview.image), (0, None))
        self.assertEqual(urls, [GH_API])

    def test_stars_must_be_a_sane_int(self):
        for bad in (-1, 10**9 + 1, True, "65000", 6.5, None, [1]):
            preview, _ = fetch(self.URL, {GH_API: self.answer(stargazers_count=bad, owner={})})
            self.assertIsNone(preview.stars, bad)
        preview, _ = fetch(self.URL, {GH_API: self.answer(stargazers_count=10**9, owner={})})
        self.assertEqual(preview.stars, 10**9)

    def test_falls_back_to_the_page(self):
        card = "https://opengraph.githubassets.com/abc/python/cpython"
        html = og(title="python/cpython: The Python programming language", site_name="GitHub", image=card)
        for broken in (
            Reply(status=403),
            Reply(status=404),
            js({"message": "Not Found"}),
            js({"full_name": 5}),
            Reply(b"{" * 10, "application/json"),
            http.client.RemoteDisconnected("gone"),
        ):
            preview, urls = fetch(self.URL, {GH_API: broken, self.URL: html, card: image()})
            self.assertEqual((preview.kind, preview.site), ("github", "GitHub"))
            self.assertEqual(preview.title, "python/cpython: The Python programming language")
            self.assertEqual((preview.stars, preview.language, preview.image), (None, "", PNG))
            self.assertEqual(urls, [GH_API, self.URL, card])

    def test_oversized_json_is_refused(self):
        big = json.dumps({"full_name": "python/cpython", "description": "x" * MAX_JSON_BYTES})
        preview, urls = fetch(self.URL, {GH_API: Reply(big, "application/json"), self.URL: og(title="From the page")})
        self.assertEqual(preview.title, "From the page")
        self.assertEqual(urls, [GH_API, self.URL])
        self.assertIsNone(fetch(self.URL, {GH_API: Reply(big, "application/json")})[0])

    def test_json_just_under_the_limit(self):
        pad = MAX_JSON_BYTES - len(json.dumps({"full_name": "python/cpython", "pad": ""}))
        body = json.dumps({"full_name": "python/cpython", "pad": "x" * pad})
        self.assertEqual(len(body), MAX_JSON_BYTES)
        self.assertEqual(fetch(self.URL, {GH_API: Reply(body, "application/json")})[0].title, "python/cpython")

    def test_other_github_pages_use_the_page(self):
        url = "https://github.com/python/cpython/issues/1"
        preview, urls = fetch(url, {url: og(title="An issue")})
        self.assertEqual((preview.kind, preview.title), ("link", "An issue"))
        self.assertEqual(urls, [url])


class BudgetTest(unittest.TestCase):
    def test_no_new_request_after_the_budget(self):
        pic = "https://example.com/i.png"
        routes = {PAGE: og(title="T", image=pic), pic: image()}
        fetcher, requests = make(routes, cost=BUDGET / 2 - 1)
        self.assertEqual(fetcher.fetch(PAGE).image, PNG)
        self.assertEqual(len(requests), 2)
        fetcher, requests = make(routes, cost=BUDGET)
        preview = fetcher.fetch(PAGE)
        self.assertEqual((preview.title, preview.image), ("T", None))  # the page that was under way still counts
        self.assertEqual(len(requests), 1)

    def test_no_fallback_after_the_budget(self):
        routes = {GH_API: Reply(status=500), GH_REPO: og(title="From the page")}
        fetcher, requests = make(routes, cost=BUDGET + 5)
        self.assertIsNone(fetcher.fetch(GH_REPO))
        self.assertEqual(len(requests), 1)
        fetcher, requests = make(routes, cost=1.0)
        self.assertEqual(fetcher.fetch(GH_REPO).title, "From the page")

    def test_redirects_share_the_budget(self):
        routes = {
            PAGE: redirect("https://example.com/2"),
            "https://example.com/2": redirect("https://example.com/3"),
            "https://example.com/3": og(title="T"),
        }
        fetcher, requests = make(routes, cost=BUDGET / 2)
        self.assertIsNone(fetcher.fetch(PAGE))
        self.assertEqual(len(requests), 2)

    def test_each_fetch_has_its_own_budget(self):
        clock = Clock()
        fetcher, requests = make({PAGE: og(title="T")}, clock=clock, cost=BUDGET - 1)
        for _ in range(3):
            self.assertEqual(fetcher.fetch(PAGE).title, "T")
        self.assertEqual(len(requests), 3)

    def test_slow_body_is_cut_off(self):
        class Ticking(Clock):
            def __call__(self):
                self.now += 5.0  # every look at the clock costs five seconds
                return self.now

        body = "<title>T</title>" + "x" * (300 * 1024)
        self.assertEqual(fetch(PAGE, {PAGE: Reply(body)})[0].title, "T")
        fetcher, _ = make({PAGE: Reply(body)}, clock=Ticking())
        self.assertIsNone(fetcher.fetch(PAGE))
        fetcher, _ = make({PAGE: Reply("<title>T</title>")}, clock=Ticking())
        self.assertEqual(fetcher.fetch(PAGE).title, "T")


class NeverRaisesTest(unittest.TestCase):
    def test_network_errors_are_none(self):
        for error in (
            OSError("network down"),
            TimeoutError("timed out"),
            ConnectionResetError("reset"),
            urllib.error.URLError(OSError("connect: timed out")),
            ValueError("bad value"),
            UnicodeError("bad text"),
            http.client.HTTPException("bad exchange"),
            http.client.BadStatusLine("garbage"),
            http.client.IncompleteRead(b"par"),
        ):
            for url in (PAGE, GH_REPO, f"https://youtu.be/{VIDEO}", "https://b23.tv/AbC123x", "https://x.com/jack/status/20"):
                fetcher, requests = make(Always(error))
                self.assertIsNone(fetcher.fetch(url), (error, url))
                self.assertGreaterEqual(len(requests), 2 if url != PAGE else 1)  # the provider, then the page

    def test_unexpected_errors_are_none_too(self):
        for error in (RuntimeError("bug"), KeyError("k"), AssertionError("parser"), RecursionError("deep")):
            fetcher, _ = make({PAGE: error})
            with self.assertLogs(LOGGER, level="WARNING") as logs:
                self.assertIsNone(fetcher.fetch(PAGE))
            self.assertIn(type(error).__name__, logs.output[0])
            self.assertIn("example.com", logs.output[0])

    def test_bad_arguments_are_none(self):
        fetcher, requests = make({})
        unsafe = ("http://example.com/", "https://127.0.0.1/", "https://localhost/x", "https://[bad")
        for url in (None, 5, b"https://example.com/", "", "not a url", *unsafe, "https://example.com/\ud800"):
            self.assertIsNone(fetcher.fetch(url), repr(url))
        self.assertEqual(requests, [])

    def test_deeply_nested_json_is_just_another_failure(self):
        routes = {GH_API: Reply("[" * 100000, "application/json"), GH_REPO: og(title="From the page")}
        self.assertEqual(fetch(GH_REPO, routes)[0].title, "From the page")
        del routes[GH_REPO]
        self.assertIsNone(fetch(GH_REPO, routes)[0])

    def test_log_names_the_host_only(self):
        url = "https://example.com/private/path?token=SECRETVALUE"
        fetcher, _ = make({url: OSError("https://example.com/private/path?token=SECRETVALUE failed")})
        with self.assertLogs(LOGGER, level="INFO") as logs:
            self.assertIsNone(fetcher.fetch(url))
        text = "\n".join(logs.output)
        self.assertIn("example.com", text)
        for secret in ("private", "token", "SECRETVALUE"):
            self.assertNotIn(secret, text)

        fetcher, _ = make({})
        with self.assertLogs(LOGGER, level="INFO") as logs:
            self.assertIsNone(fetcher.fetch("https://evil\n.example.org/private?token=SECRETVALUE"))
            self.assertIsNone(fetcher.fetch("https://x.com/jack/status/20?token=SECRETVALUE"))
        text = "\n".join(logs.output)
        for secret in ("private", "token", "SECRETVALUE", "jack"):
            self.assertNotIn(secret, text)


class CleaningTest(unittest.TestCase):
    def title(self, raw):
        payload = {"full_name": raw, "owner": {}}
        return fetch(GH_REPO, {GH_API: js(payload)})[0].title

    def test_whitespace_collapses(self):
        self.assertEqual(self.title("  a\n\nb\tc \r\n d\u3000e\u00a0f\u2028g  "), "a b c d e f g")

    def test_control_and_invisible_characters_are_dropped(self):
        self.assertEqual(self.title("a\x00b\x07c\x1bd\x7fe\x9cf"), "abcdef")
        self.assertEqual(self.title("safe\u202eexe.gpj\u202c name"), "safeexe.gpj name")
        self.assertEqual(self.title("\u2066a\u2067b\u2068c\u2069d\u202ae\u202bf\u202dg"), "abcdefg")
        self.assertEqual(self.title("zero\u200bwidth\u200c \u200djoin\ufeffer\u2060s"), "zerowidth joiners")
        self.assertEqual(self.title("a \u200b b"), "a b")
        self.assertEqual(self.title("lone\ud800surrogate"), "lonesurrogate")
        self.assertEqual(self.title("表情 🚀 ok"), "表情 🚀 ok")

    def test_only_junk_is_nothing(self):
        routes = {GH_API: js({"full_name": "\u200b\x00 \n", "description": "\u202e"}), GH_REPO: Reply(status=404)}
        self.assertIsNone(fetch(GH_REPO, routes)[0])

    def test_non_strings_are_empty(self):
        payload = {"full_name": "python/cpython", "description": ["a"], "language": {"name": "Python"}, "owner": {"avatar_url": 5}}
        preview, _ = fetch(GH_REPO, {GH_API: js(payload)})
        self.assertEqual((preview.description, preview.language, preview.image), ("", "", None))

    def test_truncation(self):
        long = "字" * 500
        payload = {"full_name": long, "description": long, "language": long, "owner": {}}
        preview, _ = fetch(GH_REPO, {GH_API: js(payload)})
        self.assertEqual(preview.title, "字" * 119 + "…")
        self.assertEqual(preview.description, "字" * 199 + "…")
        self.assertEqual(preview.language, "字" * 29 + "…")

        head = f'<meta property="og:title" content="{"t" * 120}"><meta property="og:site_name" content="{"s" * 41}">'
        preview, _ = fetch(PAGE, {PAGE: page(head)})
        self.assertEqual(preview.title, "t" * 120)  # exactly at the limit: untouched
        self.assertEqual(preview.site, "s" * 39 + "…")

        routes = {YT_OEMBED: js({"title": "T", "author_name": "a" * 61})}
        self.assertEqual(fetch(f"https://youtu.be/{VIDEO}", routes)[0].author, "a" * 59 + "…")

    def test_html_text_is_cleaned_the_same_way(self):
        head = '<title>\n\tSpaced\u200b   out \u202e title\n</title><meta name="description" content="line one&#10;line two&#x202E;">'
        preview, _ = fetch(PAGE, {PAGE: page(head)})
        self.assertEqual((preview.title, preview.description), ("Spaced out title", "line one line two"))


class EntryTest(unittest.TestCase):
    def test_omits_empty_values(self):
        stored = entry(Preview(url=PAGE, kind="link", title="T"), None, "2026-11-24")
        self.assertEqual(stored, {"kind": "link", "title": "T", "fetched": "2026-11-24"})
        self.assertEqual(entry(Preview(url=PAGE, kind="link", image=PNG), "", "2026-11-24"), {"kind": "link", "fetched": "2026-11-24"})

    def test_all_fields(self):
        preview = Preview(
            url="https://b23.tv/AbC123x",
            kind="bilibili",
            title="T",
            description="D",
            site="哔哩哔哩",
            author="UP主",
            canonical=f"https://www.bilibili.com/video/{BVID}",
            stars=0,
            language="Python",
            image=PNG,
        )
        self.assertEqual(
            entry(preview, "/assets/notes/20261124-0123456789.webp", "2026-11-24"),
            {
                "kind": "bilibili",
                "title": "T",
                "description": "D",
                "site": "哔哩哔哩",
                "author": "UP主",
                "url": f"https://www.bilibili.com/video/{BVID}",
                "image": "/assets/notes/20261124-0123456789.webp",
                "stars": 0,
                "language": "Python",
                "fetched": "2026-11-24",
            },
        )

    def test_never_contains_the_image_bytes_or_note_url(self):
        stored = entry(Preview(url=PAGE, kind="github", title="T", stars=12, image=PNG), None, "2026-11-24")
        self.assertEqual(stored, {"kind": "github", "title": "T", "stars": 12, "fetched": "2026-11-24"})
        json.dumps(stored)

    def test_fetched_previews_make_valid_entries(self):
        long = "字" * 500
        payload = {"full_name": long, "description": long, "language": long, "stargazers_count": 10**9, "owner": {}}
        preview, _ = fetch(GH_REPO, {GH_API: js(payload)})
        text = merge_previews(None, {preview.url: entry(preview, "/assets/notes/20261124-0123456789.webp", "2026-11-24")})
        validate_previews(text)


class MergeTest(unittest.TestCase):
    A = {"kind": "link", "title": "标题 A", "site": "example.com", "fetched": "2026-11-20"}
    B = {"kind": "github", "title": "python/cpython", "stars": 65000, "language": "Python", "fetched": "2026-11-24"}

    def test_new_file(self):
        for raw in (None, ""):
            text = merge_previews(raw, {"https://example.com/a": self.A})
            self.assertEqual(json.loads(text), {"version": 1, "links": {"https://example.com/a": self.A}})
            self.assertTrue(text.endswith("}\n"))
            self.assertIn("标题 A", text)  # not \u-escaped
            validate_previews(text)
        self.assertEqual(json.loads(merge_previews(None, {})), {"version": 1, "links": {}})

    def test_deterministic(self):
        one = merge_previews(None, {"https://example.com/a": self.A, GH_REPO: self.B})
        two = merge_previews(None, {GH_REPO: dict(reversed(self.B.items())), "https://example.com/a": self.A})
        self.assertEqual(one, two)
        step = merge_previews(merge_previews(None, {GH_REPO: self.B}), {"https://example.com/a": self.A})
        self.assertEqual(step, one)
        self.assertEqual(merge_previews(one, {}), one)
        self.assertEqual(merge_previews(one, {"https://example.com/a": self.A}), one)
        self.assertEqual(
            one,
            "{\n"
            '  "links": {\n'
            '    "https://example.com/a": {\n'
            '      "fetched": "2026-11-20",\n'
            '      "kind": "link",\n'
            '      "site": "example.com",\n'
            '      "title": "标题 A"\n'
            "    },\n"
            '    "https://github.com/python/cpython": {\n'
            '      "fetched": "2026-11-24",\n'
            '      "kind": "github",\n'
            '      "language": "Python",\n'
            '      "stars": 65000,\n'
            '      "title": "python/cpython"\n'
            "    }\n"
            "  },\n"
            '  "version": 1\n'
            "}\n",
        )

    def test_keeps_existing_and_replaces_same_key(self):
        raw = merge_previews(None, {"https://example.com/a": self.A, GH_REPO: self.B})
        newer = {"kind": "link", "title": "新标题", "fetched": "2026-11-25"}
        links = json.loads(merge_previews(raw, {"https://example.com/a": newer, "https://example.com/c": self.A}))["links"]
        self.assertEqual(links, {"https://example.com/a": newer, GH_REPO: self.B, "https://example.com/c": self.A})

    def test_rejects_corrupt_raw(self):
        good = merge_previews(None, {"https://example.com/a": self.A})
        for raw in (
            "{",
            "null",
            "[]",
            " ",
            "\n",
            good[:-3],
            good.replace('"version": 1', '"version": 2'),
            good.replace('"kind": "link"', '"kind": "video"'),
            good.replace("https://example.com/a", "http://example.com/a"),
            "<<<<<<< HEAD\n" + good,
        ):
            with self.assertRaises(ValueError, msg=raw[:40]):
                merge_previews(raw, {"https://example.com/c": self.A})

    def test_rejects_invalid_new_entries(self):
        for url, item in (
            ("http://example.com/a", self.A),
            ("https://127.0.0.1/", self.A),
            ("https://example.com/a", {"title": "no kind"}),
            ("https://example.com/a", dict(self.A, fetched="today")),
            ("https://example.com/a", dict(self.A, image="/etc/passwd")),
            ("https://example.com/a", dict(self.A, extra=1)),
            ("https://example.com/a", "text"),
        ):
            with self.assertRaises(ValueError):
                merge_previews(None, {url: item})


class ValidateTest(unittest.TestCase):
    URL = "https://example.com/a"
    FULL = {
        "kind": "bilibili",
        "title": "T",
        "description": "D",
        "site": "哔哩哔哩",
        "author": "UP主",
        "url": f"https://www.bilibili.com/video/{BVID}?p=2",
        "image": "/assets/notes/20261124-0123456789.webp",
        "stars": 0,
        "language": "Python",
        "fetched": "2026-11-24",
    }

    def doc(self, item=None, url=None):
        links = {self.URL if url is None else url: self.FULL if item is None else item}
        return json.dumps({"version": 1, "links": links}, ensure_ascii=False)

    def changed(self, **changes):
        item = dict(self.FULL)
        for key, value in changes.items():
            if value is None:
                del item[key]
            else:
                item[key] = value
        return self.doc(item)

    def test_accepts(self):
        validate_previews('{"version": 1, "links": {}}')
        validate_previews(self.doc())
        validate_previews(self.doc({"kind": "link", "fetched": "2026-11-24"}))
        validate_previews(self.doc({"kind": "link"}))  # kind is the one required key
        for kind in ("link", "youtube", "bilibili", "x", "github"):
            validate_previews(self.changed(kind=kind))
        validate_previews(self.changed(title="t" * 121, description="d" * 201, site="s" * 41, author="a" * 61, language="l" * 31))
        validate_previews(self.changed(stars=10**9, title=""))
        validate_previews(self.doc(url="https://zh.wikipedia.org/wiki/中文"))

    def assert_invalid(self, raw):
        with self.assertRaises(ValueError, msg=str(raw)[:80]):
            validate_previews(raw)

    def test_rejects_wrong_shape(self):
        for raw in (
            "",
            "not json",
            "[]",
            "null",
            '"text"',
            "{}",
            '{"version": 1}',
            '{"links": {}}',
            '{"version": 2, "links": {}}',
            '{"version": true, "links": {}}',
            '{"version": 1.0, "links": {}}',
            '{"version": "1", "links": {}}',
            '{"version": 1, "links": []}',
            '{"version": 1, "links": null}',
            '{"version": 1, "links": {}, "extra": 1}',
            '{"version": 1, "links": {}} trailing',
            "[" * 100000,
            None,
            b'{"version": 1, "links": {}}',
        ):
            self.assert_invalid(raw)

    def test_rejects_bad_keys(self):
        for url in (
            "http://example.com/a",
            "https://127.0.0.1/",
            "https://localhost/",
            "example.com",
            "",
            "https://example.com/a b",
            "https://user@example.com/",
        ):
            self.assert_invalid(self.doc(url=url))

    def test_rejects_bad_entries(self):
        for item in ("text", None, [], 5, {}, {"fetched": "2026-11-24"}, {"title": "T", "fetched": "2026-11-24"}):
            self.assert_invalid(self.doc(item) if item is not None else '{"version": 1, "links": {"https://example.com/a": null}}')
        for changes in (
            {"kind": "video"},
            {"kind": None},
            {"kind": 1},
            {"kind": ["link"]},
            {"extra": "x"},
            {"canonical": "https://example.com/"},
            {"title": "t" * 122},
            {"description": "d" * 202},
            {"site": "s" * 42},
            {"author": "a" * 62},
            {"language": "l" * 32},
            {"title": 5},
            {"description": ["d"]},
            {"site": {"name": "s"}},
            {"image": "/assets/notes/20261124-0123456789.png"},
            {"image": "/assets/notes/20261124-0123456789.webp\n"},
            {"image": "/assets/notes/2026112-0123456789.webp"},
            {"image": "/assets/notes/20261124-012345678G.webp"},
            {"image": "/assets/notes/20261124-0123456789ab.webp"},
            {"image": "/assets/notes/２０２６１１２４-0123456789.webp"},
            {"image": "/assets/notes/../notes/20261124-0123456789.webp"},
            {"image": "assets/notes/20261124-0123456789.webp"},
            {"image": "https://example.com/assets/notes/20261124-0123456789.webp"},
            {"image": 5},
            {"url": "http://www.bilibili.com/video/x"},
            {"url": "https://127.0.0.1/"},
            {"url": "javascript:alert(1)"},
            {"url": 5},
            {"stars": -1},
            {"stars": 10**9 + 1},
            {"stars": True},
            {"stars": "5"},
            {"stars": 1.5},
            {"fetched": ""},
            {"fetched": "2026-1-1"},
            {"fetched": "20261124"},
            {"fetched": "2026-11-24\n"},
            {"fetched": "2026-11-24T00:00:00Z"},
            {"fetched": "２０２６-11-24"},
            {"fetched": 20261124},
        ):
            self.assert_invalid(self.changed(**changes))

    def test_rejects_oversized_file(self):
        item = {"kind": "link", "title": "t" * 100, "description": "d" * 200, "fetched": "2026-11-24"}
        links = {f"https://example.com/{n}": item for n in range(3000)}
        raw = json.dumps({"version": 1, "links": links})
        self.assertGreater(len(raw), 1024 * 1024)
        self.assert_invalid(raw)
        with self.assertRaises(ValueError):
            merge_previews(None, links)
        small = json.dumps({"version": 1, "links": dict(list(links.items())[:2000])})
        self.assertLess(len(small), 1024 * 1024)
        validate_previews(small)


if __name__ == "__main__":
    unittest.main()
