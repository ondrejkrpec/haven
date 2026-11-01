"""Explicit synthetic VPS probe of real stop/SIGKILL/ExecStopPost recovery.

The caller creates one transient sleep service and one job with ExecStopPost.
No credentials, production profile or network API is used by this probe.
"""

from contextlib import closing
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys

sys.path.insert(0, "/usr/local/lib/haven-backup")
from scripts import backup_capture as capture  # noqa: E402
from scripts import backup_runtime as runtime  # noqa: E402

ROOT = Path("/var/lib/haven-backup-fixture")
UNIT = "haven-h19-fixture-source.service"
POLICY = {"schema_version": 1, "include": ["config.yaml", "state.db"], "exclude": [],
          "required": ["config.yaml", "state.db"], "sqlite": ["state.db"]}


class Controller(runtime.SystemController):
    def __init__(self):
        super().__init__({}, unit=UNIT)

    def healthy(self):
        return self.state().get("SubState") == "running"


def main(mode):
    os.umask(0o077)
    controller, journal = Controller(), runtime.PauseJournal(ROOT)
    with runtime.maintenance_lock(Path("/run/lock/haven-h19-fixture.lock")):
        if mode == "setup":
            # A stopped transient unit is unloaded and cannot be restarted.
            # Use a disposable runtime unit file, matching production semantics.
            unit_file = Path("/run/systemd/system") / UNIT
            with unit_file.open("x") as stream:
                stream.write("[Unit]\nDescription=Haven synthetic recovery fixture\n[Service]\nExecStart=/usr/bin/sleep infinity\n")
            runtime.command(["systemctl", "daemon-reload"])
            (ROOT / "source").mkdir(mode=0o700)
            (ROOT / "source/config.yaml").write_text("synthetic: true\n")
            with closing(sqlite3.connect(ROOT / "source/state.db")) as db:
                db.execute("CREATE TABLE fixture(value INTEGER)")
                db.execute("INSERT INTO fixture VALUES (42)")
                db.commit()
        elif mode == "capture":
            original = capture.stage_stopped

            def blocked(*args, **kwargs):
                receipt = original(*args, **kwargs)
                runtime.save(ROOT / "stopped.json", {"service_stopped": controller.stopped()})
                signal.pause()  # Explicit acceptance kills this disposable job.
                return receipt

            capture.stage_stopped = blocked
            capture.capture_with_pause(controller, journal, ROOT / "source", ROOT / "work", POLICY)
        elif mode == "recover":
            capture.recover_pause(controller, journal)
            runtime.clear_workspace(ROOT)
        elif mode == "verify":
            assert controller.healthy() and not journal.pending() and not (ROOT / "work").exists()
            assert runtime.load(ROOT / "stopped.json")["service_stopped"] is True
            print(json.dumps({"result": "passed", "synthetic_only": True, "real_systemd_stop_verified": True,
                              "sigkill_recovery_verified": True, "staging_cleaned": True, "resume_intent_cleared": True,
                              "production_service_stopped": False, "credentials_used": False}))
        else:
            raise ValueError("unknown_probe_mode")


if __name__ == "__main__":
    main(sys.argv[1])
