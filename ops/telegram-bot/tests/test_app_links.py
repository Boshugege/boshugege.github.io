"""Link previews in notes: the bot fetches them once and stores them with the note."""

import io
import json

from PIL import Image

from pnc_bot import app as appmod
from pnc_bot import images, linkpreview
from pnc_bot.linkpreview import Preview

from tests.test_app import AppTestBase, message

PREVIEWS = linkpreview.PREVIEWS_PATH
NOTES = "src/content/notes.md"
REPO_URL = "https://github.com/withastro/astro"
VIDEO_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def picture(size=(1280, 720), fmt="JPEG") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (40, 90, 130)).save(out, fmt)
    return out.getvalue()


class FakeFetcher:
    """Answers fetch() from a dict: a Preview, None, or an exception to raise."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        answer = self.answers.get(url)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class LinkPreviewTest(AppTestBase):
    def with_fetcher(self, answers=None):
        self.fetcher = FakeFetcher(answers)
        self.app = self.make_app(link_fetcher=self.fetcher)
        return self.fetcher

    def previews(self):
        return json.loads((self.repo.path / PREVIEWS).read_text(encoding="utf-8"))

    def test_note_with_link_stores_its_preview(self):
        self.with_fetcher(
            {
                REPO_URL: Preview(
                    url=REPO_URL,
                    kind="github",
                    title="withastro/astro",
                    description="The web framework",
                    site="GitHub",
                    stars=54321,
                    language="TypeScript",
                    image=picture((460, 460), "PNG"),
                )
            }
        )
        self.app.handle_update(self.update(message=message(f"看看这个\n{REPO_URL}", mid=20)))
        self.assertEqual(self.fetcher.calls, [REPO_URL])
        sha, msg, changed = self.repo.commits[0]
        self.assertEqual(len(changed), 3)
        self.assertEqual(changed[0], NOTES)
        self.assertEqual(changed[1], PREVIEWS)
        self.assertRegex(changed[2], r"^src/static/assets/notes/\d{8}-[0-9a-f]{10}\.webp$")
        self.assertIn("Pnc-Bot: note-add", msg)
        self.assertIn(REPO_URL, self.repo.notes())

        data = self.previews()
        linkpreview.validate_previews((self.repo.path / PREVIEWS).read_text(encoding="utf-8"))
        entry = data["links"][REPO_URL]
        self.assertEqual(entry["title"], "withastro/astro")
        self.assertEqual(entry["stars"], 54321)
        self.assertEqual(entry["kind"], "github")
        self.assertEqual("src/static" + entry["image"], changed[2])
        # An avatar is stored small, re-encoded and stripped like every note image.
        stored = (self.repo.path / changed[2]).read_bytes()
        images.assert_clean_webp(stored)
        self.assertEqual(Image.open(io.BytesIO(stored)).size, (120, 120))
        self.assertNotIn("没取到预览", "".join(self.tg.texts()))

    def test_video_cover_is_stored_wide(self):
        self.with_fetcher({VIDEO_URL: Preview(url=VIDEO_URL, kind="youtube", title="A video", image=picture((1920, 1080)))})
        self.app.handle_update(self.update(message=message(VIDEO_URL, mid=20)))
        _, _, changed = self.repo.commits[0]
        stored = (self.repo.path / changed[2]).read_bytes()
        self.assertEqual(Image.open(io.BytesIO(stored)).size, (960, 540))

    def test_failed_preview_never_holds_the_note_back(self):
        boom = "https://example.com/boom"
        self.with_fetcher({REPO_URL: None, boom: RuntimeError("fetch must not raise, but if it does")})
        with self.assertLogs("pnc_bot", level="ERROR"):
            self.app.handle_update(self.update(message=message(f"{REPO_URL}\n\n{boom}", mid=20)))
        self.assertEqual(self.fetcher.calls, [REPO_URL, boom])
        sha, _, changed = self.repo.commits[0]
        self.assertEqual(changed, [NOTES])
        self.assertFalse((self.repo.path / PREVIEWS).exists())
        self.assertIn("2 个链接没取到预览", self.tg.texts()[-1])
        # The final message replaces the receipt, so it says so again.
        _, ok, _, _ = self.tracker.watches[0]
        ok({"result": "success", "sha": sha})
        self.assertIn("已上线", self.tg.texts()[-1])
        self.assertIn("2 个链接没取到预览", self.tg.texts()[-1])

    def test_unreadable_thumbnail_keeps_the_card(self):
        self.with_fetcher({REPO_URL: Preview(url=REPO_URL, kind="github", title="withastro/astro", image=b"not an image")})
        self.app.handle_update(self.update(message=message(REPO_URL, mid=20)))
        _, _, changed = self.repo.commits[0]
        self.assertEqual(changed, [NOTES, PREVIEWS])
        self.assertNotIn("image", self.previews()["links"][REPO_URL])
        self.assertNotIn("没取到预览", "".join(self.tg.texts()))

    def test_only_card_links_are_fetched_and_at_most_three(self):
        self.with_fetcher()
        lines = [f"https://example.com/{n}" for n in range(5)]
        text = "句中 https://example.com/inline 链接\n\n" + "\n\n".join(lines) + "\n\n```\nhttps://example.com/code\n```"
        self.app.handle_update(self.update(message=message(text, mid=20)))
        self.assertEqual(self.fetcher.calls, lines[:3])
        self.assertIn("3 个链接没取到预览", self.tg.texts()[-1])

    def test_append_stores_previews_too(self):
        self.with_fetcher({VIDEO_URL: Preview(url=VIDEO_URL, kind="youtube", title="A video", author="A channel")})
        self.app.handle_update(self.update(message=message("第一条", mid=20)))
        self.assertEqual(self.fetcher.calls, [])
        self.app.handle_update(self.update(message=message(f"/append {VIDEO_URL}", mid=21)))
        self.assertEqual(self.fetcher.calls, [VIDEO_URL])
        _, msg, changed = self.repo.commits[1]
        self.assertEqual(changed, [NOTES, PREVIEWS])
        self.assertIn("Pnc-Bot: note-append", msg)
        self.assertEqual(self.previews()["links"][VIDEO_URL]["author"], "A channel")

    def test_existing_previews_are_kept_and_the_file_stays_sorted(self):
        path = self.repo.path / PREVIEWS
        path.parent.mkdir(parents=True)
        path.write_text(linkpreview.merge_previews(None, {"https://zzz-site.org/a": {"kind": "link", "title": "旧的"}}), encoding="utf-8")
        self.with_fetcher({REPO_URL: Preview(url=REPO_URL, kind="github", title="withastro/astro")})
        self.app.handle_update(self.update(message=message(REPO_URL, mid=20)))
        text = path.read_text(encoding="utf-8")
        self.assertEqual(list(json.loads(text)["links"]), [REPO_URL, "https://zzz-site.org/a"])
        self.assertEqual(text, linkpreview.merge_previews(text, {}))

    def test_broken_previews_file_is_left_alone(self):
        path = self.repo.path / PREVIEWS
        path.parent.mkdir(parents=True)
        path.write_text("{ not json", encoding="utf-8")
        self.with_fetcher({REPO_URL: Preview(url=REPO_URL, kind="github", title="withastro/astro", image=picture())})
        with self.assertLogs("pnc_bot", level="WARNING"):
            self.app.handle_update(self.update(message=message(REPO_URL, mid=20)))
        _, _, changed = self.repo.commits[0]
        self.assertEqual(changed, [NOTES])
        self.assertEqual(path.read_text(encoding="utf-8"), "{ not json")
        self.assertIn("1 个链接没取到预览", self.tg.texts()[-1])

    def test_drafts_and_bots_without_a_fetcher_fetch_nothing(self):
        self.with_fetcher()
        self.app.handle_update(self.update(message=message(f"/draft 标题 | a-draft\n{REPO_URL}", mid=20)))
        self.assertEqual(self.fetcher.calls, [])
        # The default app has no fetcher: links are posted as they are, silently.
        self.app = self.make_app()
        self.app.handle_update(self.update(message=message(REPO_URL, mid=21)))
        self.assertEqual(self.repo.commits[-1][2], [NOTES])
        self.assertNotIn("没取到预览", "".join(self.tg.texts()))

    def test_thumbnail_names_differ_per_link(self):
        data = b"same picture"
        first = appmod.link_image_name("https://github.com/a/x", data, "2026-11-24")
        second = appmod.link_image_name("https://github.com/a/y", data, "2026-11-24")
        self.assertRegex(first, images.IMAGE_NAME_RE)
        self.assertNotEqual(first, second)
        self.assertEqual(first, appmod.link_image_name("https://github.com/a/x", data, "2026-11-24"))
        with self.assertRaises(ValueError):
            appmod.link_image_name("https://github.com/a/x", data, "24 Nov")
