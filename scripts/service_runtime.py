"""Read-only H1.5 service guard and health check. Never resolve account credentials."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import stat
import sys
import time

from scripts.model_contract import check_service
from scripts.model_probe import ProbeError, check_candidate, private_file


def check(source, home):
    if os.geteuid() == 0 or os.environ.get("HERMES_HOME") != str(home):
        raise ProbeError("service_identity_or_profile_mismatch")
    info = home.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ProbeError("private_home_permissions")
    from ruamel.yaml import YAML
    check_service(YAML(typ="safe", pure=True).load(private_file(home / "config.yaml").read_text()))
    check_candidate(source)
    # Profile selection and cron stores are administrator inputs. H1.5 starts empty;
    # do not accidentally adopt other profiles, hooks, schedules or plugin sources.
    for name in ("active_profile", ".env", "shell-hooks-allowlist.json"):
        if (home / name).exists() or (home / name).is_symlink():
            raise ProbeError("unexpected_service_input")
    for name in ("profiles", "plugins"):
        path = home / name
        if path.is_symlink() or (path.exists() and any(path.iterdir())):
            raise ProbeError("unexpected_service_input")


def health(source, home):
    check(source, home)
    sys.path.insert(0, str(source))
    from gateway.status import get_process_start_time, read_runtime_status
    state = read_runtime_status(home / "gateway_state.json") or {}
    pid = state.get("pid")
    if (not isinstance(pid, int) or state.get("gateway_state") != "running"
            or state.get("start_time") is None
            or get_process_start_time(pid) != state["start_time"]):
        raise ProbeError("gateway_not_ready")
    heartbeat = json.loads((home / "state/gateway.heartbeat").read_text())
    age = time.time() - datetime.fromisoformat(heartbeat["updated_at"]).timestamp()
    if heartbeat.get("pid") != pid or not 0 <= age < 120:
        raise ProbeError("gateway_heartbeat_stale")
    return {"result": "passed", "check": "health", "pid": pid}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "health", "run"))
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.mode == "health":
            result = health(args.source, args.home)
        else:
            check(args.source, args.home)
            if args.mode == "run":
                # Stay systemd's main PID: exit 78 from this guard must park the
                # service; RestartPreventExitStatus does not cover ExecStartPre.
                os.chdir(args.source)
                os.execv(sys.executable, [sys.executable, "-m", "hermes_cli.main",
                                         "--profile", "default", "gateway", "run", "--quiet"])
            result = {"result": "passed", "check": "service_policy"}
    except Exception:
        # Provider bodies, config values, file contents and exception strings stay private.
        print(json.dumps({"result": "failed", "code": "service_check_failed"}))
        return 78
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
