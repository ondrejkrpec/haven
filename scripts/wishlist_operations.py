"""Fixed W4 installation contract and read-only database compatibility checks."""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from zoneinfo import ZoneInfo

from scripts.wishlist_store import MIGRATIONS

BASE = Path("/var/lib/haven-wishlist")
LANGUAGE_HOME = Path("/var/lib/haven-language/.hermes")
TOOLS = Path("/opt/haven-wishlist")
SOCKET = "/run/haven-wishlist/ingress.sock"
LANGUAGE_SOCKET = "/run/haven-wishlist-language/request.sock"
DOMAIN_POLICY = {"schema_version": 1, "include": ["config.json", "state"], "exclude": [],
                 "required": ["config.json", "state/wishlist.sqlite3"], "sqlite": ["state/wishlist.sqlite3"]}
LANGUAGE_POLICY = {"schema_version": 1, "include": ["config.yaml", "auth.json"],
                   "exclude": ["tools", "logs", "cache", ".cache"],
                   "required": ["config.yaml", "auth.json"], "sqlite": []}
UNITS = ("haven.service", "haven-wishlist.service", "haven-wishlist-language.service")


def configurations(*, bot_id, owner_id, timezone, ingress_uid, domain_uid, language_uid, ingress_gid, language_gid):
    for value in (bot_id, owner_id):
        if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,15}", value):
            raise ValueError("wishlist_identity")
    ZoneInfo(timezone)
    ids = (ingress_uid, domain_uid, language_uid, ingress_gid, language_gid)
    if any(type(value) is not int or not 1 <= value < 2**31 for value in ids):
        raise ValueError("wishlist_system_identity")
    if len({ingress_uid, domain_uid, language_uid}) != 3 or ingress_gid == language_gid:
        raise ValueError("wishlist_separate_identities_required")
    return {
        "domain": {"schema_version": 1, "database": str(BASE / "state/wishlist.sqlite3"),
                   "socket": SOCKET, "socket_gid": ingress_gid, "ingress_uid": ingress_uid,
                   "bot_id": bot_id, "owner_id": owner_id, "timezone": timezone,
                   "language": {"socket": LANGUAGE_SOCKET, "service_uid": language_uid}},
        "interpreter": {"schema_version": 1, "socket": LANGUAGE_SOCKET, "socket_gid": language_gid,
                        "client_uid": domain_uid, "source": str(TOOLS / "native"),
                        "home": str(LANGUAGE_HOME), "python": str(TOOLS / "native/venv/bin/python")},
        "ingress": {"socket": SOCKET, "service_uid": domain_uid, "bot_id": bot_id, "natural_language": True},
    }


def database_status(path, *, maximum_version=len(MIGRATIONS)):
    """Never open a live DB for writing, migrate it or silently drop newer state."""
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077
            or path.resolve() != path):
        raise ValueError("wishlist_database_metadata")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
        db.execute("PRAGMA trusted_schema=OFF")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if not 1 <= version <= maximum_version:
            raise ValueError("wishlist_database_version")
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("wishlist_database_integrity")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("wishlist_database_foreign_keys")
    return {"result": "passed", "schema_version": version, "writes": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--maximum-version", type=int, default=len(MIGRATIONS))
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        result = database_status(args.database, maximum_version=args.maximum_version)
    except Exception:
        print('{"result":"failed","code":"wishlist_database_check_failed"}')
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
