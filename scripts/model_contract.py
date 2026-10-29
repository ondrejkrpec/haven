"""Bounded native configuration contracts for the Haven pilot."""

from copy import deepcopy
import re

CODEX_PROVIDER = "openai-codex"
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
HERMES_COMMIT = "bf0203a794a07aac3ff764972ab9087baa4e4f4e"
PATCHED_FILES = {
    "hermes_cli/kanban_db_dispatch.py": "b411347b0a693422be4fc704974d5dde383de8eeb63b47c7a5139355e71bb056",
    "tests/hermes_cli/test_kanban_db.py": "aa1366f4cd2a71165d12802ca6608d46a958b677178121ac1f6203b34714db11",
}


class ModelConfigError(ValueError):
    """Safe diagnostics contain fixed messages, never configuration values."""


def native_config(config):
    """Translate validated Haven input; unsupported deployment features fail closed."""
    model = config["model"]
    if model["provider"] != CODEX_PROVIDER:
        raise ModelConfigError("H1.4 runtime supports only the explicit openai-codex provider")
    if "credential_ref" in model:
        raise ModelConfigError("Subscription authentication uses a separate Hermes login, not credential_ref")
    if not model["name"].startswith("gpt-"):
        raise ModelConfigError("Select a concrete Codex model before rendering runtime configuration")
    if config["host"]["service_user"] != "haven":
        raise ModelConfigError("The current host playbook requires the haven service identity")
    if config["whatsapp"]["enabled"]:
        raise ModelConfigError("WhatsApp requires the explicitly supervised service profile")
    if config.get("telegram", {}).get("enabled"):
        raise ModelConfigError("Telegram requires the explicitly supervised service profile")
    return {
        "model": {"default": model["name"], "provider": CODEX_PROVIDER,
                  "base_url": CODEX_BASE_URL, "openai_runtime": "auto"},
        "timezone": config["instance"]["timezone"],
        "auth": {"adopt_external_logins": False, "codex_login_flow": "browser"},
        "fallback_providers": [],
        "agent": {"max_turns": 2, "api_max_retries": 0, "run_budget_seconds": 60},
        "platform_toolsets": {"cli": [], "whatsapp": []},
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "compression": {"enabled": False},
        "auxiliary": {"title_generation": {"enabled": False, "model_upgrade_enabled": False}},
    }


