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


def navigation(selection):
    """Validate display metadata without admitting additional source files."""
    documents = selection.get("documents")
    if (not isinstance(documents, list) or not documents
            or any(not isinstance(name, str) for name in documents)
            or len(set(documents)) != len(documents)
            or set(selection) - {"documents", "home", "navigation"}):
        raise ValueError("Private documentation needs unique documents and supported metadata")
    home = selection.get("home")
    if home is not None and home not in documents:
        raise ValueError("Private home must be a selected document")
    items = selection.get("navigation")
    if items is None:
        return [{"title": Path(name).stem, "document": name} for name in documents]
    seen = []

    def validate(nodes, depth=0):
        if not isinstance(nodes, list) or not nodes or depth > 4:
            raise ValueError("Private navigation must be a bounded nonempty tree")
        for node in nodes:
            if (not isinstance(node, dict) or set(node) - {"title", "document", "children"}
                    or not isinstance(node.get("title"), str) or not node["title"].strip()
                    or not ({"document", "children"} & set(node))):
                raise ValueError("Invalid private navigation item")
            if "document" in node:
                if node["document"] not in documents:
                    raise ValueError("Navigation may reference only selected documents")
                seen.append(node["document"])
            if "children" in node:
                validate(node["children"], depth + 1)

    validate(items)
    if sorted(seen) != sorted(documents):
        raise ValueError("Navigation must expose each selected document exactly once")
    return items


def render_navigation(items, active=None):
    def contains(node):
        return node.get("document") == active or any(contains(child) for child in node.get("children", []))

    rows = []
    for item in items:
        label = html.escape(item["title"])
        if "document" in item:
            address = quote(str(Path(item["document"]).with_suffix(".html")))
            current = ' aria-current="page"' if item["document"] == active else ""
            label = f'<a href="/{address}"{current}>{label}</a>'
        children = render_navigation(item["children"], active) if "children" in item else ""
        if "children" in item and "document" not in item:
            opened = ' open' if contains(item) else ''
            rows.append('<li><details' + opened + '><summary>' + label + '</summary>' + children + '</details></li>')
            continue
        rows.append('<li>' + label + children + '</li>')
    return '<ul>' + ''.join(rows) + '</ul>'


def build(instance, root=ROOT):
    instance = instance.absolute()
    if instance.is_relative_to(root) or instance.is_symlink() or instance.resolve() != instance:
        raise ValueError("Private sources must be a separate repository without symlink parents")
    manifest = instance / "docs/site.json"
    tracked = set(git(instance, "ls-files", "-z").split("\0"))
    if any(part.is_symlink() for part in [manifest, *manifest.parents] if part != instance.parent) or "docs/site.json" not in tracked:
        raise ValueError("A tracked private docs/site.json allowlist is required")
    selection = json.loads(manifest.read_text(encoding="utf-8"))
    items = navigation(selection)
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
        def page(content, active=None):
            menu = '<nav aria-label="Průvodce">' + render_navigation(items, active) + '</nav>'
            return ('<!doctype html><html lang="cs"><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width, initial-scale=1">'
                    '<meta name="referrer" content="no-referrer">'
                    '<title>Haven · Průvodce</title><link rel="stylesheet" href="/site.css">'
                    '<body><a class="skip" href="#main">Přejít na obsah</a>'
                    '<header><a href="/">Haven</a><span>Průvodce mojí instancí</span></header>'
                    '<div class="layout">' + menu + '<main id="main"><article>' + content + '</article></main></div></body></html>')
        for item in pages:
            target = staging / item["path"]
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.write_text(page(item["html"], str(Path(item["path"]).with_suffix(".md"))), encoding="utf-8")
        home = selection.get("home")
        homepage = next((item["html"] for item in pages if home and item["path"] == str(Path(home).with_suffix(".html"))),
                        '<h1>Soukromé návody Haven</h1><p>Vyberte návod v navigaci.</p>')
        (staging / "index.html").write_text(page(homepage, home), encoding="utf-8")
        shutil.copyfile(root / "docs/ui/css/site.css", staging / "site.css")
        hashes = {p.relative_to(staging).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in sorted(staging.rglob("*")) if p.is_file()}
        provenance = {
            "visibility": "private", "public_revision": git(root, "rev-parse", "HEAD"),
            "instance_revision": git(instance, "rev-parse", "HEAD"),
            "files_sha256": hashes,
        }
        release_id = hashlib.sha256(json.dumps(provenance, sort_keys=True).encode()).hexdigest()
        (staging / "publication.json").write_text(json.dumps({
            **provenance, "documents": selection["documents"], "release_id": release_id,
            "source_dirty": bool(git(instance, "status", "--porcelain")),
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
