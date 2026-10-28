"""Runtime safety and privacy behavior with synthetic inputs and no provider access."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import types
import unittest
from unittest.mock import patch

import yaml

from scripts.model_contract import CODEX_BASE_URL, ModelConfigError, check_native, native_config
from scripts import model_probe, render_model
from scripts.validate_config import load_config


class ModelConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="haven-model-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / "instance.yaml"
        self.input.write_text("""schema_version: 1
instance:
  name: synthetic
  timezone: Europe/Prague
host:
  address: vps.example.invalid
model:
  provider: openai-codex
  name: gpt-synthetic
""")
        self.config = load_config(self.input)
        self.native = native_config(self.config)

    def test_render_uses_explicit_model_timezone_and_private_output(self):
        output = self.root / "config.yaml"
        render_model.render(self.input, output)
        actual = yaml.safe_load(output.read_text())
        self.assertEqual(check_native(actual), "gpt-synthetic")
        self.assertEqual(actual["timezone"], "Europe/Prague")
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertFalse(actual["auth"]["adopt_external_logins"])

    def test_existing_files_and_symlinks_are_not_overwritten(self):
        output = self.root / "existing.yaml"
        output.write_text("personal content")
        link = self.root / "linked.yaml"
        link.symlink_to(output)
        for target in (output, link):
            with self.subTest(target=target), self.assertRaises(FileExistsError):
                render_model.render(self.input, target)
        self.assertEqual(output.read_text(), "personal content")

    def test_unsupported_features_do_not_create_output(self):
        cases = []
        for field, value in (("provider", "unsupported"), ("name", "example-model"),
                             ("credential_ref", "env:SYNTHETIC_SECRET")):
            config = deepcopy(self.config)
            config["model"][field] = value
            cases.append(config)
        enabled = deepcopy(self.config)
        enabled["whatsapp"]["enabled"] = True
        cases.append(enabled)
        identity = deepcopy(self.config)
        identity["host"]["service_user"] = "other_service"
        cases.append(identity)
        for config in cases:
            with self.subTest(config=config), self.assertRaises(ModelConfigError):
                native_config(config)

    def test_policy_blocks_route_auth_and_tool_expansion(self):
        changes = (("model", "base_url", "https://other.example.invalid"),
                   ("model", "openai_runtime", "codex_app_server"),
                   ("model", "api_key", "SYNTHETIC_SECRET_SENTINEL"),
                   ("auth", "adopt_external_logins", True),
                   ("auth", "adopt_external_logins", None),
                   ("platform_toolsets", "cli", ["terminal"]))
        for section, key, value in changes:
            config = deepcopy(self.native)
            config[section][key] = value
            with self.subTest(section=section, key=key), self.assertRaises(ModelConfigError):
                check_native(config)
        config = deepcopy(self.native)
        config["fallback_providers"] = [{"provider": "openai", "model": "gpt-synthetic"}]
        with self.assertRaises(ModelConfigError):
            check_native(config)

    def test_secret_references_are_never_resolved_during_render(self):
        secret = self.root / "credential"
        secret.write_text("SYNTHETIC_SECRET_SENTINEL")
        with self.input.open("a") as stream:
            stream.write(f"  credential_ref: file:{secret}\n")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = render_model.main([str(self.input), "--output", str(self.root / "new.yaml")])
        self.assertEqual(status, 1)
        self.assertNotIn("SYNTHETIC_SECRET_SENTINEL", out.getvalue() + err.getvalue())
        self.assertFalse((self.root / "new.yaml").exists())

    @unittest.skipIf(os.geteuid() == 0, "Service checks intentionally reject root")
    def test_missing_auth_fails_before_upstream_import_or_source_inspection(self):
        home = self.root / "state"
        home.mkdir(mode=0o700)
        config = home / "config.yaml"
        config.write_text(yaml.safe_dump(self.native))
        config.chmod(0o600)
        with patch.dict(os.environ, {"HERMES_HOME": str(home)}), patch.object(model_probe.subprocess, "check_output") as git:
            with self.assertRaisesRegex(model_probe.ProbeError, "credentials_missing"):
                model_probe.prepare(self.root / "unavailable-source", home)
            git.assert_not_called()

    def test_auth_store_permissions_and_symlinks_are_rejected(self):
        secret = self.root / "auth.json"
        secret.write_text("SYNTHETIC_SECRET_SENTINEL")
        secret.chmod(0o644)
        with self.assertRaisesRegex(model_probe.ProbeError, "private_file_permissions"):
            model_probe.private_file(secret)
        secret.chmod(0o600)
        link = self.root / "auth-link.json"
        link.symlink_to(secret)
        with self.assertRaises(model_probe.ProbeError):
            model_probe.private_file(link)

    @unittest.skipIf(os.geteuid() == 0, "Service checks intentionally reject root")
    def test_invalid_auth_store_is_rejected_without_echoing_its_contents(self):
        home = self.root / "invalid-state"
        home.mkdir(mode=0o700)
        for name, value in (("config.yaml", yaml.safe_dump(self.native)),
                            ("auth.json", "SYNTHETIC_SECRET_SENTINEL invalid JSON")):
            path = home / name
            path.write_text(value)
            path.chmod(0o600)
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"HERMES_HOME": str(home)}):
            with redirect_stdout(out), redirect_stderr(err):
                status = model_probe.main(["check-auth", "--source", str(self.root), "--home", str(home)])
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(out.getvalue())["code"], "credentials_invalid")
        self.assertNotIn("SYNTHETIC_SECRET_SENTINEL", out.getvalue() + err.getvalue())

    def test_arbitrary_provider_error_text_is_not_printed(self):
        secret = "SYNTHETIC_SECRET_SENTINEL"
        out, err = io.StringIO(), io.StringIO()
        with patch.object(model_probe, "prepare", side_effect=RuntimeError(secret)):
            with redirect_stdout(out), redirect_stderr(err):
                status = model_probe.main(["query", "--source", str(self.root), "--home", str(self.root)])
        self.assertEqual(status, 1)
        self.assertNotIn(secret, out.getvalue() + err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["code"], "runtime_check_failed")

    def test_status_errors_remain_distinct_without_response_bodies(self):
        for status, code in ((401, "credentials_rejected"), (403, "credentials_rejected"),
                             (429, "quota_or_rate_limit"), (400, "model_request_rejected")):
            error = RuntimeError("SYNTHETIC_SECRET_SENTINEL")
            error.status_code = status
            self.assertEqual(model_probe.safe_error(error), code)

    def test_native_agent_gets_no_tools_fallback_or_persisted_trajectory(self):
        captured = {}
        class Agent:
            api_mode = "codex_responses"
            tools = []
            _fallback_chain = []
            def __init__(self, **kwargs):
                captured.update(kwargs)
            def chat(self, message):
                captured["chat_called"] = True
                self.message = message
                return "HAVEN_MODEL_OK"
            def close(self):
                captured["closed"] = True
        auth = types.ModuleType("hermes_cli.auth_codex")
        auth.resolve_codex_runtime_credentials = lambda **kwargs: {
            "provider": "openai-codex", "base_url": CODEX_BASE_URL,
            "api_key": "SYNTHETIC_SECRET_SENTINEL"}
        agent = types.ModuleType("run_agent")
        agent.AIAgent = Agent
        with patch.dict("sys.modules", {"hermes_cli.auth_codex": auth, "run_agent": agent}):
            result = model_probe.run_native("query", "gpt-synthetic")
        self.assertEqual(captured["enabled_toolsets"], [])
        self.assertEqual(captured["fallback_model"], [])
        self.assertFalse(captured["save_trajectories"])
        self.assertTrue(captured["closed"])
        self.assertNotIn("SYNTHETIC_SECRET_SENTINEL", json.dumps(result))
        for tools, fallback, mode in ((["terminal"], [], "codex_responses"),
                                       ([], ["paid"], "codex_responses"),
                                       ([], [], "codex_app_server")):
            Agent.tools, Agent._fallback_chain, Agent.api_mode = tools, fallback, mode
            captured["chat_called"] = False
            with patch.dict("sys.modules", {"hermes_cli.auth_codex": auth, "run_agent": agent}):
                with self.assertRaisesRegex(model_probe.ProbeError, "unexpected_tools_or_fallback"):
                    model_probe.run_native("query", "gpt-synthetic")
            self.assertFalse(captured["chat_called"])


if __name__ == "__main__":
    unittest.main()
