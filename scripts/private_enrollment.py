"""Hidden terminal input and exclusive private-file enrollment outside Git."""

import argparse
from contextlib import contextmanager
import getpass
import os
from pathlib import Path
import re
import stat
import sys
import warnings

DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally repeats unknown arguments, potentially pasted secrets.
        self.exit(2, "Invalid arguments. Use --help; enter secrets only at the hidden prompts.\n")


def exists_at(directory, name):
    try:
        return os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None


def check_parent(directory):
    """Reject unsafe writable ancestors and actual Git metadata, not values."""
    info = os.fstat(directory)
    if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
        raise ValueError("untrusted_parent")
    marker = exists_at(directory, ".git")
    if marker is not None:
        if not stat.S_ISDIR(marker.st_mode):
            raise ValueError("git_worktree")
        child = os.open(".git", DIRECTORY_FLAGS, dir_fd=directory)
        try:
            if exists_at(child, "HEAD") is not None or exists_at(child, "commondir") is not None:
                raise ValueError("git_worktree")
        finally:
            os.close(child)
    # Bare repositories have no .git wrapper.
    if all(exists_at(directory, name) is not None for name in ("HEAD", "objects", "refs")):
        raise ValueError("git_repository")


@contextmanager
def parent_directory(destination):
    """Anchor traversal to no-follow directory descriptors, including at creation."""
    destination = Path(destination)
    if not destination.is_absolute() or ".." in destination.parts or destination == Path("/"):
        raise ValueError("absolute_destination_required")
    descriptor = os.open("/", DIRECTORY_FLAGS)
    try:
        check_parent(descriptor)
        for part in destination.parts[1:-1]:
            child = os.open(part, DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            check_parent(descriptor)
        parent = os.fstat(descriptor)
        if parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
            raise ValueError("owned_parent_required")
        if exists_at(descriptor, destination.name) is not None:
            raise ValueError("new_destination_required")
        yield descriptor, destination.name
    finally:
        os.close(descriptor)


def hidden_value(prompt):
    # getpass normally falls back to echoed stdin. That fallback is forbidden.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass(prompt, stream=sys.stderr)


def collect_files(destination, read_values):
    with parent_directory(destination) as (parent, name):
        files = read_values()
        if (not isinstance(files, dict) or not 1 <= len(files) <= 8
                or any(not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9.-]{0,63}", key)
                       for key in files)
                or any(not isinstance(value, str) or not 1 <= len(value) <= 512
                       or any(not 32 <= ord(char) <= 126 for char in value)
                       for value in files.values())):
            raise ValueError("invalid_private_files")
        # Detect path changes while the owner was interacting with the terminal.
        with parent_directory(destination) as (current, _):
            before, after = os.fstat(parent), os.fstat(current)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise ValueError("parent_changed")
        # Validate all values before creating anything, and never overwrite.
        os.mkdir(name, mode=0o700, dir_fd=parent)
        directory = None
        created = []
        try:
            directory = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
            os.fchmod(directory, 0o700)
            for filename, value in files.items():
                descriptor = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                     | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
                created.append(filename)
                with os.fdopen(descriptor, "wb") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write((value + "\n").encode("ascii"))
                    stream.flush()
                    os.fsync(stream.fileno())
            os.fsync(directory)
            os.fsync(parent)
        except BaseException:
            # Remove only files created in this run; never recursively erase a path.
            if directory is not None:
                for filename in created:
                    try:
                        os.unlink(filename, dir_fd=directory)
                    except OSError:
                        pass
            try:
                os.rmdir(name, dir_fd=parent)
            except OSError:
                pass
            raise
        finally:
            if directory is not None:
                os.close(directory)

