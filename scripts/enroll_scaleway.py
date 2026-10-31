"""Store an application's S3 key pair locally using hidden prompts; no network."""

import getpass
from pathlib import Path
import re
import sys

from scripts.private_enrollment import PrivateArgumentParser, collect_files, hidden_value


def collect(destination, prompt=hidden_value):
    def read_values():
        access = prompt("Scaleway application access key (hidden): ")
        secret = prompt("Scaleway application secret key (hidden): ")
        # Only reject unsafe/obviously mistaken input. The API documents these as
        # strings, not a stable character count. No account ownership is inferred.
        if (any(not re.fullmatch(r"[A-Za-z0-9_+/=-]{8,128}", value)
                for value in (access, secret)) or access == secret):
            raise ValueError("invalid_key_pair")
        return {"access-key": access, "secret-key": secret}
    collect_files(destination, read_values)


def main(argv=None):
    parser = PrivateArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True,
                        help="new absolute directory outside Git under an existing owned parent")
    args = parser.parse_args(argv)
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        print("Interactive terminal required; no keys were requested.", file=sys.stderr)
        return 1
    try:
        collect(args.directory)
    except (OSError, ValueError, EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        print("Enrollment did not complete. Check the new destination and key pair; "
              "no account request was sent.", file=sys.stderr)
        return 1
    print("Application key pair saved locally. No request was sent; permissions are not verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
