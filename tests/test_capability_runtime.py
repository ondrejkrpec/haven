"""Guarded startup and exact-source preparation refuse drift before execution."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from scripts import capability_runtime, prepare_capabilities, service_runtime
from scripts.model_contract import service_config
from scripts.model_probe import ProbeError
from scripts.validate_config import ROOT, load_config


class CapabilityRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.home = self.root / "home"
        self.plugin = self.home / "plugins/haven-capabilities"
        self.plugin.mkdir(parents=True)
        for name in capability_runtime.PLUGIN_FILES:
            shutil.copyfile(ROOT / "plugins/haven-capabilities" / name, self.plugin / name)
        self.config = load_config(ROOT / "examples/instance.yaml")
        self.config["model"]["name"] = "gpt-synthetic"
        self.config["telegram"] = {"enabled": True, "owner_id": "123456789"}

    def test_launcher_selects_guarded_process_and_keeps_standard_chat_opt_in(self):
        native = service_config(self.config, capabilities=True)
        with patch.object(service_runtime, "check", return_value=native), \
                patch.object(service_runtime, "check_worker_bus"), patch.object(service_runtime.os, "chdir"), \
                patch("scripts.telegram_runtime.environment", side_effect=lambda home, env: env), \
                patch.object(service_runtime.os, "execve", side_effect=SystemExit) as execute:
            with self.assertRaises(SystemExit):
                service_runtime.main(["run", "--source", str(self.source), "--home", str(self.home)])
        command, environment = execute.call_args.args[1:]
        from scripts.capability_gateway import BOOTSTRAP
        self.assertEqual(command[1:3], ["-c", BOOTSTRAP])
        self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")

    def test_runtime_rejects_altered_plugin_or_unreviewed_extra_plugin(self):
        pins = json.loads((ROOT / "config/capability-runtime.json").read_text())
        pins["source_sha256"] = {"fixture.py": hashlib.sha256(b"synthetic").hexdigest()}
        (self.source / "fixture.py").write_bytes(b"synthetic")
        original = capability_runtime.trusted_bytes

        def contents(path, **kwargs):
            if path == ROOT / "config/capability-runtime.json":
                return json.dumps(pins).encode()
            return original(path, **kwargs)

        with patch.object(capability_runtime, "trusted_bytes", side_effect=contents):
            capability_runtime.check_runtime(self.source, self.home)
            (self.plugin / "__init__.py").write_text("unreviewed plugin")
            with self.assertRaisesRegex(ProbeError, "plugin_mismatch"):
                capability_runtime.check_runtime(self.source, self.home)
            shutil.copyfile(ROOT / "plugins/haven-capabilities/__init__.py", self.plugin / "__init__.py")
            (self.plugin.parent / "other").mkdir()
            with self.assertRaisesRegex(ProbeError, "unexpected_service_plugin"):
                capability_runtime.check_runtime(self.source, self.home)

    def test_native_patch_refuses_dirty_source_before_invoking_git(self):
        patchfile = self.root / "policy.patch"
        patchfile.write_text("synthetic patch")
        pins = {"patch_sha256": hashlib.sha256(patchfile.read_bytes()).hexdigest(),
                "source_sha256": {"fixture.py": "patched", "agent/deterministic_policy.py": "guard"},
                "base_sha256": {"fixture.py": "base"}}
        (self.source / "fixture.py").write_text("unexpected source")
        with patch.object(prepare_capabilities, "check_candidate"), \
                patch.object(prepare_capabilities, "trusted_bytes", side_effect=[json.dumps(pins).encode(), patchfile.read_bytes()]), \
                patch.object(prepare_capabilities.subprocess, "run") as execute:
            with self.assertRaisesRegex(ValueError, "base_mismatch"):
                prepare_capabilities.prepare(self.source, patchfile, apply=True)
        execute.assert_not_called()

    def test_public_patch_and_base_receipt_have_the_same_scope(self):
        pins = json.loads((ROOT / "config/capability-runtime.json").read_text())
        data = (ROOT / "research/h1.6.1/deterministic-policy.patch").read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), pins["patch_sha256"])
        expected = deepcopy(pins["source_sha256"])
        expected.pop("agent/deterministic_policy.py")
        self.assertEqual(set(expected), set(pins["base_sha256"]))
