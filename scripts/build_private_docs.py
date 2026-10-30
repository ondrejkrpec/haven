"""Build an explicitly selected private instance's allowlisted Markdown separately."""

import argparse
import html
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.parse import quote

from scripts.check_links import check_site
from scripts.pre_push import git
from scripts.public_files import ROOT


def build(instance, root=ROOT):
    instance = instance.absolute()
    if instance.is_relative_to(root) or instance.is_symlink() or instance.resolve() != instance:
        raise ValueError("Private sources must be a separate repository without symlink parents")
    manifest = instance / "docs/site.json"
    tracked = set(git(instance, "ls-files", "-z").split("\0"))
    if any(part.is_symlink() for part in [manifest, *manifest.parents] if part != instance.parent) or "docs/site.json" not in tracked:
        raise ValueError("A tracked private docs/site.json allowlist is required")
    selection = json.loads(manifest.read_text(encoding="utf-8"))
    if set(selection) != {"documents"} or not isinstance(selection["documents"], list) or not selection["documents"]:
        raise ValueError("Private documentation manifest needs a nonempty documents list")
    documents = []
    for name in selection["documents"]:
        relative = Path(name)
        if (relative.is_absolute() or ".." in relative.parts or relative.suffix != ".md"
                or relative.parts[0] not in {"runbooks", "instructions"} or name not in tracked):
            raise ValueError("Only explicitly tracked runbooks/instructions Markdown may be rendered")
        source = instance / relative
        if any(part.is_symlink() for part in [source, *source.parents] if part != instance.parent):
            raise ValueError("Private documentation sources must not be symlinks")
        documents.append({"path": name, "text": source.read_text(encoding="utf-8")})
    result = subprocess.run(["node", str(root / "scripts/render_private_docs.mjs")],
                            input=json.dumps({"documents": documents,
                                              "publicPages": [p.relative_to(root / "docs").as_posix()
                                                              for p in (root / "docs").rglob("*.adoc")]}),
                            text=True, capture_output=True, check=True, cwd=root)
    pages = json.loads(result.stdout)
    output = instance / "build/site"
    if (instance / "build").is_symlink() or output.is_symlink():
        raise ValueError("Private documentation output must not be a symlink")
    output.parent.mkdir(mode=0o700, exist_ok=True)
    os.chmod(output.parent, 0o700)
    with tempfile.TemporaryDirectory(prefix="private-docs-", dir=output.parent) as directory:
        staging = Path(directory)
        navigation = '<nav aria-label="Runbooks"><ul>' + ''.join(
            f'<li><a href="/{quote(page["path"])}">{html.escape(Path(page["path"]).stem)}</a></li>' for page in pages) + '</ul></nav>'
        def page(content):
            return ('<!doctype html><html lang="cs"><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width, initial-scale=1">'
                    '<meta name="referrer" content="no-referrer">'
                    '<title>Haven · Soukromé návody</title><link rel="stylesheet" href="/site.css">'
                    '<body><a class="skip" href="#main">Přejít na obsah</a>'
                    '<header><a href="/">Haven</a><span>Soukromé provozní návody</span></header>'
                    '<div class="layout">' + navigation + '<main id="main">' + content + '</main></div></body></html>')
        for item in pages:
            target = staging / item["path"]
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.write_text(page(item["html"]), encoding="utf-8")
        (staging / "index.html").write_text(page('<h1>Soukromé návody Haven</h1><p>Vyberte návod v navigaci. Obsah pochází pouze z výslovného seznamu dokumentů.</p>'), encoding="utf-8")
        shutil.copyfile(root / "docs/ui/css/site.css", staging / "site.css")
        hashes = {p.relative_to(staging).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in sorted(staging.rglob("*")) if p.is_file()}
        release_id = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        (staging / "publication.json").write_text(json.dumps({
            "visibility": "private", "public_revision": git(root, "rev-parse", "HEAD"),
            "instance_revision": git(instance, "rev-parse", "HEAD"),
            "documents": selection["documents"], "release_id": release_id,
            "source_dirty": bool(git(instance, "status", "--porcelain")),
            "files_sha256": hashes,
        }, indent=2) + "\n", encoding="utf-8")
        check_site(staging)
        if output.exists():
            shutil.rmtree(output)
        shutil.copytree(staging, output)
        os.chmod(output, 0o700)
    print(f"Private documentation built separately: {len(pages)} pages.")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instance", type=Path)
    build(parser.parse_args().instance)
