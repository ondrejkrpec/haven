#!/usr/bin/env python3
"""Exercise Hermes' internal spawn_fn seam with a synthetic container executor.

No model or gateway runs here. The trusted parent owns the board; the container
receives only a workspace. This is a feasibility probe, not a Haven dispatcher.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from isolation import IMAGE


def main():
    source = Path(sys.argv[1]).resolve()
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    assert revision == "bf0203a794a07aac3ff764972ab9087baa4e4f4e"
    with tempfile.TemporaryDirectory(prefix="haven-h0.1-dispatch-") as temp:
        root = Path(temp)
        home = root / "hermes"
        home.mkdir()
        workspace = root / "workspace"
        workspace.mkdir()
        (home / "config.yaml").write_text("kanban:\n  review_dispatch: true\n")
        (home / "synthetic-nora-session").write_text("DO-NOT-EXPOSE\n")
        os.environ["HERMES_HOME"] = str(home)
        os.environ["HERMES_KANBAN_HOME"] = str(home)
        db = home / "kanban.db"
        os.environ["HERMES_KANBAN_DB"] = str(db)
        sys.path.insert(0, str(source))
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc
        from hermes_cli import kanban_db_dispatch as kbd

        kb.init_db(db)
        client_env = {k: os.environ[k] for k in ("PATH", "LANG") if k in os.environ}
        client_env["XDG_RUNTIME_DIR"] = "/tmp/haven-h0.1-podman-runtime"
        base = ["podman", "--root", "/tmp/haven-h0.1-podman-storage", "--runroot",
                "/tmp/haven-h0.1-podman-run", "--storage-driver", "vfs", "--cgroup-manager", "cgroupfs"]
        rootless = subprocess.check_output(base + ["info", "--format", "{{.Host.Security.Rootless}}"],
                                          env=client_env, text=True).strip()
        assert rootless == "true", "Refusing a rootful probe"
        children = []
        phase = "worker"

        def spawn(task, path):
            assert Path(path) == workspace
            # Board and Nora's files are siblings of the mounted workspace, never mounted.
            script = 'test ! -e "$1"; test ! -e "$2"; '
            if phase == "worker":
                script += 'printf "synthetic fix\\n" > /workspace/fix.txt; printf "ready-for-review\\n"'
            else:
                script += 'test "$(cat /workspace/fix.txt)" = "synthetic fix"; printf "verified\\n"'
            child = subprocess.Popen(base + ["run", "--rm", "--pull=never", "--network=none",
                         "--read-only", "--cap-drop=all", "--security-opt=no-new-privileges",
                         "--userns=keep-id", "--user", f"{os.getuid()}:{os.getgid()}",
                         "--volume", f"{workspace}:/workspace:rw", IMAGE,
                         "sh", "-ec", script, "probe", str(db), str(home / "synthetic-nora-session")],
                         env=client_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         start_new_session=True)
            children.append(child)
            return child.pid

        with kbc.connect(db) as conn:
            tid = kb.create_task(conn, title="synthetic isolated fix", assignee="default",
                     workspace_kind="dir", workspace_path=str(workspace), idempotency_key="h0.1-probe")
            duplicate = kb.create_task(conn, title="duplicate intake", assignee="default",
                                      idempotency_key="h0.1-probe")
            assert duplicate == tid
            tick = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=1, max_in_progress=1)
            assert len(tick.spawned) == 1, repr(tick)
            child = children[-1]
            try:
                out, err = child.communicate(timeout=30)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
            assert child.returncode == 0, err
            assert out.strip() == "ready-for-review"
            run_id = kb.get_task(conn, tid).current_run_id
            assert kb.request_review(conn, tid, summary="synthetic fix available",
                                     reviewer="default", expected_run_id=run_id)

        # Re-open the durable board before the next dispatch, without a Nora/LLM call.
        phase = "reviewer"
        with kbc.connect(db) as conn:
            assert kb.get_task(conn, tid).status == "review"
            tick = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=1, max_in_progress=1)
            assert len(tick.spawned) == 1, repr(tick)
            child = children[-1]
            try:
                out, err = child.communicate(timeout=30)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
            assert child.returncode == 0, err
            assert out.strip() == "verified"
            run_id = kb.get_task(conn, tid).current_run_id
            assert kb.complete_task(conn, tid, summary="synthetic reviewer verified file", expected_run_id=run_id)
            assert kb.get_task(conn, tid).status == "done"
            outcomes = [r[0] for r in conn.execute("SELECT outcome FROM task_runs WHERE task_id=? ORDER BY id", (tid,))]
            assert outcomes == ["review_requested", "completed"], outcomes
        print(json.dumps({"probe": "kanban-to-isolated-executor", "revision": revision,
              "result": "PASS", "outcomes": outcomes, "idempotent_intake": True,
              "board_exposed_to_executor": False, "nora_running": False,
              "limits": "Synthetic executors, one default profile, internal spawn_fn API; no LLM, real Codex, role authorization or production supervision."}, indent=2))


if __name__ == "__main__":
    main()
