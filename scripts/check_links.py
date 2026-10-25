"""Check repository file links and generated HTML files/fragments, offline."""

from html.parser import HTMLParser
import re
from urllib.parse import unquote, urlsplit

from scripts.public_files import ROOT, public_files


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.ids = set()
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if "id" in values:
            self.ids.add(values["id"])
        for key in ("href", "src"):
            if values.get(key):
                self.links.append(values[key])


def check_sources(root=ROOT):
    count = 0
    for relative in public_files(root):
        if relative.suffix not in {".adoc", ".md"}:
            continue
        text = (root / relative).read_text(encoding="utf-8")
        if relative.suffix == ".md":
            targets = re.findall(r"\]\((<[^>]+>|[^\s)]+)\)", text)
        else:
            targets = re.findall(r"link:([^\s\[]+)\[", text)
        for target in targets:
            uri = urlsplit(target.strip("<>"))
            if uri.scheme or uri.netloc or not uri.path:
                continue
            path = (root / relative.parent / unquote(uri.path)).resolve()
            if not path.is_relative_to(root) or not path.exists():
                raise ValueError(f"Broken public source link in {relative}: {uri.path}")
            count += 1
    print(f"Public source links valid: {count}")
    return count


def check_site(site):
    site = site.resolve()
    pages = {path.resolve(): Page(path.read_text(encoding="utf-8")) for path in site.rglob("*.html")}
    if not pages:
        raise ValueError("No generated HTML pages found")
    count = 0
    for source, page in pages.items():
        for target in page.links:
            uri = urlsplit(target)
            if uri.scheme or uri.netloc:
                continue
            path = ((site if uri.path.startswith("/") else source.parent) / unquote(uri.path).lstrip("/")).resolve() if uri.path else source
            if path.is_dir():
                path /= "index.html"
            if not path.is_relative_to(site) or not path.is_file():
                raise ValueError(f"Broken generated link in {source.relative_to(site)}: {target}")
            if uri.fragment and path in pages and unquote(uri.fragment) not in pages[path].ids:
                raise ValueError(f"Broken generated fragment in {source.relative_to(site)}: {target}")
            count += 1
    print(f"Generated links valid: {count} across {len(pages)} HTML files")
    return count


if __name__ == "__main__":
    check_sources()
    check_site(ROOT / "build/site")
