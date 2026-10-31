"""Store a separately retained recovery password locally, without initializing a repository."""

import getpass
from pathlib import Path
import sys

from scripts.private_enrollment import PrivateArgumentParser, collect_files, hidden_value


def collect(destination, prompt=hidden_value):
    def read_values():
        first = prompt("Restic recovery password from your password manager (hidden): ")
        second = prompt("Repeat recovery password (hidden): ")
        if (first != second or not 32 <= len(first) <= 256
                or any(not 32 <= ord(char) <= 126 for char in first)):
            raise ValueError("invalid_recovery_password")
        return {"password": first}
    collect_files(destination, read_values)


def main(argv=None):
    parser = PrivateArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args(argv)
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        print("Interactive terminal required; no password was requested.", file=sys.stderr)
        return 1
    try:
        collect(args.directory)
    except (OSError, ValueError, EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        print("Enrollment did not complete. Check the new destination and matching 32–256 character "
              "ASCII password. No repository was initialized.", file=sys.stderr)
        return 1
    print("Recovery password saved locally. Retain its independent password-manager copy; "
          "no repository was initialized.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
