"""Build/verify the independent hash-pinned JSON Schema artifact for W4."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "wishlist-dependencies.json"


def files(root):
    result = {}
    for path in root.rglob("*"):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o022:
            raise ValueError("wishlist_dependency_file")
        relative = path.relative_to(root).as_posix()
        if relative == MANIFEST or "__pycache__" in path.parts:
            continue
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not 1 <= len(result) <= 2000:
        raise ValueError("wishlist_dependency_size")
    return result


def verify(root, *, requirements=ROOT / "requirements-wishlist.txt"):
    if root.is_symlink() or root.resolve() != root or not root.is_dir():
        raise ValueError("wishlist_dependency_directory")
    path = root / MANIFEST
    if path.is_symlink() or path.stat().st_size > 256 * 1024:
        raise ValueError("wishlist_dependency_manifest")
    manifest = json.loads(path.read_bytes())
    if (set(manifest) != {"schema_version", "requirements_sha256", "python", "files"}
            or manifest["schema_version"] != 1
            or manifest["requirements_sha256"] != hashlib.sha256(requirements.read_bytes()).hexdigest()
            or manifest["python"] != f"{sys.version_info.major}.{sys.version_info.minor}"
            or manifest["files"] != files(root)):
        raise ValueError("wishlist_dependency_mismatch")
    return manifest


def bundle(root):
    manifest = verify(root)
    target = Path(str(root) + ".tar.gz")
    expected = {**manifest["files"], MANIFEST: hashlib.sha256((root / MANIFEST).read_bytes()).hexdigest()}
    with target.open("xb") as stream:
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            for name in sorted(expected):
                archive.add(root / name, arcname=name, recursive=False)
    verify_bundle(root)


def verify_bundle(root):
    manifest = verify(root)
    expected = {**manifest["files"], MANIFEST: hashlib.sha256((root / MANIFEST).read_bytes()).hexdigest()}
    target = Path(str(root) + ".tar.gz")
    if target.is_symlink() or target.stat().st_size > 20 * 1024**2:
        raise ValueError("wishlist_dependency_archive")
    actual, total = {}, 0
    with tarfile.open(target, "r:gz") as archive:
        for member in archive:
            total += member.size
            if (not member.isfile() or member.name not in expected or member.name in actual
                    or total > 100 * 1024**2):
                raise ValueError("wishlist_dependency_archive")
            actual[member.name] = hashlib.sha256(archive.extractfile(member).read()).hexdigest()
    if actual != expected:
        raise ValueError("wishlist_dependency_archive")
    return manifest


def build(destination):
    if sys.version_info[:2] != (3, 14) or sys.platform != "linux":
        raise ValueError("wishlist_build_requires_linux_python314")
    destination.mkdir(mode=0o755)  # Exclusive: do not install into an older artifact.
    try:
        subprocess.run(["uv", "pip", "install", "--no-cache", "--no-config", "--python", sys.executable,
                        "--target", str(destination), "--require-hashes", "--only-binary=:all:", "--no-deps",
                        "-r", str(ROOT / "requirements-wishlist.txt")], check=True)
        (destination / ".lock").unlink(missing_ok=True)  # uv's completed install lease is not runtime code.
        manifest = {"schema_version": 1,
                    "requirements_sha256": hashlib.sha256((ROOT / "requirements-wishlist.txt").read_bytes()).hexdigest(),
                    "python": "3.14", "files": files(destination)}
        (destination / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        verify(destination)
    except BaseException:
        shutil.rmtree(destination)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("build", "verify", "bundle", "verify-bundle"))
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args(argv)
    os.umask(0o022)
    try:
        if args.mode == "build":
            build(args.directory)
        elif args.mode == "bundle":
            bundle(args.directory)
        elif args.mode == "verify-bundle":
            verify_bundle(args.directory)
        else:
            verify(args.directory)
    except Exception:
        print('{"result":"failed","code":"wishlist_dependencies_failed"}')
        return 1
    print('{"result":"passed","native_environment_modified":false}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
