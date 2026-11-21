import unittest

from pnc_bot.textfmt import clean_title, entities_to_markdown, sanitize_body, split_title


def utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def ent(kind: str, text: str, part: str, occurrence: int = 0, **extra) -> dict:
    """Entity of ``kind`` covering the ``occurrence``-th ``part`` in ``text``."""
    index = -1
    for _ in range(occurrence + 1):
        index = text.index(part, index + 1)
    return {"type": kind, "offset": utf16(text[:index]), "length": utf16(part), **extra}


class EntitiesBasics(unittest.TestCase):
    def test_no_entities_returns_text_unchanged(self):
        text = "随便写写 *不转义* [x](y) ## 2026"
        self.assertEqual(entities_to_markdown(text, None), text)
        self.assertEqual(entities_to_markdown(text, []), text)
        self.assertEqual(entities_to_markdown("", [{"type": "bold", "offset": 0, "length": 3}]), "")

    def test_bold_italic_strikethrough(self):
        text = "a bold b italic c gone d"
        entities = [ent("bold", text, "bold"), ent("italic", text, "italic"), ent("strikethrough", text, "gone")]
        self.assertEqual(entities_to_markdown(text, entities), "a **bold** b *italic* c ~~gone~~ d")

    def test_utf16_offsets_after_emoji(self):
        text = "😀 hello world"
        # 😀 is two UTF-16 code units, so "hello" starts at offset 3.
        entities = [{"type": "bold", "offset": 3, "length": 5}]
        self.assertEqual(entities_to_markdown(text, entities), "😀 **hello** world")

    def test_utf16_offsets_with_cjk_and_several_emoji(self):
        text = "今天👍🏽很开心🎉，代码 x=1 写完了"
        entities = [ent("bold", text, "很开心🎉"), ent("code", text, "x=1"), ent("italic", text, "写完了")]
        self.assertEqual(
            entities_to_markdown(text, entities),
            "今天👍🏽**很开心🎉**，代码 `x=1` *写完了*",
        )

    def test_entity_covering_an_emoji_only(self):
        text = "a🎉b"
        self.assertEqual(entities_to_markdown(text, [{"type": "bold", "offset": 1, "length": 2}]), "a**🎉**b")

    def test_offset_inside_surrogate_pair_does_not_split_the_emoji(self):
        text = "a🎉b"
        result = entities_to_markdown(text, [{"type": "bold", "offset": 2, "length": 2}])
        result.encode("utf-8")  # no lone surrogates
        self.assertIn("🎉", result)
        self.assertEqual(result.replace("*", ""), text)

    def test_malformed_entities_are_ignored(self):
        text = "hello"
        entities = [
            {"type": "bold", "offset": 10, "length": 3},
            {"type": "bold", "offset": -1, "length": 3},
            {"type": "bold", "offset": 0, "length": 0},
            {"type": "bold", "offset": "0", "length": 2},
            {"type": "bold", "offset": True, "length": 2},
            {"type": "bold"},
            "not a dict",
            {"type": 5, "offset": 0, "length": 5},
        ]
        self.assertEqual(entities_to_markdown(text, entities), "hello")

    def test_entity_running_past_the_end_is_clipped(self):
        self.assertEqual(entities_to_markdown("hi you", [{"type": "bold", "offset": 3, "length": 50}]), "hi **you**")


