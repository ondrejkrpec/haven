"""Bounded metadata-only inventory for H1.9; never opens file contents or follows links."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat

GROUPS = {
    "configuration": {"config.yaml", ".env", "SOUL.md", "AGENTS.md", "USER.md"},
    "credentials": {"auth.json", "credentials"},
    "runtime_data": {"state.db", "state.db-wal", "state.db-shm", "state", "sessions",
                     "memories", "memory", "profiles", "cron", "kanban", "kanban.db",
                     "kanban.db-wal", "kanban.db-shm", "pending_messages", "spawn-ledger.json",
                     "channel_directory.json"},
    "extensions_review": {"skills", "plugins", "tools", "scripts", "hooks", "platforms"},
    "local_snapshots_review": {"backups"},
    "identity_review": {"install_id", "pairing"},
    "logs": {"logs", "gateway-starts.log"},
    "rebuildable": {"cache", ".cache", "installs", "runtime", "pm-runtime", "audio_cache",
                    "image_cache", "context_length_cache.yaml", "plugin-update-checks"},
    "transient": {"gateway_state.json", "gateway.lock", "gateway.pid", "gateway_runtime.json",
                  "gateway_heartbeat.json", "gateway_voice_mode.json"},
}
KNOWN = ("config.yaml", "auth.json", "credentials/telegram-bot-token", "state.db",
         "state/capability-audit.sqlite3", "kanban.db")
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def category(relative):
    first = relative.parts[0]
    if first.startswith("telegram_update_receipts_") and first.endswith(".json"):
        return "runtime_data"
    if first.startswith("config.yaml.") and first.endswith("~"):
        return "local_snapshots_review"
    if first.endswith(".lock") or (first.startswith(".gateway_state_") and first.endswith(".tmp")):
        return "transient"
    if first == "gateway.sock":
        return "transient"
    return next((name for name, entries in GROUPS.items() if first in entries), "unclassified")


def known_metadata(root_fd, name):
    directory = os.dup(root_fd)
    try:
        for component in Path(name).parts[:-1]:
            try:
                child = os.open(component, DIRECTORY_FLAGS, dir_fd=directory)
            except FileNotFoundError:
                return {"present": False}
            except OSError:
                return {"present": None, "parent_unavailable": True}
            os.close(directory)
            directory = child
        try:
            info = os.stat(Path(name).name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return {"present": False}
        return {"present": True, "regular": stat.S_ISREG(info.st_mode),
                "bytes": info.st_size, "mode": oct(stat.S_IMODE(info.st_mode)),
                "uid": info.st_uid, "gid": info.st_gid, "links": info.st_nlink}
    finally:
        os.close(directory)


def inventory(home, *, max_entries=100000, max_depth=32):
    home = Path(home)
    if not home.is_absolute() or home.resolve(strict=True) != home or not home.is_dir():
        raise ValueError("inventory_requires_canonical_directory")
    groups = {name: {"files": 0, "bytes": 0, "symlinks": 0, "special": 0}
              for name in (*GROUPS, "unclassified")}
    root_fd = os.open(home, DIRECTORY_FLAGS)
    pending = [(os.dup(root_fd), Path(), 0)]
    entries = directories = errors = 0
    limited = False
    known = {}
    try:
        while pending and not limited:
            directory, prefix, depth = pending.pop()
            try:
                with os.scandir(directory) as children:
                    for child in children:
                        entries += 1
                        if entries > max_entries:
                            limited = True
                            break
                        try:
                            info = child.stat(follow_symlinks=False)
                            relative = prefix / child.name
                            row = groups[category(relative)]
                            if stat.S_ISDIR(info.st_mode):
                                directories += 1
                                if depth >= max_depth:
                                    limited = True
                                    break
                                child_fd = os.open(child.name, DIRECTORY_FLAGS, dir_fd=directory)
                                pending.append((child_fd, relative, depth + 1))
                            elif stat.S_ISLNK(info.st_mode):
                                row["symlinks"] += 1
                            elif stat.S_ISREG(info.st_mode):
                                row["files"] += 1
                                row["bytes"] += info.st_size
                            else:
                                row["special"] += 1
                        except OSError:
                            errors += 1
            except OSError:
                errors += 1
            finally:
                os.close(directory)
        for name in KNOWN:
            try:
                known[name] = known_metadata(root_fd, name)
            except OSError:
                errors += 1
                known[name] = {"present": None}
    finally:
        for descriptor, _, _ in pending:
            os.close(descriptor)
        os.close(root_fd)
    return {"schema_version": 1, "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "complete": not limited and errors == 0, "limited": limited, "errors": errors,
            "entries": min(entries, max_entries), "directories": directories,
            "groups": groups, "known_files": known}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = inventory(args.home)
    except (OSError, ValueError):
        print(json.dumps({"complete": False, "code": "inventory_unavailable"}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
