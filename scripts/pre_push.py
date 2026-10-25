"""Gate local pushes on a clean, checked revision without GitHub Actions."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

from scripts.public_files import ROOT


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def require_clean(root, revision):
    if git(root, "rev-parse", "HEAD") != revision or git(root, "status", "--porcelain"):
        raise ValueError("Push checks require the committed HEAD and a clean checkout, including untracked sources.")


def run_check(command, cwd):
    subprocess.run(command, cwd=cwd, check=True)


def check_push(lines, root=ROOT, public=ROOT, instance=False):
    head = git(root, "rev-parse", "HEAD")
    revisions = []
    for line in lines:
        fields = line.split()
        if len(fields) != 4:
            raise ValueError("Invalid pre-push input")
        local_ref, revision, _, _ = fields
        if revision.strip("0") == "":
            continue
        commit = git(root, "rev-parse", revision + "^{commit}")
        if commit != head:
            raise ValueError("Check out the revision being pushed before running push checks.")
        revisions.append((local_ref, commit))
    if not revisions:
        print("No source revision to check.")
        return
    require_clean(root, head)
    public_head = git(public, "rev-parse", "HEAD")
    require_clean(public, public_head)
    if instance:
        if (root / "HAVEN_REVISION").read_text(encoding="utf-8").strip() != public_head:
            raise ValueError("HAVEN_REVISION must match the clean public checkout used for instance validation.")
    run_check([sys.executable, "-m", "scripts.check"], public)
    if instance:
        run_check([sys.executable, "scripts/validate_config.py", str(root / "config/instance.yaml")], public)
        scanner = public / "build/tools/gitleaks"
        for mode in ["dir", "git"]:
            command = [str(scanner), mode, str(root), "--config", str(public / ".gitleaks.toml"),
                       "--redact", "--no-banner", "--ignore-gitleaks-allow"]
            if mode == "git":
                command.append("--log-opts=--all")
            run_check(command, public)
    require_clean(public, public_head)
    require_clean(root, head)
    # Git metadata keeps receipts outside source candidates and docs build inputs.
    receipt = Path(git(root, "rev-parse", "--absolute-git-dir")) / "haven-checks/last-success.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({
        "revision": head, "public_revision": public_head,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "private-instance" if instance else "public",
        "result": "passed",
    }, indent=2) + "\n", encoding="utf-8")
    print(f"Push checks passed for {head}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", type=Path)
    parser.add_argument("remote", nargs="?")
    parser.add_argument("url", nargs="?")
    args = parser.parse_args()
    try:
        # Git hooks can inherit repository-local variables. Other repositories
        # and disposable test fixtures must resolve their own Git metadata.
        for name in subprocess.check_output(["git", "rev-parse", "--local-env-vars"], text=True).splitlines():
            os.environ.pop(name, None)
        check_push(sys.stdin, root=args.instance.resolve() if args.instance else ROOT,
                   instance=args.instance is not None)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"Push checks failed: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
