"""Opt-in same-UID workstation wiring probe; never production isolation acceptance."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


async def run(args):
    from scripts.wishlist_browser import BrowserServer, load_config
    from scripts.wishlist_collector import CollectionServer, collect
    from scripts.wishlist_domain import Wishlist
    from scripts.wishlist_egress import Broker
    from scripts.wishlist_telegram import TelegramApplication

    os.umask(0o077)
    root = args.work.resolve()
    if root.exists() or root.is_relative_to(REPO):
        raise ValueError("Choose a new private work directory outside the repository")
    root.mkdir(mode=0o700)
    for name in ("domain", "ingress", "browser", "egress"):
        (root / name).mkdir(mode=0o700)
    config = json.loads(args.config.read_text())
    config.update(socket=str(root / "browser/request.sock"), socket_gid=os.getgid(), client_uid=os.getuid(),
                  egress_socket=str(root / "egress/request.sock"), egress_uid=os.getuid())
    config_path = root / "browser.json"
    config_path.write_text(json.dumps(config))
    load_config(config_path)
    target = json.loads(args.target.read_text())
    if not isinstance(target, dict) or set(target) != {"url"}:
        raise ValueError("Explicit one-product target JSON required")
    domain = Wishlist(root / "domain/wishlist.sqlite3", bot_id="200001", owner_id="100001")
    app = TelegramApplication(domain)

    def event(text, message_id):
        return {"bot_id": "200001", "user_id": "100001", "chat_id": "100001", "message_id": str(message_id),
                "text": text, "chat_type": "private", "is_bot": False, "edited": False}

    saved = app.handle(event(target["url"], 1))
    if not saved["committed"]:
        domain.close()
        raise ValueError("Probe intake failed")
    broker = Broker(config["egress_socket"], client_uid=os.getuid())
    browser = BrowserServer(config, config_path)
    server = CollectionServer(domain, root / "ingress/request.sock", ingress_uid=os.getuid())
    started = []
    try:
        for component in (broker, browser, server):
            await component.start()
            started.append(component)
        endpoint = {"socket": str(server.path), "service_uid": os.getuid(),
                    "browser": {"socket": config["socket"], "service_uid": os.getuid()}}
        completed = await collect(endpoint, maximum=1)
        repeated = await collect(endpoint, maximum=1)
        rows = [dict(row) for row in domain.store.db.execute(
            "SELECT price, currency, availability, adapter, observed_at FROM observations")]
        detail = app.handle(event("wl detail #1", 2))
        passed = (completed == 1 and repeated == 0 and len(rows) == 1
                  and rows[0]["price"] + " CZK" in detail["reply"])
        result = {"result": "passed" if passed else "failed", "completed": completed,
                  "immediate_second_scan": repeated, "observation_count": len(rows), "model_calls": 0,
                  "same_uid_workstation_fixture": True, "production_changed": False,
                  "observations": rows, "telegram_reply": detail["reply"]}
        (root / "result.json").write_text(json.dumps(result, indent=2))
        print(json.dumps({key: value for key, value in result.items()
                          if key not in ("observations", "telegram_reply")}))
        return 0 if passed else 1
    finally:
        for component in reversed(started):
            await component.close()
        domain.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
