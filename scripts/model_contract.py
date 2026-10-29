"""The deliberately small H1.4 native configuration contract for the pilot."""

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
        raise ModelConfigError("WhatsApp runtime translation is deferred to H1.6")
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
    except (KeyError, TypeError):
        valid = False
    if not valid:
        raise ModelConfigError("Native runtime policy differs from the bounded H1.4 configuration")
    return model["default"]


def service_config(config):
    """H1.5 has a supervised coordinator, without messaging or coding executors."""
    native = native_config(config)
    native["platform_toolsets"] = {name: [] for name in ("cli", "local", "api_server", "cron", "whatsapp")}
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
    return native


def check_service(config):
    """Refuse capability expansion before starting a persistent process."""
    model = check_native(config)
    try:
        expected = service_config({"model": {"provider": CODEX_PROVIDER, "name": model},
                                   "host": {"service_user": "haven"},
                                   "whatsapp": {"enabled": False},
                                   "instance": {"timezone": config["timezone"]}})
        # Compare the whole bounded profile, permitting only upstream's version marker.
        actual = {key: value for key, value in config.items() if key != "_config_version"}
        if actual != expected:
            raise ModelConfigError("Native service policy differs from the bounded H1.5 configuration")
    except (KeyError, TypeError):
        raise ModelConfigError("Native service policy is incomplete") from None
    return model