class EntitiesNesting(unittest.TestCase):
    def test_italic_inside_bold(self):
        text = "bold and italic"
        entities = [ent("bold", text, text), ent("italic", text, "italic")]
        self.assertEqual(entities_to_markdown(text, entities), "**bold and *italic***")

    def test_order_of_entities_in_the_list_does_not_matter(self):
        text = "bold and italic"
        entities = [ent("italic", text, "italic"), ent("bold", text, text)]
        self.assertEqual(entities_to_markdown(text, entities), "**bold and *italic***")

    def test_same_range_bold_and_italic(self):
        text = "x both y"
        entities = [ent("bold", text, "both"), ent("italic", text, "both")]
        self.assertEqual(entities_to_markdown(text, entities), "x ***both*** y")

    def test_link_containing_bold(self):
        text = "see the docs now"
        entities = [ent("text_link", text, "the docs", url="https://example.com/a"), ent("bold", text, "docs")]
        self.assertEqual(entities_to_markdown(text, entities), "see [the **docs**](https://example.com/a) now")

    def test_partial_overlap_is_clipped_without_losing_text(self):
        text = "abcdef"
        entities = [{"type": "bold", "offset": 0, "length": 4}, {"type": "italic", "offset": 2, "length": 4}]
        result = entities_to_markdown(text, entities)
        self.assertEqual(result.replace("*", ""), text)

    def test_whitespace_moves_outside_markers(self):
        text = "a  spaced  b"
        self.assertEqual(entities_to_markdown(text, [ent("bold", text, " spaced  ")]), "a  **spaced**  b")
        self.assertEqual(entities_to_markdown("x   y", [{"type": "bold", "offset": 1, "length": 3}]), "x   y")

    def test_multi_line_bold_is_wrapped_per_line(self):
        text = "one\n\ntwo"
        self.assertEqual(entities_to_markdown(text, [ent("bold", text, text)]), "**one**\n\n**two**")

    def test_emphasis_keeps_list_and_quote_markers_in_front(self):
        text = "- item\n> said"
        self.assertEqual(entities_to_markdown(text, [ent("bold", text, text)]), "- **item**\n> **said**")


class EntitiesCjkEmphasis(unittest.TestCase):
    """Markers that CommonMark would not pair become HTML tags.

    Every expected string was rendered with the site's Markdown processor
    (createMarkdownProcessor, as src/lib/notes.ts uses it): none shows a
    literal marker.
    """

    def check(self, text, entities, expected):
        self.assertEqual(entities_to_markdown(text, entities), expected)

    def test_punctuation_inside_the_marker_and_a_letter_outside(self):
        text = "注意：今天下雨"
        self.check(text, [ent("bold", text, "注意：")], "<strong>注意：</strong>今天下雨")
        text = "他说「你好」然后走了"
        self.check(text, [ent("bold", text, "「你好」")], "他说<strong>「你好」</strong>然后走了")
        text = "斜体。后面"
        self.check(text, [ent("italic", text, "斜体。")], "<em>斜体。</em>后面")
        text = "注意：今天"
        self.check(text, [ent("strikethrough", text, "注意：")], "<del>注意：</del>今天")
        text = "今天很开心。明天再说"
        self.check(text, [ent("bold", text, "今天很开心。")], "<strong>今天很开心。</strong>明天再说")
        text = "他说「你好」，然后"
        self.check(text, [ent("bold", text, "「你好」")], "他说<strong>「你好」</strong>，然后")

    def test_bold_link_followed_by_text(self):
        text = "链接后面"
        entities = [ent("bold", text, "链接"), ent("text_link", text, "链接", url="https://x.y/")]
        self.check(text, entities, "<strong>[链接](https://x.y/)</strong>后面")

    def test_markers_stay_where_they_work(self):
        text = "注意今天下雨"
        self.check(text, [ent("bold", text, "注意")], "**注意**今天下雨")
        text = "注意： 今天"
        self.check(text, [ent("bold", text, "注意：")], "**注意：** 今天")
        text = "「全部加粗」"
        self.check(text, [ent("bold", text, text)], "**「全部加粗」**")
        text = "今天👍🏽很开心🎉，代码"
        self.check(text, [ent("bold", text, "很开心🎉")], "今天👍🏽**很开心🎉**，代码")

    def test_nested_and_neighbouring_spans(self):
        text = "「外」里"
        self.check(text, [ent("bold", text, text), ent("italic", text, "「外」")], "**<em>「外」</em>里**")
        text = "x「a」y「b」z"
        entities = [ent("bold", text, "「a」"), ent("strikethrough", text, "「b」")]
        self.check(text, entities, "x<strong>「a」</strong>y<del>「b」</del>z")

    def test_per_line_with_block_markers(self):
        text = "- 注意：今天\n> 引用：内容"
        entities = [ent("bold", text, "注意："), ent("italic", text, "引用：")]
        self.check(text, entities, "- <strong>注意：</strong>今天\n> <em>引用：</em>内容")

    def test_private_use_text_keeps_plain_markers(self):
        text = "\U000F0001注意：今天"
        self.check(text, [ent("bold", text, "注意：")], "\U000F0001**注意：**今天")


