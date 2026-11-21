"""Who may talk to the bot: exactly one user, in the private chat with them.

These predicates run before anything else touches an update (no reply, no
download, no buffering for anything they reject). Types are checked strictly:
ids must be real ints (not bools or strings).
"""

from __future__ import annotations

from typing import Any

# Update kinds the Bot API can deliver; anything else is logged as "unknown"
# so no attacker-chosen key ever reaches the log.
KNOWN_KINDS = frozenset(
    {
        "message",
        "edited_message",
        "channel_post",
        "edited_channel_post",
        "business_connection",
        "business_message",
        "edited_business_message",
        "deleted_business_messages",
        "message_reaction",
        "message_reaction_count",
        "inline_query",
        "chosen_inline_result",
        "callback_query",
        "shipping_query",
        "pre_checkout_query",
        "purchased_paid_media",
        "poll",
        "poll_answer",
        "my_chat_member",
        "chat_member",
        "chat_join_request",
        "chat_boost",
        "removed_chat_boost",
    }
)


def _is_id(value: Any) -> bool:
    return type(value) is int


def _is_owner(user: Any, allowed: int) -> bool:
    return (
        isinstance(user, dict)
        and _is_id(user.get("id"))
        and user["id"] == allowed
        and user.get("is_bot") is False
    )


def _is_owner_private_chat(chat: Any, allowed: int) -> bool:
    return (
        isinstance(chat, dict)
        and chat.get("type") == "private"
        and _is_id(chat.get("id"))
        and chat["id"] == allowed
    )


def authorized_message(msg: Any, allowed: int) -> bool:
    """A message from the owner in the owner's private chat with the bot."""
    if not _is_id(allowed) or allowed <= 0 or not isinstance(msg, dict):
        return False
    if "sender_chat" in msg or "sender_business_bot" in msg or "business_connection_id" in msg:
        return False
    return _is_owner(msg.get("from"), allowed) and _is_owner_private_chat(msg.get("chat"), allowed)


def authorized_callback(cb: Any, allowed: int) -> bool:
    """A button press by the owner on a message in the owner's private chat."""
    if not _is_id(allowed) or allowed <= 0 or not isinstance(cb, dict):
        return False
    if not isinstance(cb.get("id"), str) or not cb["id"]:
        return False
    message = cb.get("message")
    if not isinstance(message, dict) or not _is_id(message.get("message_id")):
        return False
    return _is_owner(cb.get("from"), allowed) and _is_owner_private_chat(message.get("chat"), allowed)


def classify(update: Any) -> str:
    """The update's kind (``message``, ``callback_query``…) for routing and logs."""
    if not isinstance(update, dict):
        return "malformed"
    kinds = [key for key in update if key != "update_id"]
    if len(kinds) != 1:
        return "malformed"
    kind = kinds[0]
    return kind if kind in KNOWN_KINDS else "unknown"
