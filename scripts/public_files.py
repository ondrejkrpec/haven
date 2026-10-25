"""Enumerate Git-eligible public files without following filesystem links."""

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def public_files(root=ROOT):
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=True, stdout=subprocess.PIPE,
    )
    paths = sorted({Path(value.decode("utf-8")) for value in result.stdout.split(b"\0") if value})
    files = []
    for relative in paths:
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Public source path escapes the repository")
        path = root / relative
        if any(part.is_symlink() for part in [path, *path.parents] if part != root.parent):
            raise ValueError(f"Public source contains a symlink: {relative}")
        if path.is_file():
            files.append(relative)
    return files