class EntitiesCode(unittest.TestCase):
    def test_inline_code(self):
        text = "run ls -la now"
        self.assertEqual(entities_to_markdown(text, [ent("code", text, "ls -la")]), "run `ls -la` now")

    def test_inline_code_with_backticks_uses_longer_fence(self):
        text = "x a`b y"
        self.assertEqual(entities_to_markdown(text, [ent("code", text, "a`b")]), "x ``a`b`` y")
        text = "x a``b y"
        self.assertEqual(entities_to_markdown(text, [ent("code", text, "a``b")]), "x ```a``b``` y")

    def test_inline_code_starting_with_backtick_is_padded(self):
        text = "x `tick y"
        self.assertEqual(entities_to_markdown(text, [ent("code", text, "`tick")]), "x `` `tick `` y")

    def test_nothing_inside_code_is_transformed(self):
        text = "code **not** [x](javascript:y) here"
        entities = [ent("code", text, text), ent("bold", text, "not"), ent("italic", text, "here")]
        self.assertEqual(entities_to_markdown(text, entities), f"`{text}`")

    def test_pre_block_with_language(self):
        text = "Look:\nprint('hi')\nDone"
        entities = [ent("pre", text, "print('hi')", language="python")]
        self.assertEqual(entities_to_markdown(text, entities), "Look:\n```python\nprint('hi')\n```\nDone")

    def test_pre_block_gets_its_own_lines(self):
        text = "before code after"
        self.assertEqual(
            entities_to_markdown(text, [ent("pre", text, "code")]),
            "before \n```\ncode\n```\n after",
        )

    def test_pre_with_fence_inside_uses_longer_fence(self):
        text = "```\nnested\n```"
        result = entities_to_markdown(text, [ent("pre", text, text)])
        self.assertEqual(result, "````\n```\nnested\n```\n````")

    def test_pre_language_is_validated(self):
        text = "x"
        result = entities_to_markdown(text, [ent("pre", text, "x", language="py\n# evil")])
        self.assertEqual(result, "```\nx\n```")

    def test_pre_content_is_not_transformed(self):
        text = "## 2026-01-01\n# comment\n**x**"
        entities = [ent("pre", text, text), ent("bold", text, "x")]
        self.assertEqual(entities_to_markdown(text, entities), f"```\n{text}\n```")

    def test_multi_line_code_entity_becomes_a_block(self):
        text = "a\nb"
        self.assertEqual(entities_to_markdown(text, [ent("code", text, text)]), "```\na\nb\n```")


class EntitiesLinks(unittest.TestCase):
    def render_link(self, url: str, text: str = "go here") -> str:
        return entities_to_markdown(text, [ent("text_link", text, "here", url=url)])

    def test_safe_schemes_become_links(self):
        self.assertEqual(self.render_link("https://example.com/x?y=1"), "go [here](https://example.com/x?y=1)")
        self.assertEqual(self.render_link("http://example.com"), "go [here](http://example.com)")
        self.assertEqual(self.render_link("HTTPS://EXAMPLE.COM"), "go [here](HTTPS://EXAMPLE.COM)")
        self.assertEqual(self.render_link("mailto:me@example.com"), "go [here](mailto:me@example.com)")
        self.assertEqual(self.render_link("tg://user?id=1"), "go [here](tg://user?id=1)")

    def test_unsafe_schemes_become_plain_text(self):
        for url in (
            "javascript:alert(1)",
            "JavaScript:alert(1)",
            " javascript:alert(1)",
            "data:text/html,<script>",
            "vbscript:x",
            "file:///etc/passwd",
            "https:no-host",
            "//example.com",
            "example.com",
            "https://exa\nmple.com",
            "",
            None,
            42,
        ):
            with self.subTest(url=url):
                self.assertEqual(self.render_link(url), "go here")

    def test_destination_is_escaped(self):
        self.assertEqual(
            self.render_link("https://en.wikipedia.org/wiki/Foo_(bar) baz<x>"),
            "go [here](https://en.wikipedia.org/wiki/Foo_%28bar%29%20baz%3Cx%3E)",
        )

    def test_brackets_in_link_text_are_escaped(self):
        text = "see [1] ok"
        result = entities_to_markdown(text, [ent("text_link", text, "[1]", url="https://a.example")])
        self.assertEqual(result, "see [\\[1\\]](https://a.example) ok")

    def test_link_whitespace_moves_outside(self):
        text = "a link b"
        result = entities_to_markdown(text, [ent("text_link", text, " link ", url="https://a.example")])
        self.assertEqual(result, "a [link](https://a.example) b")

    def test_link_over_several_lines_is_one_link_per_line(self):
        text = "注意：\n今天下雨"
        entities = [ent("bold", text, text), ent("text_link", text, text, url="https://x.y/")]
        self.assertEqual(entities_to_markdown(text, entities), "**[注意：](https://x.y/)**\n**[今天下雨](https://x.y/)**")
        text = "前面链接，\n后面"
        entities = [ent("italic", text, "链接，\n后"), ent("text_link", text, "链接，\n后", url="https://x.y/")]
        self.assertEqual(
            entities_to_markdown(text, entities), "前面<em>[链接，](https://x.y/)</em>\n<em>[后](https://x.y/)</em>面"
        )

    def test_plain_entity_types(self):
        text = "@user #tag https://x.example a@b.example /start secret under"
        entities = [
            ent("mention", text, "@user"),
            ent("hashtag", text, "#tag"),
            ent("url", text, "https://x.example"),
            ent("email", text, "a@b.example"),
            ent("bot_command", text, "/start"),
            ent("spoiler", text, "secret"),
            ent("underline", text, "under"),
            ent("text_mention", text, "@user", user={"id": 1}),
            ent("custom_emoji", text, "#tag", custom_emoji_id="1"),
            ent("something_new", text, "secret"),
        ]
        self.assertEqual(entities_to_markdown(text, entities), text)


