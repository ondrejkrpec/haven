"""Explicitly fetch one accepted snapshot into a new private recovery directory.

Uses the restricted runtime key; no version recovery or destructive maintenance.
Never starts restored software or accepts a mutable 'latest' snapshot selector.
"""

import json
import hashlib
import os
from pathlib import Path
import shutil
import sys
from datetime import datetime

from scripts.backup_capture import require
from scripts.backup_runtime import IDENTIFIER, Repository, load, read_private, save, validate_payload
from scripts.private_enrollment import PrivateArgumentParser, parent_directory

ROOT = Path(__file__).resolve().parents[1]


def validate_receipt(accepted):
    require(IDENTIFIER.fullmatch(accepted.get("snapshot_id", ""))
            and IDENTIFIER.fullmatch(accepted.get("manifest_sha256", ""))
            and accepted.get("restored_and_verified") is True, "accepted_receipt_required")


def discover_snapshot(repo, snapshot=None):
    """Recover without a lost VPS receipt; pin an authenticated full ID first."""
    rows = json.loads(repo.run("snapshots", "--json", "--tag", "haven-production", "--host", repo.settings["hostname"]))
    require(isinstance(rows, list) and 0 < len(rows) <= 10000, "snapshot_catalogue_unavailable")
    candidates = []
    for row in rows:
        tags = row.get("tags", [])
        runs = [tag.removeprefix("haven-run-") for tag in tags if tag.startswith("haven-run-")]
        require(IDENTIFIER.fullmatch(row.get("id", "")) and row.get("hostname") == repo.settings["hostname"]
                and len(runs) == 1 and len(runs[0]) == 32
                and all(char in "0123456789abcdef" for char in runs[0])
                and set(tags) == {"haven-production", "haven-run-" + runs[0]}, "invalid_recovery_catalogue")
        if snapshot is None or row["id"] == snapshot:
            candidates.append((datetime.fromisoformat(row["time"].replace("Z", "+00:00")), row, runs[0]))
    require(candidates, "snapshot_not_found")
    _, selected, run_id = max(candidates, key=lambda row: row[0])
    # Authenticated encrypted snapshot data supplies the manifest when the
    # controller's acceptance receipt was lost. Never pass mutable 'latest'.
    raw = repo.run("dump", selected["id"], "/payload/restore-manifest.json")
    require(len(raw) <= 4 * 1024 * 1024, "restore_manifest_too_large")
    manifest = json.loads(raw)
    require(manifest["run_id"] == run_id, "restore_run_mismatch")
    return {"schema_version": 1, "snapshot_id": selected["id"], "run_id": run_id,
            "manifest_sha256": hashlib.sha256(raw).hexdigest(), "captured_at_utc": manifest["capture"]["captured_at_utc"],
            "restored_and_verified": True, "receipt_origin": "authenticated_recovery_discovery",
            "server_acceptance_status": "unavailable"}


def fetch(settings, accepted, credentials, password_directory, destination, *, snapshot=None):
    if accepted is not None:
        validate_receipt(accepted)
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
        if accepted is None:
            accepted = discover_snapshot(repo, snapshot)
        validate_receipt(accepted)
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
            "temporary_input_keys_removed": True}


def main(argv=None):
    parser = PrivateArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    for name in ("settings", "credentials", "password-directory", "destination"):
        parser.add_argument("--" + name, type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--accepted", type=Path)
    source.add_argument("--discover", action="store_true")
    parser.add_argument("--snapshot", help="optional full ID for explicit disaster recovery selection")
    args = parser.parse_args(argv)
    if not args.live:
        print("Explicit --live required; no credentials read or request sent.", file=sys.stderr)
        return 2
    os.umask(0o077)
    try:
        require(args.snapshot is None or (args.discover and IDENTIFIER.fullmatch(args.snapshot)), "full_disaster_snapshot_required")
        result = fetch(load(args.settings), load(args.accepted) if args.accepted else None,
                       args.credentials, args.password_directory, args.destination, snapshot=args.snapshot)
    except Exception:
        print('{"result":"failed","code":"recovery_fetch_failed"}')
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
