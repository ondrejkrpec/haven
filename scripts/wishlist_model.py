"""One bounded no-tools native provider request under the interpretation identity."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import json
import logging
import os
from pathlib import Path
import resource
import struct
import sys

from scripts.wishlist_domain import canonical
from scripts.wishlist_language_contract import SYSTEM_PROMPT, validate_decision, validate_input
from scripts.wishlist_contract import COMMAND_VALIDATOR
from scripts.wishlist_protocol import decode, encode


def native_request(prepared, *, source, home):
    from scripts.model_probe import prepare
    model = prepare(source, home)
    from scripts.model_contract import CODEX_BASE_URL, CODEX_PROVIDER, HERMES_COMMIT
    from scripts.wishlist_ingress import check_runtime
    check_runtime(source)
    from scripts.capability_runtime import ROOT, trusted_bytes
    pins = json.loads(trusted_bytes(ROOT / "config/wishlist-language-runtime.json"))
    if pins["hermes_commit"] != HERMES_COMMIT:
        raise ValueError("language_native_revision_mismatch")
    for item in pins["sources"]:
        if hashlib.sha256(trusted_bytes(source / item["path"], owner=source.stat().st_uid)).hexdigest() != item["sha256"]:
            raise ValueError("language_native_source_mismatch")
    from ruamel.yaml import YAML
    from scripts.model_contract import native_config
    actual = YAML(typ="safe", pure=True).load((home / "config.yaml").read_text())
    expected = native_config({"model": {"provider": CODEX_PROVIDER, "name": model},
                              "instance": {"timezone": actual["timezone"]}, "host": {"service_user": "haven"},
                              "whatsapp": {"enabled": False}})
    if {key: value for key, value in actual.items() if key != "_config_version"} != expected:
        raise ValueError("language_profile_mismatch")
    from hermes_cli.auth_codex import resolve_codex_runtime_credentials
    runtime = resolve_codex_runtime_credentials()
    if (runtime.get("provider") != CODEX_PROVIDER or runtime.get("base_url") != CODEX_BASE_URL
            or not runtime.get("api_key")):
        raise ValueError("language_credentials")
    from agent.deterministic_policy import arm_deterministic_policy
    arm_deterministic_policy(lambda action: None)
    from agent.auxiliary_client import CodexAuxiliaryClient, _create_openai_client
    client = CodexAuxiliaryClient(_create_openai_client(api_key=runtime["api_key"], base_url=CODEX_BASE_URL,
                                                       max_retries=0, timeout=10), model)
    try:
        response = client.chat.completions.create(model=model,
            messages=[{"role": "system", "content": SYSTEM_PROMPT + "\nSchéma příkazu: " + canonical(COMMAND_VALIDATOR.schema)},
                      {"role": "user", "content": canonical(prepared)}],
            tools=[], timeout=10, no_progress_timeout=8)
        if (len(response.choices) != 1 or response.choices[0].message.tool_calls
                or response.choices[0].finish_reason != "stop"):
            raise ValueError("language_tools_refused")
        content = response.choices[0].message.content
        if not isinstance(content, str) or len(content.encode()) > 8192:
            raise ValueError("language_output_size")
        decision = validate_decision(decode(content.encode()), prepared)
        usage = getattr(response, "usage", None)
        measured = {"input_tokens": getattr(usage, "prompt_tokens", None),
                    "output_tokens": getattr(usage, "completion_tokens", None)}
        if any(type(v) is not int or v < 0 for v in measured.values()):
            measured = None
        return {"request_id": prepared["request_id"], "attempt_id": prepared["attempt_id"],
                "code": "ok", "decision": decision, "usage": measured}
    finally:
        client.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    args = parser.parse_args(argv)
    request = {}
    try:
        resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
        resource.setrlimit(resource.RLIMIT_CPU, (16, 16))
        resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
        resource.setrlimit(resource.RLIMIT_FSIZE, (4 * 1024**2, 4 * 1024**2))
        size_raw = sys.stdin.buffer.read(4)
        size = struct.unpack("!I", size_raw)[0]
        if not 0 < size <= 65536:
            raise ValueError("language_input_size")
        request = validate_input(decode(sys.stdin.buffer.read(size)))
        logging.disable(logging.CRITICAL)
        with open(os.devnull, "w") as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
            result = native_request(request, source=args.source, home=args.home)
    except Exception:
        result = {"request_id": request.get("request_id"), "attempt_id": request.get("attempt_id"),
                  "code": "unavailable", "decision": None, "usage": None}
    sys.stdout.buffer.write(encode(result))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
