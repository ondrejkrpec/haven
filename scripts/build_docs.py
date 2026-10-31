"""Export public guides to Antora without moving their canonical source files."""

import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import unquote, urlsplit

from scripts.public_files import ROOT, public_files

# Deliberately bounded downloads, not a copy of the public or private repository.
ATTACHMENTS = (
    "README.md", "CONTRIBUTING.md", "AGENTS.md", "LICENSE", "config/defaults.yaml",
    "examples/instance.yaml", "examples/scaleway-backup.json", "examples/backup-capture.json", "schemas/instance-v1.schema.json",
    "research/h0.1/provenance.json", "research/h0.1/UPSTREAM-LICENSE",
    "research/h0.1/pins.json", "research/h0.1/patches/upstream-117820.patch",
    "research/h0.1/evidence/h0.2-portable-isolation.json",
    ".github/workflows/checks.yml",
    "research/h0.5/acceptance.json", "research/h0.5/source-manifest.json",
)
LOCAL_LINK = re.compile(r"link:([^\s\[]+)\[")
ATTACHMENT_ALIASES = {
    "LICENSE": "LICENSE.txt",
    "research/h0.1/UPSTREAM-LICENSE": "research/h0.1/UPSTREAM-LICENSE.txt",
}


def prepare_content(root, destination):
    """Create a native Antora component from selected Git-eligible public files."""
    eligible = set(public_files(root))
    guides = {
        path: path.relative_to("docs").as_posix()
        for path in eligible
        if path.parts[0] == "docs" and path.suffix == ".adoc" and path.name != "nav.adoc"
    }
    guides[Path("research/h0.1/README.adoc")] = "research/h0.1/README.adoc"
    required = {*guides, *(Path(path) for path in ATTACHMENTS), Path("docs/nav.adoc"), Path("docs/antora.yml")}
    if not required <= eligible:
        raise ValueError("A required public documentation input is missing or ignored")
    module = destination / "modules/ROOT"
    # Antora omits dot-directories. Extensionless xrefs become local fragments,
    # so expose license downloads with .txt names while retaining source paths.
    attachments = {
        Path(path): ATTACHMENT_ALIASES.get(path, path.removeprefix("."))
        for path in ATTACHMENTS
    }

    def rewrite(source, text):
        # No include indirection: the exporter reads only the declared inputs.
        if re.search(r"include::", text):
            raise ValueError(f"Includes are unsupported in exported guides: {source}")

        def replace(match):
            uri = urlsplit(match[1])
            if uri.scheme or uri.netloc:
                return match[0]
            target = (root / source.parent / unquote(uri.path)).resolve()
            if not target.is_relative_to(root):
                raise ValueError(f"Documentation link escapes public sources: {source}")
            relative = target.relative_to(root)
            if relative in guides:
                address = guides[relative]
            elif relative in attachments:
                address = "attachment$" + attachments[relative]
            else:
                raise ValueError(f"Undeclared documentation target in {source}: {relative}")
            if uri.query:
                raise ValueError(f"Local documentation queries are unsupported: {source}")
            if uri.fragment:
                address += "#" + uri.fragment
            # A ROOT-qualified ID also works from a nested guide.
            return "xref:ROOT:" + address + "["

        return LOCAL_LINK.sub(replace, text)

    for source, target in guides.items():
        output = module / "pages" / target
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rewrite(source, (root / source).read_text(encoding="utf-8")), encoding="utf-8")
    for source, target in attachments.items():
        output = module / "attachments" / target
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / source, output)
    shutil.copyfile(root / "docs/antora.yml", destination / "antora.yml")
    shutil.copyfile(root / "docs/nav.adoc", module / "nav.adoc")
    return len(guides)


def build(root=ROOT):
    import yaml

    output = root / "build/site"
    if (root / "build").is_symlink() or output.is_symlink():
        raise ValueError("Documentation output must not be a symlink")
    antora = root / "node_modules/.bin/antora"
    if not antora.is_file():
        raise ValueError("Install documentation dependencies with npm ci --ignore-scripts")
    with tempfile.TemporaryDirectory(prefix="haven-docs-") as temporary:
        workspace = Path(temporary)
        content = workspace / "content"
        content.mkdir()
        pages = prepare_content(root, content)
        ui = workspace / "ui"
        for source in public_files(root):
            if source.is_relative_to("docs/ui"):
                target = ui / source.relative_to("docs/ui")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / source, target)
        # Antora 3.2 supports an unborn local repository; no commits are made.
        subprocess.run(["git", "init", "--quiet", "-b", "main", str(content)], check=True)
        playbook = yaml.safe_load((root / "antora-playbook.yml").read_text(encoding="utf-8"))
        playbook["content"]["sources"] = [{"url": str(content), "branches": "HEAD", "edit_url": False}]
        playbook["ui"]["bundle"]["url"] = str(ui)
        playbook["output"]["dir"] = str(output)
        playbook_path = workspace / "playbook.yml"
        playbook_path.write_text(json.dumps(playbook), encoding="utf-8")
        subprocess.run([str(antora), str(playbook_path)], cwd=root, check=True)
    print(f"Documentation built: {pages} pages in build/site")


if __name__ == "__main__":
    build()
