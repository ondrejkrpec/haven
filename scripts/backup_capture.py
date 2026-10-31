"""Consistent private staging primitive; no CLI, upload or live service adapter yet."""

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import time

from scripts.private_enrollment import DIRECTORY_FLAGS, parent_directory


class CaptureError(Exception):
    """Fixed diagnostics only; paths, data and OS exceptions stay private."""


def require(condition, code="capture_refused"):
    if not condition:
        raise CaptureError(code)


def validate_policy(policy):
    require(isinstance(policy, dict) and set(policy) == {"schema_version", "include", "exclude", "required", "sqlite"}
            and type(policy["schema_version"]) is int and policy["schema_version"] == 1, "invalid_capture_policy")
    for key in ("include", "exclude", "required", "sqlite"):
        values = policy[key]
        require(isinstance(values, list) and len(values) <= 128
                and all(isinstance(name, str) for name in values) and len(set(values)) == len(values), "invalid_capture_policy")
        for name in values:
            require(isinstance(name, str) and 0 < len(name) <= 256
                    and not name.startswith("/") and str(PurePosixPath(name)) == name
                    and all(part not in {".", "..", ".git"} for part in PurePosixPath(name).parts)
                    and all(32 < ord(char) < 127 for char in name)
                    and not any(char in name for char in "*?[]\\"), "invalid_capture_policy")
    require(bool(policy["include"]), "invalid_capture_policy")
    require(not any(name == base or name.startswith(base + "/")
                    for name in policy["include"] for base in policy["exclude"]), "invalid_capture_policy")
    for key in ("required", "sqlite"):
        require(all(any(name == base or name.startswith(base + "/") for base in policy["include"])
                    and not any(name == base or name.startswith(base + "/") for base in policy["exclude"])
                    for name in policy[key]), "invalid_capture_policy")
    require(not any(a == b or a.startswith(b + "/") or b.startswith(a + "/")
                    for i, a in enumerate(policy["include"]) for b in policy["include"][i + 1:]), "invalid_capture_policy")


def selection(name, policy):
    """Every encountered path is included, explicitly excluded, or rejected."""
    if any(name == base or name.startswith(base + "/") for base in policy["exclude"]):
        return "exclude"
    leaf = name.rsplit("/", 1)[-1]
    if leaf.endswith(".lock") or leaf == "__pycache__":
        return "exclude"
    if re.fullmatch(r"state/gateway\.loop-tick\.[0-9]+\.sock", name):
        return "exclude"
    if re.fullmatch(r"\.gateway_state_[a-zA-Z0-9_-]+\.tmp", name):
        return "exclude"
    if any(name == db + suffix for db in policy["sqlite"] for suffix in ("-wal", "-shm", "-journal")):
        return "sidecar"
    require(not leaf.endswith(("-wal", "-shm", "-journal")), "unreviewed_database_sidecar")
    if (any(name == base or name.startswith(base + "/") for base in policy["include"])
            or re.fullmatch(r"telegram_update_receipts_[0-9]+\.json", name)
            or re.fullmatch(r"config\.yaml\.[a-zA-Z0-9.@:_+-]+~", name)):
        return "include"
    if any(base.startswith(name + "/") for base in policy["include"]):
        return "parent"
    raise CaptureError("unreviewed_source_path")


def fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def open_root(path):
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts and path != Path("/"), "invalid_source")
    descriptor = os.open("/", DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def read_tree(source, destination, policy, *, deadline, max_bytes, max_entries):
    """Anchor every source lookup to no-follow fds; never copy special files."""
    root_fd = open_root(source)
    device = os.fstat(root_fd).st_dev
    initial, metadata, excluded = {}, {}, []
    byte_count = entries = 0

    def bounded():
        require(time.monotonic() < deadline and entries <= max_entries and byte_count <= max_bytes, "capture_limit")

    def walk(directory, prefix="", depth=0):
        nonlocal byte_count, entries
        require(depth <= 32, "capture_limit")
        before_directory = os.fstat(directory)
        require(not os.listxattr(directory), "unsupported_source_metadata")
        initial[prefix] = fingerprint(before_directory)
        bounded()
        # scandir is bounded while collecting; no unbounded sorted(directory).
        names = []
        with os.scandir(directory) as children:
            for child in children:
                entries += 1
                bounded()
                names.append(child.name)
        for leaf in sorted(names):
            bounded()
            name = prefix + "/" + leaf if prefix else leaf
            decision = selection(name, policy)
            if decision == "exclude":
                excluded.append(name)
                continue
            info = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
            require(info.st_dev == device and not stat.S_ISLNK(info.st_mode), "linked_or_mounted_source")
            initial[name] = fingerprint(info)
            metadata[name] = {"uid": info.st_uid, "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode),
                              "mtime_ns": info.st_mtime_ns}
            target = destination / name if destination is not None else None
            if stat.S_ISDIR(info.st_mode):
                require(decision != "sidecar", "invalid_database_sidecar")
                if target is not None:
                    target.mkdir(mode=0o700)
                child_fd = os.open(leaf, DIRECTORY_FLAGS, dir_fd=directory)
                try:
                    require(fingerprint(os.fstat(child_fd)) == fingerprint(info), "source_changed")
                    walk(child_fd, name, depth + 1)
                finally:
                    os.close(child_fd)
                metadata[name]["kind"] = "directory"
            else:
                require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and decision != "parent", "unexpected_source_type")
                byte_count += info.st_size
                bounded()
                fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                try:
                    require(fingerprint(os.fstat(fd)) == fingerprint(info), "source_changed")
                    require(not os.listxattr(fd), "unsupported_source_metadata")
                    if target is not None:
                        digest = hashlib.sha256()
                        total = 0
                        with target.open("xb") as output:
                            os.fchmod(output.fileno(), 0o600)
                            while block := os.read(fd, 1024 * 1024):
                                if total == 0 and block.startswith(b"SQLite format 3\x00"):
                                    require(name in policy["sqlite"], "unreviewed_database")
                                total += len(block)
                                require(total <= info.st_size, "source_changed")
                                bounded()
                                output.write(block)
                                digest.update(block)
                            output.flush()
                            os.fsync(output.fileno())
                        require(total == info.st_size, "source_changed")
                        metadata[name]["sha256"] = digest.hexdigest()
                    require(fingerprint(os.fstat(fd)) == fingerprint(info), "source_changed")
                finally:
                    os.close(fd)
                metadata[name]["kind"] = "file"
            require(fingerprint(os.stat(leaf, dir_fd=directory, follow_symlinks=False)) == initial[name], "source_changed")
        require(fingerprint(os.fstat(directory)) == fingerprint(before_directory), "source_changed")

    try:
        walk(root_fd)
    finally:
        os.close(root_fd)
    require(all(name in metadata and metadata[name]["kind"] == "file" for name in policy["required"]), "required_source_missing")
    for name in metadata:
        if selection(name, policy) == "sidecar":
            require(any(name == db + suffix and db in metadata
                        for db in policy["sqlite"] for suffix in ("-wal", "-shm", "-journal")), "orphan_database_sidecar")
    return initial, metadata, excluded, byte_count


