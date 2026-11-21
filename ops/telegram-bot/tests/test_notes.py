import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pnc_bot.notes import (
    DRAFT_HINT,
    HEADING_RE,
    NOTE_SEPARATOR,
    NOTES_ASSET_DIR,
    NOTES_PATH,
    POSTS_DIR,
    SITE_HEADING_PATTERN,
    SLUG_RE,
    STATIC_DIR,
    Note,
    append_to_note,
    draft_document,
    escape_mdx,
    image_markdown,
    insert_note,
    local_asset_path,
    make_slug,
    missing_note_assets,
    note_image_assets,
    note_summary,
    parse_draft_command,
    parse_notes,
    referenced_note_images,
    today_shanghai,
    validate_notes,
)
from pnc_bot.textfmt import entities_to_markdown, sanitize_body

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parents[2]  # ops/telegram-bot/tests → repo root, when run in the blog repo
FIXTURE_NOTES = TESTS_DIR / "fixtures" / "notes.md"


def notes_files() -> dict[str, str]:
    """The snapshot fixture, plus the live notes.md when the tests run inside the repo."""
    files = {"fixture": FIXTURE_NOTES.read_text(encoding="utf-8")}
    live = REPO_ROOT / NOTES_PATH
    if live.is_file():
        files["repo"] = live.read_text(encoding="utf-8")
    return files


# Reference output of the notes.ts algorithm (computed with node from the
# exact regex and id loop in src/lib/notes.ts) for tricky inputs.
NOTES_TS_SAMPLES = [
    (
        "# 头\n\n## 2026-05-01 | 三\n\nc\n\n## 2026-05-01\n\nb\n\n## 2026-04-30 | 前一天\n\nx\n\n## 2026-05-01 | 一\n\na\n",
        [("2026-05-01-3", "三", "c"), ("2026-05-01-2", "", "b"), ("2026-04-30", "前一天", "x"), ("2026-05-01", "一", "a")],
    ),
    (
        # Quirk of the regex: "\s*" after "|" runs over blank lines, so the title is the next line.
        "## 2026-04-11 | \n\nbody line\n\n## 2026-04-10\nno blank\n",
        [("2026-04-11", "body line", ""), ("2026-04-10", "", "no blank")],
    ),
    (
        "##\n2026-04-11\n\ntext\n\n##   2026-04-11   |   spaced title   \n\nmore\n",
        [("2026-04-11-2", "", "text"), ("2026-04-11", "spaced title", "more")],
    ),
    (
        "## 2026-04-11 | t\r\n\r\nbody\r\n## 2026-04-11\r\nb2\r\n",
        [("2026-04-11-2", "t", "body"), ("2026-04-11", "", "b2")],
    ),
    (
        "## 2026-04-11 not heading\n## 2026-04-111\n## 2026-04-11 | ok\n\nend",
        [("2026-04-11", "ok", "end")],
    ),
    ("intro only, no notes\n", []),
    (
        "## 2026-04-11\t|\ttab title\n\nz\n## 2026-04-11|nospace\n\nq\n",
        [("2026-04-11", "tab title", "z\n## 2026-04-11|nospace\n\nq")],
    ),
]

# More references from the notes.ts algorithm (node, same regex and id loop)
# for whitespace that JavaScript and Python disagree on: JS \s includes U+FEFF
# but not U+001C or U+0085, \d is ASCII, and \r, U+2028, U+2029 break lines.
NOTES_TS_WHITESPACE_SAMPLES = [
    (
        "## 2026-01-01\n\n正文\n##\ufeff2025-03-04\n后半段\n",
        [("2026-01-01", "", "正文"), ("2025-03-04", "", "后半段")],
    ),
    (
        "## 2026-01-01 |\u3000全角空格标题\n\nx\n##\u00a02026-01-02\n\ny\n",
        [("2026-01-01", "全角空格标题", "x"), ("2026-01-02", "", "y")],
    ),
    (
        "## 2026-01-01\n\nx\u2028## 2026-01-03 | 换行符\n\nz\n",
        [("2026-01-01", "", "x"), ("2026-01-03", "换行符", "z")],
    ),
    ("## 2026-01-01\n\nx\r## 2026-01-04\ry\n", [("2026-01-01", "", "x"), ("2026-01-04", "", "y")]),
    (
        "## 2026-01-01\n\nx\n##\x1c2026-01-05\nq\n## \uff12\uff10\uff12\uff16-01-06\nfull\n",
        [("2026-01-01", "", "x\n##\x1c2026-01-05\nq\n## \uff12\uff10\uff12\uff16-01-06\nfull")],
    ),
    (
        "## 2026-01-01 | t\x85\n\nb\n## 2026-01-01\x0b\n\nc\n",
        [("2026-01-01-2", "t\x85", "b"), ("2026-01-01", "", "c")],
    ),
    ("## 2026-01-01 | 标题\ufeff\n\n\ufeff正文\ufeff\n", [("2026-01-01", "标题", "正文")]),
    # The quirk behind NOTE_SEPARATOR: a title-less note whose body starts with "|".
    ("## 2026-01-01\n\n| a | b |\n|---|---|\n| 1 | 2 |\n", [("2026-01-01", "a | b |", "|---|---|\n| 1 | 2 |")]),
]

