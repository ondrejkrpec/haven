"""Run with the pinned Hermes canonical runner, never the production profile.

These tests consume Haven's real renderer and Hermes's real loader and adapter.
They make no provider or WhatsApp requests. The selected test environment needs
the frozen upstream sms extra and test dependencies.
"""

import asyncio
import importlib.util
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from ruamel.yaml import YAML


@pytest.fixture
def policy(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("WHATSAPP_MODE", "bot")
    monkeypatch.setenv("WHATSAPP_ALLOW_ALL_USERS", "false")
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "false")
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("haven_model_contract", root / "scripts/model_contract.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    native = module.service_config({
        "model": {"provider": "openai-codex", "name": "gpt-synthetic"},
        "host": {"service_user": "haven"}, "instance": {"timezone": "Europe/Prague"},
        "whatsapp": {"enabled": True, "allowed_senders": ["+15555550123"]},
    })
    assert module.check_service(native) == "gpt-synthetic"
    # Only the session path changes for synthetic isolation; all policy fields
    # and their native loader path remain exactly as rendered for deployment.
    native["platforms"]["whatsapp"]["extra"]["session_path"] = str(home / "session")
    with (home / "config.yaml").open("w") as stream:
        YAML().dump(native, stream)
    from gateway.config import load_gateway_config, Platform
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter
    config = load_gateway_config()
    assert config.stt_enabled is False
    assert config.get_unauthorized_dm_behavior(Platform.WHATSAPP) == "ignore"
    adapter = WhatsAppAdapter(config.platforms[Platform.WHATSAPP])
    return config, adapter


def payload(sender="15555550123@s.whatsapp.net", **overrides):
    return {"senderId": sender, "chatId": sender, "body": "synthetic text",
            "isGroup": False, "hasMedia": False, "messageId": "synthetic-1", **overrides}


def test_native_loader_and_bridge_share_the_explicit_private_policy(policy):
    config, adapter = policy
    env = adapter._bridge_env()
    assert env["WHATSAPP_MODE"] == "bot"
    assert env["WHATSAPP_DM_POLICY"] == "allowlist"
    assert env["WHATSAPP_GROUP_POLICY"] == "disabled"
    assert env["WHATSAPP_ALLOWED_USERS"] == "15555550123"
    assert adapter._is_dm_allowed("15555550123@s.whatsapp.net")
    assert not adapter._is_dm_allowed("15555550999@s.whatsapp.net")
    assert not adapter._is_group_allowed("synthetic@g.us", "15555550123@s.whatsapp.net")


def test_denied_dm_and_group_stop_before_media_or_gateway_handoff(policy):
    _config, adapter = policy
    adapter._collect_bridge_media = AsyncMock(return_value=([], []))
    assert asyncio.run(adapter._build_message_event(payload("15555550999@s.whatsapp.net"))) is None
    assert asyncio.run(adapter._build_message_event(payload(isGroup=True, chatId="synthetic@g.us"))) is None
    adapter._collect_bridge_media.assert_not_awaited()
    event = asyncio.run(adapter._build_message_event(payload()))
    assert event is not None
    assert event.text == "synthetic text"
    adapter._collect_bridge_media.assert_awaited_once()


def test_allowed_sender_gets_chat_commands_but_no_administrative_commands(policy):
    config, _adapter = policy
    from gateway.config import Platform
    from gateway.session import SessionSource
    from gateway.slash_access import policy_for_source
    source = SessionSource(platform=Platform.WHATSAPP, chat_id="15555550123", chat_type="dm",
                           user_id="15555550123")
    access = policy_for_source(config, source)
    assert access.enabled
    for command in ("help", "whoami", "new", "stop", "status"):
        assert access.can_run(source.user_id, command)
    for command in ("terminal", "exec", "model", "config", "restart", "cron", "tools", "goal", "skills"):
        assert not access.can_run(source.user_id, command)
    assert not access.is_admin(source.user_id)


def test_actual_gateway_tool_resolution_preserves_the_empty_whatsapp_toolset(policy):
    _config, _adapter = policy
    from hermes_cli.config import load_config_readonly
    from hermes_cli.tools_config import _get_platform_tools
    native = load_config_readonly()
    assert _get_platform_tools(native, "whatsapp") == set()
    assert native["fallback_providers"] == []
    assert native["kanban"]["dispatch_in_gateway"] is False


def test_model_media_directives_cannot_upload_arbitrary_fresh_host_files(policy, tmp_path):
    _config, _adapter = policy
    from gateway.media_policy import apply_media_policy_env
    from gateway.platforms.base import validate_media_delivery_path
    private = tmp_path / "synthetic-private.txt"
    private.write_text("SYNTHETIC_PRIVATE_SENTINEL")
    with patch.dict(os.environ):
        for key in list(os.environ):
            if key.startswith("HERMES_MEDIA_"):
                del os.environ[key]
        apply_media_policy_env()
        assert os.environ["HERMES_MEDIA_DELIVERY_STRICT"] == "1"
        assert os.environ["HERMES_MEDIA_TRUST_RECENT_FILES"] == "0"
        assert validate_media_delivery_path(str(private)) is None