class EntitiesBlockquote(unittest.TestCase):
    def test_blockquote_lines_are_prefixed(self):
        text = "line one\nline two"
        self.assertEqual(entities_to_markdown(text, [ent("blockquote", text, text)]), "> line one\n> line two")

    def test_expandable_blockquote_and_blank_lines(self):
        text = "a\n\nb"
        self.assertEqual(entities_to_markdown(text, [ent("expandable_blockquote", text, text)]), "> a\n>\n> b")

    def test_text_after_quote_is_separated_by_blank_line(self):
        text = "said\nreply"
        self.assertEqual(entities_to_markdown(text, [ent("blockquote", text, "said")]), "> said\n\nreply")
        text = "intro\nsaid\n\nreply"
        self.assertEqual(entities_to_markdown(text, [ent("blockquote", text, "said")]), "intro\n> said\n\nreply")

    def test_quote_in_middle_of_line_starts_new_line(self):
        text = "x quoted"
        self.assertEqual(entities_to_markdown(text, [ent("blockquote", text, "quoted")]), "x \n> quoted")

    def test_bold_inside_quote(self):
        text = "hey you\nok"
        entities = [ent("blockquote", text, text), ent("bold", text, "you")]
        self.assertEqual(entities_to_markdown(text, entities), "> hey **you**\n> ok")

    def test_pre_inside_quote(self):
        text = "q\ncode"
        entities = [ent("blockquote", text, text), ent("pre", text, "code")]
        self.assertEqual(entities_to_markdown(text, entities), "> q\n> ```\n> code\n> ```")


