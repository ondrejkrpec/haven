"""Publish a checked public main revision as prebuilt GitHub Pages content."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from scripts.pre_push import git, require_clean
from scripts.public_files import ROOT

PAGES_REF = "refs/heads/gh-pages"


def remote_revision(root, reference):
    rows = git(root, "ls-remote", "--refs", "origin", reference).splitlines()
    return rows[0].split()[0] if rows else ""


def require_main(root, revision):
    require_clean(root, revision)
    if git(root, "symbolic-ref", "--short", "HEAD") != "main":
        raise ValueError("Documentation publication requires the main branch")
    if remote_revision(root, "refs/heads/main") != revision:
        raise ValueError("Remote main changed; synchronize and check the new revision")


def copy_site(site, destination, revision):
    """Copy only generated regular files; never expand links or source directories."""
    if site.is_symlink() or not (site / "index.html").is_file():
        raise ValueError("A generated site with index.html is required")
    hashes = {}
    for source in sorted(site.rglob("*")):
        relative = source.relative_to(site)
        if source.is_symlink() or any(part.startswith(".") for part in relative.parts):
            raise ValueError("Unexpected hidden file or symlink in generated site")
        if source.is_dir():
            continue
        if not source.is_file():
            raise ValueError("Only regular site files may be published")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        hashes[relative.as_posix()] = hashlib.sha256(target.read_bytes()).hexdigest()
    (destination / ".nojekyll").touch()
    (destination / "publication.json").write_text(json.dumps({
        "source_revision": revision,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "files_sha256": hashes,
    }, indent=2) + "\n", encoding="utf-8")


def publish(root=ROOT):
    revision = git(root, "rev-parse", "HEAD")
    require_main(root, revision)
    # A receipt from an earlier push cannot establish the current target or build.
    subprocess.run([sys.executable, "-m", "scripts.check"], cwd=root, check=True)
    require_main(root, revision)
    previous = remote_revision(root, PAGES_REF)
    with tempfile.TemporaryDirectory(prefix="haven-public-pages-") as directory:
        artifact = Path(directory)
        git(artifact, "init", "--quiet", "-b", "gh-pages")
        git(artifact, "remote", "add", "origin", git(root, "remote", "get-url", "origin"))
        if previous:
            git(artifact, "fetch", "--quiet", "origin", PAGES_REF)
            if git(artifact, "rev-parse", "FETCH_HEAD") != previous:
                raise ValueError("Pages branch changed while preparing publication")
            # Retain artifact history without checking out old, untrusted files.
            git(artifact, "update-ref", PAGES_REF, previous)
        copy_site(root / "build/site", artifact, revision)
        git(artifact, "add", "--all")
        git(artifact, "-c", "user.name=" + git(root, "var", "GIT_AUTHOR_IDENT").split(" <")[0],
            "-c", "user.email=" + git(root, "config", "user.email"),
            "commit", "--quiet", "-m", "Publish public documentation for " + revision)
        require_main(root, revision)
        # A concurrent publisher must never be silently overwritten.
        git(artifact, "push", "--porcelain", "--force-with-lease=" + PAGES_REF + ":" + previous,
            "origin", "HEAD:" + PAGES_REF)
        published = git(artifact, "rev-parse", "HEAD")
    print(f"Public documentation artifact {published} published from {revision}.")
    return published


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--if-configured", action="store_true")
    args = parser.parse_args()
    # Hooks may inherit repository-local Git variables; the artifact repository
    # must resolve its own metadata, just like the existing pre-push fixtures.
    for name in subprocess.check_output(["git", "rev-parse", "--local-env-vars"], text=True).splitlines():
        import os
        os.environ.pop(name, None)
    if args.if_configured:
        setting = subprocess.run(["git", "config", "--local", "--bool", "haven.publishDocs"],
                                 cwd=ROOT, capture_output=True, text=True, check=False)
        if setting.stdout.strip() != "true" or git(ROOT, "branch", "--show-current") != "main":
            return
    try:
        publish()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"Documentation publication failed: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
