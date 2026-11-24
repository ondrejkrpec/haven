"""Root-only bounded pre-upgrade file receipt; never copies databases or credentials."""

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

MODULES = ("wishlist_store", "wishlist_domain", "wishlist_contract", "wishlist_protocol", "wishlist_socket",
           "wishlist_service", "wishlist_web_domain", "wishlist_web_gateway", "wishlist_dependencies",
           "wishlist_operations", "wishlist_observability", "wishlist_language", "wishlist_language_contract",
           "wishlist_model", "wishlist_telegram", "wishlist_collection", "wishlist_media", "wishlist_alza_history")
FILES = (
    *(f"{root}/scripts/{module}.py" for root in ("/opt/haven-wishlist", "/var/lib/haven/haven-tools") for module in MODULES),
    *(f"/opt/haven-wishlist/scripts/{module}.py" for module in
      ("wishlist_product_preview", "wishlist_collector", "wishlist_fetch", "wishlist_extract",
       "wishlist_interpreter", "wishlist_model", "wishlist_intimissimi")),
    "/opt/haven-wishlist/schemas/wishlist-request-v1.schema.json",
    "/var/lib/haven/haven-tools/schemas/wishlist-request-v1.schema.json",
    "/opt/haven-wishlist/requirements-web.txt",
    *(f"/usr/local/lib/haven-backup/scripts/{name}.py" for name in ("backup_runtime", "wishlist_store", "wishlist_operations", "wishlist_observability")),
    "/var/lib/haven-wishlist/config.json", "/etc/haven-wishlist-web.json",
    *(f"/etc/systemd/system/{name}" for name in (
        "haven-wishlist.service", "haven-wishlist-web.service", "haven-wishlist-web.socket",
        "haven-wishlist-collector.path", "haven-wishlist-preview.service", "haven-wishlist-language.service",
    )),
    *(f"/opt/haven-wishlist/web/wishlist/{name}" for name in (
        "index.html", "styles.css", "app.mjs", "model.mjs", "api.mjs",
        "gambarino.woff2", "general-sans.woff2",
    )),
)


def snapshot():
    if os.geteuid() != 0:
        raise ValueError("root_required")
    os.umask(0o077)
    destination = Path(tempfile.mkdtemp(prefix="haven-wishlist-web-", dir="/root"))
    manifest = {"schema_version": 1, "files": []}
    for index, value in enumerate(FILES):
        path = Path(value)
        row = {"path": value, "present": path.exists()}
        if path.is_symlink():
            raise ValueError("snapshot_symlink")
        if row["present"]:
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 2 * 1024 * 1024:
                raise ValueError("snapshot_file")
            body = path.read_bytes()
            target = destination / str(index)
            target.write_bytes(body)
            target.chmod(0o600)
            row.update(uid=info.st_uid, gid=info.st_gid, mode=stat.S_IMODE(info.st_mode), sha256=hashlib.sha256(body).hexdigest())
        manifest["files"].append(row)
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return destination


if __name__ == "__main__":
    print(json.dumps({"snapshot": str(snapshot()), "database_copied": False}))