SHANGHAI_EVENING = datetime(2026, 11, 21, 14, 30, tzinfo=timezone.utc)  # 22:30 in Shanghai


class ParseNotes(unittest.TestCase):
    def test_matches_notes_ts_reference(self):
        for raw, expected in NOTES_TS_SAMPLES:
            with self.subTest(raw=raw[:30]):
                notes = parse_notes(raw)
                self.assertEqual([(n.id, n.title or "", n.body) for n in notes], expected)

    def test_matches_notes_ts_with_javascript_whitespace(self):
        for raw, expected in NOTES_TS_WHITESPACE_SAMPLES:
            with self.subTest(raw=raw[:30]):
                notes = parse_notes(raw)
                self.assertEqual([(n.id, n.title or "", n.body) for n in notes], expected)

    def test_offsets(self):
        raw = NOTES_TS_SAMPLES[0][0]
        notes = parse_notes(raw)
        for index, note in enumerate(notes):
            self.assertTrue(raw.startswith("## ", note.heading_start))
            self.assertLessEqual(note.heading_start, note.body_start)
            self.assertLessEqual(note.body_start, note.end)
            self.assertEqual(raw[note.body_start : note.end].strip(), note.body)
            if index + 1 < len(notes):
                self.assertEqual(note.end, notes[index + 1].heading_start)
        self.assertEqual(notes[-1].end, len(raw))

    def test_real_notes_file(self):
        for name, raw in notes_files().items():
            with self.subTest(file=name):
                notes = parse_notes(raw)
                self.assertTrue(notes)
                self.assertEqual(len({n.id for n in notes}), len(notes))
                for note in notes:
                    self.assertRegex(note.id, r"^\d{4}-\d{2}-\d{2}(-\d+)?$")
                    self.assertTrue(note.body)
        fixture = parse_notes(FIXTURE_NOTES.read_text(encoding="utf-8"))
        self.assertEqual([(n.id, n.title) for n in fixture], [("2026-04-11", "春天"), ("2026-04-07", "起步")])
        self.assertTrue(fixture[0].body.startswith("花粉，春雨"))
        self.assertIn("```", fixture[1].body)


class InsertNote(unittest.TestCase):
    def test_round_trip_on_real_notes(self):
        for name, raw in notes_files().items():
            with self.subTest(file=name):
                before = parse_notes(raw)
                body = sanitize_body("今天的随想\n\n# 小标题\n## 2026-01-01 假标题\n```\n# code\n```")
                new_raw, note_id = insert_note(raw, date="2099-01-01", title="测试", body=body)
                validate_notes(raw, new_raw, expect_added=1)
                after = parse_notes(new_raw)
                self.assertEqual(note_id, "2099-01-01")
                self.assertEqual(after[0].id, note_id)
                self.assertEqual(after[0].title, "测试")
                self.assertEqual(after[0].body, body)
                self.assertEqual([(n.id, n.title, n.body) for n in after[1:]], [(n.id, n.title, n.body) for n in before])
                # Everything before the first note (the page intro) is untouched.
                first = before[0].heading_start
                self.assertEqual(new_raw[:first], raw[:first])
                self.assertTrue(new_raw[first:].startswith("## 2099-01-01 | 测试\n\n今天的随想\n\n### 小标题\n\\## 2026-01-01"))
                self.assertTrue(new_raw.endswith(raw[first:]))

    def test_same_placement_as_new_note_script(self):
        raw = "# 每天随想\n\n> intro\n\n## 2026-04-11 | 春天\n\n花粉\n"
        new_raw, note_id = insert_note(raw, date="2026-04-12", title=None, body="  新的一天  ")
        self.assertEqual(new_raw, "# 每天随想\n\n> intro\n\n## 2026-04-12\n\n新的一天\n\n## 2026-04-11 | 春天\n\n花粉\n")
        self.assertEqual(note_id, "2026-04-12")

    def test_file_without_notes_appends(self):
        new_raw, note_id = insert_note("# 每天随想\n\nintro\n\n\n", date="2026-04-12", title="第一条", body="正文")
        self.assertEqual(new_raw, "# 每天随想\n\nintro\n\n## 2026-04-12 | 第一条\n\n正文\n\n")
        self.assertEqual(note_id, "2026-04-12")

    def test_several_notes_on_the_same_day(self):
        raw = "# h\n\n## 2026-05-01 | 早\n\na\n"
        ids = []
        for number in range(2, 5):
            previous = raw
            raw, note_id = insert_note(raw, date="2026-05-01", title=None, body=f"第 {number} 条")
            ids.append(note_id)
            validate_notes(previous, raw, expect_added=1)
        self.assertEqual(ids, ["2026-05-01-2", "2026-05-01-3", "2026-05-01-4"])
        notes = parse_notes(raw)
        self.assertEqual([n.id for n in notes], ["2026-05-01-4", "2026-05-01-3", "2026-05-01-2", "2026-05-01"])
        self.assertEqual(notes[-1].title, "早")  # the first note of the day keeps the bare date

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            insert_note("", date="2026-05-01", title=None, body="   \n ")
        with self.assertRaises(ValueError):
            insert_note("", date="2026/05/01", title=None, body="x")
        with self.assertRaises(ValueError):  # unsanitized body would add a second note
            insert_note("", date="2026-05-01", title=None, body="x\n\n## 2026-05-02\n\ny")

    def test_title_whitespace_is_collapsed(self):
        new_raw, _ = insert_note("", date="2026-05-01", title=" a \n b ", body="x")
        self.assertIn("## 2026-05-01 | a b\n", new_raw)

    def test_untitled_table_keeps_its_header_row(self):
        body = "| a | b |\n|---|---|\n| 1 | 2 |"
        new_raw, note_id = insert_note("# h\n", date="2026-11-21", title=None, body=body)
        self.assertIn(f"## 2026-11-21\n\n{NOTE_SEPARATOR}\n\n{body}\n", new_raw)
        added = next(n for n in parse_notes(new_raw) if n.id == note_id)
        self.assertIsNone(added.title)
        self.assertEqual(added.body, f"{NOTE_SEPARATOR}\n\n{body}")
        validate_notes("# h\n", new_raw, expect_added=1)
        for body in ("|| 分隔", "| 单独一行", "|", "| \n\n|"):
            with self.subTest(body=body):
                new_raw, note_id = insert_note("", date="2026-11-21", title=None, body=body)
                added = parse_notes(new_raw)[0]
                self.assertEqual((added.title, added.body), (None, f"{NOTE_SEPARATOR}\n\n{body}"))
        # With a title the heading line ends at the title: nothing to protect.
        new_raw, _ = insert_note("", date="2026-11-21", title="表格", body=body)
        self.assertEqual(parse_notes(new_raw)[0].body, body)
        self.assertNotIn(NOTE_SEPARATOR, new_raw)


