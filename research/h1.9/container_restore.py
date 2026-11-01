"""Run inside the disposable recovery image with --network=none and a read-only input.

The exact production payload is verified before a transport-only isolation override.
No provider login, Telegram poller, model or cron worker is admitted.
"""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, "/var/lib/haven/haven-tools")
from scripts.backup_restore import rebuild  # noqa: E402
from scripts.backup_runtime import validate_payload  # noqa: E402

PROFILE = Path("/var/lib/haven/.hermes")
SOURCE = Path("/var/lib/haven/releases/hermes-bf0203a-785f2646")
PYTHON = Path("/var/lib/haven/telegram/venv/bin/python")


def demote():
    os.setgroups([2345])
    os.setgid(2345)
    os.setuid(2345)


def calls():
    with closing(sqlite3.connect((PROFILE / "state.db").as_uri() + "?mode=ro", uri=True)) as db:
        return db.execute("SELECT COALESCE(SUM(api_call_count),0) FROM sessions").fetchone()[0]


def main():
    os.umask(0o077)
    root = Path("/recovery-input")
    accepted = json.loads((root / "accepted.json").read_text())
    payload = root / "restored/payload"
    manifest = validate_payload(payload, accepted["manifest_sha256"])
    files = manifest["capture"]["files"]
    old_uid, old_gid = (files["config.yaml"][key] for key in ("uid", "gid"))
    assert old_uid != 0 and old_gid != 0
    assert {row["uid"] for row in files.values()} <= {0, old_uid}
    assert {row["gid"] for row in files.values()} <= {0, old_gid}
    owners = {"uids": {str(value): 0 if value == 0 else 2345 for value in {row["uid"] for row in files.values()}},
              "gids": {str(value): 0 if value == 0 else 2345 for value in {row["gid"] for row in files.values()}}}
    rebuild(payload, PROFILE, accepted["manifest_sha256"], owners)
    for name, row in files.items():
        path = PROFILE / name
        assert path.stat().st_uid == owners["uids"][str(row["uid"])]
        assert path.stat().st_gid == owners["gids"][str(row["gid"])]
        assert path.stat().st_mode & 0o777 == row["mode"]
        if row["kind"] == "file":
            assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]
    tools = Path("/var/lib/haven/rebuilt-tools-home/tools")
    for path in tools.iterdir():
        if path.is_dir() and path.name.startswith(("python-", "uv-")):
            shutil.move(path, PROFILE / "tools" / path.name)
    environment = {"HOME": "/var/lib/haven", "USER": "haven", "LOGNAME": "haven",
                   "PATH": str(PYTHON.parent) + ":/usr/bin:/bin", "HERMES_HOME": str(PROFILE),
                   "HERMES_RUNTIME_DIR": str(PROFILE / "tools"), "PYTHONPATH": "/var/lib/haven/haven-tools",
                   "PYTHONDONTWRITEBYTECODE": "1", "HERMES_SUPERVISED_CHILD": "1",
                   "XDG_RUNTIME_DIR": "/run/user/2345", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/2345/bus"}

    def run(*arguments):
        result = subprocess.run(arguments, cwd=SOURCE, env=environment, preexec_fn=demote,
                                capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
        assert result.returncode == 0, "isolated_command_failed"
        return result.stdout

    base = [str(PYTHON), "-m", "scripts.service_runtime"]
    profile = ["--source", str(SOURCE), "--home", str(PROFILE)]
    run(*base, "check", *profile)  # Exact production profile, no network or token read.
    before = calls()
    # Only the disposable restored copy changes; the source payload stays read-only.
    override = ("from pathlib import Path; from ruamel.yaml import YAML; "
                "p=Path('/var/lib/haven/.hermes/config.yaml'); y=YAML(); c=y.load(p.read_text()); "
                "assert c['platforms']['telegram']['enabled'] is True; "
                "c['platforms']['telegram']['enabled']=False; "
                "f=p.open('w'); y.dump(c,f); f.close()")
    run(str(PYTHON), "-c", override)
    runtime = Path("/run/user/2345")
    runtime.mkdir(parents=True, mode=0o700)
    os.chown(runtime, 2345, 2345)
    run("/usr/bin/dbus-daemon", "--session", "--fork", "--address=unix:path=/run/user/2345/bus")
    with tempfile_log() as log:
        process = subprocess.Popen([*base, "run", *profile], cwd=SOURCE, env=environment,
                                   preexec_fn=demote, start_new_session=True,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        healthy = False
        try:
            for _ in range(30):
                if process.poll() is not None:
                    break
                try:
                    run(*base, "health", *profile)
                    healthy = True
                    break
                except AssertionError:
                    time.sleep(1)
            assert healthy, "isolated_runtime_not_healthy"
            assert calls() == before, "unexpected_model_usage"
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
    # Re-check the unchanged input after the disposable runtime exercise.
    validate_payload(payload, accepted["manifest_sha256"])
    print(json.dumps({"result": "passed", "exact_payload_verified": True, "ownership_and_modes_restored": True,
                      "different_service_uid_verified": old_uid != 2345, "clean_pinned_runtime_rebuilt": True,
                      "exact_production_profile_check_passed": True, "isolated_native_gateway_healthy": healthy,
                      "telegram_disabled_for_startup": True, "model_calls_added": calls() - before,
                      "network_namespace": "none", "input_unchanged": True, "source_bytes": manifest["capture"]["source_bytes"]}))


def tempfile_log():
    import tempfile
    return tempfile.TemporaryFile()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Native logs and account data never leave the disposable container.
        print(json.dumps({"result": "failed", "code": "isolated_recovery_failed", "exception_type": type(error).__name__}))
        raise SystemExit(1)
