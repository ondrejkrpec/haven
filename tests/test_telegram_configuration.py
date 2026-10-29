"""Telegram policy, private credential handling and enrollment boundaries."""

from contextlib import redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from scripts.model_contract import ModelConfigError, check_service, service_config
from scripts import telegram_runtime as runtime
from scripts import service_runtime
from scripts.validate_config import ConfigError, ROOT, load_config


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.config = load_config(ROOT / "examples/instance.yaml")
        self.config["model"]["name"] = "gpt-synthetic"
        self.config["telegram"] = {"enabled": True, "owner_id": "123456789"}

    def test_schema_requires_one_numeric_owner_and_rejects_plaintext_tokens(self):
        for entry in ({"enabled": True}, {"enabled": True, "owner_id": "@synthetic"},
                      {"enabled": True, "owner_id": 123}, {"enabled": False, "token": "secret"}):
            path = self.home / "instance.yaml"
            value = deepcopy(self.config)
            value["telegram"] = entry
            path.write_text(yaml.safe_dump(value))
            with self.subTest(entry=entry), self.assertRaises(ConfigError):
                load_config(path)

    def test_runtime_refuses_multiple_transports_and_policy_expansion(self):
        native = service_config(self.config)
        self.assertEqual(check_service(native), "gpt-synthetic")
        for key, value in (("allow_from", ["*"]), ("guest_mode", True),
                           ("group_allow_from", ["123456789"]), ("allowed_chats", [])):
            changed = deepcopy(native)
            changed["platforms"]["telegram"]["extra"][key] = value
            with self.subTest(key=key), self.assertRaises(ModelConfigError):
                check_service(changed)
        self.config["whatsapp"] = {"enabled": True, "allowed_senders": ["+" + "1" * 11]}
        with self.assertRaises(ModelConfigError):
            service_config(self.config)

    def test_environment_uses_only_the_private_token_and_refuses_linked_or_shared_files(self):
        directory = self.home / "credentials"
        directory.mkdir(mode=0o700)
        token = "123456:" + "SYNTHETIC_" * 4
        path = directory / runtime.TOKEN_NAME
        path.write_text(token)
        path.chmod(0o600)
        actual = runtime.environment(self.home, {"TELEGRAM_BOT_TOKEN": "wrong", "TELEGRAM_PROXY": "wrong", "USER": "test"})
        self.assertEqual(actual, {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_ALLOW_ALL_USERS": "false", "USER": "test"})
        path.chmod(0o644)
        with self.assertRaises(runtime.ProbeError):
            runtime.read_token(self.home)
        path.unlink()
        target = self.home / "other"
        target.write_text(token)
        target.chmod(0o600)
        path.symlink_to(target)
        with self.assertRaises(runtime.ProbeError):
            runtime.read_token(self.home)

    def test_enrollment_accepts_only_the_challenge_in_the_human_owners_private_chat(self):
        def update(number, kind, user, text, bot=False):
            return {"update_id": number, "message": {"text": text, "chat": {"type": kind, "id": user},
                                                    "from": {"id": user, "is_bot": bot}}}
        replies = [[update(1, "group", 123, "proof"), update(2, "private", 123, "wrong"),
                    update(3, "private", 123, "proof", True)], [update(4, "private", 456, "proof")], []]
        with patch.object(runtime, "rpc", side_effect=replies) as rpc:
            self.assertEqual(runtime.identify("synthetic", "proof"), "456")
        self.assertEqual(rpc.call_args.kwargs["offset"], 5)

    def test_supervised_launcher_injects_token_only_after_validated_policy(self):
        directory = self.home / "credentials"
        directory.mkdir(mode=0o700)
        token = "123456:" + "SYNTHETIC_" * 4
        path = directory / runtime.TOKEN_NAME
        path.write_text(token)
        path.chmod(0o600)
        native = service_config(self.config)
        with patch.object(service_runtime, "check", return_value=native), \
                patch.object(service_runtime, "check_worker_bus"), patch.object(service_runtime.os, "chdir"), \
                patch.object(service_runtime.os, "execve", side_effect=SystemExit) as execute:
            with self.assertRaises(SystemExit):
                service_runtime.main(["run", "--source", str(self.home), "--home", str(self.home)])
        self.assertEqual(execute.call_args.args[2]["TELEGRAM_BOT_TOKEN"], token)
        self.assertEqual(execute.call_args.args[2]["TELEGRAM_ALLOW_ALL_USERS"], "false")

    def test_enrollment_rejects_ambiguous_ownership(self):
        updates = [{"update_id": i, "message": {"text": "proof", "chat": {"type": "private", "id": i},
                                                "from": {"id": i, "is_bot": False}}} for i in (1, 2)]
        with patch.object(runtime, "rpc", return_value=updates), self.assertRaises(runtime.ProbeError):
            runtime.identify("synthetic", "proof")

    def test_enrollment_requires_terminal_and_fixed_diagnostics_hide_exception_values(self):
        with patch.object(runtime.sys.stdin, "isatty", return_value=False), self.assertRaises(runtime.ProbeError):
            runtime.enroll()
        out = io.StringIO()
        with patch.object(runtime.sys, "argv", ["telegram_runtime", "check"]), \
                patch.object(runtime, "check_dependency", side_effect=ValueError("SYNTHETIC_SECRET")), redirect_stdout(out):
            self.assertEqual(runtime.main(), 78)
        self.assertNotIn("SYNTHETIC_SECRET", out.getvalue())


if __name__ == "__main__":
    unittest.main()