def check_native(config):
    """Check the security-relevant runtime fields before any credential resolution."""
    try:
        model = config["model"]
        valid = (
            model["provider"] == CODEX_PROVIDER
            and model["base_url"] == CODEX_BASE_URL
            and model["openai_runtime"] == "auto"
            and isinstance(model["default"], str) and model["default"].startswith("gpt-")
            and "api_key" not in model
            and config["auth"]["adopt_external_logins"] is False
            and config["fallback_providers"] == []
            and config["platform_toolsets"]["cli"] == []
            and config["platform_toolsets"]["whatsapp"] == []
        )
    except (KeyError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ModelConfigError("Native runtime policy differs from the bounded H1.4 configuration")
    return model["default"]


def service_config(config):
    """Supervised coordinator; one optional private transport, no coding executors."""
    base = deepcopy(config)
    base["whatsapp"]["enabled"] = False
    base["telegram"] = {"enabled": False}
    native = native_config(base)
    native["platform_toolsets"] = {name: [] for name in ("cli", "local", "api_server", "cron", "whatsapp", "telegram")}
    native["gateway"] = {"multiplex_profiles": False, "systemd_watchdog_seconds": 120,
                         "max_concurrent_sessions": 1, "delivery_ledger": True}
    native["kanban"] = {"dispatch_in_gateway": False, "auto_decompose": False}
    native["cron"] = {"max_parallel_jobs": 1, "allow_agent_scheduling": False,
                      "require_restart_safe_scope": True}
    native["updates"] = {"check": False}
    native["security"] = {"allow_lazy_installs": False}
    native["plugins"] = {"enabled": []}
    native["mcp_servers"] = {}
    native["hooks"] = {}
    native["hooks_auto_accept"] = False
    # First-contact profile building and one-shot busy/tool hints persist flags
    # into config.yaml. Their prompts offer capabilities outside this pilot;
    # declare them handled so ordinary messaging cannot change the contract.
    native["onboarding"] = {"profile_build": "off", "seen": {
        "busy_input_prompt": True, "tool_progress_prompt": True,
    }}
    telegram = config.get("telegram", {})
    if telegram.get("enabled") and config["whatsapp"]["enabled"]:
        raise ModelConfigError("The pilot accepts only one messaging transport at a time")
    if telegram.get("enabled"):
        owner = telegram.get("owner_id")
        if not isinstance(owner, str) or not re.fullmatch(r"[1-9][0-9]{0,15}", owner):
            raise ModelConfigError("Telegram requires one explicit numeric owner ID")
        native["stt"] = {"enabled": False}
        native["gateway"].update(strict=True, trust_recent_files=False, media_delivery_allow_dirs=[])
        native["unauthorized_dm_behavior"] = "ignore"
        native["platforms"] = {"telegram": {"enabled": True, "extra": {
            "allow_from": [owner], "allowed_chats": [owner],
            "dm_policy": "allowlist", "group_policy": "disabled",
            "group_allow_from": ["haven-console-only"], "group_allowed_chats": [],
            "allow_admin_from": ["haven-console-only"],
            "user_allowed_commands": ["new", "stop", "status"],
            "unauthorized_dm_behavior": "ignore", "guest_mode": False,
            "allow_bots": "none", "observe_unmentioned_group_messages": False,
            "disable_link_previews": True, "dm_topic_auto_transfer": False,
            "drop_pending_on_cold_boot": False,
        }}}
    if config["whatsapp"]["enabled"]:
        senders = config["whatsapp"].get("allowed_senders")
        if (not isinstance(senders, list) or not senders or len(senders) > 100
                or any(not isinstance(sender, str) or not re.fullmatch(r"\+[1-9][0-9]{7,14}", sender)
                       for sender in senders) or len(set(senders)) != len(senders)):
            raise ModelConfigError("WhatsApp requires an explicit E.164 sender allowlist")
        native["stt"] = {"enabled": False}
        native["gateway"].update(strict=True, trust_recent_files=False, media_delivery_allow_dirs=[])
        native["unauthorized_dm_behavior"] = "ignore"
        native["platforms"] = {"whatsapp": {"enabled": True, "extra": {
            "bridge_port": 3000,
            "bridge_script": "/var/lib/haven/whatsapp/bridge/bridge.js",
            "session_path": "/var/lib/haven/.hermes/platforms/whatsapp/session",
            "dm_policy": "allowlist", "allow_from": [sender[1:] for sender in senders],
            "group_policy": "disabled", "group_allow_from": [],
            "unauthorized_dm_behavior": "ignore", "send_read_receipts": False,
            "reply_prefix": "Nora", "text_batch_delay_seconds": 0.3,
            # Native slash gating is disabled by an empty admin list. This
            # deliberately nonnumeric sentinel cannot be a WhatsApp principal.
            "allow_admin_from": ["haven-console-only"],
            "user_allowed_commands": ["new", "stop", "status"],
        }}}
    return native


def check_service(config):
    """Refuse capability expansion before starting a persistent process."""
    model = check_native(config)
    try:
        whatsapp = config.get("platforms", {}).get("whatsapp", {})
        telegram = config.get("platforms", {}).get("telegram", {})
        owners = telegram.get("extra", {}).get("allow_from", [])
        senders = whatsapp.get("extra", {}).get("allow_from", [])
        expected = service_config({"model": {"provider": CODEX_PROVIDER, "name": model},
                                   "host": {"service_user": "haven"},
                                   "whatsapp": {"enabled": whatsapp.get("enabled", False),
                                                "allowed_senders": ["+" + s for s in senders]},
                                   "telegram": {"enabled": telegram.get("enabled", False),
                                                "owner_id": owners[0] if len(owners) == 1 else None},
                                   "instance": {"timezone": config["timezone"]}})
        # Compare the whole bounded profile, permitting only upstream's version marker.
        actual = {key: value for key, value in config.items() if key != "_config_version"}
        if actual != expected:
            raise ModelConfigError("Native service policy differs from the bounded configuration")
    except (KeyError, TypeError, AttributeError):
        raise ModelConfigError("Native service policy is incomplete") from None
    return model
