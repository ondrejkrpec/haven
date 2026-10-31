"""Opt-in initialization and synthetic restore of a dedicated Scaleway repository."""

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile

from scripts.private_enrollment import PrivateArgumentParser, check_parent, DIRECTORY_FLAGS
from scripts.scaleway_backup_policy import ROOT, validate
from scripts.scaleway_preflight import Journal, clients, private_keys, require


def password_value(directory):
    """Validate bounded secret bytes through no-follow descriptors; never log them."""
    directory = Path(directory)
    require(directory.is_absolute() and ".." not in directory.parts)
    parent = os.open("/", DIRECTORY_FLAGS)
    try:
        check_parent(parent)
        for part in directory.parts[1:]:
            child = os.open(part, DIRECTORY_FLAGS, dir_fd=parent)
            os.close(parent)
            parent = child
            check_parent(parent)
        info = os.fstat(parent)
        require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700)
        fd = os.open("password", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                    and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
                    and 33 <= info.st_size <= 257)
            value = os.read(fd, 258)
            require(value.endswith(b"\n") and 32 <= len(value[:-1]) <= 256
                    and all(32 <= char <= 126 for char in value[:-1]))
            return value
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def initial_prefix(client, settings, preflight):
    """Refuse adoption of any existing repository or unknown objects before init."""
    require(preflight.get("result") == "passed" and preflight.get("synthetic_writes") is True
            and preflight.get("bucket") == settings["bucket"]
            and preflight.get("prefix") == settings["prefix"]
            and preflight.get("checks", {}).get("lock_cleanup_verified") is True
            and preflight.get("multipart_aborted") is True)
    run = preflight.get("run_id", "")
    require(re.fullmatch("[a-f0-9]{32}", run))
    allowed = {f"{settings['prefix']}/preflight/{run}/payload"}
    response = client.list_objects_v2(Bucket=settings["bucket"], Prefix=settings["prefix"] + "/", MaxKeys=100)
    require(not response.get("IsTruncated")
            and {row["Key"] for row in response.get("Contents", [])} <= allowed)
    response = client.list_multipart_uploads(Bucket=settings["bucket"], Prefix=settings["prefix"] + "/", MaxUploads=1)
    require(not response.get("Uploads") and not response.get("IsTruncated"))
    response = client.get_bucket_versioning(Bucket=settings["bucket"])
    require(response.get("Status") == "Enabled")


class Restic:
    def __init__(self, binary, settings, keys, root, journal):
        self.binary, self.root, self.journal = binary, root, journal
        # A fresh environment excludes login caches, proxies and debug settings.
        self.environment = {"PATH": "/usr/bin:/bin", "HOME": str(root), "TMPDIR": str(root),
                            "LANG": "C.UTF-8", "AWS_ACCESS_KEY_ID": keys[0],
                            "AWS_SECRET_ACCESS_KEY": keys[1], "AWS_DEFAULT_REGION": "fr-par",
                            "AWS_SHARED_CREDENTIALS_FILE": "/nonexistent", "AWS_CONFIG_FILE": "/nonexistent",
                            "AWS_EC2_METADATA_DISABLED": "true"}
        self.repository = f"s3:https://s3.fr-par.scw.cloud/{settings['bucket']}/{settings['prefix']}"

    def run(self, stage, *args, password="password", expected=0):
        self.journal.data["stage"] = stage
        self.journal.save()  # Persist intent before a command with external effects.
        command = [str(self.binary), "--no-cache", "--repo", self.repository,
                   "--password-file", str(self.root / password), "--stuck-request-timeout", "20s",
                   "-o", "s3.region=fr-par", "-o", "s3.bucket-lookup=path",
                   "-o", "s3.connections=2", "-o", "s3.retries=1", "-o", "s3.storage-class=STANDARD", *args]
        result = subprocess.run(command, cwd=self.root, env=self.environment, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=180)
        # Raw output may contain endpoint/identity information and is never persisted.
        self.journal.data.setdefault("commands", []).append({"stage": stage, "exit_code": result.returncode})
        self.journal.save()
        require(result.returncode == expected)
        return result.stdout


def manifest(root):
    return {str(path.relative_to(root)): {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                        "mode": stat.S_IMODE(path.stat().st_mode)}
            for path in root.rglob("*") if path.is_file()}


