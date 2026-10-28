"""Explicit, bounded native Hermes checks; no credentials or raw exceptions are printed.

Run under the service identity with its isolated environment and the pinned
Hermes Python. Public checks use synthetic doubles and never make live requests.
"""

import argparse
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import stat
import subprocess
import sys

from scripts.model_contract import CODEX_BASE_URL, CODEX_PROVIDER, HERMES_COMMIT, PATCHED_FILES, check_native


class ProbeError(ValueError):
    """Only fixed diagnostic codes may cross the output boundary."""


def private_file(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 1024 * 1024):
        raise ProbeError("private_file_permissions")
    return path


def prepare(source, home):
    if os.geteuid() == 0:
        raise ProbeError("service_identity_required")
    info = home.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ProbeError("private_home_permissions")
    # The launcher must select the same profile before any upstream import.
    if os.environ.get("HERMES_HOME") != str(home):
        raise ProbeError("profile_environment_mismatch")
    import yaml
    config = yaml.safe_load(private_file(home / "config.yaml").read_text())
    model = check_native(config)
    try:
        auth = private_file(home / "auth.json")
    except FileNotFoundError:
        raise ProbeError("credentials_missing") from None
    try:
        if not isinstance(json.loads(auth.read_text()), dict):
            raise ProbeError("credentials_invalid")
    except (json.JSONDecodeError, UnicodeError):
        raise ProbeError("credentials_invalid") from None
    head = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True,
                                   stderr=subprocess.DEVNULL).strip()
    if head != HERMES_COMMIT:
        raise ProbeError("source_revision_mismatch")
    for name, digest in PATCHED_FILES.items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != digest:
            raise ProbeError("candidate_patch_mismatch")
    sys.path.insert(0, str(source))
    return model


def run_native(mode, model):
    from hermes_cli.auth_codex import resolve_codex_runtime_credentials
    runtime = resolve_codex_runtime_credentials(read_only=mode == "check-auth")
    if runtime.get("provider") != CODEX_PROVIDER or runtime.get("base_url") != CODEX_BASE_URL:
        raise ProbeError("credential_route_mismatch")
    if not runtime.get("api_key"):
        raise ProbeError("credentials_missing")
    if mode == "check-auth":
        return {"result": "passed", "check": mode, "provider": CODEX_PROVIDER}
    if mode == "catalog":
        import httpx
        from agent.codex_headers import codex_account_headers
        from agent.model_metadata import fetch_codex_catalog_entries
        headers = {"Authorization": "Bearer " + runtime["api_key"], **codex_account_headers(runtime["api_key"])}
        entries, _status = fetch_codex_catalog_entries(
            lambda url: httpx.get(url, headers=headers, timeout=15), base_url=CODEX_BASE_URL)
        models = sorted({entry["slug"] for entry in entries if isinstance(entry.get("slug"), str)
                         and entry.get("visibility") not in {"hide", "hidden"}})
        if not models:
            raise ProbeError("model_catalog_unavailable")
        return {"result": "passed", "check": mode, "models": models}
    from run_agent import AIAgent
    agent = AIAgent(
        provider=CODEX_PROVIDER, api_mode="codex_responses", base_url=CODEX_BASE_URL,
        api_key=runtime["api_key"], model=model, enabled_toolsets=[], fallback_model=[],
        max_iterations=2, run_budget_seconds=60, quiet_mode=True, save_trajectories=False,
        verbose_logging=False, skip_context_files=True, skip_memory=True,
        skip_background_review=True, load_soul_identity=False, platform="cli",
    )
    try:
        if agent.tools or agent._fallback_chain or agent.api_mode != "codex_responses":
            raise ProbeError("unexpected_tools_or_fallback")
        response = agent.chat("Odpověz pouze přesným řetězcem HAVEN_MODEL_OK.")
        if response.strip() != "HAVEN_MODEL_OK":
            raise ProbeError("unexpected_model_response")
    finally:
        agent.close()
    return {"result": "passed", "check": mode, "provider": CODEX_PROVIDER,
            "model": model, "transport": "codex_responses", "tools": 0, "fallbacks": 0}


def safe_error(error):
    if isinstance(error, ProbeError):
        return str(error)
    status = getattr(error, "status_code", None)
    if status in {401, 403}:
        return "credentials_rejected"
    if status == 429:
        return "quota_or_rate_limit"
    if status in {400, 404}:
        return "model_request_rejected"
    if error.__class__.__name__ == "AuthError":
        return "credentials_unavailable"
    if isinstance(error, OSError):
        return "runtime_file_unavailable"
    return "runtime_check_failed"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check-auth", "catalog", "query"))
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    args = parser.parse_args(argv)
    logging.disable(logging.CRITICAL)
    # Upstream notices/HTTP diagnostics can contain private values. Return a
    # fixed code instead of echoing captured output, exception repr or a body.
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            model = prepare(args.source.resolve(), args.home.absolute())
            result = run_native(args.mode, model)
    except Exception as error:
        print(json.dumps({"result": "failed", "code": safe_error(error)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
