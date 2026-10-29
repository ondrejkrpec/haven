"""Private Telegram enrollment and bounded runtime checks; never print tokens."""

import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import urllib.request

from scripts.model_probe import ProbeError, private_file

HOME = Path("/var/lib/haven/.hermes")
SOURCE = Path("/var/lib/haven/releases/hermes-bf0203a-785f2646")
PINS = Path(__file__).resolve().parents[1] / "config/telegram-runtime.json"
TOKEN_NAME = "telegram-bot-token"


def check_dependency(source=SOURCE):
    import telegram
    pins = json.loads(PINS.read_text())
    if telegram.__version__ != pins["python_telegram_bot"]:
        raise ProbeError("telegram_dependency_mismatch")
    for name, digest in pins["source_sha256"].items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != digest:
            raise ProbeError("telegram_source_mismatch")


def credential_path(home=HOME):
    directory = home / "credentials"
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ProbeError("telegram_private_directory_required")
    return private_file(directory / TOKEN_NAME)


def read_token(home=HOME):
    token = credential_path(home).read_text().strip()
    if not re.fullmatch(r"[1-9][0-9]{4,19}:[A-Za-z0-9_-]{30,128}", token):
        raise ProbeError("telegram_token_invalid")
    return token


def environment(home, env):
    clean = {k: v for k, v in env.items() if not k.startswith("TELEGRAM_")}
    clean["TELEGRAM_BOT_TOKEN"] = read_token(home)
    clean["TELEGRAM_ALLOW_ALL_USERS"] = "false"
    return clean


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProbeError("telegram_redirect_refused")


def rpc(token, method, **data):
    # Only used in the owner's terminal with no gateway poller. Errors may hold
    # the credential-bearing URL, so callers expose fixed diagnostics only.
    request = urllib.request.Request(
        "https://api.telegram.org/bot" + token + "/" + method,
        data=json.dumps(data).encode(), headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=20) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ProbeError("telegram_response_too_large")
    result = json.loads(raw)
    if result.get("ok") is not True:
        raise ProbeError("telegram_api_rejected")
    return result["result"]


def identify(token, challenge):
    offset = 0
    for _ in range(18):
        updates = rpc(token, "getUpdates", offset=offset, timeout=10, allowed_updates=["message"])
        found = set()
        for update in updates:
            offset = max(offset, int(update["update_id"]) + 1)
            message = update.get("message", {})
            chat, sender = message.get("chat", {}), message.get("from", {})
            if (message.get("text") == challenge and chat.get("type") == "private"
                    and sender.get("is_bot") is False and type(sender.get("id")) is int
                    and sender["id"] > 0 and chat.get("id") == sender["id"]):
                found.add(str(sender["id"]))
        if len(found) > 1:
            raise ProbeError("telegram_enrollment_ambiguous")
        if found:
            rpc(token, "getUpdates", offset=offset, timeout=0, allowed_updates=["message"])
            return found.pop()
    raise ProbeError("telegram_enrollment_timeout")


def enroll():
    if os.geteuid() == 0 or not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ProbeError("telegram_owner_terminal_required")
    if os.environ.get("HERMES_HOME") != str(HOME):
        raise ProbeError("telegram_profile_mismatch")
    info = HOME.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ProbeError("telegram_profile_mismatch")
    from ruamel.yaml import YAML
    from scripts.model_contract import check_service
    config = YAML(typ="safe", pure=True).load(private_file(HOME / "config.yaml").read_text())
    check_service(config)
    if config.get("platforms", {}).get("telegram", {}).get("enabled"):
        raise ProbeError("telegram_disable_before_enrollment")
    directory = HOME / "credentials"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ProbeError("telegram_private_directory_required")
    destination = directory / TOKEN_NAME
    if destination.exists() or destination.is_symlink():
        raise ProbeError("telegram_existing_token_requires_review")
    token = getpass.getpass("Telegram bot token (hidden): ").strip()
    if not re.fullmatch(r"[1-9][0-9]{4,19}:[A-Za-z0-9_-]{30,128}", token):
        raise ProbeError("telegram_token_invalid")
    bot = rpc(token, "getMe")
    if bot.get("is_bot") is not True or rpc(token, "getWebhookInfo").get("url"):
        raise ProbeError("telegram_bot_or_webhook_mismatch")
    challenge = "HAVEN_ENROLL_" + secrets.token_hex(12)
    print("Open your bot @" + str(bot["username"]) + " and send this exact text:", flush=True)
    print(challenge, flush=True)
    owner = identify(token, challenge)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(token + "\n")
    print(json.dumps({"result": "enrolled", "owner_id": owner, "bot_username": bot["username"]}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "enroll"))
    args = parser.parse_args()
    try:
        if args.mode == "enroll":
            enroll()
        else:
            check_dependency()
            print(json.dumps({"result": "passed", "check": "telegram_dependency"}))
    except Exception:
        print(json.dumps({"result": "failed", "code": "telegram_check_failed"}))
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
