"""Install pinned Linux check binaries after verifying their archive hashes."""

import hashlib
import io
import json
import platform
import tarfile
from urllib.request import urlopen

from scripts.public_files import ROOT


def install():
    architecture = {"amd64": "x86_64", "arm64": "aarch64"}.get(platform.machine(), platform.machine())
    if platform.system() != "Linux" or architecture not in {"x86_64", "aarch64"}:
        raise SystemExit("Pinned check binaries support Linux x86_64 and aarch64")
    pins = json.loads((ROOT / "scripts/check-tools.json").read_text(encoding="utf-8"))
    destination = ROOT / "build/tools"
    if destination.is_symlink() or destination.parent.is_symlink():
        raise SystemExit("Check tool destination must not be a symlink")
    destination.mkdir(parents=True, exist_ok=True)
    for name, tool in pins.items():
        pin = tool["platforms"][architecture]
        with urlopen(pin["url"], timeout=60) as response:
            archive = response.read(64 * 1024 * 1024 + 1)
        if hashlib.sha256(archive).hexdigest() != pin["sha256"]:
            raise SystemExit(f"Archive checksum mismatch: {name}")
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
            member = bundle.getmember(name)
            if not member.isfile() or member.size > 64 * 1024 * 1024:
                raise SystemExit(f"Unexpected binary archive member: {name}")
            binary = bundle.extractfile(member).read()
        output = destination / name
        if output.is_symlink():
            raise SystemExit("Check tool must not be a symlink")
        output.write_bytes(binary)
        output.chmod(0o755)
        print(f"Installed {name} {tool['version']} (verified SHA-256)")


if __name__ == "__main__":
    install()
