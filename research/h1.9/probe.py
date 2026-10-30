"""Opt-in local restic/SQLite experiment; synthetic data and a fresh local repository only."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import bz2
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
PINS = Path(__file__).with_name("pins.json")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install(binary, pins):
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
        raise ValueError("probe_platform_not_verified")
    with urlopen(pins["url"], timeout=30) as response:
        archive = response.read(32 * 1024 * 1024 + 1)
    if hashlib.sha256(archive).hexdigest() != pins["archive_sha256"]:
        raise ValueError("archive_checksum")
    data = bz2.decompress(archive)
    if hashlib.sha256(data).hexdigest() != pins["binary_sha256"]:
        raise ValueError("binary_checksum")
    binary.parent.mkdir(parents=True, exist_ok=True)
    if binary.is_symlink() or binary.parent.resolve() != binary.parent.absolute():
        raise ValueError("probe_destination")
    binary.write_bytes(data)
    binary.chmod(0o755)


def probe(binary, pins):
    if binary.is_symlink() or sha(binary) != pins["binary_sha256"]:
        raise ValueError("probe_binary_not_pinned")
    with tempfile.TemporaryDirectory(prefix="haven-h19-synthetic-") as temporary:
        root = Path(temporary)
        repository, source, staging = root / "repository", root / "source", root / "payload"
        source.mkdir(mode=0o700)
        staging.mkdir(mode=0o700)
        password = root / "password"
        password.write_text(secrets.token_hex(32))
        password.chmod(0o600)
        environment = {"PATH": "/usr/bin:/bin", "TMPDIR": temporary, "LANG": "C.UTF-8"}

        def restic(*args, repo=repository, key=password, success=True):
            result = subprocess.run([str(binary), "--no-cache", "--repo", str(repo),
                                     "--password-file", str(key), *args],
                                    cwd=root, env=environment, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=60)
            if (result.returncode == 0) != success:
                diagnostic = ROOT / "build/h1.9/probe-failure.txt"
                diagnostic.write_text((result.stdout + result.stderr).replace(temporary, "<fixture>"))
                raise ValueError("unexpected_restic_result_" + args[0] + "_" + str(result.returncode))
            return result

        marker = "H19_SYNTHETIC_CREDENTIAL_" + secrets.token_hex(32)
        with closing(sqlite3.connect(source / "state.db")) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA wal_autocheckpoint=0")
            db.execute("CREATE TABLE messages(body TEXT)")
            db.execute("INSERT INTO messages VALUES (?)", ("synthetic committed WAL row",))
            db.commit()
            assert (source / "state.db-wal").stat().st_size > 0
            with closing(sqlite3.connect(staging / "state.db")) as target:
                db.backup(target)
        (staging / "state.db").chmod(0o600)
        (staging / "credentials").mkdir(mode=0o700)
        token = staging / "credentials/token"
        token.write_text(marker)
        token.chmod(0o600)
        configuration = staging / "config.json"
        configuration.write_text('{"synthetic":true}')
        configuration.chmod(0o600)

        def manifest():
            return {str(p.relative_to(staging)): {"sha256": sha(p), "mode": stat.S_IMODE(p.stat().st_mode)}
                    for p in staging.rglob("*") if p.is_file()}

        def backup():
            result = restic("backup", "--json", "--host", "haven-synthetic", "--tag", "h1.9-probe", staging.name)
            summaries = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
            return next(row["snapshot_id"] for row in summaries if row.get("message_type") == "summary")

        def restore(snapshot, destination, expected):
            restic("restore", snapshot, "--target", str(destination), "--verify")
            candidates = list(destination.rglob("config.json"))
            assert len(candidates) == 1
            restored = candidates[0].parent
            actual = {str(p.relative_to(restored)): {"sha256": sha(p), "mode": stat.S_IMODE(p.stat().st_mode)}
                      for p in restored.rglob("*") if p.is_file()}
            assert actual == expected
            with closing(sqlite3.connect((restored / "state.db").as_uri() + "?mode=ro", uri=True)) as db:
                assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
                assert db.execute("SELECT body FROM messages").fetchall() == [("synthetic committed WAL row",)]

        expected = manifest()
        restic("init", "--repository-version", "2")
        first = backup()
        assert json.loads(restic("snapshots", "--json").stdout)[0]["id"] == first
        assert all(marker.encode() not in path.read_bytes() for path in repository.rglob("*") if path.is_file())
        restic("check", "--read-data")
        restore(first, root / "restore-first", expected)
        wrong = root / "wrong-password"
        wrong.write_text(secrets.token_hex(32))
        wrong.chmod(0o600)
        assert restic("restore", first, "--target", str(root / "wrong-restore"), key=wrong, success=False).returncode == 12
        assert not list((root / "wrong-restore").rglob("token"))
        assert restic("snapshots", repo=root / "missing-repository", success=False).returncode == 10
        # Retention/prune are exercised only on this disposable synthetic repository.
        configuration.write_text('{"synthetic":true,"revision":2}')
        second_expected = manifest()
        second = backup()
        assert second != first
        restic("forget", "--keep-last", "1", "--group-by", "host,paths,tags", "--prune")
        remaining = json.loads(restic("snapshots", "--json").stdout)
        assert [row["id"] for row in remaining] == [second]
        restic("check", "--read-data")
        shutil.rmtree(staging)
        shutil.rmtree(source)
        restore(second, root / "restore-final", second_expected)
        # Corruption is injected into a separate copy; the accepted repository stays healthy.
        damaged = root / "damaged-repository"
        shutil.copytree(repository, damaged)
        pack = next(path for path in (damaged / "data").rglob("*") if path.is_file())
        data = bytearray(pack.read_bytes())
        data[-1] ^= 1
        pack.chmod(0o600)
        pack.write_bytes(data)
        restic("check", "--read-data", repo=damaged, success=False)
        restic("check", "--read-data")
    return {"result": "passed", "scope": "synthetic local repository only", "restic_version": pins["version"],
            "sqlite_committed_wal_restored": True, "exact_files_and_modes_restored": True,
            "restored_after_sources_removed": True, "plaintext_marker_absent_from_repository": True,
            "wrong_password_refused": True, "missing_repository_refused": True,
            "retention_prune_and_remaining_restore": True, "corruption_detected": True,
            "production_credentials_used": False, "remote_repository_tested": False,
            "external_alert_delivery_tested": False, "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_sha256": {name: sha(ROOT / name) for name in
                              ("research/h1.9/probe.py", "research/h1.9/pins.json")}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="explicitly download the pinned public binary")
    args = parser.parse_args()
    pins = json.loads(PINS.read_text())
    binary = ROOT / "build/h1.9/restic"
    if args.download:
        install(binary, pins)
    output = ROOT / "build/h1.9/probe-result.json"
    output.unlink(missing_ok=True)
    (ROOT / "build/h1.9/probe-failure.txt").unlink(missing_ok=True)
    previous = os.umask(0o077)
    try:
        result = probe(binary, pins)
    finally:
        os.umask(previous)
    result["python_version"] = platform.python_version()
    result["sqlite_version"] = sqlite3.sqlite_version
    result["source_revision"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    result["source_dirty"] = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip())
    output.write_text(json.dumps(result, indent=2) + "\n")
    output.chmod(0o600)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
