"""Apply the optional host policy only to the exact accepted Hermes candidate."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

from scripts.capability_runtime import ROOT, trusted_bytes
from scripts.model_probe import check_candidate


def prepare(source, patch, *, apply=False):
    check_candidate(source)
    pins = json.loads(trusted_bytes(ROOT / "config/capability-runtime.json"))
    if hashlib.sha256(trusted_bytes(patch)).hexdigest() != pins["patch_sha256"]:
        raise ValueError("capability_patch_mismatch")
    actual = {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
              if (source / name).is_file() and not (source / name).is_symlink() else None
              for name in pins["source_sha256"]}
    if actual == pins["source_sha256"]:
        return False
    expected = {**pins["base_sha256"], "agent/deterministic_policy.py": None}
    if actual != expected:
        raise ValueError("capability_base_mismatch")
    subprocess.run(["git", "apply", "--check", str(patch)], cwd=source, check=True, capture_output=True)
    if apply:
        subprocess.run(["git", "apply", str(patch)], cwd=source, check=True, capture_output=True)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--patch", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        changed = prepare(args.source, args.patch, apply=args.apply)
    except Exception:
        print(json.dumps({"result": "failed", "code": "capability_preparation_failed"}))
        return 78
    print(json.dumps({"result": "passed", "changed": changed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