def normalize_databases(destination, policy, metadata, deadline):
    """Read SQLite only from the private copy, never from the live profile."""
    for name in policy["sqlite"]:
        if name not in metadata:
            continue
        require(metadata[name]["kind"] == "file", "invalid_database")
        source = destination / name
        target = source.with_name(source.name + ".capture-normalized")
        require(not target.exists(), "capture_destination_collision")
        with target.open("xb") as output:
            os.fchmod(output.fileno(), 0o600)

        def progress(*_):
            require(time.monotonic() < deadline, "capture_limit")

        try:
            with closing(sqlite3.connect(source.as_uri() + "?mode=rw", uri=True, timeout=1)) as db:
                db.execute("PRAGMA trusted_schema=OFF")
                with closing(sqlite3.connect(target, timeout=1)) as backup:
                    backup.execute("PRAGMA trusted_schema=OFF")
                    db.backup(backup, pages=64, progress=progress, sleep=0.01)
                    require(backup.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "invalid_database")
            target.chmod(0o600)
            os.replace(target, source)
        finally:
            target.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = name + suffix
            (destination / sidecar).unlink(missing_ok=True)
            metadata.pop(sidecar, None)
        metadata[name]["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()


def stage_stopped(source, destination, policy, *, max_bytes=256 * 1024 * 1024, max_entries=20000, max_seconds=60):
    """Caller must hold the maintenance lock and have stopped every writer."""
    validate_policy(policy)
    source, destination = Path(source), Path(destination)
    require(not destination.is_relative_to(source) and not source.is_relative_to(destination), "overlapping_capture_paths")
    deadline = time.monotonic() + max_seconds
    with parent_directory(destination) as (parent, leaf):
        os.mkdir(leaf, mode=0o700, dir_fd=parent)
        directory = os.open(leaf, DIRECTORY_FLAGS, dir_fd=parent)
        info = os.fstat(directory)
        try:
            os.fchmod(directory, 0o700)
            # Linux /proc anchors all destination I/O to this verified fd.
            anchored = Path(f"/proc/self/fd/{directory}")
            initial, metadata, excluded, size = read_tree(source, anchored, policy, deadline=deadline,
                                                         max_bytes=max_bytes, max_entries=max_entries)
            final, _, _, _ = read_tree(source, None, policy, deadline=deadline,
                                      max_bytes=max_bytes, max_entries=max_entries)
            require(initial == final, "source_changed")
            normalize_databases(anchored, policy, metadata, deadline)
            require(time.monotonic() < deadline, "capture_limit")
            require(destination.resolve(strict=True) == destination
                    and destination.stat().st_ino == info.st_ino and destination.stat().st_dev == info.st_dev,
                    "capture_parent_changed")
            return {"schema_version": 1, "captured_at_utc": datetime.now(timezone.utc).isoformat(),
                    "policy_sha256": hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest(),
                    "files": metadata, "excluded": excluded, "source_bytes": size,
                    "absent_optional": [name for name in policy["include"] if name not in metadata],
                    "consistency": "caller stopped writers; source metadata unchanged across copy"}
        except BaseException:
            # Do not follow a swapped parent path or remove a replacement tree.
            current = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISDIR(current.st_mode) and current.st_ino == info.st_ino and current.st_dev == info.st_dev:
                shutil.rmtree(leaf, dir_fd=parent)
            raise
        finally:
            os.close(directory)


def capture_with_pause(controller, journal, source, destination, policy, **limits):
    """Adapters provide a durable intent, one unit's control, and full health."""
    require(not journal.pending(), "unfinished_capture_requires_recovery")
    validate_policy(policy)
    require(controller.healthy(), "service_not_healthy")
    journal.arm()  # Must fsync intent before the stop can reach systemd.
    succeeded = False
    try:
        controller.stop()
        require(controller.stopped(), "service_not_stopped")
        receipt = stage_stopped(source, destination, policy, **limits)
        succeeded = True
    finally:
        try:
            controller.start()
            require(controller.healthy(), "service_resume_failed")
            journal.disarm()
        except BaseException:
            if succeeded:
                shutil.rmtree(destination)
            raise
    return receipt


def recover_pause(controller, journal):
    """For an eventual ExecStopPost/next-run adapter after the main job dies."""
    if journal.pending():
        controller.start()
        require(controller.healthy(), "service_resume_failed")
        journal.disarm()
