import unittest

from pnc_bot import auth

OWNER = 424242
STRANGER = 777


def message(*, uid=OWNER, chat_id=OWNER, chat_type="private", is_bot=False, **extra):
    msg = {
        "message_id": 1,
        "date": 0,
        "from": {"id": uid, "is_bot": is_bot, "first_name": "x"},
        "chat": {"id": chat_id, "type": chat_type},
        "text": "hi",
    }
    msg.update(extra)
    return msg


def callback(*, uid=OWNER, chat_id=OWNER, chat_type="private", **extra):
    cb = {
        "id": "123",
        "from": {"id": uid, "is_bot": False, "first_name": "x"},
        "message": {"message_id": 5, "date": 0, "chat": {"id": chat_id, "type": chat_type}},
        "data": "cf:0123456789abcdef:y",
    }
    cb.update(extra)
    return cb


class AuthorizedMessageTest(unittest.TestCase):
    def test_owner_in_private_chat(self):
        self.assertTrue(auth.authorized_message(message(), OWNER))

    def test_stranger_in_private_chat(self):
        self.assertFalse(auth.authorized_message(message(uid=STRANGER, chat_id=STRANGER), OWNER))

    def test_owner_in_group(self):
        self.assertFalse(auth.authorized_message(message(chat_id=-1001234, chat_type="group"), OWNER))
        self.assertFalse(auth.authorized_message(message(chat_id=-1001234, chat_type="supergroup"), OWNER))

    def test_owner_id_but_not_private(self):
        self.assertFalse(auth.authorized_message(message(chat_type="group"), OWNER))

    def test_chat_id_mismatch(self):
        self.assertFalse(auth.authorized_message(message(chat_id=STRANGER), OWNER))

    def test_string_ids_rejected(self):
        self.assertFalse(auth.authorized_message(message(uid=str(OWNER)), OWNER))
        self.assertFalse(auth.authorized_message(message(chat_id=str(OWNER)), OWNER))
        self.assertFalse(auth.authorized_message(message(), str(OWNER)))

    def test_bool_is_not_an_id(self):
        self.assertFalse(auth.authorized_message(message(uid=True, chat_id=True), 1))

    def test_bot_sender_rejected(self):
        self.assertFalse(auth.authorized_message(message(is_bot=True), OWNER))

    def test_missing_is_bot_rejected(self):
        msg = message()
        del msg["from"]["is_bot"]
        self.assertFalse(auth.authorized_message(msg, OWNER))

    def test_sent_on_behalf_of_chat_rejected(self):
        self.assertFalse(auth.authorized_message(message(sender_chat={"id": -1, "type": "channel"}), OWNER))

    def test_missing_parts(self):
        for broken in ({}, {"from": {"id": OWNER, "is_bot": False}}, None, "x", []):
            self.assertFalse(auth.authorized_message(broken, OWNER))

    def test_non_positive_allowed_id(self):
        self.assertFalse(auth.authorized_message(message(uid=0, chat_id=0), 0))


class AuthorizedCallbackTest(unittest.TestCase):
    def test_owner_on_private_message(self):
        self.assertTrue(auth.authorized_callback(callback(), OWNER))

    def test_stranger_on_owner_chat(self):
        self.assertFalse(auth.authorized_callback(callback(uid=STRANGER), OWNER))

    def test_owner_on_group_message(self):
        self.assertFalse(auth.authorized_callback(callback(chat_id=-100, chat_type="group"), OWNER))
        self.assertFalse(auth.authorized_callback(callback(chat_type="supergroup"), OWNER))

    def test_inline_callback_without_message(self):
        cb = callback()
        del cb["message"]
        cb["inline_message_id"] = "abc"
        self.assertFalse(auth.authorized_callback(cb, OWNER))

    def test_bad_types(self):
        self.assertFalse(auth.authorized_callback(callback(uid=str(OWNER)), OWNER))
        self.assertFalse(auth.authorized_callback(callback(id=123), OWNER))
        cb = callback()
        cb["message"]["message_id"] = "5"
        self.assertFalse(auth.authorized_callback(cb, OWNER))
        self.assertFalse(auth.authorized_callback(None, OWNER))


class ClassifyTest(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(auth.classify({"update_id": 1, "message": {}}), "message")
        self.assertEqual(auth.classify({"update_id": 1, "edited_message": {}}), "edited_message")
        self.assertEqual(auth.classify({"update_id": 1, "channel_post": {}}), "channel_post")
        self.assertEqual(auth.classify({"update_id": 1, "callback_query": {}}), "callback_query")
        self.assertEqual(auth.classify({"update_id": 1, "my_chat_member": {}}), "my_chat_member")

    def test_unknown_and_malformed(self):
        self.assertEqual(auth.classify({"update_id": 1, "secret text here": {}}), "unknown")
        self.assertEqual(auth.classify({"update_id": 1}), "malformed")
        self.assertEqual(auth.classify({"update_id": 1, "message": {}, "callback_query": {}}), "malformed")
        self.assertEqual(auth.classify(None), "malformed")


if __name__ == "__main__":
    unittest.main()
