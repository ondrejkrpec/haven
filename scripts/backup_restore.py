"""Verify a restored payload and rebuild its profile into a new isolated directory.

This command does not start services, connect to providers or install dependencies.
UID/GID mappings are explicit because numeric identities differ on a new host.
"""

import argparse
import json
import os
from pathlib import Path
import shutil

from scripts.backup_capture import require
from scripts.backup_runtime import validate_payload
from scripts.private_enrollment import parent_directory


def rebuild(payload, destination, digest, owners):
    manifest = validate_payload(payload, digest)
    files = manifest["capture"]["files"]
    require(set(owners) == {"uids", "gids"}, "invalid_owner_mapping")
    for name, field in (("uids", "uid"), ("gids", "gid")):
        require(set(owners[name]) == {str(row[field]) for row in files.values()}
                and all(type(value) is int and 0 <= value < 2 ** 31 for value in owners[name].values()), "owner_mapping_incomplete")
    for row in files.values():
        require(type(row["mode"]) is int and 0 <= row["mode"] <= 0o777
                and row["kind"] in {"file", "directory"}, "invalid_restore_metadata")
    require(not destination.is_relative_to(payload) and not payload.is_relative_to(destination), "overlapping_restore_paths")
    # A descriptor anchors writes even if a parent is renamed; no existing profile
    # can be overwritten, and no symlinked/Git ancestor is accepted.
    with parent_directory(destination) as (parent, leaf):
        os.mkdir(leaf, mode=0o700, dir_fd=parent)
        descriptor = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            anchored = Path(f"/proc/self/fd/{descriptor}")
            for name, row in sorted(files.items(), key=lambda item: (item[0].count("/"), item[0])):
                target = anchored / name
                if row["kind"] == "directory":
                    target.mkdir(mode=0o700)
                else:
                    with (payload / "home" / name).open("rb") as source, target.open("xb") as output:
                        os.fchmod(output.fileno(), 0o600)
                        shutil.copyfileobj(source, output)
                        output.flush()
                        os.fsync(output.fileno())
            # Children first: no restrictive directory mode interrupts creation.
            for name, row in sorted(files.items(), key=lambda item: item[0].count("/"), reverse=True):
                target = anchored / name
                os.chown(target, owners["uids"][str(row["uid"])], owners["gids"][str(row["gid"])], follow_symlinks=False)
                os.chmod(target, row["mode"], follow_symlinks=False)
                os.utime(target, ns=(row["mtime_ns"], row["mtime_ns"]), follow_symlinks=False)
            # The captured profile itself belongs to the owner of its config.
            os.fchown(descriptor, owners["uids"][str(files["config.yaml"]["uid"])], owners["gids"][str(files["config.yaml"]["gid"])])
            os.fchmod(descriptor, 0o700)
            os.fsync(descriptor)
            os.fsync(parent)
        except BaseException:
            shutil.rmtree(leaf, dir_fd=parent)
            raise
        finally:
            os.close(descriptor)
    return {"result": "passed", "files_verified": len(files), "services_started": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--owners", type=Path, required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        result = rebuild(args.payload, args.destination, args.manifest_sha256, json.loads(args.owners.read_text()))
    except Exception:
        print('{"result":"failed","code":"isolated_restore_failed"}')
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
