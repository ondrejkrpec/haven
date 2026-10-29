"""Bounded service policy, safe diagnostics and restart health without live accounts."""

from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

import yaml

from scripts.model_contract import ModelConfigError, check_service, service_config
from scripts import service_runtime
from scripts.validate_config import load_config


class ServiceRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        config = load_config(Path(__file__).resolve().parents[1] / "examples/instance.yaml")
        config["model"]["name"] = "gpt-synthetic"
        self.config = service_config(config)
        (self.home / "config.yaml").write_text(yaml.safe_dump(self.config))
        (self.home / "config.yaml").chmod(0o600)

    def test_service_explicitly_limits_each_entrypoint_and_disables_coding(self):
        self.assertEqual(check_service(self.config), "gpt-synthetic")
        for platform in ("cli", "local", "api_server", "cron", "whatsapp"):
            self.assertEqual(self.config["platform_toolsets"][platform], [])
        self.assertFalse(self.config["kanban"]["dispatch_in_gateway"])
        self.assertFalse(self.config["auth"]["adopt_external_logins"])

    def test_service_refuses_injected_capabilities_and_unbounded_budgets(self):
        cases = [("kanban", "dispatch_in_gateway", True), ("agent", "max_turns", 100),
                 ("gateway", "multiplex_profiles", True), ("hooks", "synthetic", ["shell"]),
                 ("onboarding", "profile_build", "ask"), ("onboarding", "seen", {}),
                 ("mcp_servers", "synthetic", {"command": "sh"}),
                 ("platform_toolsets", "cron", ["terminal"]),
                 ("cron", "require_restart_safe_scope", False),
                 ("platform_toolsets", "api_server", ["terminal"])]
        for section, key, value in cases:
            with self.subTest(section=section, key=key):
                changed = deepcopy(self.config)
                changed[section][key] = value
                with self.assertRaises(ModelConfigError):
                    check_service(changed)
        changed = deepcopy(self.config)
        changed["unreviewed_extension"] = True
        with self.assertRaises(ModelConfigError):
            check_service(changed)

    @unittest.skipIf(os.geteuid() == 0, "The service guard rejects root")
    def test_scheduler_can_start_without_credentials_and_never_opens_auth(self):
        # Invalid contents prove the guard does not require or resolve credentials.
        (self.home / "auth.json").write_text("SYNTHETIC_SECRET_SENTINEL invalid auth")
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home)}):
            with patch.object(service_runtime, "check_candidate"):
                service_runtime.check(self.home, self.home)

    @unittest.skipIf(os.geteuid() == 0, "The service guard rejects root")
    def test_profile_and_login_environment_cannot_be_adopted(self):
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home)}):
            with patch.object(service_runtime, "check_candidate"):
                for name in (".env", "active_profile"):
                    path = self.home / name
                    path.write_text("SYNTHETIC_SECRET_SENTINEL")
                    with self.subTest(name=name), self.assertRaises(service_runtime.ProbeError):
                        service_runtime.check(self.home, self.home)
                    path.unlink()

    def test_health_requires_live_process_identity_and_a_fresh_heartbeat(self):
        status = types.ModuleType("gateway.status")
        state = {"pid": 123, "gateway_state": "running", "start_time": 456}
        status.read_runtime_status = lambda path: state
        status.get_process_start_time = lambda pid: 456
        (self.home / "state").mkdir()
        heartbeat = self.home / "state/gateway.heartbeat"
        heartbeat.write_text(json.dumps({"pid": 123, "updated_at": datetime.now(timezone.utc).isoformat()}))
        with patch.dict("sys.modules", {"gateway.status": status}):
            with patch.object(service_runtime, "check", return_value=self.config), patch.object(service_runtime.sys, "path", []):
                self.assertEqual(service_runtime.health(self.home, self.home)["result"], "passed")
                status.get_process_start_time = lambda pid: 999
                with self.assertRaisesRegex(service_runtime.ProbeError, "gateway_not_ready"):
                    service_runtime.health(self.home, self.home)
                status.get_process_start_time = lambda pid: 456
                heartbeat.write_text(json.dumps({"pid": 123, "updated_at": "2000-01-01T00:00:00+00:00"}))
                with self.assertRaisesRegex(service_runtime.ProbeError, "gateway_heartbeat_stale"):
                    service_runtime.health(self.home, self.home)

    def test_failure_parks_the_unit_with_a_fixed_diagnostic(self):
        out = io.StringIO()
        with patch.object(service_runtime, "check", side_effect=ValueError("SYNTHETIC_SECRET_SENTINEL")):
            with redirect_stdout(out):
                rc = service_runtime.main(["check", "--source", str(self.home), "--home", str(self.home)])
        self.assertEqual(rc, 78)
        self.assertEqual(json.loads(out.getvalue()), {"result": "failed", "code": "service_check_failed"})
        self.assertNotIn("SYNTHETIC_SECRET_SENTINEL", out.getvalue())

    def test_guard_executes_native_gateway_in_the_same_process_after_validation(self):
        with patch.object(service_runtime, "check", return_value=self.config) as guard, patch.object(service_runtime.os, "chdir"), \
                patch.object(service_runtime, "check_worker_bus") as bus:
            with patch.object(service_runtime.os, "execve", side_effect=SystemExit(0)) as execute:
                with self.assertRaises(SystemExit):
                    service_runtime.main(["run", "--source", str(self.home), "--home", str(self.home)])
        guard.assert_called_once_with(self.home, self.home)
        bus.assert_called_once_with()
        self.assertEqual(execute.call_args.args[1][1:],
                         ["-m", "hermes_cli.main", "--profile", "default", "gateway", "run", "--quiet"])
        self.assertEqual(execute.call_args.args[2]["HERMES_HOME"], str(self.home))

    def test_missing_worker_bus_prevents_native_start(self):
        with patch.object(service_runtime, "check"), patch.object(service_runtime.os, "execve") as execute:
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/unavailable-synthetic-runtime"}):
                with redirect_stdout(io.StringIO()):
                    rc = service_runtime.main(["run", "--source", str(self.home), "--home", str(self.home)])
        self.assertEqual(rc, 78)
        execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
