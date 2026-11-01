"""Explicitly fetch one accepted snapshot into a new private recovery directory.

Uses the restricted runtime key; no version recovery or destructive maintenance.
Never starts restored software or accepts a mutable 'latest' snapshot selector.
"""

import json
import os
from pathlib import Path
import shutil
import sys

from scripts.backup_capture import require
from scripts.backup_runtime import IDENTIFIER, Repository, load, read_private, save, validate_payload
from scripts.private_enrollment import PrivateArgumentParser, parent_directory

ROOT = Path(__file__).resolve().parents[1]


def fetch(settings, accepted, credentials, password_directory, destination):
    require(IDENTIFIER.fullmatch(accepted.get("snapshot_id", ""))
            and IDENTIFIER.fullmatch(accepted.get("manifest_sha256", ""))
            and accepted.get("restored_and_verified") is True, "accepted_receipt_required")
    with parent_directory(destination) as (parent, name):
        os.mkdir(name, mode=0o700, dir_fd=parent)
    temporary = destination / "private-inputs"
    temporary.mkdir(mode=0o700)
    try:
        for name, path in (("access-key", credentials / "access-key"), ("secret-key", credentials / "secret-key"),
                           ("password", password_directory / "password")):
            with (temporary / name).open("xb") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(read_private(path, 258))
        repo = Repository(settings, temporary, destination, binary=ROOT / "build/h1.9/restic")
        repo.identity()
        rows = repo.snapshots(accepted["run_id"])
        require(len(rows) == 1 and rows[0]["id"] == accepted["snapshot_id"], "snapshot_identity_mismatch")
        repo.run("restore", accepted["snapshot_id"], "--target", str(destination / "restored"), "--verify")
        payload = destination / "restored/payload"
        manifest = validate_payload(payload, accepted["manifest_sha256"])
        require(manifest["run_id"] == accepted["run_id"], "restore_run_mismatch")
        save(destination / "accepted.json", accepted)
    except BaseException:
        shutil.rmtree(destination)
        raise
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return {"result": "passed", "exact_snapshot_verified": True, "services_started": False,
            "configuration_secrets_removed": True}


def main(argv=None):
    parser = PrivateArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    for name in ("settings", "accepted", "credentials", "password-directory", "destination"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.live:
        print("Explicit --live required; no credentials read or request sent.", file=sys.stderr)
        return 2
    os.umask(0o077)
    try:
        result = fetch(load(args.settings), load(args.accepted), args.credentials, args.password_directory, args.destination)
    except Exception:
        print('{"result":"failed","code":"recovery_fetch_failed"}')
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
