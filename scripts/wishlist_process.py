"""Linux browser descendant cleanup, including Chromium's detached process groups."""

import os
from pathlib import Path
import signal

GROUP = "0::/system.slice/haven-wishlist-browser.service\n"


def process_start(path):
    fields = path.read_text().rsplit(")", 1)[1].split()
    return int(fields[1]), int(fields[19])


def kill_snapshot(snapshot, proc=Path("/proc")):
    for pid, start in snapshot.items():
        if pid == os.getpid():
            continue
        try:
            if process_start(proc / str(pid) / "stat")[1] == start:
                os.kill(pid, signal.SIGKILL)
        except (OSError, ValueError, IndexError):
            pass


def descendants(root_pid, proc=Path("/proc"), *, expected_start=None):
    table = {}
    for path in proc.iterdir():
        if path.name.isdecimal():
            try:
                table[int(path.name)] = process_start(path / "stat")
            except (OSError, ValueError, IndexError):
                pass
    if root_pid not in table or expected_start is not None and table[root_pid][1] != expected_start:
        return {}
    selected = {root_pid}
    while True:
        more = {pid for pid, (parent, _) in table.items() if parent in selected}
        if more <= selected:
            break
        selected |= more
    return {pid: table[pid][1] for pid in selected - {root_pid} if pid in table}


def managed_group(proc=Path("/proc")):
    try:
        return (proc / "self/cgroup").read_text() == GROUP
    except OSError:
        return False


def clean_managed_group(proc=Path("/proc")):
    # Never sweep a developer shell, shared user cgroup or another service.
    if not managed_group(proc):
        return
    snapshot = {}
    for path in proc.iterdir():
        if path.name.isdecimal() and int(path.name) != os.getpid():
            try:
                if (path / "cgroup").read_text() == GROUP:
                    snapshot[int(path.name)] = process_start(path / "stat")[1]
            except (OSError, ValueError, IndexError):
                pass
    kill_snapshot(snapshot, proc)
