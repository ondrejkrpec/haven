"""Collect two Healthchecks UUID URLs locally, without echo, network or Git writes."""

import getpass
from pathlib import Path
import re
import sys

from scripts.private_enrollment import PrivateArgumentParser, collect_files, hidden_value

URL_PATTERN = re.compile(
    r"https://hc-ping\.com/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
NAMES = ("backup.url", "service.url")


def hidden_url(prompt):
    return hidden_value(prompt)


def collect(destination, prompt=hidden_url):
    def read_values():
        urls = (prompt("Healthchecks haven-backup UUID URL (hidden): "),
                prompt("Healthchecks haven-service UUID URL (hidden): "))
        if any(not URL_PATTERN.fullmatch(value) for value in urls) or urls[0] == urls[1]:
            raise ValueError("distinct_uuid_urls_required")
        return dict(zip(NAMES, urls, strict=True))
    collect_files(destination, read_values)


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
