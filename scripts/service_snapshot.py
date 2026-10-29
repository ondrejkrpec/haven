"""Root-only, bounded application rollback. OAuth, sessions and databases are never copied."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile


FILES = (
    "/etc/systemd/system/haven.service",
    "/var/lib/haven/.hermes/config.yaml",
    *(f"/var/lib/haven/haven-tools/scripts/{name}.py"
      for name in ("model_contract", "model_probe", "service_runtime")),
)


def systemctl(*args):
    return subprocess.run(["systemctl", *args, "haven.service"], capture_output=True, text=True, check=False)


def snapshot():
    root = Path(tempfile.mkdtemp(prefix="haven-service-", dir="/root"))
    manifest = {"active": systemctl("is-active").returncode == 0,
                "enabled": systemctl("is-enabled").stdout.strip() == "enabled", "files": []}
    for index, name in enumerate(FILES):
        path = Path(name)
        row = {"path": name, "present": path.exists()}
        if path.is_symlink():
            raise ValueError("Unexpected application symlink")
        if row["present"]:
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Unexpected application file")
            data = path.read_bytes()
            target = root / str(index)
            target.write_bytes(data)
            target.chmod(0o600)
            row.update(uid=info.st_uid, gid=info.st_gid, mode=stat.S_IMODE(info.st_mode),
                       sha256=hashlib.sha256(data).hexdigest())
        manifest["files"].append(row)
    (root / "manifest.json").write_text(json.dumps(manifest))
    (root / "manifest.json").chmod(0o600)
    return root


def read_manifest(root):
    manifest = json.loads((root / "manifest.json").read_text())
    if tuple(row["path"] for row in manifest["files"]) != FILES:
        raise ValueError("Snapshot scope differs")
    # Verify every receipt before changing any live file.
    for index, row in enumerate(manifest["files"]):
        if row["present"] and hashlib.sha256((root / str(index)).read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("Snapshot contents differ")
    return manifest


def restore(root):
    info = root.lstat()
    if (root.parent != Path("/root") or not root.name.startswith("haven-service-")
            or not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("Untrusted application snapshot")
    manifest = read_manifest(root)
    stopped = systemctl("stop")
    if stopped.returncode != 0 and systemctl("is-active").stdout.strip() not in {"inactive", "failed", "unknown"}:
        raise ValueError("Application could not be stopped")
    systemctl("disable")
    for index, row in enumerate(manifest["files"]):
        path = Path(row["path"])
        if path.is_symlink():
            raise ValueError("Refusing live application symlink")
        if row["present"]:
            shutil.copyfile(root / str(index), path)
            path.chmod(row["mode"])
            os.chown(path, row["uid"], row["gid"])
        else:
            path.unlink(missing_ok=True)
    subprocess.run(["systemctl", "daemon-reload"], check=True, capture_output=True)
    systemctl("reset-failed")
    if manifest["enabled"]:
        systemctl("enable").check_returncode()
    if manifest["active"]:
        systemctl("start").check_returncode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("snapshot", "restore"))
    parser.add_argument("--snapshot", type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.exit(1, "Application snapshots require administrator sudo.\n")
    if args.mode == "snapshot":
        print(json.dumps({"snapshot": str(snapshot())}))
    else:
        if args.snapshot is None:
            parser.error("restore requires an explicit root-only snapshot")
        restore(args.snapshot)
        print(json.dumps({"result": "restored"}))


if __name__ == "__main__":
    main()
