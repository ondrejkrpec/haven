"""W2 Linux local service. Explicit configuration; no network fetches or Telegram poller."""

import argparse
import asyncio
import os
from pathlib import Path
import re
import signal
import stat

from scripts.wishlist_domain import IngressContext, Wishlist
from scripts.wishlist_protocol import MAX_FRAME, decode
from scripts.wishlist_socket import PeerServer, private_directory, socket_path
from scripts.wishlist_telegram import TelegramApplication


def validate_settings(settings):
    if (not isinstance(settings, dict) or not {"socket", "service_uid", "bot_id"} <= set(settings)
            or set(settings) - {"socket", "service_uid", "bot_id", "natural_language"}
            or ("natural_language" in settings and type(settings["natural_language"]) is not bool)
            or type(settings["service_uid"]) is not int or not 1 <= settings["service_uid"] <= 2**31 - 1
            or not isinstance(settings["bot_id"], str)
            or not re.fullmatch(r"[1-9][0-9]{0,19}", settings["bot_id"])):
        raise ValueError("wishlist_settings")
    socket_path(settings["socket"])
    return dict(settings)


def load_service_config(path):
    with path.open("rb") as stream:
        config = decode(stream.read(MAX_FRAME + 1))
    keys = {"schema_version", "database", "socket", "socket_gid", "ingress_uid", "bot_id", "owner_id", "timezone"}
    if not keys <= set(config) or set(config) - keys - {"language"} or type(config["schema_version"]) is not int or config["schema_version"] != 1:
        raise ValueError("service_config")
    validate_settings({"socket": config["socket"], "service_uid": config["ingress_uid"], "bot_id": config["bot_id"]})
    if type(config["socket_gid"]) is not int or not 1 <= config["socket_gid"] <= 2**31 - 1:
        raise ValueError("socket_group")
    database = Path(config["database"])
    if not database.is_absolute() or database.resolve() != database:
        raise ValueError("database_path")
    if "language" in config:
        from scripts.wishlist_interpreter import validate_language_settings
        settings = validate_language_settings(config["language"])
        if settings["service_uid"] in (os.geteuid(), config["ingress_uid"]):
            raise ValueError("language_identity_must_be_separate")
    return config


def private_database(path):
    private_directory(path.parent, uid=os.geteuid())
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise ValueError("private_database_required")
    finally:
        os.close(descriptor)


class Server(PeerServer):
    def __init__(self, domain, path, *, ingress_uid, language=None, **kwargs):
        self.domain = domain
        self.application = TelegramApplication(domain)
        self.language = language
        if language is not None:
            from scripts.wishlist_language import LanguageFlow
            self.language = LanguageFlow(domain, self.application, language)
            kwargs.setdefault("deadline", 24)
        super().__init__(path, ingress_uid=ingress_uid, **kwargs)

    async def respond(self, request):
        if set(request) == {"protocol_version", "operation", "event"}:
            if type(request["protocol_version"]) is not int or request["protocol_version"] != 1:
                return self.domain.response(None, "invalid_request")
            if request["operation"] != "telegram.message":
                return self.domain.response(None, "denied")
            if self.language is not None:
                return await self.language.handle(request["event"])
            return self.application.handle(request["event"])
        source = request.get("source", {})
        context = IngressContext(self.domain.bot_id, source.get("user_id"), source.get("chat_id"),
                                 source.get("message_id"), source.get("text_sha256"),
                                 source.get("platform")) if isinstance(source, dict) else None
        return self.domain.handle(request, context)


async def serve(config):
    path = Path(config["database"])
    private_database(path)
    domain = Wishlist(path, bot_id=config["bot_id"], owner_id=config["owner_id"], timezone=config["timezone"])
    server = Server(domain, config["socket"], ingress_uid=config["ingress_uid"], socket_gid=config["socket_gid"],
                    language=config.get("language"))
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    try:
        await server.start()
        print('{"result":"ready","service":"wishlist"}', flush=True)
        await stopped.wait()
    finally:
        await server.close()
        domain.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        os.umask(0o077)
        asyncio.run(serve(load_service_config(args.config)))
    except Exception:
        print('{"result":"failed","code":"wishlist_service_failed"}', flush=True)
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
