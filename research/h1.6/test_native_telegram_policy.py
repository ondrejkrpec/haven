"""Rendered Haven policy through actual Hermes/PTB inputs; no live requests."""

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from ruamel.yaml import YAML


@pytest.fixture
def policy(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:" + "SYNTHETIC_" * 4)
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "false")
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("haven_telegram_contract", root / "scripts/model_contract.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    native = module.service_config({
        "model": {"provider": "openai-codex", "name": "gpt-synthetic"},
        "host": {"service_user": "haven"}, "instance": {"timezone": "Europe/Prague"},
        "whatsapp": {"enabled": False}, "telegram": {"enabled": True, "owner_id": "123456789"},
    })
    assert module.check_service(native) == "gpt-synthetic"
    with (home / "config.yaml").open("w") as stream:
        YAML().dump(native, stream)
    from gateway.config import Platform, load_gateway_config
    from plugins.platforms.telegram.adapter import TelegramAdapter
    config = load_gateway_config()
    adapter = TelegramAdapter(config.platforms[Platform.TELEGRAM])
    adapter._bot = SimpleNamespace(id=999, username="synthetic_nora_bot")
    return config, adapter


def message(user=123456789, chat_type="private"):
    from telegram import Message
    return Message.de_json({
        "message_id": 1, "date": 1800000000, "text": "synthetic text",
        "chat": {"id": user if chat_type == "private" else -100123, "type": chat_type},
        "from": {"id": user, "is_bot": False, "first_name": "Synthetic"},
    }, None)


def test_native_adapter_accepts_only_owner_dm_and_denies_groups_and_strangers(policy):
    _config, adapter = policy
    assert adapter._is_user_authorized_from_message(message())
    assert not adapter._is_user_authorized_from_message(message(user=987654321))
    for kind in ("group", "supergroup", "channel"):
        assert not adapter._is_user_authorized_from_message(message(chat_type=kind))
    assert adapter._drop_pending_on_cold_boot is False


def test_denied_updates_stop_before_event_building_or_model_handoff(policy):
    from telegram import Update
    _config, adapter = policy
    adapter._build_message_event = AsyncMock()
    adapter.handle_message = AsyncMock()
    for incoming in (message(user=987654321), message(chat_type="supergroup")):
        asyncio.run(adapter._handle_text_message(Update(1, message=incoming), SimpleNamespace()))
    adapter._build_message_event.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()


def test_native_slash_gate_does_not_grant_owner_remote_administration(policy):
    from gateway.config import Platform
    from gateway.session import SessionSource
    from gateway.slash_access import policy_for_source
    config, _adapter = policy
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123456789", chat_type="dm", user_id="123456789")
    access = policy_for_source(config, source)
    assert access.enabled and not access.is_admin(source.user_id)
    for command in ("help", "whoami", "new", "stop", "status"):
        assert access.can_run(source.user_id, command)
    for command in ("terminal", "exec", "model", "config", "restart", "cron", "tools", "goal", "skills"):
        assert not access.can_run(source.user_id, command)


def test_actual_tool_resolver_and_stt_keep_text_pilot_bounded(policy):
    from hermes_cli.config import load_config_readonly
    from hermes_cli.tools_config import _get_platform_tools
    config, _adapter = policy
    native = load_config_readonly()
    assert _get_platform_tools(native, "telegram") == set()
    assert config.stt_enabled is False
    assert native["fallback_providers"] == []
    assert native["kanban"]["dispatch_in_gateway"] is False


def test_first_contact_and_seen_hints_leave_the_bounded_profile_unchanged(policy):
    from agent.onboarding import (BUSY_INPUT_FLAG, PLAIN_INTRO_NOTE, TOOL_PROGRESS_FLAG,
                                  first_contact_turn_note, mark_seen)
    from hermes_constants import get_hermes_home

    path = get_hermes_home() / "config.yaml"
    before = path.read_bytes()
    config = YAML(typ="safe", pure=True).load(before)
    note = first_contact_turn_note(config, path, session_history_empty=True, install_has_prior_sessions=False)
    assert note == PLAIN_INTRO_NOTE
    for flag in (BUSY_INPUT_FLAG, TOOL_PROGRESS_FLAG):
        assert mark_seen(path, flag)
    sys.modules["haven_telegram_contract"].check_service(YAML(typ="safe", pure=True).load(path.read_text()))
    assert path.read_bytes() == before
