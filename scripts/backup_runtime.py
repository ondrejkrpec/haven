"""Trusted backup controller, durable pause recovery and independent monitoring.

Only the explicit CLI operates on a host. Importing this module has no effects.
Raw subprocess output, source names and credential values never reach diagnostics.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import time
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request
from uuid import uuid4

from scripts.backup_capture import CaptureError, capture_with_pause, open_root, recover_pause, require, validate_policy

STATE = Path("/var/lib/haven-backup")
CONFIG = Path("/etc/haven-backup")
LOCK = Path("/run/lock/haven-maintenance.lock")
TOOLS = Path("/usr/local/lib/haven-backup")
IDENTIFIER = re.compile(r"[0-9a-f]{64}")
URL = re.compile(r"https://hc-ping\.com/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


class CommandError(CaptureError):
    def __init__(self, exit_code):
        super().__init__("command_failed")
        self.exit_code = exit_code


def now():
    return datetime.now(timezone.utc).isoformat()


def protected_directory(path):
    fd = open_root(path)
    try:
        info = os.fstat(fd)
        require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700, "private_directory_required")
    finally:
        os.close(fd)


def read_private(path, limit=1024 * 1024):
    """Require trusted ancestors and a bounded, owned, single-link regular file."""
    parent = open_root(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid()
                    and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size <= limit, "private_file_required")
            value = os.read(fd, limit + 1)
            require(len(value) <= limit, "private_file_too_large")
            return value
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def save(path, value):
    """Atomic replace plus file and parent fsync; never leave an unflushed intent."""
    protected_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = open_root(path.parent)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load(path):
    return json.loads(read_private(path))


@contextmanager
def maintenance_lock(path=LOCK):
    parent = open_root(path.parent)
    try:
        fd = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1, "invalid_maintenance_lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CaptureError("maintenance_busy") from None
        yield
    finally:
        os.close(fd)


class PauseJournal:
    def __init__(self, state):
        self.path = state / "resume.json"

    def pending(self):
        if not self.path.exists():
            require(not self.path.is_symlink(), "invalid_pause_journal")
            return False
        require(load(self.path) == {"resume": "haven.service", "schema_version": 1}, "invalid_pause_journal")
        return True

    def arm(self):
        save(self.path, {"schema_version": 1, "resume": "haven.service"})

    def disarm(self):
        self.path.unlink()
        directory = open_root(self.path.parent)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def command(argv, *, timeout=30, cwd=None, env=None):
    # Keep descriptor/credential data out of inherited environments and all output.
    result = subprocess.run(argv, cwd=cwd, env=env or {"PATH": "/usr/sbin:/usr/bin:/bin", "LANG": "C.UTF-8"},
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    if result.returncode != 0:
        raise CommandError(result.returncode)
    require(len(result.stdout) <= 8 * 1024 * 1024, "command_output_limit")
    return result.stdout


class SystemController:
    def __init__(self, settings):
        self.settings = settings

    def state(self):
        output = command(["/usr/bin/systemctl", "show", "haven.service", "-p", "ActiveState", "-p", "SubState", "-p", "MainPID"])
        return dict(line.split("=", 1) for line in output.decode().splitlines() if "=" in line)

    def healthy(self):
        if self.state().get("SubState") != "running":
            return False
        try:
            command(["/usr/sbin/runuser", "-u", "haven", "--", "/usr/bin/env", "-i",
                     "HOME=/var/lib/haven", "PATH=/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE=1",
                     "HERMES_HOME=" + self.settings["source_home"], "PYTHONPATH=/var/lib/haven/haven-tools",
                     self.settings["runtime_python"], "-m", "scripts.service_runtime", "health",
                     "--source", self.settings["runtime_source"], "--home", self.settings["source_home"]], timeout=20)
            return True
        except (CaptureError, subprocess.TimeoutExpired):
            return False

    def stop(self):
        command(["/usr/bin/systemctl", "stop", "haven.service"], timeout=195)

    def stopped(self):
        state = self.state()
        return state.get("ActiveState") == "inactive" and state.get("MainPID") == "0"

    def start(self):
        command(["/usr/bin/systemctl", "start", "haven.service"], timeout=195)
        for _ in range(12):
            if self.healthy():
                return
            time.sleep(2)
        raise CaptureError("service_resume_failed")


def settings_from(config):
    settings = load(config / "settings.json")
    require(set(settings) == {"schema_version", "repository", "repository_id", "restic_sha256", "source_home",
                              "runtime_source", "runtime_python", "public_revision", "private_revision", "hostname"}
            and settings["schema_version"] == 1, "invalid_backup_settings")
    require(re.fullmatch(r"s3:https://s3\.fr-par\.scw\.cloud/[a-z0-9][a-z0-9-]{1,61}[a-z0-9]/[a-z][a-z0-9-]{0,31}",
                         settings["repository"]), "invalid_repository")
    for field in ("repository_id", "restic_sha256"):
        require(IDENTIFIER.fullmatch(settings[field]), "invalid_backup_settings")
    for field in ("public_revision", "private_revision"):
        require(re.fullmatch(r"[0-9a-f]{40}", settings[field]), "invalid_backup_settings")
    require(settings["source_home"] == "/var/lib/haven/.hermes"
            and settings["runtime_source"] == "/var/lib/haven/releases/hermes-bf0203a-785f2646"
            and settings["runtime_python"] == "/var/lib/haven/telegram/venv/bin/python"
            and re.fullmatch(r"[a-z][a-z0-9-]{0,62}", settings["hostname"]), "unsupported_backup_profile")
    return settings


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def ping(config, name, suffix=""):
    require(name in {"backup", "service"} and suffix in {"", "/start", "/fail"}, "invalid_monitor_request")
    value = read_private(config / (name + ".url"), 128).decode("ascii").removesuffix("\n")
    require(URL.fullmatch(value), "invalid_monitor_url")
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open(Request(value + suffix, data=b"", method="POST"), timeout=10) as response:
        require(response.status == 200 and response.read(16) == b"OK", "monitor_request_failed")


class Repository:
    def __init__(self, settings, config, work, binary=TOOLS / "restic"):
        self.settings, self.config, self.work, self.binary = settings, config, work, binary
        require(not binary.is_symlink() and hashlib.sha256(binary.read_bytes()).hexdigest() == settings["restic_sha256"],
                "restic_pin_mismatch")
        self.environment = {"PATH": "/usr/bin:/bin", "HOME": str(work), "TMPDIR": str(work), "LANG": "C.UTF-8",
                            "AWS_DEFAULT_REGION": "fr-par", "AWS_EC2_METADATA_DISABLED": "true",
                            "AWS_SHARED_CREDENTIALS_FILE": "/nonexistent", "AWS_CONFIG_FILE": "/nonexistent"}
        for name, variable in (("access-key", "AWS_ACCESS_KEY_ID"), ("secret-key", "AWS_SECRET_ACCESS_KEY")):
            value = read_private(config / name, 130).decode("ascii").removesuffix("\n")
            require(re.fullmatch(r"[A-Za-z0-9_+/=-]{8,128}", value), "invalid_storage_credential")
            self.environment[variable] = value
        require(32 <= len(read_private(config / "password", 258).rstrip(b"\n")) <= 256, "invalid_repository_password")

    def run(self, *arguments):
        return command([str(self.binary), "--no-cache", "--repo", self.settings["repository"],
                        "--password-file", str(self.config / "password"), "--stuck-request-timeout", "20s",
                        "-o", "s3.region=fr-par", "-o", "s3.bucket-lookup=path", "-o", "s3.connections=2",
                        "-o", "s3.retries=1", "-o", "s3.storage-class=STANDARD", *arguments],
                       timeout=300, cwd=self.work, env=self.environment)

    def identity(self):
        config = json.loads(self.run("cat", "config"))
        require(config.get("id") == self.settings["repository_id"] and config.get("version") == 2, "repository_identity_mismatch")

    def snapshots(self, run_id):
        rows = json.loads(self.run("snapshots", "--json", "--tag", "haven-run-" + run_id, "--host", self.settings["hostname"]))
        require(isinstance(rows, list) and len(rows) <= 1, "ambiguous_snapshot")
        for row in rows:
            require(IDENTIFIER.fullmatch(row["id"]) and row["hostname"] == self.settings["hostname"]
                    and set(row.get("tags", [])) == {"haven-production", "haven-run-" + run_id}, "snapshot_identity_mismatch")
        return rows


def validate_payload(payload, expected_digest):
    """Validate exact restored file set, digests and SQLite integrity without starting it."""
    from contextlib import closing
    import sqlite3
    manifest_path = payload / "restore-manifest.json"
    require(hashlib.sha256(read_private(manifest_path, 4 * 1024 * 1024)).hexdigest() == expected_digest, "restore_manifest_mismatch")
    manifest = json.loads(manifest_path.read_bytes())
    files = manifest["capture"]["files"]
    actual = set()
    require(not payload.is_symlink() and payload.is_dir(), "invalid_restore_tree")
    for path in payload.rglob("*"):
        info = path.lstat()
        require((stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
                or stat.S_ISDIR(info.st_mode), "invalid_restore_type")
        name = path.relative_to(payload).as_posix()
        if name == "restore-manifest.json":
            continue
        if name == "home":
            require(path.is_dir(), "invalid_restore_tree")
            continue
        require(name.startswith("home/") and name[5:] in files, "unexpected_restore_path")
        name = name[5:]
        expected = files[name]
        actual.add(name)
        require(stat.S_IMODE(info.st_mode) == (0o700 if expected["kind"] == "directory" else 0o600), "restore_mode_mismatch")
        if expected["kind"] == "file":
            require(stat.S_ISREG(info.st_mode) and hashlib.sha256(path.read_bytes()).hexdigest() == expected["sha256"], "restore_hash_mismatch")
        else:
            require(stat.S_ISDIR(info.st_mode), "invalid_restore_type")
    require(actual == set(files), "missing_restore_paths")
    for name in manifest["policy"]["sqlite"]:
        if name in files:
            path = payload / "home" / name
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=1)) as db:
                db.execute("PRAGMA trusted_schema=OFF")
                require(db.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "restored_database_invalid")
    return manifest


def clear_workspace(state):
    path = state / "work"
    if path.exists() or path.is_symlink():
        protected_directory(path)
        require(shutil.rmtree.avoids_symlink_attacks, "safe_cleanup_unavailable")
        shutil.rmtree(path)


def accept_snapshot(repo, journal, state, config):
    rows = repo.snapshots(journal["run_id"])
    require(len(rows) == 1, "snapshot_not_found")
    snapshot = rows[0]["id"]
    if journal.get("snapshot_id"):
        require(snapshot == journal["snapshot_id"], "snapshot_identity_mismatch")
    journal.update(stage="verifying", snapshot_id=snapshot)
    save(state / "run.json", journal)
    repo.run("check", "--read-data")
    destination = repo.work / "restored"
    if destination.exists():
        shutil.rmtree(destination)
    repo.run("restore", snapshot, "--target", str(destination), "--verify")
    manifest = validate_payload(destination / "payload", journal["manifest_sha256"])
    require(manifest["run_id"] == journal["run_id"], "restore_run_mismatch")
    accepted = {"schema_version": 1, "snapshot_id": snapshot, "run_id": journal["run_id"],
                "captured_at_utc": manifest["capture"]["captured_at_utc"], "verified_at_utc": now(),
                "manifest_sha256": journal["manifest_sha256"], "restored_and_verified": True}
    save(state / "accepted.json", accepted)
    journal.update(stage="accepted", finished_at_utc=now())
    save(state / "run.json", journal)
    ping(config, "backup")


def recover(settings, state):
    # ExecStopPost and boot recovery hold the same lock as every backup/maintenance.
    recover_pause(SystemController(settings), PauseJournal(state))
    clear_workspace(state)


def backup(settings, state, config, policy):
    recover(settings, state)
    previous = load(state / "run.json") if (state / "run.json").exists() else {}
    require(previous.get("stage") not in {"uploading", "uploaded", "verifying"}, "reconcile_required")
    validate_policy(policy)
    work = state / "work"
    work.mkdir(mode=0o700)
    require(shutil.disk_usage(work).free >= 768 * 1024 * 1024, "insufficient_staging_space")
    journal = {"schema_version": 1, "run_id": uuid4().hex, "stage": "preflight", "started_at_utc": now()}
    save(state / "run.json", journal)
    repo = Repository(settings, config, work)
    repo.identity()  # Reject missing/wrong password or wrong repository before pausing.
    ping(config, "backup", "/start")
    payload = work / "payload"
    payload.mkdir(mode=0o700)
    journal["stage"] = "capturing"
    save(state / "run.json", journal)
    receipt = capture_with_pause(SystemController(settings), PauseJournal(state), Path(settings["source_home"]), payload / "home", policy)
    manifest = {"schema_version": 1, "run_id": journal["run_id"], "capture": receipt, "policy": policy,
                "provenance": {key: settings[key] for key in ("public_revision", "private_revision", "runtime_source", "runtime_python")}}
    save(payload / "restore-manifest.json", manifest)
    journal.update(stage="uploading", manifest_sha256=hashlib.sha256((payload / "restore-manifest.json").read_bytes()).hexdigest())
    save(state / "run.json", journal)  # An uncertain upload is reconciled, never retried blindly.
    try:
        output = repo.run("backup", "--json", "--host", settings["hostname"], "--tag", "haven-production",
                          "--tag", "haven-run-" + journal["run_id"], "payload")
    except CommandError as error:
        if error.exit_code == 3:
            journal.update(stage="rejected_partial_snapshot", exit_code=3, finished_at_utc=now())
            save(state / "run.json", journal)
        raise
    summaries = [row for row in map(json.loads, output.splitlines()) if row.get("message_type") == "summary"]
    require(len(summaries) == 1 and IDENTIFIER.fullmatch(summaries[0].get("snapshot_id", "")), "incomplete_snapshot")
    journal.update(stage="uploaded", snapshot_id=summaries[0]["snapshot_id"])
    save(state / "run.json", journal)
    accept_snapshot(repo, journal, state, config)
    clear_workspace(state)


def reconcile(settings, state, config):
    recover(settings, state)
    journal = load(state / "run.json")
    require(journal.get("stage") in {"uploading", "uploaded", "verifying"}
            and re.fullmatch(r"[a-f0-9]{32}", journal.get("run_id", "")), "no_uncertain_upload")
    work = state / "work"
    work.mkdir(mode=0o700)
    repo = Repository(settings, config, work)
    repo.identity()
    rows = repo.snapshots(journal["run_id"])
    if not rows:
        journal.update(stage="interrupted_without_snapshot", finished_at_utc=now())
        save(state / "run.json", journal)
    else:
        accept_snapshot(repo, journal, state, config)
    clear_workspace(state)


def monitor(settings, state, config):
    require(SystemController(settings).healthy(), "coordinator_unhealthy")
    accepted = load(state / "accepted.json")
    require(accepted.get("restored_and_verified") is True
            and 0 <= time.time() - datetime.fromisoformat(accepted["captured_at_utc"]).timestamp() <= 86400,
            "backup_missing_or_stale")
    snapshot = json.loads(Path("/var/lib/haven/web/dashboard/snapshot.json").read_text())
    require(0 <= time.time() - datetime.fromisoformat(snapshot["generated_at_utc"]).timestamp() < 120,
            "dashboard_stale")
    ping(config, "service")


def maintenance_daemon():
    with maintenance_lock():
        address = os.environ.get("NOTIFY_SOCKET", "")
        require(address.startswith(("/", "@")), "systemd_notification_required")
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as connection:
            connection.connect("\0" + address[1:] if address.startswith("@") else address)
            connection.sendall(b"READY=1")
        while True:
            signal.pause()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("backup", "recover", "reconcile", "monitor", "maintenance"))
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        require(os.geteuid() == 0, "root_required")
        if args.mode == "maintenance":
            maintenance_daemon()
        else:
            protected_directory(STATE)
            protected_directory(CONFIG)
            settings = settings_from(CONFIG)
            if args.mode == "monitor":
                try:
                    with maintenance_lock():
                        monitor(settings, STATE, CONFIG)
                except CaptureError as error:
                    if str(error) != "maintenance_busy":
                        raise
                    # A bounded pause is not an outage. Do not forge a heartbeat;
                    # a prolonged maintenance window still expires externally.
                    print('{"result":"deferred","code":"maintenance_busy"}')
                    return 0
            else:
                with maintenance_lock():
                    if args.mode == "backup":
                        backup(settings, STATE, CONFIG, load(CONFIG / "capture.json"))
                    elif args.mode == "reconcile":
                        reconcile(settings, STATE, CONFIG)
                    else:
                        recover(settings, STATE)
        print(json.dumps({"result": "passed", "operation": args.mode}))
        return 0
    except Exception as error:
        if isinstance(error, CaptureError) and str(error) == "maintenance_busy":
            print('{"result":"deferred","code":"maintenance_busy"}')
            return 1
        if args.mode in {"backup", "monitor"}:
            try:
                ping(CONFIG, "service" if args.mode == "monitor" else "backup", "/fail")
            except Exception:
                pass
        # CaptureError codes are internal literals; unknown exceptions stay opaque.
        code = str(error) if isinstance(error, CaptureError) else "operation_failed"
        print(json.dumps({"result": "failed", "operation": args.mode, "code": code}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
