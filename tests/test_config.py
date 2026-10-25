"""Behavioral checks for configuration layering, rejection and safe diagnostics."""

from copy import deepcopy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from scripts.validate_config import ConfigError, ROOT, load_config


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="haven-config-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = yaml.safe_load((ROOT / "examples/instance.yaml").read_text())
        self.serial = 0

    def write(self, value):
        self.serial += 1
        path = self.root / f"layer-{self.serial}.yaml"
        path.write_text(value if isinstance(value, str) else yaml.safe_dump(value), encoding="utf-8")
        return path

    def config(self, value=None, overrides=()):
        return load_config(self.write(self.base if value is None else value), overrides)

    def test_public_example_and_private_template(self):
        example = load_config(ROOT / "examples/instance.yaml")
        template = load_config(ROOT / "templates/private/config/instance.yaml.example")
        self.assertEqual(example, template)
        self.assertEqual(example["instance"]["timezone"], "Etc/UTC")
        self.assertEqual(example["host"]["service_user"], "haven")
        self.assertEqual(example["whatsapp"], {"enabled": False, "allowed_senders": []})

    def test_nested_overrides_preserve_defaults_and_last_value_wins(self):
        first = self.write({"schema_version": 1, "instance": {"timezone": "Europe/Prague"}})
        second = self.write({"schema_version": 1, "instance": {"name": "second"}})
        actual = self.config(overrides=[first, second])
        self.assertEqual(actual["instance"], {"name": "second", "timezone": "Europe/Prague"})
        self.assertEqual(actual["model"], self.base["model"])

    def test_lists_replace_instead_of_extending_access(self):
        sender_a, sender_b = "+" + "9" * 10, "+" + "8" * 10
        base = deepcopy(self.base)
        base["whatsapp"] = {"enabled": True, "allowed_senders": [sender_a]}
        override = self.write({"schema_version": 1, "whatsapp": {"allowed_senders": [sender_b]}})
        self.assertEqual(self.config(base, [override])["whatsapp"]["allowed_senders"], [sender_b])
        clear = self.write({"schema_version": 1, "whatsapp": {"enabled": False, "allowed_senders": []}})
        self.assertEqual(self.config(base, [clear])["whatsapp"]["allowed_senders"], [])

    def test_enabled_whatsapp_requires_allowlist_after_all_merges(self):
        enabled = self.write({"schema_version": 1, "whatsapp": {"enabled": True}})
        with self.assertRaisesRegex(ConfigError, "allowed_senders.*at least 1"):
            self.config(overrides=[enabled])
        allowlist = self.write({"schema_version": 1, "whatsapp": {"allowed_senders": ["+" + "9" * 10]}})
        self.assertTrue(self.config(overrides=[enabled, allowlist])["whatsapp"]["enabled"])

    def test_all_layers_require_supported_version(self):
        for version in [None, 0, 2, True, "1"]:
            with self.subTest(version=version):
                value = deepcopy(self.base)
                if version is None:
                    value.pop("schema_version")
                else:
                    value["schema_version"] = version
                with self.assertRaises(ConfigError):
                    self.config(value)
        for override in [{"instance": {"name": "second"}}, {"schema_version": 2}]:
            with self.assertRaises(ConfigError):
                self.config(overrides=[self.write(override)])

    def test_missing_required_input(self):
        for group, field in [("instance", "name"), ("host", "address"), ("model", "provider"), ("model", "name")]:
            with self.subTest(group=group, field=field):
                value = deepcopy(self.base)
                value[group].pop(field)
                with self.assertRaisesRegex(ConfigError, "missing required"):
                    self.config(value)

    def test_unknown_keys_at_every_level(self):
        for group in [None, "instance", "host", "model", "whatsapp"]:
            with self.subTest(group=group):
                value = deepcopy(self.base)
                target = value if group is None else value.setdefault(group, {})
                target["SYNTHETIC-SECRET-AS-KEY"] = "SYNTHETIC-SECRET-AS-VALUE"
                with self.assertRaises(ConfigError) as error:
                    self.config(value)
                self.assertIn("unknown field", str(error.exception))
                self.assertNotIn("SYNTHETIC-SECRET", str(error.exception))

    def test_bad_layer_cannot_be_hidden_by_later_override(self):
        bad = self.write({"schema_version": 1, "instance": {"timezone": 42}})
        fixed = self.write({"schema_version": 1, "instance": {"timezone": "Etc/UTC"}})
        with self.assertRaisesRegex(ConfigError, "override 1"):
            self.config(overrides=[bad, fixed])

    def test_invalid_values(self):
        cases = [
            ("instance", "name", "bad name"), ("instance", "timezone", "Missing/Zone"),
            ("instance", "name", "example\n"),
            ("instance", "timezone", "/etc/passwd"), ("instance", "name", None),
            ("host", "address", "https://vps.example.invalid"), ("host", "address", "user@host"),
            ("host", "address", "999.999.999.999"), ("host", "service_user", "root"),
            ("host", "service_user", "haven\n"),
            ("model", "name", ""), ("model", "name", "has whitespace"),
            ("model", "name", "example-model\n"), ("model", "provider", "openai-codex\n"),
            ("model", "provider", False), ("model", "credential_ref", "SYNTHETIC-RAW-SECRET"),
            ("model", "credential_ref", "file:relative/path"),
            ("model", "credential_ref", "file:/run/../secret"),
            ("whatsapp", "enabled", "true"), ("whatsapp", "allowed_senders", None),
            ("whatsapp", "allowed_senders", [999]),
            ("whatsapp", "allowed_senders", ["*"]),
            ("whatsapp", "allowed_senders", ["+" + "9" * 10 + "\n"]),
            ("whatsapp", "allowed_senders", ["+" + "9" * 10] * 2),
        ]
        for group, field, item in cases:
            with self.subTest(group=group, field=field, item=item):
                value = deepcopy(self.base)
                value.setdefault(group, {})[field] = item
                with self.assertRaises(ConfigError):
                    self.config(value)

    def test_host_accepts_dns_and_ip_without_network_lookup(self):
        for address in ["vps.example.invalid", "192.0.2.1", "2001:db8::1"]:
            with self.subTest(address=address):
                value = deepcopy(self.base)
                value["host"]["address"] = address
                self.assertEqual(self.config(value)["host"]["address"], address)

    def test_secret_references_do_not_read_files_or_environment_values(self):
        secret = self.root / "secret"
        secret.write_text("SYNTHETIC-SECRET-FILE-CONTENT")
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path == secret:
                self.fail("validator attempted to read a secret file")
            return original_open(path, *args, **kwargs)

        for reference in [f"file:{secret}", "env:HAVEN_TEST_SECRET"]:
            with self.subTest(reference=reference):
                value = deepcopy(self.base)
                value["model"]["credential_ref"] = reference
                with patch.object(Path, "open", guarded_open), patch.dict(os.environ, {"HAVEN_TEST_SECRET": "SYNTHETIC-ENV-VALUE"}):
                    self.assertEqual(self.config(value)["model"]["credential_ref"], reference)

    def test_yaml_ambiguity_and_unsafe_constructs_are_rejected(self):
        for text in [
            "schema_version: 1\nschema_version: 1\n",
            "schema_version: 1\nmodel: {name: first, name: second}\n",
            "schema_version: 1\n3: SYNTHETIC-SECRET\n",
            "schema_version: 1\nmodel: &x {name: example}\n",
            "schema_version: 1\nmodel: *missing\n",
            "schema_version: 1\nmodel: {<<: {name: example}}\n",
            "schema_version: !!int 1\n",
            "schema_version: 1\nmodel: !!python/object:os.system {}\n",
            "schema_version: 1\n---\nschema_version: 1\n",
            "null\n", "[]\n", "", "schema_version: [\nSYNTHETIC-SECRET",
        ]:
            with self.subTest(text=text), self.assertRaises(ConfigError) as error:
                self.config(text)
            self.assertNotIn("SYNTHETIC-SECRET", str(error.exception))

    def test_only_true_and_false_are_booleans(self):
        for spelling in ["yes", "no", "on", "off", "True", "False"]:
            override = self.write(f"schema_version: 1\nwhatsapp:\n  enabled: {spelling}\n")
            with self.subTest(spelling=spelling), self.assertRaisesRegex(ConfigError, "must be boolean"):
                self.config(overrides=[override])

    def test_resource_bounds_and_utf8_errors_are_controlled(self):
        for text in ["#" * 65537, "[" * 34 + "]" * 34,
                     "[" + ",".join("0" for _ in range(10001)) + "]",
                     "schema_version: " + "9" * 5000]:
            with self.subTest(length=len(text)), self.assertRaises(ConfigError):
                self.config(text)
        path = self.write("placeholder")
        path.write_bytes(b"\xffSYNTHETIC-SECRET")
        with self.assertRaisesRegex(ConfigError, "UTF-8"):
            load_config(path)

    def test_cli_exit_codes_and_no_private_values_on_either_stream(self):
        path = self.write(self.base)
        good = subprocess.run([sys.executable, str(ROOT / "scripts/validate_config.py"), str(path)],
                              cwd=self.root, capture_output=True, text=True)
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertEqual(good.stdout, "Configuration valid (schema_version: 1).\n")
        self.assertEqual(good.stderr, "")
        path.write_text("schema_version: 1\nmodel:\n  credential_ref: SYNTHETIC-SECRET\n")
        bad = subprocess.run([sys.executable, str(ROOT / "scripts/validate_config.py"), str(path)],
                             cwd=self.root, capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)
        self.assertEqual(bad.stdout, "")
        self.assertIn("$.model.credential_ref", bad.stderr)
        self.assertNotIn("SYNTHETIC-SECRET", bad.stderr)
        self.assertNotIn(str(self.root), bad.stderr)
        self.assertNotIn("Traceback", bad.stderr)

    def test_missing_file_is_reported_without_traceback_or_path(self):
        with self.assertRaisesRegex(ConfigError, "config: cannot read") as error:
            load_config(self.root / "SYNTHETIC-SECRET-FILENAME")
        self.assertNotIn("SYNTHETIC-SECRET", str(error.exception))


if __name__ == "__main__":
    unittest.main()
