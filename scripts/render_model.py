"""Render explicit Haven inputs to a fresh native Hermes YAML file, without credentials."""

import argparse
import os
from pathlib import Path
import sys

import yaml

from scripts.model_contract import ModelConfigError, native_config, service_config
from scripts.validate_config import ConfigError, load_config


def render(config, output, overrides=(), *, service=False):
    native = (service_config if service else native_config)(load_config(Path(config), overrides))
    # O_EXCL refuses an existing file or symlink; do not clobber runtime state.
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        yaml.safe_dump(native, stream, allow_unicode=True, sort_keys=False)
    return native


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--override", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--service", action="store_true", help="Render the bounded supervised service profile")
    args = parser.parse_args(argv)
    try:
        render(args.config, args.output, args.override, service=args.service)
    except (ConfigError, ModelConfigError) as error:
        print(f"Cannot render runtime: {error}", file=sys.stderr)
        return 1
    except OSError:
        print("Cannot create runtime output: use a fresh writable file path.", file=sys.stderr)
        return 1
    print("Native runtime configuration rendered; credentials were not read.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
