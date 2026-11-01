"""Render explicit non-secret backup inputs with exact clean source revisions."""

import json
import os
from pathlib import Path
import subprocess

from scripts.backup_capture import require
from scripts.backup_runtime import validate_settings
from scripts.private_enrollment import PrivateArgumentParser, parent_directory

ROOT = Path(__file__).resolve().parents[1]


def revision(root):
    require(Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=root, text=True).strip()) == root, "repository_root_required")
    require(not subprocess.check_output(["git", "status", "--porcelain"], cwd=root), "clean_source_required")
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()


def render(instance, output):
    require(instance.is_absolute() and instance.resolve(strict=True) == instance and not instance.is_relative_to(ROOT), "explicit_private_instance_required")
    public_revision, private_revision = revision(ROOT), revision(instance)
    require((instance / "HAVEN_REVISION").read_text().strip() == public_revision, "private_revision_binding_required")
    source = instance / "ops/backup/runtime.json"
    require(not source.is_symlink() and source.stat().st_size < 16384, "invalid_backup_inputs")
    settings = json.loads(source.read_text())
    require("public_revision" not in settings and "private_revision" not in settings, "revisions_are_generated")
    settings.update(public_revision=public_revision, private_revision=private_revision)
    validate_settings(settings)
    with parent_directory(output) as (parent, name):
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        with os.fdopen(fd, "w") as stream:
            json.dump(settings, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())


def main(argv=None):
    parser = PrivateArgumentParser(description=__doc__)
    parser.add_argument("instance", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        render(args.instance, args.output)
    except Exception:
        print('{"result":"failed","code":"backup_render_refused"}')
        return 1
    print('{"result":"passed","credentials_resolved":false}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
