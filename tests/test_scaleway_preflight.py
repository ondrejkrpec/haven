"""Exercise preflight boundaries with an in-memory S3 double, never a cloud account."""

from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from scripts.enroll_scaleway import collect
from scripts import scaleway_preflight as preflight

SETTINGS = json.loads((Path(__file__).resolve().parents[1] / "examples/scaleway-backup.json").read_text())


class ApiError(Exception):
    def __init__(self, status=403, code="AccessDenied"):
        self.response = {"ResponseMetadata": {"HTTPStatusCode": status}, "Error": {"Code": code}}
        super().__init__("SYNTHETIC_PRIVATE_ERROR")


class S3Double:
    def __init__(self, *, anonymous=False, unsafe_delete=False, fail_parts=False):
        self.calls, self.objects, self.uploads = [], {}, {}
        self.anonymous, self.unsafe_delete, self.fail_parts = anonymous, unsafe_delete, fail_parts

    def request(self, operation, **args):
        self.calls.append((operation, args))
        key = args.get("Key")
        if operation == "get_bucket_location":
            return {"LocationConstraint": "fr-par"}
        if operation == "get_bucket_versioning":
            return {"Status": "Enabled"}
        if operation == "list_objects_v2":
            prefix = args.get("Prefix", "")
            if not prefix.startswith("haven/"):
                raise ApiError()
            return {"Contents": [{"Key": x} for x in self.objects if x.startswith(prefix)][:1]}
        if operation == "put_object":
            self.objects.setdefault(key, []).append(args["Body"])
            return {"VersionId": str(len(self.objects[key]))}
        if operation == "get_object":
            if self.anonymous or "VersionId" in args:
                raise ApiError()
            if key not in self.objects:
                raise ApiError(404, "NoSuchKey")
            return {"Body": io.BytesIO(self.objects[key][-1])}
        if operation == "delete_object":
            if "/locks/" not in key and not self.unsafe_delete or "VersionId" in args:
                raise ApiError()
            self.objects.pop(key, None)
            return {}
        if operation == "create_multipart_upload":
            self.uploads["synthetic-upload"] = key
            return {"UploadId": "synthetic-upload"}
        if operation == "list_parts":
            if self.fail_parts:
                raise ApiError(500, "UnclassifiedPrivateValue")
            return {"Parts": []}
        if operation == "list_multipart_uploads":
            return {"Uploads": [{"UploadId": x} for x, key in self.uploads.items()
                                if key.startswith(args["Prefix"])]}
        if operation == "abort_multipart_upload":
            self.uploads.pop(args["UploadId"], None)
            return {}
        raise AssertionError("Unexpected operation")

    def __getattr__(self, name):
        return lambda **kwargs: self.request(name, **kwargs)


