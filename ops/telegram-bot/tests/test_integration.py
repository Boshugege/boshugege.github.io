"""End-to-end flows: Telegram updates in, git commits and Telegram replies out.

Real: App with its worker threads, TelegramClient (over a fake Bot API
transport), State, gitrepo.Repo on a clone of a temporary bare remote, the
Pillow image pipeline, DeployHistory/DeployTracker and CommentStore.
Fake: the HTTP layer of the Bot API (records every call, serves files), the
local HTTP checks and the ops subprocess runner. Fully offline.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from typing import NamedTuple
from unittest import mock

from PIL import Image

from pnc_bot import notes
from pnc_bot.app import NOT_A_NOTE_REPLY, App
from pnc_bot.comments import CommentStore
from pnc_bot.deploys import DeployHistory
from pnc_bot.gitrepo import BOT_EMAIL, BOT_NAME
from pnc_bot.images import assert_clean_webp
from pnc_bot.notes import NOTES_ASSET_DIR, NOTES_PATH, insert_note, parse_notes
from pnc_bot.ops import HttpResult, Ops
from pnc_bot.state import State
from pnc_bot.telegram import TelegramClient

from tests.runtime_fakes import OWNER, TOKEN, FakeRunner, make_blog, make_comments_db, make_config, write_records
from tests.test_gitrepo import FIXTURE_NOTES, Sandbox

DATE = "2026-11-21"  # notes.today_shanghai() is pinned to this
NOW = 1_800_000_000  # message dates; the app clock runs 5 s later
STRANGER = 777
RELEASE = "20261121-100000-aaaaaaaaaaaa"
GROUP = {"id": -100123, "type": "group", "title": "alerts"}
SUPERGROUP = {"id": -100456, "type": "supergroup", "title": "alerts"}
CHANNEL = {"id": -100789, "type": "channel", "title": "news"}
SITE = "https://example.test"
LOCAL = "http://127.0.0.1:46213"
RED, GREEN, BLUE = (220, 30, 30), (30, 200, 30), (30, 30, 220)


# --- fixtures --------------------------------------------------------------------


def jpeg_with_gps(size: tuple[int, int] = (2400, 1800)) -> bytes:
    """A camera-like JPEG: left half red, right half blue; EXIF orientation 6 + GPS."""
    image = Image.new("RGB", size, BLUE)
    image.paste(RED, (0, 0, size[0] // 2, size[1]))
    exif = Image.Exif()
    exif[0x0112] = 6  # display rotated 90° clockwise
    exif[0x010F] = "SecretCam"
    exif[0x8825] = {1: "N", 2: (39.0, 54.0, 27.0), 3: "E", 4: (116.0, 23.0, 17.0)}  # GPS IFD
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=90, exif=exif.tobytes())
    return buffer.getvalue()


def solid_png(colour: tuple[int, int, int], size: tuple[int, int] = (64, 48)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, "PNG")
    return buffer.getvalue()


def near(pixel, colour, tolerance: int = 40) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(pixel[:3], colour))


def user(uid: int = OWNER) -> dict:
    return {"id": uid, "is_bot": False, "first_name": "PNC"}


def private(uid: int = OWNER) -> dict:
    return {"id": uid, "type": "private", "first_name": "PNC"}


def message(mid: int, *, uid: int = OWNER, chat: dict | None = None, date: int = NOW, **fields) -> dict:
    return {"message_id": mid, "date": date, "from": user(uid), "chat": chat or private(uid), **fields}


def replying_to(mid: int) -> dict:
    return {"reply_to_message": {"message_id": mid, "date": NOW, "chat": private(), "text": "…"}}


def deploy_record(sha: str, result: str, **extra) -> dict:
    record = {
        "id": "20261121-120000-1",
        "mode": "deploy",
        "sha": sha,
        "release": None,
        "result": result,
        "started_at": "2026-11-21T04:00:00Z",
        "finished_at": "2026-11-21T04:01:00Z",
        "log": "",
        "error": "",
        "trigger": "github-actions",
        "request": "",
    }
    record.update(extra)
    return record


class Commit(NamedTuple):
    sha: str
    name: str
    email: str
    subject: str
    trailers: dict


# --- the Bot API, as TelegramClient sees it ----------------------------------------------


class FakeResponse(io.BytesIO):
    def __init__(self, status: int, body: bytes):
        super().__init__(body)
        self.status = status
        self.headers = {"Content-Length": str(len(body))}


class FakeBotAPI:
    """Stands in for urllib's opener inside a real TelegramClient.

    Records every method call with its JSON parameters, numbers sent messages,
    keeps each message's text history (sent text, then every edit) and serves
    registered files through getFile + the file download URL.
    """

    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.file_base = f"https://api.telegram.org/file/bot{token}/"
        self.lock = threading.Lock()
        self.calls: list[tuple[str, dict]] = []
        self.updates: list[list[dict]] = []  # batches returned by getUpdates
        self.files: dict[str, bytes] = {}  # file_id -> bytes
        self.paths: dict[str, str] = {}  # file_path -> file_id
        self.downloads: list[str] = []  # file ids, in download order
        self.history: dict[int, list[str]] = {}  # bot message id -> texts
        self.markup: dict[int, dict | None] = {}  # bot message id -> keyboard as sent
        self.replies: dict[int, int] = {}  # bot message id -> message it replies to
        self.unexpected: list[str] = []
        self.next_id = 5000

    # urllib opener interface
    def open(self, request, timeout=None):
        url = request.full_url
        with self.lock:
            if url.startswith(self.file_base):
                file_id = self.paths[urllib.parse.unquote(url[len(self.file_base) :])]
                self.downloads.append(file_id)
                return FakeResponse(200, self.files[file_id])
            if not url.startswith(self.base):
                self.unexpected.append("unexpected URL")
                return FakeResponse(404, b'{"ok": false, "error_code": 404, "description": "Not Found"}')
            method = url[len(self.base) :]
            params = json.loads(request.data.decode("utf-8")) if request.data else {}
            self.calls.append((method, params))
            ok, result = self._answer(method, params)
        if ok:
            return FakeResponse(200, json.dumps({"ok": True, "result": result}).encode("utf-8"))
        body = {"ok": False, "error_code": 400, "description": result}
        return FakeResponse(400, json.dumps(body).encode("utf-8"))

    def _answer(self, method: str, params: dict) -> tuple[bool, object]:
        if method == "getUpdates":
            return True, self.updates.pop(0) if self.updates else []
        if method == "sendMessage":
            self.next_id += 1
            mid = self.next_id
            self.history[mid] = [params["text"]]
            self.markup[mid] = params.get("reply_markup")
            if "reply_parameters" in params:
                self.replies[mid] = params["reply_parameters"]["message_id"]
            chat = {"id": params["chat_id"], "type": "private"}
            return True, {"message_id": mid, "date": NOW, "chat": chat, "text": params["text"]}
        if method == "editMessageText":
            self.history.setdefault(params["message_id"], []).append(params["text"])
            return True, True
        if method in ("answerCallbackQuery", "setMyCommands"):
            return True, True
        if method == "getFile":
            file_id = params.get("file_id")
            if file_id not in self.files:
                return False, "Bad Request: invalid file_id"
            path = f"photos/file_{len(self.paths)}.jpg"
            self.paths[path] = file_id
            info = {"file_id": file_id, "file_unique_id": "u", "file_size": len(self.files[file_id]), "file_path": path}
            return True, info
        self.unexpected.append(method)
        return False, "Bad Request: unexpected method"

    # views for assertions
    def methods(self) -> list[str]:
        with self.lock:
            return [method for method, _ in self.calls]

    def params(self, method: str) -> list[dict]:
        with self.lock:
            return [params for name, params in self.calls if name == method]

    def receipt_for(self, source_id: int) -> int:
        with self.lock:
            ids = [m for m, src in self.replies.items() if src == source_id and self.history[m][0].startswith("⏳")]
        assert len(ids) == 1, f"expected one receipt for message {source_id}, got {ids}"
        return ids[0]

    def last_sent(self) -> int:
        with self.lock:
            return max(self.history)

    def text(self, mid: int) -> str:
        with self.lock:
            return self.history[mid][-1]

    def button(self, mid: int, choice: str = "y") -> str:
        with self.lock:
            rows = (self.markup[mid] or {}).get("inline_keyboard", [])
        for row in rows:
            for button in row:
                if button["callback_data"].endswith(":" + choice):
                    return button["callback_data"]
        raise AssertionError(f"message {mid} has no {choice!r} button")


# --- the harness -------------------------------------------------------------------------


class Flow(unittest.TestCase):
    """A real App wired to a sandbox remote, a fake Bot API and a temp blog root."""

    PRIMED_OFFSET = True  # False: start like a fresh install (no stored offset)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="pnc-bot-integ-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "git").mkdir()
        self.box = Sandbox(self.root / "git")
        self.seed = self.box.remote_head()
        self.blog = make_blog(self.root, [RELEASE], current=RELEASE)
        make_comments_db(self.root / "pnc.db", [])
        self.config = make_config(self.root, repo=self.box.bot_path, site_url=SITE, local_site=LOCAL, token=TOKEN)

        patcher = mock.patch.object(notes, "today_shanghai", return_value=DATE)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.api = FakeBotAPI(TOKEN)
        self.addCleanup(lambda: self.assertEqual(self.api.unexpected, []))
        self.state = State(self.config.state_dir)
        self.addCleanup(self.state.close)
        self.http_calls: list[str] = []
        self.http_status: list[int] = []  # answered first, then 200
        self.mono = 1000.0
        repo = self.box.repo()
        history = DeployHistory(self.blog)
        self.app = App(
            self.config,
            tg=TelegramClient(TOKEN, self.config.proxy, opener=self.api, sleep=lambda seconds: None),
            state=self.state,
            repo=repo,
            history=history,
            comments=CommentStore(self.config.comments_db),
            ops=Ops(self.config, history, repo, runner=FakeRunner(), http_get=self.local_http),
            clock=lambda: NOW + 5,
            monotonic=lambda: self.mono,
            sleep=lambda seconds: None,
            http_get=self.local_http,
        )
        self.addCleanup(self.app.shutdown)
        self.app.tracker.prime()  # App.run() does this through tracker.start()
        self.next_update = 100
        if self.PRIMED_OFFSET:
            self.state.set("update_offset", self.next_update)

    # --- driving the app ---------------------------------------------------

    def local_http(self, url: str, **kwargs) -> HttpResult:
        self.http_calls.append(url)
        return HttpResult(self.http_status.pop(0) if self.http_status else 200, 3)

    def deliver(self, *payloads: dict) -> None:
        """One getUpdates batch through the real loop, then wait for the workers."""
        batch = []
        for payload in payloads:
            batch.append({"update_id": self.next_update, **payload})
            self.next_update += 1
        self.api.updates.append(batch)
        self.app.poll_once()
        self.settle()

    def deliver_now(self, *payloads: dict) -> None:
        """Like deliver(), but without waiting for the workers."""
        batch = []
        for payload in payloads:
            batch.append({"update_id": self.next_update, **payload})
            self.next_update += 1
        self.api.updates.append(batch)
        self.app.poll_once()

    def flush_albums(self) -> None:
        self.mono += 3
        self.api.updates.append([])
        self.app.poll_once()
        self.settle()

    def settle(self, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        idle = 0
        while idle < 3:
            busy = self.app.git.pending() or self.app.pool.pending()
            idle = 0 if busy else idle + 1
            if time.monotonic() > deadline:
                self.fail("workers did not finish")
            time.sleep(0.01)

    def click(self, mid: int, choice: str = "y", *, data: str | None = None, uid: int = OWNER, chat: dict | None = None) -> None:
        query = {
            "id": f"cb{self.next_update}",
            "from": user(uid),
            "message": {"message_id": mid, "date": NOW, "chat": chat or private()},
            "chat_instance": "1",
            "data": data or self.api.button(mid, choice),
        }
        self.deliver({"callback_query": query})

    def text(self, text: str, mid: int, **kwargs) -> dict:
        return {"message": message(mid, text=text, **kwargs)}

    def photo(self, file_id: str, data: bytes, mid: int, *, caption: str | None = None, group: str | None = None, **kwargs) -> dict:
        """A photo message; only the largest size is registered for download."""
        self.api.files[file_id] = data
        fields = {
            "photo": [
                {"file_id": file_id + "-thumb", "file_unique_id": "t", "width": 90, "height": 60, "file_size": 900},
                {"file_id": file_id, "file_unique_id": "f", "width": 1280, "height": 960, "file_size": len(data)},
            ]
        }
        if caption is not None:
            fields["caption"] = caption
        if group is not None:
            fields["media_group_id"] = group
        return {"message": message(mid, **fields, **kwargs)}

    # --- reading the remote --------------------------------------------------

    def commits(self) -> list[Commit]:
        """Commits on the remote since the seed, newest first."""
        # The subject goes last: Python's strip() (used by Sandbox.git) counts
        # \x1e and \x1f as whitespace, so an empty last field would vanish.
        fmt = "%H%x1f%an%x1f%ae%x1f%(trailers:only=true,unfold=true)%x1f%s%x1e"
        out = self.box.remote_git("log", f"--format={fmt}", f"{self.seed}..main")
        commits = []
        for record in out.split("\x1e"):
            record = record.strip("\n")
            if record:
                sha, name, email, trailers, subject = record.split("\x1f")
                pairs = (line.split(": ", 1) for line in trailers.splitlines() if ": " in line)
                commits.append(Commit(sha, name, email, subject, dict(pairs)))
        return commits

    def remote_notes(self) -> list[notes.Note]:
        return parse_notes(self.box.remote_file(NOTES_PATH))

    def remote_files(self, folder: str) -> list[str]:
        out = self.box.remote_git("ls-tree", "-r", "--name-only", "main", "--", folder)
        return sorted(out.splitlines()) if out else []

    def remote_blob(self, path: str) -> bytes:
        proc = subprocess.run(
            ["git", "--git-dir", str(self.box.remote), "cat-file", "blob", f"main:{path}"],
            env=self.box.env,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, f"{path} is not on the remote")
        return proc.stdout

    def assert_clean_image(self, path: str) -> Image.Image:
        """A committed image: clean WebP, content-addressed name, ≤ 1600 px wide."""
        data = self.remote_blob(path)
        assert_clean_webp(data)
        self.assertEqual(path.rsplit("/", 1)[1], f"{DATE.replace('-', '')}-{hashlib.sha256(data).hexdigest()[:10]}.webp")
        self.assertNotIn(b"SecretCam", data)
        image = Image.open(io.BytesIO(data))
        image.load()
        self.assertEqual(image.format, "WEBP")
        self.assertLessEqual(image.width, 1600)
        self.assertEqual(len(image.getexif()), 0)
        for key in ("exif", "icc_profile", "xmp"):
            self.assertNotIn(key, image.info)
        return image

    def assert_bot_commit(self, commit: Commit, subject: str, trailers: dict) -> None:
        self.assertEqual((commit.name, commit.email), (BOT_NAME, BOT_EMAIL))
        self.assertEqual(commit.subject, subject)
        self.assertEqual(commit.trailers, trailers)

    def submitted(self, commit: Commit) -> str:
        return f"📝 已提交 <code>{commit.sha[:7]}</code>，部署中…"


# --- (a)–(e): content flows --------------------------------------------------------------


class NoteFlowTests(Flow):
    def test_photo_with_caption_becomes_a_note(self):
        source = jpeg_with_gps()
        exif = Image.open(io.BytesIO(source)).getexif()  # the fixture really carries GPS
        self.assertEqual((exif[0x0112], exif.get_ifd(0x8825)[1]), (6, "N"))

        self.deliver(self.photo("cam", source, mid=10, caption="# 标题\n正文 **粗体**"))

        [commit] = self.commits()
        self.assert_bot_commit(commit, "note: 标题", {"Pnc-Bot": "note-add", "Pnc-Bot-Note": DATE})
        raw = self.box.remote_file(NOTES_PATH)
        top = FIXTURE_NOTES.index("## 2026-04-11")
        self.assertTrue(raw.startswith(FIXTURE_NOTES[:top] + f"## {DATE} | 标题\n\n正文 **粗体**\n\n![标题](/assets/notes/"))
        self.assertTrue(raw.endswith("\n\n" + FIXTURE_NOTES[top:]))
        note = self.remote_notes()[0]
        self.assertEqual((note.id, note.title), (DATE, "标题"))
        [name] = notes.referenced_note_images(note.body)
        path = f"{NOTES_ASSET_DIR}/{name}"
        self.assertEqual(self.remote_files(NOTES_ASSET_DIR), [path])
        changed = self.box.remote_git("diff-tree", "--no-commit-id", "--name-only", "-r", commit.sha).split("\n")
        self.assertEqual(sorted(changed), sorted([NOTES_PATH, path]))

        image = self.assert_clean_image(path).convert("RGB")
        self.assertEqual(image.size, (1600, 2133))  # EXIF orientation applied, then scaled to 1600
        self.assertTrue(near(image.getpixel((800, 100)), RED))  # the left (red) half is now on top
        self.assertTrue(near(image.getpixel((800, 2000)), BLUE))

        receipt = self.api.receipt_for(10)
        self.assertEqual(self.api.history[receipt], ["⏳ 处理中…", self.submitted(commit)])
        self.assertEqual(self.api.methods(), ["getUpdates", "sendMessage", "getFile", "editMessageText"])
        self.assertEqual(self.api.params("getFile"), [{"file_id": "cam"}])  # the largest size only
        self.assertEqual(self.api.downloads, ["cam"])
        self.assertEqual(self.state.note_for_message(receipt), DATE)
        self.assertEqual(self.state.note_for_message(10), DATE)
        self.assertEqual(self.box.status(), "")

    def test_bold_entity_in_a_caption(self):
        caption = "# 标题\n正文 粗体"  # Telegram sends the formatting as an entity
        self.deliver(self.photo("p", solid_png(RED), mid=11, caption=caption, caption_entities=[{"type": "bold", "offset": 8, "length": 2}]))
        note = self.remote_notes()[0]
        self.assertTrue(note.body.startswith("正文 **粗体**\n\n![标题](/assets/notes/"))

    def test_album_becomes_one_note_with_images_in_order(self):
        colours = [RED, GREEN, BLUE]
        items = [
            self.photo(f"album-{i}", solid_png(colour), mid=20 + i, group="g1", caption="三张图" if i == 0 else None)
            for i, colour in enumerate(colours)
        ]
        self.deliver(items[2], items[0], items[1])  # Telegram does not promise the order
        self.assertEqual(self.commits(), [])
        self.assertEqual(self.api.methods(), ["getUpdates"])  # buffered: no receipt, no download yet
        self.flush_albums()

        [commit] = self.commits()
        self.assert_bot_commit(commit, "note: 三张图", {"Pnc-Bot": "note-add", "Pnc-Bot-Note": DATE})
        note = self.remote_notes()[0]
        self.assertEqual((note.id, note.title), (DATE, None))
        names = notes.referenced_note_images(note.body)
        self.assertEqual(note.body, "三张图\n\n" + "\n\n".join(f"![随想配图](/assets/notes/{n})" for n in names))
        self.assertEqual(len(names), 3)
        for name, colour in zip(names, colours):
            image = self.assert_clean_image(f"{NOTES_ASSET_DIR}/{name}")
            self.assertTrue(near(image.convert("RGB").getpixel((32, 24)), colour))
        self.assertEqual(self.api.downloads, ["album-0", "album-1", "album-2"])
        receipt = self.api.receipt_for(20)
        self.assertEqual(self.api.text(receipt), self.submitted(commit))
        for mid in (20, 21, 22, receipt):
            self.assertEqual(self.state.note_for_message(mid), DATE)

    def test_reply_to_receipt_or_source_appends(self):
        self.deliver(self.text("第一条", mid=30))
        receipt = self.api.receipt_for(30)
        self.deliver(self.text("回复回执", mid=31, **replying_to(receipt)))
        self.deliver(self.photo("more", solid_png(GREEN), mid=32, **replying_to(30)))

        commits = self.commits()
        self.assertEqual([c.subject for c in commits], [f"note: append to {DATE}"] * 2 + ["note: 第一条"])
        for commit in commits[:2]:
            self.assertEqual(commit.trailers, {"Pnc-Bot": "note-append", "Pnc-Bot-Note": DATE})
        current = self.remote_notes()
        self.assertEqual(len(current), len(parse_notes(FIXTURE_NOTES)) + 1)
        [name] = notes.referenced_note_images(current[0].body)
        self.assertEqual(current[0].body, f"第一条\n\n回复回执\n\n![随想配图](/assets/notes/{name})")
        self.assert_clean_image(f"{NOTES_ASSET_DIR}/{name}")
        self.assertEqual(self.api.text(self.api.receipt_for(31)), self.submitted(commits[1]))
        self.assertEqual(self.state.note_for_message(self.api.receipt_for(32)), DATE)
        self.assertEqual(self.box.status(), "")

    def test_reply_to_a_receipt_while_its_note_is_still_being_pushed(self):
        with self.app.repo_lock:  # the first job is still syncing / pushing
            self.deliver_now(self.text("今天去爬山", mid=33))
            receipt = self.api.receipt_for(33)
            self.deliver_now(self.photo("summit", solid_png(BLUE), mid=34, caption="山顶很冷", **replying_to(receipt)))
            self.deliver_now(self.text("下山了", mid=35, **replying_to(33)))
        self.settle()
        commits = self.commits()
        self.assertEqual([c.subject for c in commits], [f"note: append to {DATE}"] * 2 + ["note: 今天去爬山"])
        [note] = [n for n in self.remote_notes() if n.id == DATE]
        [name] = notes.referenced_note_images(note.body)
        self.assertEqual(note.body, f"今天去爬山\n\n山顶很冷\n\n![随想配图](/assets/notes/{name})\n\n下山了")
        self.assertEqual(len(self.remote_notes()), len(parse_notes(FIXTURE_NOTES)) + 1)

    def test_reply_to_a_message_that_is_no_note_is_not_published(self):
        self.deliver(self.text("/status", mid=36))
        status = self.api.last_sent()
        self.deliver(self.text("关于评论者的私话", mid=37, **replying_to(status)))
        self.deliver(self.text("/draft 秘密计划 | secret-plan\n正文", mid=38))
        self.deliver(self.text("更多私密草稿内容", mid=39, **replying_to(self.api.receipt_for(38))))
        self.assertEqual([c.subject for c in self.commits()], ["draft: secret-plan"])
        hints = [mid for mid, texts in self.api.history.items() if texts == [NOT_A_NOTE_REPLY]]
        self.assertEqual([self.api.replies[mid] for mid in hints], [37, 39])

    def test_append_command_with_explicit_id(self):
        self.deliver(self.text("/append 2026-04-07 最后补一句", mid=40))
        [commit] = self.commits()
        self.assert_bot_commit(commit, "note: append to 2026-04-07", {"Pnc-Bot": "note-append", "Pnc-Bot-Note": "2026-04-07"})
        self.assertEqual(self.box.remote_file(NOTES_PATH), FIXTURE_NOTES.rstrip() + "\n\n最后补一句\n")

        self.deliver(self.text("/append 2099-01-01 不存在", mid=41))
        self.assertEqual(len(self.commits()), 1)
        self.assertEqual(self.api.text(self.api.receipt_for(41)), "❌ 没有找到随想 2099-01-01")
        self.assertEqual(self.box.status(), "")

    def test_append_without_id_targets_the_newest_bot_note(self):
        self.deliver(self.text("机器人的随想", mid=42))
        self.deliver(self.text("/append 默认追加", mid=43))
        self.assertEqual(self.commits()[0].trailers, {"Pnc-Bot": "note-append", "Pnc-Bot-Note": DATE})
        self.assertEqual(self.remote_notes()[0].body, "机器人的随想\n\n默认追加")

    def test_draft_with_photo(self):
        wide = solid_png((10, 120, 200), (2000, 100))
        self.deliver(self.photo("draft-pic", wide, mid=50, caption="/draft 标题 | my-slug\n第一段正文"))

        [commit] = self.commits()
        self.assert_bot_commit(commit, "draft: my-slug", {"Pnc-Bot": "draft"})
        files = self.remote_files("src/content/posts/my-slug")
        self.assertEqual(len(files), 2)
        [image_path] = [f for f in files if f.endswith(".webp")]
        name = image_path.rsplit("/", 1)[1]
        document = self.box.remote_file("src/content/posts/my-slug/index.mdx")
        self.assertEqual(document, notes.draft_document("标题", DATE, "第一段正文", [name]))
        self.assertTrue(document.startswith(f'---\ntitle: "标题"\ndate: "{DATE}"\ntags: []\nexcerpt: ""\ndraft: true\n---\n'))
        self.assertTrue(document.endswith(f"第一段正文\n\n![](./{name})\n"))
        self.assertEqual(self.assert_clean_image(image_path).size, (1600, 80))
        self.assertEqual(self.box.remote_file(NOTES_PATH), FIXTURE_NOTES)
        self.assertEqual(self.api.text(self.api.receipt_for(50)), self.submitted(commit))

    def test_list_reads_origin_main(self):
        self.deliver(self.text("给 list 看的", mid=55))
        self.box.other_commit({"README.md": "laptop\n"})
        self.deliver(self.text("/list", mid=56))
        reply = self.api.text(self.api.last_sent())
        self.assertTrue(reply.startswith("<b>最近 5 条随想</b>"))
        first = reply.index(f"<code>{DATE}</code> 给 list 看的")
        self.assertLess(first, reply.index("<code>2026-04-11</code> 春天"))
        self.assertIn(f'<a href="{SITE}/notes/{DATE}.html">', reply)


# --- (f) undo ----------------------------------------------------------------------------


class UndoFlowTests(Flow):
    def test_undo_twice_then_nothing_left(self):
        self.deliver(self.photo("first", solid_png(RED), mid=60, caption="第一条"))
        after_first = self.box.remote_file(NOTES_PATH)
        first_images = self.remote_files(NOTES_ASSET_DIR)
        self.deliver(self.photo("second", solid_png(BLUE), mid=61, caption="# 第二条\n内容"))
        second, first = self.commits()
        [second_image] = sorted(set(self.remote_files(NOTES_ASSET_DIR)) - set(first_images))

        self.deliver(self.text("/undo", mid=62))
        confirm = self.api.last_sent()
        question = self.api.text(confirm)
        for expected in (second.sha[:7], "note: 第二条", f"<code>{NOTES_PATH}</code>", f"<code>{second_image}</code>"):
            self.assertIn(expected, question)
        self.assertEqual(len(self.commits()), 2)  # nothing happens before the click
        self.click(confirm)

        revert = self.commits()[0]
        self.assert_bot_commit(revert, 'Revert "note: 第二条"', {"Pnc-Bot": "undo", "Pnc-Bot-Reverts": second.sha})
        self.assertEqual(self.box.remote_file(NOTES_PATH), after_first)
        self.assertEqual(self.remote_files(NOTES_ASSET_DIR), first_images)
        self.assertEqual(
            self.api.history[confirm][-2:],
            ["⏳ 执行中…", f"↩️ 已撤销 <code>{second.sha[:7]}</code>（提交 <code>{revert.sha[:7]}</code>），部署中…"],
        )

        self.deliver(self.text("/undo", mid=63))
        confirm = self.api.last_sent()
        self.assertIn(first.sha[:7], self.api.text(confirm))
        self.assertIn("note: 第一条", self.api.text(confirm))
        self.click(confirm)
        self.assertEqual(self.commits()[0].trailers, {"Pnc-Bot": "undo", "Pnc-Bot-Reverts": first.sha})
        self.assertEqual(self.box.remote_file(NOTES_PATH), FIXTURE_NOTES)
        self.assertEqual(self.remote_files(NOTES_ASSET_DIR), [])

        self.deliver(self.text("/undo", mid=64))
        self.assertEqual(self.api.text(self.api.last_sent()), "没有可以撤销的机器人提交")
        self.assertEqual(self.box.status(), "")

    def test_undone_note_keeps_its_replies_away_from_the_next_one(self):
        self.deliver(self.text("笔记A", mid=67))
        receipt_a = self.api.receipt_for(67)
        self.deliver(self.text("/undo", mid=68))
        self.click(self.api.last_sent())
        self.deliver(self.text("笔记B", mid=69))
        self.assertEqual(self.remote_notes()[0].id, DATE)  # B got A's id back
        self.deliver(self.text("回复A", mid=70, **replying_to(receipt_a)))
        self.deliver(self.text("回复A的原消息", mid=71, **replying_to(67)))
        self.assertEqual([c.subject for c in self.commits()], ["note: 笔记B", 'Revert "note: 笔记A"', "note: 笔记A"])
        self.assertEqual(self.remote_notes()[0].body, "笔记B")
        self.assertEqual(self.api.text(self.api.last_sent()), NOT_A_NOTE_REPLY)

    def test_undo_a_draft(self):
        self.deliver(self.photo("d", solid_png(GREEN), mid=65, caption="/draft 草稿 | undo-me"))
        self.deliver(self.text("/undo", mid=66))
        self.click(self.api.last_sent())
        self.assertEqual(self.commits()[0].subject, 'Revert "draft: undo-me"')
        self.assertEqual(self.remote_files("src/content/posts/undo-me"), [])


# --- (g) authorization -----------------------------------------------------------------


class AuthorizationTests(Flow):
    def test_unauthorized_updates_change_nothing(self):
        self.deliver(self.text("主人的随想", mid=70))
        self.deliver(self.text("/undo", mid=71))
        confirm = self.api.last_sent()
        yes = self.api.button(confirm)
        remote_head = self.box.remote_head()
        bot_head = self.box.git(self.box.bot_path, "rev-parse", "HEAD")
        calls_before = len(self.api.calls)
        pic = solid_png(GREEN)

        def query(data, *, uid=OWNER, chat=None, mid=confirm):
            return {"callback_query": {"id": "x", "from": user(uid), "message": {"message_id": mid, "date": NOW, "chat": chat or private()}, "chat_instance": "1", "data": data}}

        attempts = [
            self.text("/note 陌生人", mid=72, uid=STRANGER),
            self.photo("s1", pic, mid=73, uid=STRANGER, caption="陌生人的图"),
            self.photo("s2", pic, mid=74, uid=STRANGER, group="evil-album"),
            self.photo("s3", pic, mid=75, uid=STRANGER, group="evil-album"),
            self.text("/append 2026-04-07 陌生人", mid=76, uid=STRANGER, **replying_to(confirm)),
            self.photo("g1", pic, mid=77, chat=GROUP, caption="/note 群里"),
            self.text("/undo", mid=78, chat=SUPERGROUP),
            self.text("/deploy", mid=79, chat={"id": OWNER, "type": "group"}),
            self.text("/note 字符串 id", mid=80, chat={"id": str(OWNER), "type": "private"}),
            {"message": {**message(81, text="/note 机器人"), "from": {"id": OWNER, "is_bot": True}}},
            {"message": message(82, text="/note 匿名", sender_chat=CHANNEL)},
            {"channel_post": message(83, chat=CHANNEL, photo=[{"file_id": "c1", "width": 9, "height": 9}])},
            {"edited_message": message(84, text="/note 编辑过")},
            {"edited_channel_post": message(85, chat=CHANNEL, text="x")},
            query(yes, uid=STRANGER),
            query(yes, chat=GROUP),
            query("dc:1", uid=STRANGER),
            {"inline_query": {"id": "1", "from": user(), "query": "x", "offset": ""}},
            {"my_chat_member": {"chat": GROUP, "from": user()}},
            {"chat_join_request": {"chat": GROUP, "from": user()}},
            {"message_reaction": {"chat": private(), "message_id": 70, "user": user()}},
            {"message": message(86, text="/note 两种"), "edited_message": message(86, text="x")},
        ]
        self.api.files["c1"] = pic
        with self.assertLogs("pnc_bot", level="INFO") as logs:
            self.deliver(*attempts)
            self.flush_albums()

        for line in logs.output:
            self.assertRegex(line, r"ignored [a-z_]+$")
            self.assertNotIn(str(OWNER), line)
            self.assertNotIn(str(STRANGER), line)
        self.assertEqual(len(logs.output), len(attempts))
        self.assertEqual(set(self.api.methods()[calls_before:]), {"getUpdates"})
        self.assertEqual(self.api.downloads, [])
        self.assertEqual(self.box.remote_head(), remote_head)
        self.assertEqual(self.box.git(self.box.bot_path, "rev-parse", "HEAD"), bot_head)
        self.assertFalse(self.app.media.pending())
        self.assertEqual(len(self.app.confirmations), 1)
        self.assertEqual(self.state.get_int("update_offset"), self.next_update)
        self.assertEqual(self.state.recent_note_ids(), [DATE])

        self.click(confirm)  # the owner's own click still works afterwards
        self.assertEqual(self.commits()[0].subject, 'Revert "note: 主人的随想"')


# --- (h) confirmation nonces ------------------------------------------------------------


class ConfirmationTests(Flow):
    def setUp(self):
        super().setUp()
        self.deliver(self.text("要撤销的随想", mid=90))
        [self.note] = self.commits()

    def ask_undo(self, mid: int) -> int:
        self.deliver(self.text("/undo", mid=mid))
        return self.api.last_sent()

    def test_reused_nonce_runs_once(self):
        confirm = self.ask_undo(91)
        self.click(confirm)
        result = self.api.text(confirm)
        self.assertTrue(result.startswith("↩️ 已撤销"), result)
        self.click(confirm)  # a second tap: answered, the result stays
        self.assertEqual([c.subject for c in self.commits()], ['Revert "note: 要撤销的随想"', "note: 要撤销的随想"])
        self.assertEqual(self.api.params("answerCallbackQuery")[-1].get("text"), "已处理")
        self.assertEqual(self.api.text(confirm), result)

    def test_expired_nonce_does_nothing(self):
        confirm = self.ask_undo(92)
        self.mono += 121
        self.click(confirm)
        self.assertEqual(self.commits(), [self.note])
        self.assertEqual(self.api.text(confirm), "已过期")

    def test_cancel_consumes_the_nonce(self):
        confirm = self.ask_undo(93)
        self.click(confirm, "n")
        self.click(confirm, "y")
        self.assertEqual(self.commits(), [self.note])
        self.assertEqual(self.api.history[confirm][-1], "已取消")
        self.assertEqual(self.api.params("answerCallbackQuery")[-1].get("text"), "已处理")

    def test_nonce_is_bound_to_its_message(self):
        first = self.ask_undo(94)
        second = self.ask_undo(95)
        self.click(second, data=self.api.button(first))
        self.assertEqual(self.commits(), [self.note])
        self.click(second)
        self.assertEqual(len(self.commits()), 2)

    def test_forged_nonce(self):
        confirm = self.ask_undo(96)
        self.click(confirm, data="cf:0123456789abcdef:y")
        self.assertEqual(self.commits(), [self.note])
        self.click(confirm)
        self.assertEqual(len(self.commits()), 2)


# --- (i) push race, (j) deploy tracking ---------------------------------------------------


class PushRaceTests(Flow):
    def test_push_race_recomputes_the_note_id(self):
        laptop_notes, laptop_id = insert_note(FIXTURE_NOTES, date=DATE, title="笔记本", body="电脑上写的")
        self.assertEqual(laptop_id, DATE)
        raced: list[str] = []

        def race(argv):  # the laptop pushes between the bot's sync and its push
            if not raced:
                raced.append(self.box.other_commit({NOTES_PATH: laptop_notes}, "note from the laptop"))
            return None

        self.box.before_push = race
        self.deliver(self.text("机器人写的", mid=97))

        bot, laptop = self.commits()
        self.assertEqual(laptop.sha, raced[0])
        self.assertEqual(self.box.remote_git("rev-parse", f"{bot.sha}^"), laptop.sha)
        self.assert_bot_commit(bot, "note: 机器人写的", {"Pnc-Bot": "note-add", "Pnc-Bot-Note": f"{DATE}-2"})
        current = self.remote_notes()
        self.assertEqual(
            [(n.id, n.title, n.body) for n in current[:2]], [(f"{DATE}-2", None, "机器人写的"), (DATE, "笔记本", "电脑上写的")]
        )
        self.assertEqual([n.id for n in current[2:]], [n.id for n in parse_notes(FIXTURE_NOTES)])
        receipt = self.api.receipt_for(97)
        self.assertEqual(self.api.text(receipt), self.submitted(bot))
        self.assertEqual(self.state.note_for_message(receipt), f"{DATE}-2")
        self.assertEqual(self.box.status(), "")

        write_records(self.blog, deploy_record(bot.sha, "success"))
        self.app.tracker.poll_once()
        self.assertIn(f"{SITE}/notes/{DATE}-2.html", self.api.text(receipt))
        self.assertEqual(self.http_calls, [f"{LOCAL}/notes/{DATE}-2.html"])


class PushAmbiguityTests(Flow):
    def test_push_that_landed_despite_a_dropped_connection(self):
        def hung_up_after_pushing(argv):
            self.box.before_push = None
            try:
                proc = self.box.run(argv, input=b"", capture_output=True, env=self.app.repo._env(), timeout=60)
            finally:
                self.box.before_push = hung_up_after_pushing
            self.assertEqual(proc.returncode, 0)
            return subprocess.CompletedProcess(argv, 128, b"", b"fatal: the remote end hung up unexpectedly\n")

        self.box.before_push = hung_up_after_pushing
        with self.assertLogs("pnc_bot.gitrepo", level="WARNING"):
            self.deliver(self.text("只推送一次", mid=101))
        [commit] = self.commits()
        receipt = self.api.receipt_for(101)
        self.assertEqual(self.api.text(receipt), self.submitted(commit))
        self.assertEqual(self.state.note_for_message(receipt), DATE)
        self.assertEqual(self.box.status(), "")
        write_records(self.blog, deploy_record(commit.sha, "success"))
        self.app.tracker.poll_once()
        self.assertTrue(self.api.text(receipt).startswith(f"✅ 已上线 <b>{DATE}</b>"))


class DeployTrackingTests(Flow):
    def test_receipt_goes_live_when_a_descendant_is_deployed(self):
        self.deliver(self.text("上线测试", mid=98))
        [commit] = self.commits()
        receipt = self.api.receipt_for(98)

        write_records(self.blog, deploy_record(self.seed, "success"))  # older: does not contain the note
        self.app.tracker.poll_once()
        self.assertEqual(self.api.text(receipt), self.submitted(commit))
        self.assertEqual(self.app.tracker.pending(), 1)

        later = self.box.other_commit({"README.md": "later\n"}, "later commit")  # only this one gets deployed
        self.http_status = [404]  # the first local check misses, the retry finds the page
        write_records(self.blog, deploy_record(later, "success"))
        self.app.tracker.poll_once()

        url = f"{SITE}/notes/{DATE}.html"
        self.assertEqual(self.api.text(receipt), f'✅ 已上线 <b>{DATE}</b>\n<a href="{url}">{url}</a>')
        self.assertEqual(self.http_calls, [f"{LOCAL}/notes/{DATE}.html"] * 2)
        self.assertEqual(self.app.tracker.pending(), 0)

    def test_draft_receipt_after_deploy(self):
        self.deliver(self.photo("d", solid_png(RED), mid=99, caption="/draft 部署的草稿 | deployed-draft"))
        [commit] = self.commits()
        write_records(self.blog, deploy_record(commit.sha, "success"))
        self.app.tracker.poll_once()
        self.assertEqual(self.api.text(self.api.receipt_for(99)), "✅ 草稿已推送：<code>src/content/posts/deployed-draft/</code>")
        self.assertEqual(self.http_calls, [])  # drafts are not published, so nothing to check

    def test_failed_deploy_on_the_receipt_and_standalone(self):
        self.deliver(self.text("会失败", mid=100))
        [commit] = self.commits()
        receipt = self.api.receipt_for(100)
        log = self.blog / "logs" / "deploy-20261121-120000-abcdef123456.log"
        log.write_text("step 1\nnpm ERR! build failed\nleaked 123456789:" + "A" * 35 + "\n", encoding="utf-8")
        write_records(self.blog, deploy_record(commit.sha, "failure", error="npm run build failed", log=str(log)))
        self.app.tracker.poll_once()

        text = self.api.text(receipt)
        self.assertTrue(text.startswith("❌ <b>部署失败</b>"))
        for expected in ("npm run build failed", "npm ERR! build failed", "[REDACTED]", "/logs deploy", "/undo"):
            self.assertIn(expected, text)
        self.assertNotIn("123456789:", text)

        sent = len(self.api.params("sendMessage"))
        write_records(self.blog, deploy_record("b" * 40, "failure", error="someone else's deploy"))
        self.app.tracker.poll_once()  # nobody waits for this one: it becomes its own message
        self.assertEqual(len(self.api.params("sendMessage")), sent + 1)
        standalone = self.api.text(self.api.last_sent())
        self.assertTrue(standalone.startswith("❌ <b>部署失败</b>"))
        self.assertIn("someone else&#x27;s deploy", standalone)


# --- the loop ---------------------------------------------------------------------------


class FirstStartTests(Flow):
    PRIMED_OFFSET = False

    def test_startup_drains_the_backlog_then_handles_new_updates(self):
        self.app.startup()
        [commands] = self.api.params("setMyCommands")
        self.assertEqual(commands["scope"], {"type": "chat", "chat_id": OWNER})

        self.api.updates.append([{"update_id": 500, **self.text("/note 积压的旧消息", mid=1)}])
        self.app.poll_once()
        self.assertEqual(self.api.params("getUpdates")[0], {"timeout": 0, "allowed_updates": ["message", "callback_query"], "offset": -1})
        self.assertEqual(self.state.get_int("update_offset"), 501)
        self.assertEqual(self.commits(), [])

        self.next_update = 501
        self.deliver(self.text("新消息", mid=2))
        self.assertEqual(self.api.params("getUpdates")[1]["offset"], 501)
        self.assertEqual([c.subject for c in self.commits()], ["note: 新消息"])
        self.assertEqual(self.state.get_int("update_offset"), 502)

    def test_old_message_and_status(self):
        self.state.set("update_offset", self.next_update)
        self.deliver(self.text("/note 十一分钟前", mid=3, date=NOW - 660))
        self.assertEqual(self.api.text(self.api.last_sent()), "消息太旧，已忽略")
        self.assertEqual(self.commits(), [])

        self.deliver(self.text("/status", mid=4))
        status = self.api.text(self.api.last_sent())
        self.assertTrue(status.startswith("<b>📊 状态</b>"))
        self.assertIn(f"<code>{RELEASE}</code>", status)
        self.assertIn("队列 0", status)


if __name__ == "__main__":
    unittest.main()
