#!/usr/bin/env python3
"""Probe a rootless container boundary with synthetic files; does not run Hermes."""
import json
import os
from pathlib import Path
import subprocess
import tempfile

IMAGE = "docker.io/library/busybox@sha256:66a6306db78bf2dbf3487f293aa8d6990d8e506fdffab9cc43fe422becf886e4"


def main():
    with tempfile.TemporaryDirectory(prefix="haven-h0.1-isolation-") as temp:
        root = Path(temp)
        runtime = root / "runtime"
        runtime.mkdir(mode=0o700)
        workspace = root / "workspace"
        workspace.mkdir()
        credentials = root / "nora" / "session"
        credentials.parent.mkdir()
        credentials.write_text("SYNTHETIC-SESSION-DO-NOT-MOUNT\n")
        host_config = root / "host-config"
        host_config.write_text("original\n")
        vault = root / "vault" / "private-note"
        vault.parent.mkdir()
        vault.write_text("SYNTHETIC-VAULT-DO-NOT-MOUNT\n")
        env = {k: os.environ[k] for k in ("PATH", "LANG") if k in os.environ}
        env["XDG_RUNTIME_DIR"] = str(runtime)
        # Intentionally present in the Podman client's environment, never in the container.
        env["HAVEN_SYNTHETIC_SECRET"] = "SYNTHETIC-ENV-DO-NOT-FORWARD"
        base = ["podman", "--root", "/tmp/haven-h0.1-podman-storage", "--runroot",
                "/tmp/haven-h0.1-podman-run", "--storage-driver", "vfs",
                "--cgroup-manager", "cgroupfs"]
        info = subprocess.run(base + ["info", "--format", "{{.Host.Security.Rootless}}"],
                              env=env, check=True, capture_output=True, text=True)
        assert info.stdout.strip() == "true", "Refusing a rootful probe"
        script = '''
test ! -e "$1"
test ! -e "$2"
test ! -e "$3"
test ! -e "$4"
test -z "${HAVEN_SYNTHETIC_SECRET-}"
test ! -S /var/run/docker.sock
test ! -S /run/podman/podman.sock
if echo changed > /tmp/haven-probe 2>/dev/null; then exit 10; fi
routes="$(ip route)"
test -z "$routes"
printf 'workspace writable\n' > /workspace/probe.txt
printf 'PASS: hidden session, hidden host config, no inherited secret, no vault, no socket, read-only root, no route, writable workspace\n'
'''
        proc = subprocess.run(base + ["run", "--rm", "--pull=never", "--network=none",
                              "--read-only", "--read-only-tmpfs=false", "--cap-drop=all", "--security-opt=no-new-privileges",
                              "--userns=keep-id", "--user", f"{os.getuid()}:{os.getgid()}",
                              "--volume", f"{workspace}:/workspace:rw", IMAGE,
                              "sh", "-ec", script, "probe", str(credentials), str(host_config),
                              str(Path.home() / ".codex"), str(vault)],
                              env=env, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f"rc={proc.returncode}: {proc.stderr} {proc.stdout}"
        assert credentials.read_text() == "SYNTHETIC-SESSION-DO-NOT-MOUNT\n"
        assert host_config.read_text() == "original\n"
        assert vault.read_text() == "SYNTHETIC-VAULT-DO-NOT-MOUNT\n"
        assert (workspace / "probe.txt").read_text() == "workspace writable\n"
        result = {"probe": "rootless-container-boundary", "rootless": True,
                  "image": IMAGE, "returncode": proc.returncode,
                  "result": proc.stdout.strip(),
                  "limits": "BusyBox filesystem/network probe; Hermes, Codex and Kanban were not run in this container."}
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
