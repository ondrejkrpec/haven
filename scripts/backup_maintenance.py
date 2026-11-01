"""Separate-operator retention from the workstation, never a routine VPS timer.

The live VPS maintenance lease prevents simultaneous capture/upload. The accepted
snapshot must survive the explicit 7 daily / 4 weekly / 3 monthly retention plan.
"""

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
from uuid import uuid4

from scripts.backup_capture import require
from scripts.backup_runtime import IDENTIFIER, Repository, load, read_private, save
from scripts.private_enrollment import PrivateArgumentParser

ROOT = Path(__file__).resolve().parents[1]


def retention(repository, accepted, *, apply=False, journal=None, receipt=None):
    arguments = ["forget", "--json", "--host", repository.settings["hostname"], "--tag", "haven-production",
                 "--group-by", "host", "--keep-daily", "7", "--keep-weekly", "4", "--keep-monthly", "3"]

    def ids(groups, field):
        require(isinstance(groups, list), "invalid_retention_plan")
        require(all(group.get(field) is None or isinstance(group.get(field), list) for group in groups), "invalid_retention_plan")
        result = {row["id"] for group in groups for row in (group.get(field) or [])}
        require(all(IDENTIFIER.fullmatch(value) for value in result), "invalid_retention_plan")
        return result

    repository.identity()
    current = repository.snapshots(accepted["run_id"])
    require(len(current) == 1 and current[0]["id"] == accepted["snapshot_id"], "accepted_snapshot_not_found")
    repository.run("check", "--read-data")
    plan = json.loads(repository.run(*arguments, "--dry-run"))
    keep, remove = ids(plan, "keep"), ids(plan, "remove")
    require(accepted["snapshot_id"] in keep and accepted["snapshot_id"] not in remove, "accepted_snapshot_would_be_removed")
    journal.update(stage="retention_planned", keep=sorted(keep), remove=sorted(remove), apply=apply)
    save(receipt, journal)
    if apply:
        journal["stage"] = "forget_intended"
        save(receipt, journal)
        actual = json.loads(repository.run(*arguments))
        require(ids(actual, "remove") == remove and accepted["snapshot_id"] in ids(actual, "keep"), "retention_plan_changed")
        journal["stage"] = "prune_intended"
        save(receipt, journal)
        repository.run("prune")
        repository.run("check", "--read-data")
        require(repository.snapshots(accepted["run_id"])[0]["id"] == accepted["snapshot_id"], "accepted_snapshot_missing")
    journal.update(stage="complete", result="passed", accepted_snapshot_preserved=True)
    save(receipt, journal)


def main(argv=None):
    parser = PrivateArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--ssh-alias", required=True)
    parser.add_argument("--ssh-config", type=Path, default=Path.home() / ".ssh/config")
    for name in ("settings", "accepted", "operator-credentials", "password-directory", "receipt"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.live:
        print("Explicit --live is required; no request sent.")
        return 2
    os.umask(0o077)
    lease = "haven-maintenance-" + uuid4().hex
    acquired = False
    journal = {"schema_version": 1, "stage": "created", "result": "running", "lease": lease}
    ssh = ["ssh", "-F", str(args.ssh_config), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", args.ssh_alias]

    def remote(*arguments):
        subprocess.run([*ssh, shlex.join(["sudo", "-n", *arguments])], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=True)

    try:
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", args.ssh_alias), "invalid_ssh_alias")
        require(not args.receipt.exists() and not args.receipt.is_symlink(), "new_receipt_required")
        settings, accepted = load(args.settings), load(args.accepted)
        save(args.receipt, journal)
        remote("systemd-run", "--unit=" + lease, "--collect", "--property=Type=notify", "--property=TimeoutStartSec=10",
               "--setenv=PYTHONPATH=/usr/local/lib/haven-backup", "--setenv=PYTHONDONTWRITEBYTECODE=1",
               "/usr/bin/python3", "-m", "scripts.backup_runtime", "maintenance")
        acquired = True
        with tempfile.TemporaryDirectory(prefix="haven-retention-") as temporary:
            root = Path(temporary)
            for name, source in (("access-key", args.operator_credentials / "access-key"),
                                 ("secret-key", args.operator_credentials / "secret-key"),
                                 ("password", args.password_directory / "password")):
                with (root / name).open("xb") as output:
                    os.fchmod(output.fileno(), 0o600)
                    output.write(read_private(source, 258))
            repository = Repository(settings, root, root, binary=ROOT / "build/h1.9/restic")
            retention(repository, accepted, apply=args.apply, journal=journal, receipt=args.receipt)
    except Exception:
        print('{"result":"failed","code":"maintenance_failed","reconcile_private_receipt":true}')
        return 1
    finally:
        if acquired:
            try:
                remote("systemctl", "stop", lease + ".service")
                journal["maintenance_lease_released"] = True
                save(args.receipt, journal)
            except Exception:
                print('{"result":"failed","code":"maintenance_lease_requires_release"}')
                journal["maintenance_lease_released"] = False
    print(json.dumps({"result": "passed" if journal.get("maintenance_lease_released") else "failed",
                      "retention_applied": args.apply, "accepted_snapshot_preserved": True}))
    return 0 if journal.get("maintenance_lease_released") else 1


if __name__ == "__main__":
    raise SystemExit(main())
