"""Offline full-controller experiment: local restic and synthetic profile only."""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts import backup_runtime as runtime  # noqa: E402
from scripts.backup_restore import rebuild  # noqa: E402


def probe():
    pins = json.loads((ROOT / "research/h1.9/pins.json").read_text())
    binary = ROOT / "build/h1.9/restic"
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == pins["binary_sha256"]
    with tempfile.TemporaryDirectory(prefix="haven-controller-synthetic-") as temporary:
        root = Path(temporary)
        state, source, config = [root / name for name in ("state", "source", "config")]
        for path in (state, source, config):
            path.mkdir(mode=0o700)
        password = config / "password"
        password.write_text(secrets.token_hex(32) + "\n")
        password.chmod(0o600)
        repository = root / "repository"
        policy = {"schema_version": 1, "include": ["config.yaml", "state.db", "skills"],
                  "exclude": [], "required": ["config.yaml", "state.db"], "sqlite": ["state.db"]}
        (source / "config.yaml").write_text("synthetic: true\n")
        (source / "skills").mkdir(mode=0o700)
        (source / "skills/custom.sh").write_text("#!/bin/sh\nexit 0\n")
        (source / "skills/custom.sh").chmod(0o750)

        class Controller:
            running = True

            def healthy(self):
                return self.running

            def stop(self):
                assert runtime.PauseJournal(state).pending()
                self.running = False

            def stopped(self):
                return not self.running

            def start(self):
                self.running = True

        controller = Controller()

        class LocalRepository(runtime.Repository):
            def __init__(self, settings, config, work):
                self.settings, self.config, self.work = settings, config, work

            def run(self, *arguments):
                assert controller.running, "network/archive work ran before service resume"
                return runtime.command([str(binary), "--no-cache", "--repo", str(repository),
                                        "--password-file", str(password), *arguments], cwd=self.work, timeout=60)

        settings = {"source_home": str(source), "hostname": "haven-synthetic", "public_revision": "a" * 40,
                    "private_revision": "b" * 40, "runtime_source": "/synthetic/source", "runtime_python": "/synthetic/python"}
        local = LocalRepository(settings, config, root)
        local.run("init", "--repository-version", "2")
        settings["repository_id"] = json.loads(local.run("cat", "config"))["id"]
        pings = []
        with closing(sqlite3.connect(source / "state.db")) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA wal_autocheckpoint=0")
            db.execute("CREATE TABLE events(value INTEGER)")
            db.execute("INSERT INTO events VALUES (42)")
            db.commit()
            with patch.object(runtime, "Repository", LocalRepository), \
                    patch.object(runtime, "SystemController", return_value=controller), \
                    patch.object(runtime, "ping", side_effect=lambda *args: pings.append(args[1:])):
                runtime.backup(settings, state, config, policy)
        accepted = runtime.load(state / "accepted.json")
        assert accepted["restored_and_verified"] and controller.running
        assert not (state / "resume.json").exists() and not (state / "work").exists()
        assert pings == [("backup", "/start"), ("backup",)]
        local.run("restore", accepted["snapshot_id"], "--target", str(root / "isolated"), "--verify")
        payload = root / "isolated/payload"
        manifest = runtime.validate_payload(payload, accepted["manifest_sha256"])
        owners = {"uids": {str(os.getuid()): os.getuid()}, "gids": {str(os.getgid()): os.getgid()}}
        rebuild(payload, root / "rebuilt", accepted["manifest_sha256"], owners)
        with closing(sqlite3.connect(root / "rebuilt/state.db")) as db:
            assert db.execute("SELECT value FROM events").fetchall() == [(42,)]
        assert (root / "rebuilt/skills/custom.sh").stat().st_mode & 0o777 == 0o750
        assert manifest["run_id"] == accepted["run_id"]
        result = {"result": "passed", "scope": "synthetic local controller only", "restic_version": pins["version"],
                  "exact_snapshot_restored": True, "sqlite_wal_restored": True, "metadata_rebuilt": True,
                  "service_resumed_before_archive": True, "intent_and_staging_removed": True,
                  "production_credentials_used": False, "external_requests": False,
                  "source_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                  "source_dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)),
                  "source_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                    for path in (ROOT / "scripts/backup_runtime.py", ROOT / "scripts/backup_restore.py", Path(__file__))}}
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    probe()
