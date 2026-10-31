"""Exercise candidate boundaries offline; this is not a provider policy evaluator."""

from contextlib import redirect_stderr, redirect_stdout
import copy
from fnmatch import fnmatchcase
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from scripts import enroll_scaleway, private_enrollment
from scripts.scaleway_backup_policy import bucket_policy, iam_plan, main

SETTINGS = json.loads((Path(__file__).resolve().parents[1] / "examples/scaleway-backup.json").read_text())
PAIR = ("synthetic_access_value", "synthetic_secret_value")


def candidate_allows(policy, principal, action, resource, prefix=None, tls=True):
    """Only match the generated Allow/Bool/StringLike subset for regression tests.

    No ambient IAM, ACL, missing-resource or service-side behavior is simulated.
    """
    for row in policy["Statement"]:
        if (row["Principal"]["SCW"] != principal or not tls
                or not any(fnmatchcase(action, item) for item in row["Action"])
                or not any(fnmatchcase(resource, item) for item in row["Resource"])):
            continue
        pattern = row["Condition"].get("StringLike", {}).get("s3:prefix")
        if pattern is None or (prefix is not None and fnmatchcase(prefix, pattern)):
            return True
    return False


class ScalewayBackupTests(unittest.TestCase):
    def test_candidate_runtime_matrix_and_owner_recovery(self):
        policy = bucket_policy(SETTINGS)
        app = "application_id:" + SETTINGS["application_id"]
        owner = "user_id:" + SETTINGS["owner_user_id"]
        bucket = SETTINGS["bucket"]
        for action in ("s3:GetObject", "s3:PutObject", "s3:ListMultipartUploadParts", "s3:AbortMultipartUpload"):
            self.assertTrue(candidate_allows(policy, app, action, bucket + "/haven/data/test"))
            self.assertFalse(candidate_allows(policy, app, action, bucket + "/outside/test"))
            self.assertFalse(candidate_allows(policy, app, action, "other-bucket/haven/data/test"))
        self.assertTrue(candidate_allows(policy, app, "s3:DeleteObject", bucket + "/haven/locks/test"))
        for suffix in ("data/test", "snapshots/test", "index/test", "keys/test", "config"):
            self.assertFalse(candidate_allows(policy, app, "s3:DeleteObject", bucket + "/haven/" + suffix))
        for action in ("s3:DeleteObjectVersion", "s3:GetObjectVersion", "s3:PutObjectRetention",
                       "s3:PutObjectAcl", "s3:PutBucketPolicy", "s3:PutBucketVersioning",
                       "s3:PutLifecycleConfiguration", "s3:DeleteBucket"):
            for resource in (bucket, bucket + "/haven/data/test", bucket + "/haven/locks/test"):
                self.assertFalse(candidate_allows(policy, app, action, resource), (action, resource))
        self.assertTrue(candidate_allows(policy, owner, "s3:GetObjectVersion", bucket + "/haven/config"))
        self.assertTrue(candidate_allows(policy, owner, "s3:PutBucketPolicy", bucket))
        for principal in (app, owner, "anonymous"):
            self.assertFalse(candidate_allows(policy, principal, "s3:GetObject", bucket + "/haven/config", tls=False))
        self.assertFalse(candidate_allows(policy, "anonymous", "s3:GetObject", bucket + "/haven/config"))

    def test_prefix_listing_does_not_grant_unfiltered_head_or_global_listing(self):
        policy = bucket_policy(SETTINGS)
        app = "application_id:" + SETTINGS["application_id"]
        for prefix in ("haven/", "haven/data/", "haven/locks/"):
            self.assertTrue(candidate_allows(policy, app, "s3:ListBucket", SETTINGS["bucket"], prefix))
        for prefix in (None, "", "other/", "haven-other/"):
            self.assertFalse(candidate_allows(policy, app, "s3:ListBucket", SETTINGS["bucket"], prefix))
        plan = iam_plan(SETTINGS)
        self.assertEqual(plan["project_id"], SETTINGS["project_id"])
        self.assertNotIn("ObjectStorageFullAccess", plan["permission_sets"])
        self.assertFalse(any("BucketsWrite" in item or "Policy" in item for item in plan["permission_sets"]))

    def test_owner_missing_blocks_bucket_rendering_and_unsafe_metadata_is_refused(self):
        draft = {**SETTINGS, "owner_user_id": None}
        self.assertEqual(iam_plan(draft)["application_id"], SETTINGS["application_id"])
        with self.assertRaises(ValueError):
            bucket_policy(draft)
        for updates in ({"prefix": "*"}, {"prefix": "../other"}, {"bucket": "*"},
                        {"region": "unreviewed-region"}, {"schema_version": True},
                        {"application_id": "*"}, {"owner_user_id": SETTINGS["application_id"]},
                        {"secret_key": "SYNTHETIC_PRIVATE_VALUE"}):
            with self.subTest(fields=tuple(updates)), self.assertRaises(ValueError):
                bucket_policy({**copy.deepcopy(SETTINGS), **updates})

    def test_renderer_uses_fresh_private_output_and_redacts_invalid_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, target = root / "settings.json", root / "policy.json"
            source.write_text(json.dumps(SETTINGS))
            output = io.StringIO()
            args = ["bucket", "--settings", str(source), "--output", str(target)]
            with redirect_stdout(output), redirect_stderr(output):
                self.assertEqual(main(args), 0)
                self.assertEqual(main(args), 1)
                source.write_text(json.dumps({**SETTINGS, "owner_user_id": "SYNTHETIC_PRIVATE_VALUE"}))
                self.assertEqual(main([*args[:-1], str(root / "failed.json")]), 1)
            self.assertNotIn("SYNTHETIC_PRIVATE_VALUE", output.getvalue())
            self.assertEqual(json.loads(target.read_text()), bucket_policy(SETTINGS))
            self.assertFalse((root / "failed.json").exists())

    def test_key_enrollment_private_storage_and_no_network_or_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "keys"
            values = iter(PAIR)
            with patch("socket.socket", side_effect=AssertionError("network forbidden")):
                enroll_scaleway.collect(target, prompt=lambda _: next(values))
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
            for filename, value in zip(("access-key", "secret-key"), PAIR, strict=True):
                path = target / filename
                self.assertEqual(path.read_text(), value + "\n")
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(ValueError):
                enroll_scaleway.collect(target, prompt=lambda _: self.fail("existing destination"))

    def test_key_invalid_inputs_and_shared_path_injection_leave_no_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "keys"
            for values in ((PAIR[0], PAIR[0]), (PAIR[0], "bad\nvalue"), ("short", PAIR[1])):
                iterator = iter(values)
                with self.assertRaises(ValueError):
                    enroll_scaleway.collect(target, prompt=lambda _: next(iterator))
                self.assertFalse(target.exists())
            for filename in ("../escape", "/absolute", ".git", "nested/file"):
                with self.assertRaises(ValueError):
                    private_enrollment.collect_files(target, lambda: {filename: PAIR[0]})
                self.assertFalse(target.exists())

    def test_key_cli_requires_tty_and_keeps_diagnostics_private(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            with patch.object(enroll_scaleway.sys.stdin, "isatty", return_value=False), \
                    patch.object(enroll_scaleway, "collect", side_effect=AssertionError("no input")):
                self.assertEqual(enroll_scaleway.main(["--directory", "/synthetic"]), 1)
            with self.assertRaises(SystemExit):
                enroll_scaleway.main(["--directory", "/synthetic", "--secret", PAIR[1]])
            with patch.object(enroll_scaleway.sys.stdin, "isatty", return_value=True), \
                    patch.object(enroll_scaleway.sys.stderr, "isatty", return_value=True), \
                    patch.object(enroll_scaleway, "collect", side_effect=OSError(PAIR[1])):
                self.assertEqual(enroll_scaleway.main(["--directory", "/synthetic"]), 1)
        self.assertNotIn(PAIR[1], output.getvalue())


if __name__ == "__main__":
    unittest.main()
