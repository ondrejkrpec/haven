"""Collect two Healthchecks UUID URLs locally, without echo, network or Git writes."""

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
URL_PATTERN = re.compile(
    r"https://hc-ping\.com/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
NAMES = ("backup.url", "service.url")


class PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally repeats unknown arguments, potentially pasted secrets.
        self.exit(2, "Invalid arguments. Use --help; enter URLs only at the hidden prompts.\n")


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


def hidden_url(prompt):
    # getpass normally falls back to echoed stdin. That fallback is forbidden.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass(prompt, stream=sys.stderr)


def collect(destination, prompt=hidden_url):
    with parent_directory(destination) as (parent, name):
        urls = (
            prompt("Healthchecks haven-backup UUID URL (hidden): "),
            prompt("Healthchecks haven-service UUID URL (hidden): "),
        )
        if any(not URL_PATTERN.fullmatch(value) for value in urls) or urls[0] == urls[1]:
            raise ValueError("distinct_uuid_urls_required")
        # Detect path changes while the owner was interacting with the terminal.
        with parent_directory(destination) as (current, _):
            before, after = os.fstat(parent), os.fstat(current)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise ValueError("parent_changed")
        # Validate both inputs before creating anything, and never overwrite.
        os.mkdir(name, mode=0o700, dir_fd=parent)
        directory = None
        created = []
        try:
            directory = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
            os.fchmod(directory, 0o700)
            for filename, value in zip(NAMES, urls, strict=True):
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


def main(argv=None):
    parser = PrivateArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True,
                        help="new absolute directory under an existing owned parent, outside Git")
    args = parser.parse_args(argv)
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        print("Interactive terminal required; no URLs were requested.", file=sys.stderr)
        return 1
    try:
        collect(args.directory)
    except (OSError, ValueError, EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        print("Enrollment did not complete. Check the destination and UUID URL format; "
              "no monitoring was activated.", file=sys.stderr)
        return 1
    print("Two private URL files saved locally. No request was sent; monitoring is not activated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