class SplitTitle(unittest.TestCase):
    def test_hash_heading(self):
        self.assertEqual(split_title("# 春天\n\n花粉，春雨"), ("春天", "花粉，春雨"))
        self.assertEqual(split_title("# 只有标题"), ("只有标题", ""))

    def test_only_exactly_one_hash_and_space(self):
        self.assertEqual(split_title("## 二级\n正文"), (None, "## 二级\n正文"))
        self.assertEqual(split_title("#标签 正文"), (None, "#标签 正文"))
        self.assertEqual(split_title("# \n正文"), (None, "# \n正文"))

    def test_pipe_title(self):
        self.assertEqual(split_title("春天 | 花粉，春雨\n第二行"), ("春天", "花粉，春雨\n第二行"))
        self.assertEqual(split_title("春天 | 花粉"), ("春天", "花粉"))
        self.assertEqual(split_title("标题 | "), ("标题", ""))

    def test_first_pipe_wins(self):
        self.assertEqual(split_title("a | b | c"), ("a", "b | c"))

    def test_pipe_title_length_limit(self):
        title = "字" * 60
        self.assertEqual(split_title(f"{title} | 正文"), (title, "正文"))
        long_line = "字" * 61 + " | 正文"
        self.assertEqual(split_title(long_line), (None, long_line))

    def test_not_a_title(self):
        for text in (
            "| a | b |\n|---|---|",
            "Name | Age\n--- | ---\nBob | 3",
            "`a | b` is code",
            "> quote | not title",
            "- item | not title",
            "1. item | not title",
            " | starts with space and pipe",
            "a|b without spaces",
            "plain text",
            "",
        ):
            with self.subTest(text=text):
                self.assertEqual(split_title(text), (None, text))

    def test_crlf_and_leading_blank_lines(self):
        self.assertEqual(split_title("\n\n# 标题\r\n正文\r\n"), ("标题", "正文"))

    def test_pipe_inside_markup_is_not_a_title(self):
        for text in (
            "看看 [foo | bar](https://x.y/) 不错",
            "**周报 | 第3周**\n内容",
            "*周报 | 第3周* 内容",
            "~~周报 | 第3周~~ 内容",
            "<strong>周报 | 第3周</strong>后面",
            "前面 ***粗 | 斜*** 后面",
            "``a | b`` c",
            "[链接](https://x.y/a | b) 后面",
        ):
            with self.subTest(text=text):
                self.assertEqual(split_title(text), (None, text))

    def test_pipe_after_closed_markup_is_a_title(self):
        self.assertEqual(split_title("**周报** | 正文"), ("**周报**", "正文"))
        self.assertEqual(split_title("[链接](https://x.y/(a)) 标题 | 正文"), ("[链接](https://x.y/(a)) 标题", "正文"))
        # Only the first " | " counts: inside code here, so no title.
        self.assertEqual(split_title("``a | b`` 标题 | 正文"), (None, "``a | b`` 标题 | 正文"))
        self.assertEqual(split_title("``a`` 标题 | 正文"), ("``a`` 标题", "正文"))
        self.assertEqual(split_title("<strong>注意：</strong>标题 | 正文"), ("<strong>注意：</strong>标题", "正文"))
        self.assertEqual(split_title("a ` b | c"), ("a ` b", "c"))  # a lone backtick is literal

    def test_markup_split_from_entities(self):
        text = "看看 foo | bar 不错"
        markdown = entities_to_markdown(text, [ent("text_link", text, "foo | bar", url="https://x.y/")])
        self.assertEqual(split_title(markdown), (None, "看看 [foo | bar](https://x.y/) 不错"))
        text = "周报 | 第3周\n内容"
        markdown = entities_to_markdown(text, [ent("bold", text, "周报 | 第3周")])
        self.assertEqual(split_title(markdown), (None, "**周报 | 第3周**\n内容"))


class CleanTitle(unittest.TestCase):
    def test_emphasis_tags_and_feff_go(self):
        self.assertEqual(clean_title("<strong>注意：</strong>今天"), "注意：今天")
        self.assertEqual(clean_title("<em>a</em> <del>b</del>"), "a b")
        self.assertEqual(clean_title("\ufeff标题\ufeff"), "标题")

    def test_whitespace_and_markers(self):
        self.assertEqual(clean_title("  春天\n  来了  "), "春天 来了")
        self.assertEqual(clean_title("# **春天** #"), "春天")
        self.assertEqual(clean_title("`code`"), "code")
        self.assertEqual(clean_title("*强调*"), "强调")

    def test_hash_inside_a_word_stays(self):
        self.assertEqual(clean_title("学 C#"), "学 C#")
        self.assertEqual(clean_title("## C# ##"), "C#")

    def test_length_limit(self):
        self.assertEqual(clean_title("字" * 60), "字" * 60)
        with self.assertRaises(ValueError) as caught:
            clean_title("字" * 61)
        self.assertEqual(str(caught.exception), "标题最多 60 个字")

    def test_empty(self):
        for title in ("", "   ", "# ", "****", "``"):
            with self.subTest(title=title), self.assertRaises(ValueError):
                clean_title(title)


