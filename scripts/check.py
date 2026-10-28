"""Run the public foundation checks used locally and by GitHub Actions."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from scripts.public_files import ROOT, public_files


def run(*command, **kwargs):
    print("+ " + " ".join(map(str, command)), flush=True)
    subprocess.run(list(map(str, command)), cwd=kwargs.pop("cwd", ROOT), check=True, **kwargs)


def main():
    files = public_files()
    python = sys.executable
    pins = json.loads((ROOT / "scripts/check-tools.json").read_text(encoding="utf-8"))
    for tool, pin in pins.items():
        binary = ROOT / "build/tools" / tool
        if not binary.is_file():
            raise SystemExit("Install check tools: python -m scripts.install_check_tools")
        version = subprocess.check_output([str(binary), "--version"], text=True)
        if pin["version"] not in version:
            raise SystemExit(f"Unexpected {tool} version; reinstall check tools")
    run(python, "-m", "ruff", "check", "scripts", "tests")
    yaml_files = [str(path) for path in files if path.suffix in {".yaml", ".yml"} or path.name.endswith(".yaml.example")]
    run(python, "-m", "yamllint", "--strict", *yaml_files)
    ansible_env = {**os.environ, "ANSIBLE_CONFIG": str(ROOT / "ops/ansible.cfg"),
                   "ANSIBLE_HOME": str(ROOT / "build/ansible"),
                   "PATH": str(Path(python).parent) + os.pathsep + os.environ.get("PATH", "")}
    for playbook in ("bootstrap", "verify", "reboot"):
        run(Path(python).parent / "ansible-playbook", "-i", "ops/inventory.example.yml",
            f"ops/{playbook}.yml", "--syntax-check", env=ansible_env)
    run(Path(python).parent / "ansible-lint", "--offline", "ops/bootstrap.yml",
        "ops/verify.yml", "ops/reboot.yml", env=ansible_env)
    run(ROOT / "build/tools/actionlint", "-shellcheck=", "-pyflakes=", ".github/workflows/checks.yml")
    run(python, "scripts/validate_config.py", "examples/instance.yaml")
    run(python, "scripts/validate_config.py", "templates/private/config/instance.yaml.example")
    run(python, "-m", "unittest", "discover", "-s", "tests", "-v")
    run("sha256sum", "-c", "SHA256SUMS", cwd=ROOT / "research/h0.1")
    # Include tracked and untracked public candidates, without ignored local data.
    with tempfile.TemporaryDirectory(prefix="haven-secret-scan-") as temporary:
        snapshot = Path(temporary)
        for relative in files:
            destination = snapshot / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, destination)
        run(ROOT / "build/tools/gitleaks", "dir", snapshot, "--config", ROOT / ".gitleaks.toml",
            "--redact", "--no-banner", "--ignore-gitleaks-allow")
    # Full history is fetched in CI. An unborn local repository has no history.
    if subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=ROOT,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        run(ROOT / "build/tools/gitleaks", "git", ROOT, "--config", ROOT / ".gitleaks.toml",
            "--log-opts=--all", "--redact", "--no-banner", "--ignore-gitleaks-allow")
    else:
        print("No commits: history scan skipped; public candidate files scanned.", flush=True)
    run(python, "-m", "scripts.build_docs")
    run(python, "-m", "scripts.check_links")
    run("git", "diff", "--check")
    print("Public foundation checks passed.")


if __name__ == "__main__":
    main()
