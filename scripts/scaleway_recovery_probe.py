"""Restore overwritten encrypted repository objects using a separate owner key.

Only a new disposable prefix with synthetic data is written. Production objects,
IAM, policy, versioning and lifecycle settings are never changed.
"""

from contextlib import closing
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import tempfile
from uuid import uuid4

from scripts.backup_runtime import command, save
from scripts.private_enrollment import PrivateArgumentParser
from scripts.scaleway_backup_policy import ROOT, validate
from scripts.scaleway_preflight import clients, denied, private_keys, require


def probe(settings, operator, runtime_keys, receipt):
    validate(settings)
    require(operator != runtime_keys)
    settings = dict(settings, prefix="recovery-" + uuid4().hex[:16])
    journal = {"schema_version": 1, "result": "running", "scope": "disposable synthetic version recovery",
               "bucket": settings["bucket"], "prefix": settings["prefix"], "versions": [], "stage": "preflight"}
    require(not receipt.exists() and not receipt.is_symlink())
    save(receipt, journal)
    owner, _ = clients(settings, operator, True, request_limit=128)
    limited, _ = clients(settings, runtime_keys, False)
    bucket, prefix = settings["bucket"], settings["prefix"] + "/"
    require(owner.get_bucket_versioning(Bucket=bucket).get("Status") == "Enabled")
    require(not owner.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1).get("Contents"))
    try:
        limited.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
    except Exception as error:
        require(denied(error))
        journal["runtime_outside_prefix_list_denied"] = True
    else:
        raise ValueError("runtime_boundary_failed")
    pins = json.loads((ROOT / "research/h1.9/pins.json").read_text())
    binary = ROOT / "build/h1.9/restic"
    require(hashlib.sha256(binary.read_bytes()).hexdigest() == pins["binary_sha256"])

    def put(key, value):
        journal.update(stage="write", intended_key=key)
        save(receipt, journal)
        response = owner.put_object(Bucket=bucket, Key=key, Body=value, StorageClass="STANDARD")
        version = response.get("VersionId")
        require(isinstance(version, str) and version != "null")
        journal["versions"].append({"key": key, "version": version})
        save(receipt, journal)
        return version

    def get(key, version=None):
        arguments = {"Bucket": bucket, "Key": key}
        if version is not None:
            arguments["VersionId"] = version
        response = owner.get_object(**arguments)
        with closing(response["Body"]) as body:
            value = body.read(4 * 1024 * 1024 + 1)
            require(len(value) <= 4 * 1024 * 1024)
            return value

    with tempfile.TemporaryDirectory(prefix="haven-version-recovery-") as temporary:
        root = Path(temporary)
        password = root / "password"
        password.write_text(secrets.token_hex(32) + "\n")
        password.chmod(0o600)
        local = root / "repository"

        def restic(repository, *arguments):
            return command([str(binary), "--no-cache", "--repo", str(repository),
                            "--password-file", str(password), *arguments], cwd=root, timeout=60)

        payload = root / "payload"
        payload.mkdir(mode=0o700)
        (payload / "config.json").write_text('{"synthetic":true}\n')
        with closing(sqlite3.connect(payload / "state.db")) as db:
            db.execute("CREATE TABLE events(value INTEGER)")
            db.execute("INSERT INTO events VALUES (42)")
            db.commit()
        expected = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in payload.iterdir()}
        restic(local, "init", "--repository-version", "2")
        result = restic(local, "backup", "--json", "--host", "haven-version-fixture", "payload")
        summaries = [row for row in map(json.loads, result.splitlines()) if row.get("message_type") == "summary"]
        require(len(summaries) == 1)
        snapshot = summaries[0]["snapshot_id"]
        objects = {}
        for path in sorted(local.rglob("*")):
            if path.is_file():
                key = prefix + path.relative_to(local).as_posix()
                objects[key] = put(key, path.read_bytes())
        require(1 <= len(objects) <= 12)
        pack = next(key for key in objects if key.startswith(prefix + "data/"))
        try:
            response = limited.get_object(Bucket=bucket, Key=pack)
        except Exception as error:
            require(denied(error))
            journal["runtime_outside_prefix_read_denied"] = True
        else:
            response["Body"].close()
            raise ValueError("runtime_boundary_failed")
        put(pack, b"HAVEN_SYNTHETIC_CORRUPT_PACK")
        journal.update(stage="overwritten", snapshot_id=snapshot)
        save(receipt, journal)
        shutil.rmtree(payload)
        shutil.rmtree(local)

        def download(destination, *, previous=False):
            destination.mkdir(mode=0o700)
            for key, version in objects.items():
                path = destination / key.removeprefix(prefix)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(get(key, version if previous else None))

        broken = root / "broken"
        download(broken)
        try:
            restic(broken, "check", "--read-data")
        except Exception:
            journal["overwritten_repository_corruption_detected"] = True
        else:
            raise ValueError("corruption_not_detected")
        previous = root / "previous"
        download(previous, previous=True)
        restic(previous, "check", "--read-data")
        restic(previous, "restore", snapshot, "--target", str(root / "restored-previous"), "--verify")
        require({p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in (root / "restored-previous/payload").iterdir()} == expected)
        # Repair the current disposable object by creating a new latest version.
        put(pack, get(pack, objects[pack]))
        repaired = root / "repaired"
        download(repaired)
        restic(repaired, "check", "--read-data")
        restic(repaired, "restore", snapshot, "--target", str(root / "restored-current"), "--verify")
        require({p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in (root / "restored-current/payload").iterdir()} == expected)
        journal.update(stage="verified", previous_versions_restore_verified=True, latest_object_repair_verified=True,
                       source_removed_before_recovery=True, production_data_used=False, operator_key_on_vps=False)
        save(receipt, journal)
    # Permanently delete only exact synthetic versions recorded by this run.
    # An interrupted/uncertain run is not retried with a fresh receipt.
    journal["stage"] = "cleanup"
    save(receipt, journal)
    for row in reversed(journal["versions"]):
        owner.delete_object(Bucket=bucket, Key=row["key"], VersionId=row["version"])
    require(not owner.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1).get("Contents"))
    response = owner.list_object_versions(Bucket=bucket, Prefix=prefix, MaxKeys=1)
    require(not response.get("Versions") and not response.get("DeleteMarkers") and not response.get("IsTruncated"))
    journal.update(result="passed", stage="complete", exact_synthetic_versions_removed=True)
    save(receipt, journal)
    return journal


def main(argv=None):
    parser = PrivateArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    for name in ("settings", "operator-credentials", "runtime-credentials", "receipt"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.live:
        print("Explicit --live is required; no credentials read or request sent.")
        return 2
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    try:
        journal = probe(json.loads(args.settings.read_text()), private_keys(args.operator_credentials),
                        private_keys(args.runtime_credentials), args.receipt)
    except BaseException:
        print('{"result":"failed","code":"version_recovery_probe_failed","reconcile_private_receipt":true}')
        return 1
    print(json.dumps({"result": journal["result"], "previous_versions_restore_verified": True,
                      "latest_object_repair_verified": True, "runtime_outside_prefix_denied": True,
                      "synthetic_versions_removed": True, "production_data_used": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
