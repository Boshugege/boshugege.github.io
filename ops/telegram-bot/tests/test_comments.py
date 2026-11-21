import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pnc_bot import comments
from pnc_bot.comments import (
    CommentPoller,
    CommentStore,
    PostTitles,
    comment_buttons,
    format_comment_list,
    format_new_comment,
)
from pnc_bot.state import State

from tests.runtime_fakes import make_blog, make_comments_db


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "pnc.db"
        make_comments_db(
            self.db,
            [
                {"content": "first"},
                {"content": "second", "status": "hidden"},
                {"content": "third", "post_slug": "/posts/other.html"},
            ],
        )
        self.store = CommentStore(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def statuses(self):
        conn = sqlite3.connect(self.db)
        try:
            return dict(conn.execute("SELECT id, status FROM comments").fetchall())
        finally:
            conn.close()

    def test_reads(self):
        self.assertEqual(self.store.max_id(), 3)
        self.assertEqual([c.id for c in self.store.since(1)], [2, 3])
        self.assertEqual([c.id for c in self.store.since(0, limit=1)], [1])
        self.assertEqual([c.id for c in self.store.latest(2)], [3, 2])
        self.assertEqual(self.store.get(2).status, "hidden")
        self.assertIsNone(self.store.get(99))

    def test_empty_table(self):
        empty = Path(self.tmp.name) / "empty.db"
        make_comments_db(empty, [])
        self.assertEqual(CommentStore(empty).max_id(), 0)

    def test_reads_use_read_only_connection(self):
        real_connect = sqlite3.connect
        calls = []

        def spy(*args, **kwargs):
            calls.append((args, kwargs))
            return real_connect(*args, **kwargs)

        with mock.patch.object(comments.sqlite3, "connect", spy):
            self.store.max_id()
            self.store.since(0)
            self.store.latest()
            self.store.get(1)
        self.assertEqual(len(calls), 4)
        for args, kwargs in calls:
            self.assertTrue(args[0].startswith("file:"))
            self.assertTrue(args[0].endswith("?mode=ro"))
            self.assertTrue(kwargs.get("uri"))

    def test_read_connection_cannot_write(self):
        conn = self.store._read()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("UPDATE comments SET status = 'hidden'")
        finally:
            conn.close()
        self.assertEqual(self.statuses()[1], "visible")

    def test_path_with_special_characters(self):
        odd = Path(self.tmp.name) / "a?b#c%d.db"
        make_comments_db(odd, [{"content": "x"}])
        self.assertEqual(CommentStore(odd).max_id(), 1)

    def test_hide_only_visible_rows(self):
        self.assertTrue(self.store.hide(1))
        self.assertEqual(self.statuses(), {1: "hidden", 2: "hidden", 3: "visible"})
        self.assertFalse(self.store.hide(1))  # already hidden
        self.assertFalse(self.store.hide(2))  # was hidden
        self.assertFalse(self.store.hide(99))  # missing
        self.assertEqual(self.statuses(), {1: "hidden", 2: "hidden", 3: "visible"})

    def test_hide_does_not_touch_other_states(self):
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE comments SET status = 'spam' WHERE id = 3")
        conn.commit()
        conn.close()
        self.assertFalse(self.store.hide(3))
        self.assertEqual(self.statuses()[3], "spam")


class FormatTest(unittest.TestCase):
    def comment(self, **values):
        base = dict(id=7, post_slug="/posts/hello.html", author_name="读者", content="写得好", status="visible",
                    created_at="2026-11-21T14:03:00.000Z")
        base.update(values)
        return comments.Comment(**base)

    def test_new_comment(self):
        html = format_new_comment(self.comment(), "https://example.test", "你好 <世界>")
        self.assertIn("💬 <b>新评论 #7</b>", html)
        self.assertIn('<a href="https://example.test/posts/hello.html">你好 &lt;世界&gt;</a>', html)
        self.assertIn("2026-11-21 22:03", html)

    def test_escaping_and_length(self):
        html = format_new_comment(
            self.comment(author_name="<b>evil</b>", content="<script>alert(1)</script>&" + "长" * 2000),
            "https://example.test",
            None,
        )
        self.assertNotIn("<script>", html)
        self.assertNotIn("<b>evil</b>", html)
        self.assertIn("&lt;b&gt;evil&lt;/b&gt;", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertLess(html.count("长"), 801)

    def test_bad_slug_is_not_linked(self):
        html = format_new_comment(self.comment(post_slug='javascript:alert(1)"'), "https://example.test", None)
        self.assertNotIn("<a ", html)
        self.assertIn("javascript:alert(1)&quot;", html)

    def test_buttons(self):
        self.assertEqual(comment_buttons(self.comment()), [[("隐藏这条", "dc:7")]])
        self.assertIsNone(comment_buttons(self.comment(status="hidden")))

    def test_list_marks_hidden(self):
        html = format_comment_list(
            [self.comment(), self.comment(id=6, status="hidden", content="<x>")], "https://example.test", None
        )
        self.assertIn("#7", html)
        self.assertIn("#6</b> 🙈 已隐藏", html)
        self.assertIn("&lt;x&gt;", html)
        self.assertEqual(format_comment_list([], "https://example.test", None), "还没有评论。")


class TitlesTest(unittest.TestCase):
    def test_titles_from_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            blog = make_blog(Path(tmp), ["20261121-100000-aaaaaaaaaaaa"], current="20261121-100000-aaaaaaaaaaaa")
            (blog / "current" / "index.json").write_text(
                json.dumps([{"url": "posts/hello.html", "title": "你好"}, {"url": "../x", "title": "bad"}])
            )
            titles = PostTitles(blog)
            self.assertEqual(titles.get("/posts/hello.html"), "你好")
            self.assertIsNone(titles.get("/posts/missing.html"))
            self.assertIsNone(titles.get("/../x"))

    def test_missing_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(PostTitles(Path(tmp)).get("/posts/a.html"))


class PollerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "pnc.db"
        make_comments_db(self.db, [{"content": "old 1"}, {"content": "old 2"}])
        self.state = State(Path(self.tmp.name) / "state")
        self.sent = []
        self.result = True
        self.poller = CommentPoller(CommentStore(self.db), self.state, self.notify)

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def notify(self, comment):
        self.sent.append(comment.id)
        return self.result

    def add(self, n):
        conn = sqlite3.connect(self.db)
        for i in range(n):
            conn.execute(
                "INSERT INTO comments (post_slug, author_name, content, created_at) VALUES (?, ?, ?, ?)",
                ("/posts/hello.html", "a", f"new {i}", "2026-11-21T00:00:00Z"),
            )
        conn.commit()
        conn.close()

    def test_first_run_has_no_backlog(self):
        self.assertEqual(self.poller.poll_once(), 0)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.state.get_int("comments_last_id"), 2)

    def test_new_comments_notified_in_order(self):
        self.poller.poll_once()
        self.add(2)
        self.assertEqual(self.poller.poll_once(), 2)
        self.assertEqual(self.sent, [3, 4])
        self.assertEqual(self.state.get_int("comments_last_id"), 4)
        self.assertEqual(self.poller.poll_once(), 0)

    def test_transient_failure_retried(self):
        self.poller.poll_once()
        self.add(2)
        self.result = False
        self.assertEqual(self.poller.poll_once(), 0)
        self.assertEqual(self.state.get_int("comments_last_id"), 2)
        self.result = True
        self.assertEqual(self.poller.poll_once(), 2)
        self.assertEqual(self.sent, [3, 3, 4])

    def test_database_reset(self):
        self.state.set("comments_last_id", 50)
        self.poller.poll_once()
        self.assertEqual(self.state.get_int("comments_last_id"), 2)
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
