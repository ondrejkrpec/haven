"""Bounded WhatsApp runtime checks and owner-terminal-only pairing. No secret output."""

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import urllib.request

from scripts.model_probe import ProbeError, private_file


RUNTIME = Path("/var/lib/haven/whatsapp")
HOME = Path("/var/lib/haven/.hermes")
SESSION = HOME / "platforms/whatsapp/session"
PINS = Path(__file__).resolve().parents[1] / "config/whatsapp-runtime.json"
SOURCE = Path("/var/lib/haven/releases/hermes-bf0203a-785f2646")
SAFE_CODES = frozenset({
    "whatsapp_pm_lock_mismatch", "whatsapp_pm_toolchain_mismatch", "whatsapp_bridge_source_mismatch",
    "whatsapp_node_version_mismatch", "whatsapp_dependencies_not_prepared",
    "whatsapp_private_directory_required", "whatsapp_private_session_required",
    "whatsapp_not_connected", "whatsapp_pairing_requires_owner_terminal", "whatsapp_profile_mismatch",
    "whatsapp_disable_before_pairing", "whatsapp_existing_session_requires_review",
})


@contextmanager
def pm_scope():
    """Read this optional store through PM; discovery deliberately ignores PATH."""
    old = os.environ.get("HERMES_RUNTIME_DIR")
    os.environ["HERMES_RUNTIME_DIR"] = str(RUNTIME / "tools")
    try:
        sys.path.insert(0, str(SOURCE))
        yield
    finally:
        if old is None:
            os.environ.pop("HERMES_RUNTIME_DIR", None)
        else:
            os.environ["HERMES_RUNTIME_DIR"] = old
        sys.path.remove(str(SOURCE))


def selected_node():
    pins = json.loads(PINS.read_text())
    if hashlib.sha256((SOURCE / "pm/lock.json").read_bytes()).hexdigest() != pins["pm_lock_sha256"]:
        raise ProbeError("whatsapp_pm_lock_mismatch")
    with pm_scope():
        import pm
        node, npm = pm.installed_package("node"), pm.installed_package("npm")
    if (node is None or npm is None or node.binary is None or npm.binary is None
            or node.version != pins["node_version"] or npm.version != pins["npm_version"]
            or not node.binary.resolve().is_relative_to((RUNTIME / "tools").resolve())
            or not npm.binary.resolve().is_relative_to((RUNTIME / "tools").resolve())):
        raise ProbeError("whatsapp_pm_toolchain_mismatch")
    return node.binary


def check_runtime():
    pins = json.loads(PINS.read_text())
    node = selected_node()
    for name, digest in pins["bridge_sha256"].items():
        path = RUNTIME / "bridge" / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ProbeError("whatsapp_bridge_source_mismatch")
    version = subprocess.check_output([str(node), "--version"], text=True,
                                      stderr=subprocess.DEVNULL, timeout=10).strip()
    if version != "v" + pins["node_version"]:
        raise ProbeError("whatsapp_node_version_mismatch")
    stamp = RUNTIME / "bridge/node_modules/.hermes-pkg-hash"
    if stamp.read_text().strip() != pins["bridge_sha256"]["package.json"][:16]:
        raise ProbeError("whatsapp_dependencies_not_prepared")
    import aiohttp  # noqa: F401 -- explicit frozen sms extra; never install at startup
    return node


def check_session(*, paired=True):
    for path in (HOME, HOME / "platforms", SESSION.parent, SESSION):
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise ProbeError("whatsapp_private_directory_required")
    # Session state includes device credentials, encryption keys and LID mapping.
    # Inspect metadata only: checks never read or print any credential contents.
    for path in SESSION.rglob("*"):
        info = path.lstat()
        expected = 0o700 if stat.S_ISDIR(info.st_mode) else 0o600
        if (path.is_symlink() or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))
                or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != expected):
            raise ProbeError("whatsapp_private_session_required")
    if paired:
        private_file(SESSION / "creds.json")


def environment(home=HOME, *, whatsapp=False):
    """Do not inherit policy overrides from a pairing shell or service environment."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("WHATSAPP_", "GATEWAY_", "NODE_", "NPM_", "npm_", "HERMES_MEDIA_"))}
    env.update(WHATSAPP_MODE="bot", WHATSAPP_DEBUG="false",
               WHATSAPP_FORWARD_OWNER_MESSAGES="false", WHATSAPP_ALLOW_ALL_USERS="false",
               GATEWAY_ALLOW_ALL_USERS="false", HOME=str(home.parent), HERMES_HOME=str(home))
    env["PATH"] = str(Path(sys.executable).parent) + ":/usr/local/bin:/usr/bin:/bin"
    if whatsapp:
        env["HERMES_RUNTIME_DIR"] = str(RUNTIME / "tools")
        with pm_scope():
            import pm
            env = pm.env_for("npm", base_env=env)
    return env


def health():
    check_runtime()
    check_session()
    # This endpoint contains session paths but no credential material. Return
    # only a fixed verdict; never proxy its response into operational artifacts.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("http://127.0.0.1:3000/health", timeout=5) as response:
        data = json.load(response)
    if data.get("status") != "connected" or data.get("session") != str(SESSION):
        raise ProbeError("whatsapp_not_connected")
    return {"result": "passed", "check": "whatsapp_connected"}


def pair():
    if os.geteuid() == 0 or not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ProbeError("whatsapp_pairing_requires_owner_terminal")
    if os.environ.get("HERMES_HOME") != str(HOME):
        raise ProbeError("whatsapp_profile_mismatch")
    node = check_runtime()
    # Do not pair over an active adapter or replace an existing linked identity.
    from ruamel.yaml import YAML
    config = YAML(typ="safe", pure=True).load(private_file(HOME / "config.yaml").read_text())
    from scripts.model_contract import check_service
    check_service(config)
    if config.get("platforms", {}).get("whatsapp", {}).get("enabled"):
        raise ProbeError("whatsapp_disable_before_pairing")
    os.umask(0o077)
    for path in (HOME / "platforms", SESSION.parent, SESSION):
        path.mkdir(mode=0o700, exist_ok=True)
    check_session(paired=False)
    if (SESSION / "creds.json").exists():
        raise ProbeError("whatsapp_existing_session_requires_review")
    os.execve(str(node), [str(node), str(RUNTIME / "bridge/bridge.js"),
                          "--pair-only", "--mode", "bot", "--session", str(SESSION)],
              environment(whatsapp=True))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "health", "pair"))
    args = parser.parse_args(argv)
    try:
        if args.mode == "pair":
            pair()
        elif args.mode == "health":
            print(json.dumps(health()))
        else:
            check_runtime()
            print(json.dumps({"result": "passed", "check": "whatsapp_runtime"}))
    except Exception as error:
        code = str(error) if isinstance(error, ProbeError) and str(error) in SAFE_CODES else "whatsapp_check_failed"
        print(json.dumps({"result": "failed", "code": code}))
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
