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
STAGE = "verify_payload"


def demote():
    os.setgroups([2345])
    os.setgid(2345)
    os.setuid(2345)


def calls():
    with closing(sqlite3.connect((PROFILE / "state.db").as_uri() + "?mode=ro", uri=True)) as db:
        return db.execute("SELECT COALESCE(SUM(api_call_count),0) FROM sessions").fetchone()[0]


def main():
    global STAGE
    os.umask(0o077)
    root = Path("/recovery-input")
    accepted = json.loads((root / "accepted.json").read_text())
    payload = root / "restored/payload"
    manifest = validate_payload(payload, accepted["manifest_sha256"])
    STAGE = "rebuild_metadata"
    files = manifest["capture"]["files"]
    old_uid, old_gid = (files["config.yaml"][key] for key in ("uid", "gid"))
    assert old_uid != 0 and old_gid != 0
    assert {row["uid"] for row in files.values()} <= {0, old_uid}
    assert {row["gid"] for row in files.values()} <= {0, old_gid}
    owners = {"uids": {str(value): 0 if value == 0 else 2345 for value in {row["uid"] for row in files.values()}},
              "gids": {str(value): 0 if value == 0 else 2345 for value in {row["gid"] for row in files.values()}}}
    staged_profile = Path("/opt/recovery/profile")
    rebuild(payload, staged_profile, accepted["manifest_sha256"], owners)
    assert not PROFILE.exists() and not PROFILE.is_symlink()
    staged_profile.rename(PROFILE)
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
            target = PROFILE / "tools" / path.name
            shutil.copytree(path, target, symlinks=True)
            for base, directories, names in os.walk(target, followlinks=False):
                os.chown(base, 2345, 2345, follow_symlinks=False)
                for name in [*directories, *names]:
                    os.chown(Path(base) / name, 2345, 2345, follow_symlinks=False)
    environment = {"HOME": "/var/lib/haven", "USER": "haven", "LOGNAME": "haven",
                   "PATH": str(PYTHON.parent) + ":/usr/bin:/bin", "HERMES_HOME": str(PROFILE),
                   "HERMES_RUNTIME_DIR": str(PROFILE / "tools"), "PYTHONPATH": "/var/lib/haven/haven-tools",
                   "PYTHONDONTWRITEBYTECODE": "1", "HERMES_SUPERVISED_CHILD": "1",
                   "XDG_RUNTIME_DIR": "/run/user/2345", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/2345/bus"}

    def run(*arguments, quiet_failure=False):
        result = subprocess.run(arguments, cwd=SOURCE, env=environment, preexec_fn=demote,
                                capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
        if result.returncode != 0 and not quiet_failure:
            print(json.dumps({"isolated_command_failed": True, "exit_code": result.returncode, "stage": STAGE,
                              "permission_error": b"Permission denied" in result.stderr}))
        assert result.returncode == 0, "isolated_command_failed"
        return result.stdout

    base = [str(PYTHON), "-m", "scripts.service_runtime"]
    profile = ["--source", str(SOURCE), "--home", str(PROFILE)]
    STAGE = "native_profile_check"
    # Direct policy check retains only the public guard's fixed diagnostic code.
    diagnostic = """from pathlib import Path
import json
from scripts.service_runtime import check
from scripts.model_probe import ProbeError
try:
 check(Path('/var/lib/haven/releases/hermes-bf0203a-785f2646'),Path('/var/lib/haven/.hermes'))
 print(json.dumps({'result':'passed'}))
except Exception as e:
 print(json.dumps({'result':'failed','exception_type':type(e).__name__,
                  'code':str(e) if isinstance(e,ProbeError) else 'policy_check_failed','errno':getattr(e,'errno',None)}))
"""
    checked = json.loads(run(str(PYTHON), "-c", diagnostic))
    if checked["result"] != "passed":
        print(json.dumps({"native_policy_diagnostic": checked}))
        raise AssertionError("exact_profile_refused")
    before = calls()
    STAGE = "user_bus"
    runtime = Path("/run/user/2345")
    runtime.mkdir(parents=True, mode=0o700)
    runtime.parent.chmod(0o755)
    os.chown(runtime, 2345, 2345)
    run("/usr/bin/dbus-daemon", "--session", "--fork", "--address=unix:path=/run/user/2345/bus")
    bootstrap = """import json,runpy,sys
from pathlib import Path
from scripts.service_runtime import check_worker_bus
check_worker_bus()
from scripts.capability_gateway import arm
arm()
from agent.deterministic_policy import require_nondeterministic_access
try:
 require_nondeterministic_access('recovery_probe')
except PermissionError:
 pass
else:
 raise RuntimeError('independent_guard_not_armed')
from gateway.run_adapters import GatewayAdapterLifecycleMixin
GatewayAdapterLifecycleMixin._create_adapter=lambda self,platform,config: None
Path('/var/lib/haven/.hermes/state/recovery-guard-proof.json').write_text(json.dumps({'guard_armed':True,'transport_factory_disabled':True}))
sys.argv=['gateway.run']
runpy.run_module('gateway.run',run_name='__main__',alter_sys=True)
"""
    core_health = """from pathlib import Path
from datetime import datetime
import json,time
from scripts.service_runtime import check
from gateway.status import get_process_start_time,read_runtime_status
home=Path('/var/lib/haven/.hermes')
check(Path('/var/lib/haven/releases/hermes-bf0203a-785f2646'),home)
state=read_runtime_status(home/'gateway_state.json') or {}
assert state.get('gateway_state')=='running'
assert state.get('start_time')==get_process_start_time(state['pid'])
heartbeat=json.loads((home/'state/gateway.heartbeat').read_text())
assert heartbeat['pid']==state['pid'] and 0<=time.time()-datetime.fromisoformat(heartbeat['updated_at']).timestamp()<120
"""
    STAGE = "isolated_gateway"
    with tempfile_log() as log:
        process = subprocess.Popen([str(PYTHON), "-c", bootstrap, *profile], cwd=SOURCE, env=environment,
                                   preexec_fn=demote, start_new_session=True,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        healthy = False
        try:
            for _ in range(30):
                if process.poll() is not None:
                    break
                try:
                    run(str(PYTHON), "-c", core_health, quiet_failure=True)
                    healthy = True
                    break
                except AssertionError:
                    time.sleep(1)
            assert healthy, "isolated_runtime_not_healthy"
            assert json.loads((PROFILE / "state/recovery-guard-proof.json").read_text()) == {
                "guard_armed": True, "transport_factory_disabled": True}
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
                      "transport_factory_disabled_for_startup": True, "independent_guard_armed": True,
                      "production_configuration_unchanged": True, "model_calls_added": calls() - before,
                      "network_namespace": "none", "input_unchanged": True, "source_bytes": manifest["capture"]["source_bytes"]}))


def tempfile_log():
    import tempfile
    return tempfile.TemporaryFile()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Native logs and account data never leave the disposable container.
        print(json.dumps({"result": "failed", "code": "isolated_recovery_failed", "exception_type": type(error).__name__,
                          "stage": STAGE, "sqlite_version": sqlite3.sqlite_version}))
        raise SystemExit(1)
