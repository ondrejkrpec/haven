"""Optional native Hermes bridge: capture first, execute only after native admission."""

import asyncio
from contextvars import ContextVar
import hashlib
import os
from pathlib import Path
import socket
import stat

from scripts.wishlist_domain import digest
from scripts.wishlist_protocol import DEADLINE, close_writer, peer_uid, read_frame, write_frame
from scripts.wishlist_service import private_directory, socket_path, validate_settings
from scripts.wishlist_telegram import UNAVAILABLE, relevant, validate_event


class Client:
    def __init__(self, settings):
        self.settings = validate_settings(settings)

    async def request(self, event):
        path = socket_path(self.settings["socket"])
        private_directory(path.parent, uid=self.settings["service_uid"], socket_directory=True)
        info = path.lstat()
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.settings["service_uid"]
                or stat.S_IMODE(info.st_mode) != 0o660):
            raise ValueError("wishlist_socket_untrusted")
        writer = None
        try:
            async with asyncio.timeout(26 if self.settings.get("natural_language") else DEADLINE):
                reader, writer = await asyncio.open_unix_connection(path, limit=65536)
                if peer_uid(writer.get_extra_info("socket")) != self.settings["service_uid"]:
                    raise PermissionError("wishlist_server_denied")
                await write_frame(writer, {"protocol_version": 1, "operation": "telegram.message", "event": event})
                response = await read_frame(reader)
            expected = digest([event["bot_id"], "telegram", event["user_id"], event["chat_id"], event["message_id"]])
            if (set(response) != {"protocol_version", "request_id", "code", "committed", "reply"}
                    or type(response["protocol_version"]) is not int or response["protocol_version"] != 1
                    or response["request_id"] != expected or type(response["committed"]) is not bool
                    or response["code"] not in {"ok", "needs_clarification", "conflict", "not_found",
                                                 "invalid_request", "denied", "unavailable"}
                    or not isinstance(response["reply"], str) or not response["reply"]
                    or len(response["reply"].encode("utf-16-le")) > 7600):
                raise ValueError("wishlist_response_invalid")
            return response
        finally:
            if writer:
                await close_writer(writer)


class Bridge:
    def __init__(self, owner_id, settings):
        self.owner_id, self.client = owner_id, Client(settings)
        self.settings = self.client.settings
        self.captured = ContextVar("haven_wishlist_native_message", default=None)

    def capture(self, event, **kwargs):
        """Synchronous by design: plugin dispatch invokes sync capture in the parent task."""
        self.captured.set(None)
        try:
            raw, source = event.raw_message, event.source
            bot = raw.get_bot()
            snapshot = {"bot_id": str(bot.id), "user_id": str(raw.from_user.id), "chat_id": str(raw.chat.id),
                        "message_id": str(raw.message_id), "text": raw.text, "chat_type": raw.chat.type,
                        "is_bot": raw.from_user.is_bot, "edited": raw.edit_date is not None}
            validate_event(snapshot, bot_id=self.settings["bot_id"], owner_id=self.owner_id)
            if (event.internal or event.media_urls or getattr(event, "edit_revision", None)
                    or source.platform.value != "telegram" or source.chat_type != "dm" or source.thread_id
                    or source.user_id != snapshot["user_id"] or source.chat_id != snapshot["chat_id"]
                    or event.message_id != snapshot["message_id"] or event.text != snapshot["text"]):
                raise ValueError("native_event_mismatch")
            self.captured.set((snapshot, bot))
        except Exception:
            pass
        return {"action": "allow"}

    async def consume(self, session_key, platform, source, message_id, text, **kwargs):
        if not relevant(text) and not self.settings.get("natural_language"):
            return None
        captured = self.captured.get()
        self.captured.set(None)
        try:
            if captured is None:
                raise PermissionError("native_capture_required")
            event, bot = captured
            if (platform != "telegram" or source.get("platform") != "telegram" or source.get("chat_type") != "dm"
                    or source.get("user_id") != event["user_id"] or source.get("chat_id") != event["chat_id"]
                    or source.get("is_bot") is True or source.get("thread_id")
                    or message_id != event["message_id"] or text != event["text"]):
                raise PermissionError("native_source_mismatch")
            result = await self.client.request(event)
            # Explicit None disables bot defaults. User/shop text is never Markdown or HTML.
            from telegram import ReplyParameters
            async with asyncio.timeout(3 if self.settings.get("natural_language") else 10):
                await bot.send_message(chat_id=int(event["chat_id"]), text=result["reply"], parse_mode=None,
                                       disable_web_page_preview=True,
                                       reply_parameters=ReplyParameters(message_id=int(message_id),
                                                                        allow_sending_without_reply=True))
            return {"action": "handled"}  # Existing bot delivered the plain-text reply.
        except Exception:
            # The independent model/tool guard remains armed even if this consumer fails.
            reply = ("Výsledek wishlistu teď nemohu ověřit. Než změnu zopakuješ, zeptej se, co máš na wishlistu."
                     if self.settings.get("natural_language") else UNAVAILABLE)
            return {"action": "handled", "reply": reply}


def check_runtime(source):
    """Verify the optional bridge's exact native admission/dispatch assumptions."""
    import json
    from scripts.capability_runtime import ROOT, trusted_bytes
    from scripts.model_contract import HERMES_COMMIT
    pins = json.loads(trusted_bytes(ROOT / "config/wishlist-runtime.json"))
    if pins["hermes_commit"] != HERMES_COMMIT:
        raise ValueError("wishlist_revision_mismatch")
    for name, expected in pins["source_sha256"].items():
        actual = hashlib.sha256(trusted_bytes(Path(source) / name, owner=Path(source).stat().st_uid)).hexdigest()
        if actual != expected:
            raise ValueError("wishlist_source_mismatch")
    if os.name != "posix" or not hasattr(socket, "SO_PEERCRED"):
        raise ValueError("wishlist_linux_required")