class ScalewayPreflightTests(unittest.TestCase):
    def test_read_only_never_writes_and_journal_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "journal.json"
            journal = preflight.Journal(path, SETTINGS, False)
            client = S3Double()
            self.assertTrue(preflight.Probe(client, S3Double(anonymous=True), SETTINGS, journal).run())
            self.assertTrue(all(name.startswith(("get_", "list_")) for name, _ in client.calls))
            with self.assertRaises(FileExistsError):
                preflight.Journal(path, SETTINGS, False)
            self.assertEqual(json.loads(path.read_text())["result"], "passed")

    def test_full_probe_protects_versions_and_removes_own_lock_and_upload(self):
        with tempfile.TemporaryDirectory() as temporary:
            journal = preflight.Journal(Path(temporary) / "journal.json", SETTINGS, True)
            client = S3Double()
            probe = preflight.Probe(client, S3Double(anonymous=True), SETTINGS, journal)
            self.assertTrue(probe.run(writes=True))
            self.assertEqual(client.objects, {probe.object: [preflight.V1, preflight.V2]})
            self.assertFalse(client.uploads)
            self.assertTrue(journal.data["checks"]["version_delete_denied"])
            self.assertTrue(journal.data["checks"]["lock_cleanup_verified"])
            self.assertLessEqual(journal.data["operation_count"], 32)

    def test_unexpected_delete_allow_fails_and_touches_only_synthetic_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            journal = preflight.Journal(Path(temporary) / "journal.json", SETTINGS, True)
            client = S3Double(unsafe_delete=True)
            probe = preflight.Probe(client, S3Double(anonymous=True), SETTINGS, journal)
            self.assertFalse(probe.run(writes=True))
            self.assertFalse(journal.data["checks"]["data_delete_denied"])
            self.assertEqual(journal.data["stage"], "data_delete_denied")
            for operation, args in client.calls:
                if operation in {"put_object", "delete_object"}:
                    self.assertIn(args["Key"], journal.data["synthetic_objects"])

    def test_nonempty_prefix_blocks_all_writes_and_failed_upload_is_cleaned(self):
        with tempfile.TemporaryDirectory() as temporary:
            client = S3Double()
            client.objects["haven/config"] = [b"SYNTHETIC_EXISTING_REPOSITORY"]
            journal = preflight.Journal(Path(temporary) / "existing.json", SETTINGS, True)
            self.assertFalse(preflight.Probe(client, S3Double(), SETTINGS, journal).run(writes=True))
            self.assertFalse(any(name == "put_object" for name, _ in client.calls))
            client = S3Double(fail_parts=True)
            journal = preflight.Journal(Path(temporary) / "failed.json", SETTINGS, True)
            self.assertFalse(preflight.Probe(client, S3Double(anonymous=True), SETTINGS, journal).run(writes=True))
            self.assertFalse(client.uploads)
            self.assertTrue(journal.data["failure_multipart_cleanup_sent"])
            self.assertNotIn("PRIVATE", json.dumps(journal.data))

    def test_key_reader_enforces_modes_links_and_bounded_regular_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            keys = root / "keys"
            values = ("synthetic-access", "synthetic-secret")
            iterator = iter(values)
            collect(keys, prompt=lambda _: next(iterator))
            self.assertEqual(preflight.private_keys(keys), list(values))
            (keys / "secret-key").chmod(0o644)
            with self.assertRaises(preflight.PreflightError):
                preflight.private_keys(keys)
            (keys / "secret-key").chmod(0o600)
            (root / "link").symlink_to(keys, target_is_directory=True)
            with self.assertRaises(OSError):
                preflight.private_keys(root / "link")

    def test_missing_live_flag_and_private_sdk_errors_are_not_disclosed(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            with patch.object(preflight, "private_keys", side_effect=AssertionError("no credential reads")):
                self.assertEqual(preflight.main(["--settings", "/none", "--credentials", "/none",
                                                 "--receipt", "/none"]), 2)
        self.assertNotIn("SYNTHETIC_PRIVATE_ERROR", preflight.diagnostic(ApiError()))
        self.assertNotIn("PRIVATE", preflight.diagnostic(ApiError(500, "PRIVATE")))

    def test_sdk_configuration_never_inherits_credentials_or_redirects_to_other_hosts(self):
        sdk, session_module, config_module = (ModuleType(x) for x in ("botocore", "botocore.session", "botocore.config"))
        sdk.__version__, sdk.UNSIGNED = preflight.SDK_VERSION, "UNSIGNED"
        created, hooks = [], []

        def create(*args, **kwargs):
            created.append(kwargs)
            return SimpleNamespace(meta=SimpleNamespace(events=SimpleNamespace(register=lambda event, hook: hooks.append(hook))))
        session_module.get_session = lambda: SimpleNamespace(set_config_variable=lambda *args: None, create_client=create)
        config_module.Config = lambda **kwargs: kwargs
        sdk.session = session_module
        with patch.dict("sys.modules", {"botocore": sdk, "botocore.session": session_module, "botocore.config": config_module}):
            preflight.clients(SETTINGS, ["synthetic-access", "synthetic-secret"], False)
        self.assertEqual(created[0]["aws_secret_access_key"], "synthetic-secret")
        self.assertEqual(created[0]["config"]["proxies"], {})
        self.assertEqual(created[0]["config"]["retries"]["total_max_attempts"], 1)
        self.assertTrue(created[0]["verify"])
        self.assertEqual(created[1]["config"]["signature_version"], "UNSIGNED")
        bucket = SETTINGS["bucket"]
        hooks[0](SimpleNamespace(url=f"https://s3.fr-par.scw.cloud/{bucket}", method="GET"))
        for url, method in ((f"http://s3.fr-par.scw.cloud/{bucket}", "GET"),
                            (f"https://other.test/{bucket}", "GET"),
                            (f"https://s3.fr-par.scw.cloud/{bucket}", "DELETE"),
                            (f"https://s3.fr-par.scw.cloud/{bucket}/haven/config", "PUT")):
            with self.assertRaises(preflight.PreflightError):
                hooks[0](SimpleNamespace(url=url, method=method))


if __name__ == "__main__":
    unittest.main()
