"""Remote-probe boundaries without network or production credentials."""

from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from scripts import restic_remote_probe as probe
from scripts.enroll_restic import collect
from scripts.scaleway_preflight import Journal

SETTINGS = json.loads((Path(__file__).resolve().parents[1] / "examples/scaleway-backup.json").read_text())


class RemoteResticTests(unittest.TestCase):
    def test_password_reader_rejects_permissions_links_and_invalid_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "credentials"
            collect(directory, prompt=lambda _: "synthetic-password-" * 2)
            self.assertEqual(probe.password_value(directory), b"synthetic-password-synthetic-password-\n")
            (directory / "password").chmod(0o644)
            with self.assertRaises(Exception):
                probe.password_value(directory)
            (directory / "password").chmod(0o600)
            os.link(directory / "password", root / "hardlink")
            with self.assertRaises(Exception):
                probe.password_value(directory)
            (root / "hardlink").unlink()
            (root / "alias").symlink_to(directory, target_is_directory=True)
            with self.assertRaises(Exception):
                probe.password_value(root / "alias")
            (directory / "password").write_bytes(b"x" * 32 + b"\x00\n")
            with self.assertRaises(Exception):
                probe.password_value(directory)

    def test_unknown_objects_existing_config_and_uploads_refuse_initialization(self):
        class Client:
            rows, uploads, truncated = [], [], False

            def list_objects_v2(self, **kwargs):
                return {"Contents": self.rows, "IsTruncated": self.truncated}

            def list_multipart_uploads(self, **kwargs):
                return {"Uploads": self.uploads}

            def get_bucket_versioning(self, **kwargs):
                return {"Status": "Enabled"}

        preflight = {"result": "passed", "synthetic_writes": True, "bucket": SETTINGS["bucket"],
                     "prefix": SETTINGS["prefix"], "run_id": "a" * 32,
                     "checks": {"lock_cleanup_verified": True}, "multipart_aborted": True}
        client = Client()
        probe.initial_prefix(client, SETTINGS, preflight)
        for key in ("haven/config", "haven/data/existing", "haven/preflight/unknown/payload"):
            client.rows = [{"Key": key}]
            with self.assertRaises(Exception):
                probe.initial_prefix(client, SETTINGS, preflight)
        client.rows = [{"Key": "haven/preflight/" + "a" * 32 + "/payload"}]
        probe.initial_prefix(client, SETTINGS, preflight)
        client.truncated = True
        with self.assertRaises(Exception):
            probe.initial_prefix(client, SETTINGS, preflight)
        client.truncated = False
        client.uploads = [{"UploadId": "uncertain"}]
        with self.assertRaises(Exception):
            probe.initial_prefix(client, SETTINGS, preflight)

    def test_subprocess_environment_and_exit_three_failure_are_sanitized(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = Journal(root / "receipt.json", SETTINGS, True)
            restic = probe.Restic(Path("/synthetic/restic"), SETTINGS,
                                  ("synthetic-access", "synthetic-secret"), root, journal)
            with patch.dict(os.environ, {"HTTPS_PROXY": "private-proxy", "RESTIC_PASSWORD": "private-password"}), \
                    patch.object(probe.subprocess, "run", return_value=SimpleNamespace(
                        returncode=3, stdout="PRIVATE_OUTPUT", stderr="PRIVATE_DIAGNOSTIC")) as run:
                with self.assertRaises(Exception):
                    restic.run("backup", "backup", "payload")
            self.assertNotIn("HTTPS_PROXY", run.call_args.kwargs["env"])
            self.assertNotIn("RESTIC_PASSWORD", run.call_args.kwargs["env"])
            self.assertNotIn("synthetic-secret", " ".join(run.call_args.args[0]))
            self.assertNotIn("PRIVATE", journal.path.read_text())
            self.assertEqual(journal.data["commands"], [{"stage": "backup", "exit_code": 3}])

    def test_restore_checks_exact_snapshot_manifest_and_committed_wal_after_source_deletion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = Journal(root / "receipt.json", SETTINGS, True)

            class Double:
                def __init__(self):
                    self.root, self.calls = root, []

                def run(self, stage, *args, **kwargs):
                    self.calls.append(stage)
                    if stage == "backup":
                        shutil.copytree(root / "payload", root / "remote-double")
                        return json.dumps({"message_type": "summary", "snapshot_id": "a" * 64})
                    if stage == "snapshot_identity":
                        return json.dumps([{"id": "a" * 64, "hostname": "haven-synthetic",
                                            "tags": ["h1.9-remote-probe"]}])
                    if stage == "restore":
                        assert not (root / "payload").exists() and not (root / "source").exists()
                        assert args[1] == "a" * 64
                        shutil.copytree(root / "remote-double", root / "restored/payload")
                    return ""

            restic = Double()
            probe.synthetic_restore(restic, journal)
            self.assertTrue(journal.data["checks"]["sqlite_committed_wal_restored"])
            self.assertEqual(restic.calls[-1], "wrong_password")

    def test_no_live_flag_never_reads_credentials(self):
        args = ["--" + name for name in ("settings", "credentials", "password-directory", "preflight", "receipt")]
        with patch.object(probe, "password_value") as read, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(probe.main([value for arg in args for value in (arg, "/nonexistent")]), 2)
            read.assert_not_called()