class AppendToNote(unittest.TestCase):
    RAW = "# h\n\n## 2026-05-02 | 新\n\n第一段\n\n## 2026-05-01\n\n旧的\n"

    def test_append_to_middle_note_keeps_blank_line_before_next(self):
        new_raw = append_to_note(self.RAW, "2026-05-02", "补充\n\n![x](/assets/notes/a.webp)")
        self.assertEqual(
            new_raw,
            "# h\n\n## 2026-05-02 | 新\n\n第一段\n\n补充\n\n![x](/assets/notes/a.webp)\n\n## 2026-05-01\n\n旧的\n",
        )
        validate_notes(self.RAW, new_raw, expect_added=0)

    def test_append_to_last_note(self):
        new_raw = append_to_note(self.RAW, "2026-05-01", "  再补一句  ")
        self.assertEqual(new_raw, "# h\n\n## 2026-05-02 | 新\n\n第一段\n\n## 2026-05-01\n\n旧的\n\n再补一句\n")

    def test_append_to_note_with_empty_body(self):
        raw = "## 2026-05-02\n\n## 2026-05-01\n\nx\n"
        self.assertEqual(append_to_note(raw, "2026-05-02", "y"), "## 2026-05-02\n\ny\n\n## 2026-05-01\n\nx\n")

    def test_append_table_to_untitled_empty_note(self):
        raw = "## 2026-05-02\n\n## 2026-05-01\n\nx\n"
        self.assertEqual(append_to_note(raw, "2026-05-02", "|"), f"## 2026-05-02\n\n{NOTE_SEPARATOR}\n\n|\n\n## 2026-05-01\n\nx\n")
        new_raw = append_to_note(raw, "2026-05-02", "| a |\n|---|\n| 1 |")
        self.assertEqual(new_raw, f"## 2026-05-02\n\n{NOTE_SEPARATOR}\n\n| a |\n|---|\n| 1 |\n\n## 2026-05-01\n\nx\n")
        self.assertIsNone(parse_notes(new_raw)[0].title)
        validate_notes(raw, new_raw, expect_added=0)

    def test_append_on_real_notes(self):
        for name, raw in notes_files().items():
            with self.subTest(file=name):
                notes = parse_notes(raw)
                for note in notes:
                    new_raw = append_to_note(raw, note.id, "追加的一段")
                    validate_notes(raw, new_raw, expect_added=0)
                    updated = {n.id: n for n in parse_notes(new_raw)}
                    self.assertEqual(updated[note.id].body, note.body + "\n\n追加的一段")
                    for other in notes:
                        if other.id != note.id:
                            self.assertEqual(updated[other.id].body, other.body)

    def test_same_day_ids(self):
        raw = "## 2026-05-01\n\nb\n\n## 2026-05-01\n\na\n"
        self.assertEqual(append_to_note(raw, "2026-05-01-2", "c"), "## 2026-05-01\n\nb\n\nc\n\n## 2026-05-01\n\na\n")
        self.assertEqual(append_to_note(raw, "2026-05-01", "c"), "## 2026-05-01\n\nb\n\n## 2026-05-01\n\na\n\nc\n")

    def test_errors(self):
        with self.assertRaises(KeyError):
            append_to_note(self.RAW, "2026-05-03", "x")
        with self.assertRaises(ValueError):
            append_to_note(self.RAW, "2026-05-01", "  ")
        with self.assertRaises(ValueError):
            append_to_note(self.RAW, "2026-05-01", "x\n## 2026-05-03\ny")


