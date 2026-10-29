"""WhatsApp capability limits and credential-free operational checks."""

from contextlib import redirect_stdout
from copy import deepcopy
import io
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import whatsapp_runtime as runtime
from scripts.model_contract import ModelConfigError, check_service, native_config, service_config
from scripts.validate_config import load_config


class WhatsAppConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.input = load_config(Path(__file__).resolve().parents[1] / "examples/instance.yaml")
        self.input["model"]["name"] = "gpt-synthetic"
        self.input["whatsapp"] = {"enabled": True, "allowed_senders": ["+15555550123"]}
        self.config = service_config(self.input)

    def test_explicit_allowlist_is_translated_without_mutating_the_instance(self):
        self.assertEqual(check_service(self.config), "gpt-synthetic")
        extra = self.config["platforms"]["whatsapp"]["extra"]
        self.assertEqual(extra["allow_from"], ["15555550123"])
        self.assertEqual(extra["dm_policy"], "allowlist")
        self.assertEqual(extra["group_policy"], "disabled")
        self.assertFalse(self.config["stt"]["enabled"])
        self.assertTrue(self.config["gateway"]["strict"])
        self.assertFalse(self.config["gateway"]["trust_recent_files"])
        self.assertTrue(self.input["whatsapp"]["enabled"])
        with self.assertRaises(ModelConfigError):
            native_config(self.input)

    def test_guard_rejects_access_or_capability_expansion(self):
        changes = {"dm_policy": "open", "allow_from": ["*"], "group_policy": "open",
                   "allow_admin_from": ["15555550123"], "user_allowed_commands": ["terminal"],
                   "session_path": "/tmp/synthetic-session", "bridge_port": 3001,
                   "bridge_script": "/tmp/synthetic.js"}
        for key, value in changes.items():
            config = deepcopy(self.config)
            config["platforms"]["whatsapp"]["extra"][key] = value
            with self.subTest(key=key), self.assertRaises(ModelConfigError):
                check_service(config)
        for key, value in (("strict", False), ("trust_recent_files", True),
                           ("media_delivery_allow_dirs", ["/"])):
            config = deepcopy(self.config)
            config["gateway"][key] = value
            with self.subTest(key=key), self.assertRaises(ModelConfigError):
                check_service(config)
        for senders in ([], ["*"], ["+15555550123", "+15555550123"], [123], ["+15555550123\n"]):
            with self.subTest(senders=senders), self.assertRaises(ModelConfigError):
                service_config({**self.input, "whatsapp": {"enabled": True, "allowed_senders": senders}})

    def test_launcher_removes_environment_overrides_and_does_not_expand_access(self):
        with patch.dict(os.environ, {"WHATSAPP_ALLOWED_USERS": "*", "WHATSAPP_ENABLED": "true",
                                     "WHATSAPP_DEBUG": "true", "GATEWAY_ALLOW_ALL_USERS": "true",
                                     "NODE_OPTIONS": "--require=/tmp/synthetic.js",
                                     "HERMES_MEDIA_DELIVERY_STRICT": "0",
                                     "NOTIFY_SOCKET": "/synthetic-notify"}):
            env = runtime.environment()
        self.assertNotIn("WHATSAPP_ALLOWED_USERS", env)
        self.assertNotIn("WHATSAPP_ENABLED", env)
        self.assertNotIn("NODE_OPTIONS", env)
        self.assertNotIn("HERMES_MEDIA_DELIVERY_STRICT", env)
        self.assertEqual(env["WHATSAPP_MODE"], "bot")
        self.assertEqual(env["WHATSAPP_ALLOW_ALL_USERS"], "false")
        self.assertEqual(env["GATEWAY_ALLOW_ALL_USERS"], "false")
        self.assertEqual(env["WHATSAPP_DEBUG"], "false")
        self.assertEqual(env["NOTIFY_SOCKET"], "/synthetic-notify")

    def test_session_check_uses_metadata_and_rejects_shared_or_linked_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            session = home / "platforms/whatsapp/session"
            session.mkdir(parents=True)
            for path in (home, home / "platforms", session.parent, session):
                path.chmod(0o700)
            creds = session / "creds.json"
            creds.write_text("SYNTHETIC_SECRET_SENTINEL not even JSON")
            creds.chmod(0o600)
            with patch.object(runtime, "HOME", home), patch.object(runtime, "SESSION", session):
                runtime.check_session()
                creds.chmod(0o644)
                with self.assertRaises(runtime.ProbeError):
                    runtime.check_session()
                creds.chmod(0o600)
                link = session / "linked.json"
                link.symlink_to(creds)
                with self.assertRaises(runtime.ProbeError):
                    runtime.check_session()

    def test_pairing_is_refused_without_an_owner_terminal(self):
        with patch.object(runtime.sys.stdin, "isatty", return_value=False), \
                patch.object(runtime.os, "execve") as execute:
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(runtime.main(["pair"]), 78)
        execute.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()),
                         {"result": "failed", "code": "whatsapp_pairing_requires_owner_terminal"})

    def test_node_must_be_selected_by_pm_inside_the_optional_store(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            (source / "pm").mkdir(parents=True)
            (source / "pm/lock.json").write_text("synthetic frozen lock")
            pins = root / "pins.json"
            pins.write_text(json.dumps({"node_version": "synthetic", "npm_version": "synthetic",
                                        "pm_lock_sha256": hashlib.sha256(b"synthetic frozen lock").hexdigest()}))
            node = SimpleNamespace(version="synthetic", binary=root / "tools/node/bin/node")
            npm = SimpleNamespace(version="synthetic", binary=root / "tools/npm/bin/npm")
            pm = SimpleNamespace(installed_package=lambda name: {"node": node, "npm": npm}[name])
            with patch.object(runtime, "SOURCE", source), patch.object(runtime, "RUNTIME", root), \
                    patch.object(runtime, "PINS", pins), patch.dict("sys.modules", {"pm": pm}), \
                    patch.dict(os.environ, {"HERMES_RUNTIME_DIR": "/synthetic-core-store"}):
                self.assertEqual(runtime.selected_node(), node.binary)
                self.assertEqual(os.environ["HERMES_RUNTIME_DIR"], "/synthetic-core-store")
                node.binary = root / "unmanaged/node"
                with self.assertRaises(runtime.ProbeError):
                    runtime.selected_node()
                node.binary = root / "tools/node/bin/node"
                node.version = "different"
                with self.assertRaises(runtime.ProbeError):
                    runtime.selected_node()

    def test_operational_failure_never_prints_the_upstream_exception(self):
        for error in (ValueError("SYNTHETIC_SECRET_SENTINEL"), runtime.ProbeError("SYNTHETIC_SECRET_SENTINEL")):
            output = io.StringIO()
            with patch.object(runtime, "health", side_effect=error):
                with redirect_stdout(output):
                    self.assertEqual(runtime.main(["health"]), 78)
            self.assertNotIn("SYNTHETIC_SECRET_SENTINEL", output.getvalue())


if __name__ == "__main__":
    unittest.main()
