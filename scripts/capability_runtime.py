"""Verify the exact optional host guard and the only permitted local plugin."""

import hashlib
import json
from pathlib import Path
import stat

from scripts.model_contract import HERMES_COMMIT
from scripts.model_probe import ProbeError

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_FILES = ("plugin.yaml", "__init__.py")


def trusted_bytes(path, *, owner=None):
    info = path.lstat()
    if owner is None:
        owner = ROOT.stat().st_uid
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner
            or info.st_mode & 0o022 or info.st_size > 4 * 1024 * 1024):
        raise ProbeError("capability_file_untrusted")
    return path.read_bytes()


def check_runtime(source, home):
    pins = json.loads(trusted_bytes(ROOT / "config/capability-runtime.json"))
    if pins["hermes_commit"] != HERMES_COMMIT:
        raise ProbeError("capability_revision_mismatch")
    for name, expected in pins["source_sha256"].items():
        if hashlib.sha256(trusted_bytes(source / name, owner=source.stat().st_uid)).hexdigest() != expected:
            raise ProbeError("capability_source_mismatch")
    directory = home / "plugins"
    if directory.is_symlink() or {p.name for p in directory.iterdir()} != {"haven-capabilities"}:
        raise ProbeError("unexpected_service_plugin")
    plugin = directory / "haven-capabilities"
    if plugin.is_symlink() or {p.name for p in plugin.iterdir()} != set(PLUGIN_FILES):
        raise ProbeError("unexpected_service_plugin")
    for name in PLUGIN_FILES:
        if trusted_bytes(plugin / name) != trusted_bytes(ROOT / "plugins/haven-capabilities" / name):
            raise ProbeError("capability_plugin_mismatch")
    from scripts.capability_policy import load_registry
    trusted_bytes(ROOT / "config/capabilities.json")
    load_registry()
