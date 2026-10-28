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