class ValidateNotes(unittest.TestCase):
    RAW = "# h\n\n## 2026-05-01 | b\n\nB\n\n## 2026-05-01\n\nA\n"

    def test_valid(self):
        new_raw, _ = insert_note(self.RAW, date="2026-05-02", title="c", body="C")
        validate_notes(self.RAW, new_raw, expect_added=1)
        validate_notes(self.RAW, self.RAW, expect_added=0)

    def test_wrong_count(self):
        new_raw, _ = insert_note(self.RAW, date="2026-05-02", title="c", body="C")
        with self.assertRaisesRegex(ValueError, "随想条数"):
            validate_notes(self.RAW, new_raw, expect_added=0)
        with self.assertRaisesRegex(ValueError, "随想条数"):
            validate_notes(self.RAW, self.RAW, expect_added=1)
        removed = self.RAW.replace("## 2026-05-01 | b\n\nB\n\n", "")
        with self.assertRaisesRegex(ValueError, "随想条数"):
            validate_notes(self.RAW, removed, expect_added=0)

    def test_malformed_heading_lines(self):
        for line in ("## 2026-05-02 标题没有竖线", "## 2026/05/02", "## 2026-5-2 | x", "##  2026.05.02", "## 2026年5月2日"):
            with self.subTest(line=line):
                after = self.RAW.replace("## 2026-05-01 | b", f"{line}\n\n## 2026-05-01 | b")
                with self.assertRaisesRegex(ValueError, "格式不对"):
                    validate_notes(self.RAW, after, expect_added=0)

    def test_dotted_date_heading_inside_a_code_block_is_fine(self):
        body = sanitize_body("我的模板：\n```markdown\n## 2025.12.31 年终总结\n正文\n```")
        self.assertIn("\n## 2025.12.31 年终总结\n", body)  # code is left alone
        after, _ = insert_note(self.RAW, date="2026-05-02", title=None, body=body)
        validate_notes(self.RAW, after, expect_added=1)
        tilde = sanitize_body("~~~~\n## 2025/12/31\n~~~\n## 2025年12月\n~~~~~")
        after, _ = insert_note(self.RAW, date="2026-05-02", title=None, body=tilde)
        validate_notes(self.RAW, after, expect_added=1)

    def test_fence_does_not_leak_into_the_next_note(self):
        # An unclosed fence in one note ends at the next heading (notes.ts
        # renders each note on its own), so a typo after it is still caught.
        before = "## 2026-05-01\n\n```\nunclosed\n"
        after = "## 2026-05-02\n\n## 2026.05.02 typo\n\n" + before
        with self.assertRaisesRegex(ValueError, "格式不对"):
            validate_notes(before, after, expect_added=1)
        after = "## 2026-05-02\n\n```\n## 2026.05.02 in code\n```\n\n" + before
        validate_notes(before, after, expect_added=1)

    def test_existing_malformed_lines_do_not_block(self):
        before = self.RAW + "\n## 2026-05-01 old typo\n"
        after, _ = insert_note(before, date="2026-05-02", title=None, body="C")
        validate_notes(before, after, expect_added=1)

    def test_existing_id_changes_are_rejected(self):
        # A same-day note inserted *below* the existing one renumbers it.
        after = self.RAW.replace("## 2026-05-01\n\nA\n", "## 2026-05-01\n\nA\n\n## 2026-05-01\n\nolder\n")
        with self.assertRaisesRegex(ValueError, "链接会变"):
            validate_notes(self.RAW, after, expect_added=1)