class SanitizeBody(unittest.TestCase):
    def test_note_heading_lines_are_escaped(self):
        self.assertEqual(sanitize_body("a\n## 2026-04-11\nb"), "a\n\\## 2026-04-11\nb")
        self.assertEqual(sanitize_body("## 2026-04-11 | 标题"), "\\## 2026-04-11 | 标题")
        self.assertEqual(sanitize_body("##\t2026-04-11"), "\\##\t2026-04-11")
        self.assertEqual(sanitize_body("## 2026-04-11 不是标题"), "\\## 2026-04-11 不是标题")

    def test_bare_hashes_followed_by_a_date_line_are_escaped(self):
        self.assertEqual(sanitize_body("x\n##\n\n2026-04-11\ny"), "x\n\\##\n\n2026-04-11\ny")
        # "##" alone is an empty heading otherwise (see test_empty_headings_are_dropped).
        self.assertEqual(sanitize_body("x\n##\ny"), "x\n\ny")

    def test_javascript_whitespace_before_the_date_is_escaped(self):
        # notes.ts runs in JavaScript, where \s also matches U+FEFF (Python's
        # does not), U+00A0 and U+3000, and runs over line breaks.
        self.assertEqual(sanitize_body("正文\n##\u00a02025-03-04\n后半段"), "正文\n\\##\u00a02025-03-04\n后半段")
        self.assertEqual(sanitize_body("正文\n##\u3000\n2025-03-04"), "正文\n\\##\u3000\n2025-03-04")
        self.assertEqual(sanitize_body("正文\n##\u3000 \n\u3000\n2025-03-04"), "正文\n\\##\u3000\n\u3000\n2025-03-04")

    def test_feff_is_removed(self):
        # "##" + U+FEFF + date is a note heading to notes.ts; without the
        # U+FEFF it is not even a Markdown heading.
        self.assertEqual(sanitize_body("正文\n##\ufeff2025-03-04\n后半段"), "正文\n##2025-03-04\n后半段")
        self.assertEqual(sanitize_body("\ufeff开头\ufeff"), "开头")

    def test_empty_headings_are_dropped(self):
        for body in ("#", "##", "# ", "###", "## ##", "#\t", "   #"):
            with self.subTest(body=body):
                self.assertEqual(sanitize_body(body), "")
        self.assertEqual(sanitize_body("a\n#\nb"), "a\n\nb")
        self.assertEqual(sanitize_body("#\n\n![随想配图](/assets/notes/a.webp)"), "![随想配图](/assets/notes/a.webp)")

    def test_any_atx_heading_is_demoted(self):
        self.assertEqual(sanitize_body("#\t标题"), "### 标题")
        self.assertEqual(sanitize_body("##\t标题"), "### 标题")
        self.assertEqual(sanitize_body("  # 缩进"), "### 缩进")
        self.assertEqual(sanitize_body("   ## 缩进"), "### 缩进")
        self.assertEqual(sanitize_body("x\n\n    # 代码"), "x\n\n    # 代码")  # indented code
        self.assertEqual(sanitize_body("\n    # 标题\n正文"), "### 标题\n正文")  # notes.ts trims the body
        self.assertEqual(sanitize_body("#\u3000不是标题"), "#\u3000不是标题")

    def test_headings_are_demoted(self):
        self.assertEqual(sanitize_body("# 大标题\n## 小标题\n### 保留\n#### 也保留"), "### 大标题\n### 小标题\n### 保留\n#### 也保留")
        self.assertEqual(sanitize_body("#tag 不是标题"), "#tag 不是标题")

    def test_code_fences_keep_comments_but_not_note_headings(self):
        body = "```bash\n# install\n## 2026-01-01\n```\n# after"
        self.assertEqual(sanitize_body(body), "```bash\n# install\n\\## 2026-01-01\n```\n### after")

    def test_tilde_fences_and_longer_closing_fence(self):
        body = "~~~~\n# keep\n~~~\n# still code\n~~~~~\n# demote"
        self.assertEqual(sanitize_body(body), "~~~~\n# keep\n~~~\n# still code\n~~~~~\n### demote")

    def test_whitespace_cleanup(self):
        self.assertEqual(sanitize_body("\r\n\n  a  \r\nb\t\n\n\n\n\nc\n\n"), "a\nb\n\n\nc")
        self.assertEqual(sanitize_body("a\n\n\nb"), "a\n\n\nb")

    def test_other_line_separators(self):
        self.assertEqual(sanitize_body("a\r## 2026-01-01"), "a\n\\## 2026-01-01")
        self.assertEqual(sanitize_body("a\u2028## 2026-01-01\u2029b"), "a\n\\## 2026-01-01\nb")


if __name__ == "__main__":
    unittest.main()
