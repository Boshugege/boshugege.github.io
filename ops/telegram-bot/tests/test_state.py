import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from pnc_bot.state import State


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "state"
        self.state = State(self.dir)

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def test_permissions(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.dir / "state.db").st_mode) & 0o077, 0)

    def test_kv(self):
        self.assertIsNone(self.state.get("update_offset"))
        self.assertIsNone(self.state.get_int("update_offset"))
        self.state.set("update_offset", 5)
        self.state.set("update_offset", 6)
        self.assertEqual(self.state.get("update_offset"), "6")
        self.assertEqual(self.state.get_int("update_offset"), 6)
        self.state.set("x", "not a number")
        self.assertIsNone(self.state.get_int("x"))

    def test_notes_and_messages(self):
        self.state.record_note("2026-11-21", "a" * 40, 101, [100])
        self.state.record_note("2026-11-21-2", "b" * 40, 201, [198, 199])
        self.assertEqual(self.state.note_for_message(101), "2026-11-21")
        self.assertEqual(self.state.note_for_message(100), "2026-11-21")
        self.assertEqual(self.state.note_for_message(198), "2026-11-21-2")
        self.assertEqual(self.state.note_for_message(199), "2026-11-21-2")
        self.assertIsNone(self.state.note_for_message(5))
        self.assertEqual(self.state.recent_note_ids(), ["2026-11-21-2", "2026-11-21"])
        self.assertEqual(self.state.last_note_id(), "2026-11-21-2")

    def test_receipt_may_be_missing(self):
        self.state.record_note("2026-11-21", "a" * 40, None, [7])
        self.assertEqual(self.state.note_for_message(7), "2026-11-21")

    def test_map_messages(self):
        self.state.map_messages("2026-01-01", [10, None, 11])
        self.assertEqual(self.state.note_for_message(11), "2026-01-01")
        self.state.map_messages("2026-01-02", [11])
        self.assertEqual(self.state.note_for_message(11), "2026-01-02")
        self.assertEqual(self.state.recent_note_ids(), [])  # appends are not new notes

    def test_forget_note(self):
        """An undone note: its id is free again and must name nothing the bot made."""
        self.state.record_note("2026-11-21", "a" * 40, 101, [100])
        self.state.map_messages("2026-11-21", [102, 103])  # an append's receipt and source
        self.state.record_note("2026-11-20", "b" * 40, 91, [90])
        self.assertEqual(self.state.forget_note("2026-11-21"), 4)
        for mid in (100, 101, 102, 103):
            self.assertIsNone(self.state.note_for_message(mid))
        self.assertEqual(self.state.recent_note_ids(), ["2026-11-20"])
        self.assertEqual(self.state.note_for_message(91), "2026-11-20")
        # The id is reused by the next note of that day: only the new messages map to it.
        self.state.record_note("2026-11-21", "c" * 40, 201, [200])
        self.assertEqual(self.state.note_for_message(201), "2026-11-21")
        self.assertIsNone(self.state.note_for_message(101))
        self.assertEqual(self.state.forget_note("2099-01-01"), 0)

    def test_recent_ids_deduplicated(self):
        self.state.record_note("a", "1" * 40, 1, [])
        self.state.record_note("b", "2" * 40, 2, [])
        self.state.record_note("a", "3" * 40, 3, [])
        self.assertEqual(self.state.recent_note_ids(), ["a", "b"])

    def test_persists(self):
        self.state.set("comments_last_id", 12)
        self.state.record_note("n", "1" * 40, 1, [2])
        self.state.close()
        self.state = State(self.dir)
        self.assertEqual(self.state.get_int("comments_last_id"), 12)
        self.assertEqual(self.state.note_for_message(2), "n")

    def test_threads(self):
        def worker(n):
            for i in range(50):
                self.state.set(f"k{n}", i)
                self.state.map_messages(f"note{n}", [n * 1000 + i])

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for n in range(4):
            self.assertEqual(self.state.get_int(f"k{n}"), 49)
            self.assertEqual(self.state.note_for_message(n * 1000 + 49), f"note{n}")


if __name__ == "__main__":
    unittest.main()