class Helpers(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(NOTES_PATH, "src/content/notes.md")
        self.assertEqual(NOTES_ASSET_DIR, "src/static/assets/notes")
        self.assertEqual(POSTS_DIR, "src/content/posts")
        self.assertEqual(STATIC_DIR, "src/static")
        # HEADING_RE is this JavaScript pattern with JavaScript's \s, \d, ^, $ and .
        self.assertEqual(SITE_HEADING_PATTERN, r"^##\s+(\d{4}-\d{2}-\d{2})(?:\s+\|\s*(.+))?\s*$")
        self.assertTrue(HEADING_RE.flags & re.MULTILINE)
        notes_ts = REPO_ROOT / "src/lib/notes.ts"
        if notes_ts.is_file():
            self.assertIn(f"/{SITE_HEADING_PATTERN}/gm", notes_ts.read_text(encoding="utf-8"))

    def test_today_shanghai(self):
        self.assertEqual(today_shanghai(SHANGHAI_EVENING), "2026-11-21")
        self.assertEqual(today_shanghai(SHANGHAI_EVENING + timedelta(hours=2)), "2026-11-22")
        self.assertRegex(today_shanghai(), r"^\d{4}-\d{2}-\d{2}$")

    def test_image_markdown(self):
        self.assertEqual(
            image_markdown(["20261121-0123456789.webp", "20261121-abcdefabcd.webp"], "春天"),
            "![春天](/assets/notes/20261121-0123456789.webp)\n\n![春天](/assets/notes/20261121-abcdefabcd.webp)",
        )
        self.assertEqual(image_markdown(["a.webp"], "a [b] \\ c\nd", prefix="./"), "![a \\[b\\] \\\\ c d](./a.webp)")
        self.assertEqual(image_markdown([], "x"), "")
        with self.assertRaises(ValueError):
            image_markdown(["a b.webp"], "x")

    def test_note_summary(self):
        def note(title, body):
            return Note(id="2026-05-01-2", date="2026-05-01", title=title, heading_start=0, body_start=0, end=0, body=body)

        self.assertEqual(note_summary(note("春天", "x")), "春天")
        self.assertEqual(note_summary(note(None, "\n\n**花粉**，[春雨](https://x)\n第二行")), "花粉，春雨")
        self.assertEqual(note_summary(note(None, "![图](/assets/notes/a.webp)\n\n> 引用 `代码`")), "引用 代码")
        self.assertEqual(note_summary(note(None, "```\ncode\n```")), "code")
        self.assertEqual(note_summary(note(None, "![](/assets/notes/a.webp)")), "（图片）")
        self.assertEqual(note_summary(note(None, "")), "2026-05-01-2")
        self.assertEqual(note_summary(note(None, f"{NOTE_SEPARATOR}\n\n| a | b |")), "| a | b |")
        self.assertEqual(note_summary(note(None, "<strong>注意：</strong>今天下雨")), "注意：今天下雨")
        long = note_summary(note(None, "字" * 100))
        self.assertEqual(len(long), 40)
        self.assertTrue(long.endswith("…"))
        self.assertEqual(len(note_summary(note("t" * 50, ""), limit=10)), 10)

    def test_referenced_note_images(self):
        markdown = (
            "![a](/assets/notes/20261121-0123456789.webp)\n"
            "![b](/assets/notes/20261121-abcdefabcd.webp \"title\")\n"
            "again /assets/notes/20261121-0123456789.webp\n"
            "<img src=\"/assets/notes/x.png\">\n"
            "https://example.com/assets/notes/not-ours.webp\n"
            "![c](/assets/img/other.webp)"
        )
        self.assertEqual(
            referenced_note_images(markdown),
            ["20261121-0123456789.webp", "20261121-abcdefabcd.webp", "x.png"],
        )


class Slugs(unittest.TestCase):
    def test_ascii_words(self):
        self.assertEqual(make_slug("Hello, World!", set(), SHANGHAI_EVENING), "hello-world")
        self.assertEqual(make_slug("macOS 26 体验", set(), SHANGHAI_EVENING), "macos-26")
        self.assertEqual(make_slug("用 Tailscale 做 exit node", set(), SHANGHAI_EVENING), "tailscale-exit-node")

    def test_fallback_uses_shanghai_time(self):
        self.assertEqual(make_slug("纯中文标题", set(), SHANGHAI_EVENING), "post-20261121-2230")
        self.assertEqual(make_slug("C++", set(), SHANGHAI_EVENING), "post-20261121-2230")
        self.assertEqual(make_slug("", set(), datetime(2026, 11, 21, 17, 5, tzinfo=timezone.utc)), "post-20261122-0105")

    def test_max_length_at_word_boundary(self):
        slug = make_slug("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda", set(), SHANGHAI_EVENING)
        self.assertLessEqual(len(slug), 50)
        self.assertEqual(slug, "alpha-beta-gamma-delta-epsilon-zeta-eta-theta-iota")
        self.assertEqual(make_slug("x" * 80, set(), SHANGHAI_EVENING), "x" * 50)

    def test_collisions(self):
        existing = {"hello-world", "hello-world-2"}
        self.assertEqual(make_slug("Hello World", existing, SHANGHAI_EVENING), "hello-world-3")
        self.assertEqual(make_slug("中文", {"post-20261121-2230"}, SHANGHAI_EVENING), "post-20261121-2230-2")

    def test_results_match_slug_regex(self):
        for title in ("Ünïcödé façade", "--a--", "2026 年总结", "ABC", "a_b_c", "🎉 Party 🎉"):
            with self.subTest(title=title):
                self.assertRegex(make_slug(title, set(), SHANGHAI_EVENING), SLUG_RE)


class DraftCommand(unittest.TestCase):
    def test_title_only(self):
        self.assertEqual(parse_draft_command("  我的文章  "), ("我的文章", None, ""))

    def test_title_slug_and_body(self):
        self.assertEqual(
            parse_draft_command("我的文章 | my-post\n\n第一段\n第二段\n"),
            ("我的文章", "my-post", "第一段\n第二段"),
        )
        self.assertEqual(parse_draft_command("标题|slug-2"), ("标题", "slug-2", ""))
        self.assertEqual(parse_draft_command("标题 | \n正文"), ("标题", None, "正文"))

    def test_invalid(self):
        for arg in ("", "  \n ", "标题 | Bad Slug", "标题 | -dash", "标题 | ../x", "字" * 61, " | slug", "标题 | " + "a" * 101):
            with self.subTest(arg=arg[:20]), self.assertRaises(ValueError):
                parse_draft_command(arg)


NEW_POST_TEMPLATE = """---
title: "我的文章"
date: "2026-11-21"
tags: []
excerpt: ""
draft: true
---

{/* 图片放在同一目录，用 ![说明](./image.png) 引用。 */}

在这里写正文。
"""


class DraftDocument(unittest.TestCase):
    def test_empty_body_matches_new_post_script(self):
        self.assertEqual(draft_document("我的文章", "2026-11-21", "", []), NEW_POST_TEMPLATE)

    def test_matches_live_new_post_script(self):
        script = REPO_ROOT / "scripts/new-post.mjs"
        if not script.is_file():
            self.skipTest("scripts/new-post.mjs is not available outside the blog repo")
        source = script.read_text(encoding="utf-8")
        template = re.search(r"writeFile\(file, `(.*?)`, \{ flag", source, re.S).group(1)
        for title in ("我的文章", 'He said "hi" \\ ok', "Tab\there"):
            with self.subTest(title=title):
                expected = template.replace("${JSON.stringify(title)}", json.dumps(title, ensure_ascii=False))
                expected = expected.replace("${today}", "2026-11-21")
                self.assertNotIn("${", expected)
                self.assertEqual(draft_document(title, "2026-11-21", "", []), expected)

    def test_body_and_images(self):
        document = draft_document("T", "2026-11-21", "第一段\n\n第二段", ["20261121-0123456789.webp"])
        self.assertTrue(document.endswith(f"{DRAFT_HINT}\n\n第一段\n\n第二段\n\n![](./20261121-0123456789.webp)\n"))
        images_only = draft_document("T", "2026-11-21", "", ["a.webp", "b.webp"])
        self.assertTrue(images_only.endswith("在这里写正文。\n\n![](./a.webp)\n\n![](./b.webp)\n"))

    def test_title_is_json_quoted(self):
        document = draft_document('引号 "x" 和 \\', "2026-11-21", "", [])
        self.assertIn('title: "引号 \\"x\\" 和 \\\\"\n', document)

    def test_body_is_escaped_for_mdx(self):
        document = draft_document("T", "2026-11-21", "a < b {x}\n`{code}` and $\\frac{a}{b}$", [])
        self.assertIn("a &lt; b &#123;x&#125;\n`{code}` and $\\frac{a}{b}$", document)

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            draft_document("", "2026-11-21", "", [])
        with self.assertRaises(ValueError):
            draft_document("T", "21/11/2026", "", [])
        with self.assertRaises(ValueError):
            draft_document("T", "2026-11-21", "", ["a b.webp"])


class EscapeMdx(unittest.TestCase):
    def test_plain_text(self):
        self.assertEqual(escape_mdx("a < b, {c} > d"), "a &lt; b, &#123;c&#125; > d")
        self.assertEqual(escape_mdx("<!-- comment --> <div>"), "&lt;!-- comment --> &lt;div>")

    def test_backslash_escapes_become_references(self):
        self.assertEqual(escape_mdx("\\{ok\\} \\<b> \\* \\\\{"), "&#123;ok&#125; &lt;b> \\* \\\\&#123;")

    def test_code_and_math_untouched(self):
        text = "```js\nconst a = {b: 1} < 2;\n```\n~~~\n<x>\n~~~\n$$\n\\{x\\}\n{y}\n$$\n``a {`` b {"
        self.assertEqual(escape_mdx(text), text[:-1] + "&#123;")

    def test_unclosed_backtick_is_literal(self):
        self.assertEqual(escape_mdx("a ` {b}"), "a ` &#123;b&#125;")

    def test_import_and_export_lines_are_not_esm(self):
        # MDX reads a line starting "import " / "export " as JavaScript; a
        # character reference renders the same letter.
        self.assertEqual(escape_mdx("配置一下：\n\nexport PATH=$HOME/bin:$PATH"), "配置一下：\n\n&#101;xport PATH=$HOME/bin:$PATH")
        self.assertEqual(escape_mdx("import 了一些照片"), "&#105;mport 了一些照片")
        self.assertEqual(escape_mdx("export\tX=1\n  export X"), "&#101;xport\tX=1\n  export X")
        self.assertEqual(escape_mdx("important exports"), "important exports")
        self.assertEqual(escape_mdx("```sh\nexport PATH=x\n```"), "```sh\nexport PATH=x\n```")

    def test_backtick_fence_with_backtick_in_info_is_not_a_fence(self):
        self.assertEqual(escape_mdx("```npm i``` 很好用\n然后 {"), "```npm i``` 很好用\n然后 &#123;")
        self.assertEqual(escape_mdx("~~~a`b\n{x}\n~~~"), "~~~a`b\n{x}\n~~~")  # tilde fences allow it

    def test_fence_closers_and_indentation(self):
        # MDX has no indented code: fences may be indented any amount.
        self.assertEqual(escape_mdx("para\n      ```\n{x}\n   ```\n{y}"), "para\n      ```\n{x}\n   ```\n&#123;y&#125;")
        self.assertEqual(escape_mdx("````\n```\n{x}\n````\n{y}"), "````\n```\n{x}\n````\n&#123;y&#125;")
        self.assertEqual(escape_mdx("```\n{x}\n``` x\n{y}\n```"), "```\n{x}\n``` x\n{y}\n```")
        self.assertEqual(escape_mdx("$$ meta\n{x}\n$$$\n{y}"), "$$ meta\n{x}\n$$$\n&#123;y&#125;")

    def test_unsure_structure_escapes_everything_after(self):
        cases = {
            # A fence in a list item ends with the item: {y} is text in MDX.
            "- ```\n  {x}\n  ```\n  {y}": "- ```\n  &#123;x&#125;\n  ```\n  &#123;y&#125;",
            "- a\n  ```\n{x}": "- a\n  ```\n&#123;x&#125;",
            "> ```\n> {x}\n> ```": "> ```\n> &#123;x&#125;\n> ```",
            "1. ```\n   {x}\n   ```\n   {y}": "1. ```\n   &#123;x&#125;\n   ```\n   &#123;y&#125;",
            "\t```\n{x}\n\t```": "\t```\n&#123;x&#125;\n\t```",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(escape_mdx(text), expected)
        # A fence at the start of a line after a list is top level: certain.
        self.assertEqual(escape_mdx("- a\n\n```\n{x}\n```\n{y}"), "- a\n\n```\n{x}\n```\n&#123;y&#125;")

    def test_inline_code_is_kept_only_where_certain(self):
        cases = {
            # A code span may continue on the next line of the paragraph.
            "a `b\nc` {x} `d`": "a `b\nc` &#123;x&#125; `d`",
            # A backtick inside a link destination is not a code span.
            "[a](`b) {x} `c`": "[a](`b) &#123;x&#125; `c`",
            "[a]: `b\n{x} `c`": "[a]: `b\n&#123;x&#125; `c`",
            # A GFM autolink literal may swallow the backtick.
            "see https://x.y/` {z} `b`": "see https://x.y/` &#123;z&#125; `b`",
            # Table cells split at | even inside code spans.
            "| `a | {x}` |\n| - |": "| `a | &#123;x&#125;` |\n| - |",
            # Runs pair by length only.
            "$ {a} $$": "$ &#123;a&#125; $$",
            "$$ {x} $ y": "$$ &#123;x&#125; $ y",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(escape_mdx(text), expected)
        self.assertEqual(escape_mdx("`{a}` and ``b ` {c}`` and $\\frac{a}{b}$ {d}"), "`{a}` and ``b ` {c}`` and $\\frac{a}{b}$ &#123;d&#125;")
        self.assertEqual(escape_mdx("`{a}`\n\n[x](y) `{b}`"), "`{a}`\n\n[x](y) `&#123;b&#125;`")  # per paragraph
        self.assertEqual(escape_mdx("示例：`[a](b) {c}`"), "示例：`[a](b) {c}`")  # "](" inside code is code

    def test_emphasis_tags(self):
        # Plain text inside: stays JSX. Anything else could cross Markdown
        # (a link, emphasis…): back to the Markdown markers.
        self.assertEqual(escape_mdx("<strong>注意：</strong>今天 <em>a</em> <del>b</del>"), "<strong>注意：</strong>今天 <em>a</em> <del>b</del>")
        self.assertEqual(escape_mdx("<strong>[链接](https://x.y/)</strong>后面"), "**[链接](https://x.y/)**后面")
        self.assertEqual(escape_mdx("<strong>a [b</strong> c](d)"), "**a [b** c](d)")
        self.assertEqual(escape_mdx("<strong>a {b}</strong>"), "**a &#123;b&#125;**")
        self.assertEqual(escape_mdx("use <strong> for bold"), "use &lt;strong> for bold")
        self.assertEqual(escape_mdx("</em> and <em></em>"), "&lt;/em> and &lt;em>&lt;/em>")
        self.assertEqual(escape_mdx("`<strong>x</strong>`"), "`<strong>x</strong>`")

    def test_after_a_url(self):
        # A GFM autolink literal takes a backslash into the URL, so "\\<"
        # right after a URL would leave a bare "<" (JSX). References leave none.
        self.assertEqual(escape_mdx("https://x.y/<em>"), "https://x.y/&lt;em>")
        self.assertEqual(escape_mdx("www.x.y/\\</del> {a}"), "www.x.y/&lt;/del> &#123;a&#125;")
        for text in ("https://x.y/<em>", "www.x.\\</del>%</b>", "https://x.y/\\<!-->", "a@b.co{x}"):
            with self.subTest(text=text):
                escaped = escape_mdx(text)
                self.assertFalse(set(escaped) & set("{}<"), escaped)

    def test_lone_carriage_returns_break_lines(self):
        self.assertEqual(escape_mdx("```\r{x}\r```\r{y}"), "```\n{x}\n```\n&#123;y&#125;")


class DraftImages(unittest.TestCase):
    ATTACHED = "20261121-0123456789.webp"

    def draft(self, body, images=()):
        return draft_document("游记", "2026-11-21", body, list(images))

    def test_missing_relative_images_are_rejected(self):
        for body in (
            "第一天\n\n![图](./later.png)",
            "![图](later.png)",
            "![图](../other-post/a.png)",
            "![图](<./my photo.png>)",
            "![图](./later.png \"title\")",
            "![图](\n./later.png)",
            "[![图](./later.png)](https://x.y/)",
            "![图][ref]\n\n[ref]: ./later.png",
            "![ref]\n\n> [ref]: ./later.png",
            "![图](#top)",
            "![图](file:///etc/passwd)",
            "![图](http://256.1.1.1/a.png)",
            "![图](http://x.y:99999/a.png)",
            "![图](%2Fassets/a.png)",
            "<strong>![图](./later.png)</strong>",
        ):
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, "草稿引用的图片不存在"):
                self.draft(body, [self.ATTACHED])

    def test_malformed_percent_encoding_is_rejected(self):
        for body in ("![a](https://x.y/50%off.png)", "![a](/assets/%E4.png)", "![a][r]\n\n[r]: /a%zz.png"):
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, "% 编码"):
                self.draft(body)

    def test_allowed_images(self):
        for body in (
            f"![图](./{self.ATTACHED})",
            "![图](https://example.com/a.png)",
            "![图](https://example.com:8443/a%20b.png?x=1#y)",
            "![图](http://1.2.3.4/a.png)",
            "![图](/assets/img/cover.png)",
            "![图](data:image/png;base64,AAAA)",
            "![图](https://example.com/a b.png)",  # not an image: a raw destination has no spaces
            "[链接](./not-an-image.md) 和 \\![不是图](./x.png)",
            "```md\n![示例](./example.png)\n```",
            "示例：`![示例](./example.png)`",
        ):
            with self.subTest(body=body):
                self.draft(body, [self.ATTACHED])

    def test_attached_images_are_appended(self):
        document = self.draft("正文", [self.ATTACHED])
        self.assertTrue(document.endswith(f"正文\n\n![](./{self.ATTACHED})\n"))


class NoteAssets(unittest.TestCase):
    PNG = b"\x89PNG\r\n\x1a\n" + bytes(16)

    def test_local_asset_path_mirrors_note_images_ts(self):
        cases = {
            "/assets/notes/x.webp": "assets/notes/x.webp",
            "/assets/img/a%20b.png?v=1#x": "assets/img/a b.png",
            "/assets/img/%E5%9B%BE.png": "assets/img/图.png",
            "/assets/a%2Fb.png": "assets/a%2Fb.png",  # decodeURI keeps reserved characters encoded
            "/assets/../secret.png": None,
            "/assets//x.png": None,
            "/assets/./x.png": None,
            "/assets/x%zz.png": None,
            "/img/x.png": None,
            "https://x.y/assets/x.png": None,
            "assets/x.png": None,
        }
        for src, expected in cases.items():
            with self.subTest(src=src):
                self.assertEqual(local_asset_path(src), expected)

    def test_note_image_assets(self):
        markdown = (
            "![a](/assets/notes/20261121-0123456789.webp)\n"
            "![b](/assets/img/other.webp \"t\") ![b again](/assets/img/other.webp)\n"
            "![c](https://x.y/assets/img/remote.png) [link](/assets/img/doc.pdf)\n"
            "![d](</assets/img/图 片.png>) ![e][r]\n\n[r]: </assets/img/ref.png>\n"
            "![f](/assets/img/a\\_b.png)"
        )
        self.assertEqual(
            note_image_assets(markdown),
            [
                "assets/notes/20261121-0123456789.webp",
                "assets/img/other.webp",
                "assets/img/图 片.png",
                "assets/img/a_b.png",
                "assets/img/ref.png",  # definitions come after inline images
            ],
        )

    def test_missing_note_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            static = Path(tmp) / "src/static"
            (static / "assets/img").mkdir(parents=True)
            (static / "assets/img/cover.png").write_bytes(self.PNG)
            (static / "assets/img/fake.png").write_text("not an image", encoding="utf-8")
            (static / "assets/img/icon.svg").write_text('<?xml version="1.0"?>\n<svg xmlns="x"/>', encoding="utf-8")
            (static / "assets/img/folder.png").mkdir()
            markdown = (
                "今天换了头像 ![](/assets/img/avatr.png)\n\n"
                "![](/assets/img/cover.png) ![](/assets/img/icon.svg) ![](/assets/img/fake.png)\n"
                "![](/assets/img/folder.png) ![](https://x.y/a.png) ![](/assets/../x.png)"
            )
            self.assertEqual(
                missing_note_assets(markdown, static),
                ["/assets/img/avatr.png", "/assets/img/fake.png", "/assets/img/folder.png"],
            )
            self.assertEqual(missing_note_assets("![x](/assets/old-photo.png)", static), ["/assets/old-photo.png"])
            self.assertEqual(missing_note_assets("![](/assets/img/cover.png)", static), [])
            self.assertEqual(missing_note_assets("![](/assets/img/cover.png)", Path(tmp) / "nowhere"), ["/assets/img/cover.png"])

    def test_on_bot_output(self):
        # What the owner might type in a note: Markdown is passed through.
        text = "今天换了头像 ![](/assets/img/avatr.png)"
        self.assertEqual(note_image_assets(entities_to_markdown(text, [])), ["assets/img/avatr.png"])


if __name__ == "__main__":
    unittest.main()
