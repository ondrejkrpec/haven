"""Render a candidate bucket allowlist and a Project-scoped IAM plan, offline."""

import argparse
import json
import os
from pathlib import Path
import re
import sys
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
FIELDS = {"schema_version", "project_id", "bucket", "application_id", "owner_user_id", "region", "prefix"}
PERMISSION_SETS = ["ObjectStorageBucketsRead", "ObjectStorageObjectsRead",
                   "ObjectStorageObjectsWrite", "ObjectStorageObjectsDelete"]


def validate(settings, require_owner=True):
    if (not isinstance(settings, dict) or set(settings) != FIELDS
            or type(settings["schema_version"]) is not int or settings["schema_version"] != 1
            or settings["region"] != "fr-par"):
        raise ValueError("invalid_settings")
    fields = ("project_id", "application_id")
    if require_owner or settings["owner_user_id"] is not None:
        fields += ("owner_user_id",)
    for field in fields:
        value = settings[field]
        if not isinstance(value, str) or str(UUID(value)) != value or UUID(value).int == 0:
            raise ValueError("invalid_identity")
    if settings["owner_user_id"] == settings["application_id"]:
        raise ValueError("distinct_principals_required")
    if (not isinstance(settings["bucket"], str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", settings["bucket"])
            or not isinstance(settings["prefix"], str)
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", settings["prefix"])):
        raise ValueError("invalid_resource")


def iam_plan(settings):
    validate(settings, require_owner=False)
    # This is a UI checklist, deliberately not an IAM API request body.
    return {"schema_version": 1, "kind": "scaleway-project-iam-plan",
            "name": "haven-backup-runtime", "application_id": settings["application_id"],
            "project_id": settings["project_id"], "permission_sets": PERMISSION_SETS.copy()}


def bucket_policy(settings):
    validate(settings)
    bucket, prefix = settings["bucket"], settings["prefix"]
    app = "application_id:" + settings["application_id"]
    owner = "user_id:" + settings["owner_user_id"]

    def allow(sid, principal, actions, resources, *, listing=False):
        condition = {"Bool": {"aws:SecureTransport": "true"}}
        if listing:
            condition["StringLike"] = {"s3:prefix": prefix + "/*"}
        return {"Sid": sid, "Effect": "Allow", "Principal": {"SCW": principal},
                "Action": actions, "Resource": resources, "Condition": condition}

    return {"Version": "2023-04-17", "Id": "HavenBackupRuntime", "Statement": [
        allow("OwnerAdministration", owner, ["s3:*"], [bucket, bucket + "/*"]),
        allow("RuntimeBucketMetadata", app, ["s3:GetBucketLocation", "s3:GetBucketVersioning"], [bucket]),
        allow("RuntimePrefixListing", app, ["s3:ListBucket", "s3:ListBucketMultipartUploads"],
              [bucket], listing=True),
        allow("RuntimeRepositoryObjects", app, ["s3:GetObject", "s3:PutObject",
              "s3:ListMultipartUploadParts", "s3:AbortMultipartUpload"], [bucket + "/" + prefix + "/*"]),
        allow("RuntimeLockCleanup", app, ["s3:DeleteObject"], [bucket + "/" + prefix + "/locks/*"]),
    ]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("iam", "bucket"))
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        source, output = args.settings, args.output
        if (source.is_symlink() or not source.is_file() or source.stat().st_size > 16384
                or not output.is_absolute() or ".." in output.parts
                or output.parent.resolve(strict=True) != output.parent
                or output.is_relative_to(ROOT)):
            raise ValueError("explicit_private_paths_required")
        settings = json.loads(source.read_text(encoding="utf-8"))
        value = (iam_plan if args.kind == "iam" else bucket_policy)(settings)
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(value, indent=2) + "\n")
        except BaseException:
            output.unlink(missing_ok=True)
            raise
    except (OSError, ValueError, TypeError):
        print("Policy rendering failed. Check metadata, owner identity and new private output path.", file=sys.stderr)
        return 1
    print("Candidate policy written privately. No cloud request was sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
