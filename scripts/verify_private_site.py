"""Verify the exact private artifact before copying it to the internal web host."""

import argparse
import hashlib
import json
from pathlib import Path


def verify(site, revision):
    if site.resolve() != site.absolute():
        raise ValueError("Private site must not have symlink parents")
    metadata = site / "publication.json"
    if metadata.is_symlink():
        raise ValueError("Private metadata must not be a symlink")
    manifest = json.loads(metadata.read_text(encoding="utf-8"))
    if manifest["visibility"] != "private" or manifest["source_dirty"] or manifest["public_revision"] != revision:
        raise ValueError("Private artifact must come from clean private sources and the selected public revision")
    actual = {}
    for source in sorted(site.rglob("*")):
        if source.is_symlink():
            raise ValueError("Private artifact must not contain symlinks")
        if source.is_file() and source != metadata:
            actual[source.relative_to(site).as_posix()] = hashlib.sha256(source.read_bytes()).hexdigest()
    provenance = {key: manifest[key] for key in ("visibility", "public_revision", "instance_revision", "files_sha256")}
    if (actual != manifest["files_sha256"] or "index.html" not in actual
            or hashlib.sha256(json.dumps(provenance, sort_keys=True).encode()).hexdigest() != manifest["release_id"]):
        raise ValueError("Private artifact contents differ from its manifest")
    print("Private artifact integrity verified.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("site", type=Path)
    parser.add_argument("revision")
    args = parser.parse_args()
    verify(args.site, args.revision)