def synthetic_restore(restic, journal):
    root = restic.root
    source, payload = root / "source", root / "payload"
    source.mkdir(mode=0o700)
    payload.mkdir(mode=0o700)
    with closing(sqlite3.connect(source / "state.db")) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("CREATE TABLE events(value TEXT)")
        db.execute("INSERT INTO events VALUES ('synthetic committed WAL row')")
        db.commit()
        require((source / "state.db-wal").stat().st_size > 0)
        with closing(sqlite3.connect(payload / "state.db")) as target:
            db.backup(target)
    (payload / "state.db").chmod(0o600)
    (payload / "config.json").write_text('{"synthetic":true}\n')
    (payload / "config.json").chmod(0o600)
    expected = manifest(payload)
    journal.data["manifest"] = expected
    output = restic.run("backup", "backup", "--json", "--host", "haven-synthetic",
                        "--tag", "h1.9-remote-probe", "payload")
    summaries = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    snapshots = [row["snapshot_id"] for row in summaries if row.get("message_type") == "summary"]
    require(len(snapshots) == 1 and re.fullmatch("[a-f0-9]{64}", snapshots[0]))
    snapshot = snapshots[0]
    journal.data["snapshot_id"] = snapshot
    journal.save()
    listed = json.loads(restic.run("snapshot_identity", "snapshots", "--json"))
    require(len(listed) == 1 and listed[0]["id"] == snapshot
            and listed[0]["hostname"] == "haven-synthetic"
            and listed[0]["tags"] == ["h1.9-remote-probe"])
    restic.run("integrity", "check", "--read-data")
    shutil.rmtree(source)
    shutil.rmtree(payload)
    restic.run("restore", "restore", snapshot, "--target", "restored", "--verify")
    restored = root / "restored/payload"
    require(manifest(restored) == expected)
    with closing(sqlite3.connect((restored / "state.db").as_uri() + "?mode=ro", uri=True)) as db:
        require(db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
                and db.execute("SELECT value FROM events").fetchall() == [("synthetic committed WAL row",)])
    (root / "wrong-password").write_text(secrets.token_hex(32) + "\n")
    (root / "wrong-password").chmod(0o600)
    restic.run("wrong_password", "restore", snapshot, "--target", "wrong-restore",
               password="wrong-password", expected=12)
    require(not (root / "wrong-restore").exists())
    journal.data["checks"].update(exact_files_and_modes_restored=True, sqlite_committed_wal_restored=True,
                                  restored_after_sources_removed=True, wrong_password_refused=True,
                                  full_data_check_passed=True, exact_snapshot_verified=True)
    journal.save()


def main(argv=None):
    parser = PrivateArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    for name in ("settings", "credentials", "password-directory", "preflight", "receipt"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.live:
        print("Explicit --live is required; no credentials read or request sent.", file=sys.stderr)
        return 2
    logging.disable(logging.CRITICAL)
    journal = None
    previous = os.umask(0o077)
    try:
        settings = json.loads(args.settings.read_text())
        validate(settings)
        pins = json.loads((ROOT / "research/h1.9/pins.json").read_text())
        binary = ROOT / "build/h1.9/restic"
        require(not binary.is_symlink()
                and hashlib.sha256(binary.read_bytes()).hexdigest() == pins["binary_sha256"])
        password = password_value(args.password_directory)
        keys = private_keys(args.credentials)
        preflight = json.loads(args.preflight.read_text())
        journal = Journal(args.receipt, settings, True)
        journal.data.update(scope="synthetic remote restic only", commands=[], checks={},
                            source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                            source_revision=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                            restic_version=pins["version"], binary_sha256=pins["binary_sha256"],
                            not_tested=["production_backup", "clean_host_recovery", "owner_version_recovery", "external_alerts"])
        journal.data["stage"] = "initial_prefix"
        journal.save()
        client, _ = clients(settings, keys, False)
        initial_prefix(client, settings, preflight)
        with tempfile.TemporaryDirectory(prefix="haven-restic-remote-") as temporary:
            root = Path(temporary)
            (root / "password").write_bytes(password)
            (root / "password").chmod(0o600)
            restic = Restic(binary, settings, keys, root, journal)
            restic.run("missing_repository", "cat", "config", expected=10)
            restic.run("initialize", "init", "--repository-version", "2")
            configuration = json.loads(restic.run("repository_identity", "cat", "config"))
            require(configuration.get("version") == 2 and re.fullmatch("[a-f0-9]{64}", configuration.get("id", "")))
            journal.data["repository_id"] = configuration["id"]
            journal.save()
            synthetic_restore(restic, journal)
            require(not client.list_objects_v2(Bucket=settings["bucket"], Prefix=settings["prefix"] + "/locks/", MaxKeys=1).get("Contents"))
            journal.data["checks"]["no_current_locks"] = True
        journal.data.update(result="passed", stage="complete")
    except BaseException:
        if journal is not None:
            journal.data.update(result="failed", failed_stage=journal.data["stage"])
        # An uncertain init/backup is never retried or erased automatically.
    finally:
        os.umask(previous)
        if journal is not None:
            journal.data["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
            journal.save()
    passed = journal is not None and journal.data["result"] == "passed"
    print(json.dumps({"result": "passed" if passed else "failed",
                      "stage": journal.data["stage"] if journal is not None else "local_validation",
                      "values_disclosed": False}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
