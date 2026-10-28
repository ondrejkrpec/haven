"""Bounded Ubuntu host checks, also copied transiently by Ansible over SSH.

Only a short result is printed. Tailscale state/preferences and command diagnostics
are inspected in memory, never dumped. No package or configuration writes occur.
"""

import argparse
import grp
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess


SSH_VALUES = {
    "port": "22", "permitrootlogin": "no", "passwordauthentication": "no",
    "kbdinteractiveauthentication": "no", "pubkeyauthentication": "yes",
}
PRIVATE_RULE = "ufw allow in on tailscale0 to any port 22 proto tcp"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def command(*argv):
    result = subprocess.run(argv, capture_output=True, text=True,
                            env={**os.environ, "LC_ALL": "C"}, check=False)
    require(result.returncode == 0, f"Required host command failed: {Path(argv[0]).name}")
    return result.stdout.strip()


def check_ssh(text):
    pairs = [line.split(None, 1) for line in text.splitlines() if " " in line]
    require(all([value for name, value in pairs if name == key] == [expected]
                for key, expected in SSH_VALUES.items()),
            "Effective SSH authentication/port differs from the accepted baseline")


def check_tailnet(status, prefs, connection):
    fields = connection.split()
    require(len(fields) == 4, "A real SSH connection context is required")
    client, _, server, port = fields
    ipaddress.ip_address(client)
    ipaddress.ip_address(server)
    require(status.get("BackendState") == "Running", "Tailscale is not connected")
    node = status.get("Self", {})
    require(server in node.get("TailscaleIPs", []) and port == "22",
            "Run bootstrap through private Tailscale SSH on port 22")
    require("tag:haven" in node.get("Tags", []), "Server needs authorized tag:haven")
    require(prefs.get("WantRunning") is True and prefs.get("RunSSH") is False
            and prefs.get("CorpDNS") is False and prefs.get("RouteAll") is False
            and not prefs.get("AdvertiseRoutes") and prefs.get("NetfilterMode") == 2,
            "Tailscale preferences differ from the private OpenSSH design")
    return client, server


def check_firewall(added, status, final):
    rules = {line.strip() for line in added.splitlines() if line.startswith("ufw ")}
    allowed = {PRIVATE_RULE} if final else {PRIVATE_RULE, "ufw allow 22/tcp"}
    require(rules <= allowed, "Unrecognized UFW user rules; review before bootstrap")
    require("Status: active" in status, "UFW must already be active")
    if final:
        require(rules == {PRIVATE_RULE}, "Private-only SSH allowance is missing")
        require("deny (incoming), allow (outgoing)" in status,
                "Unexpected UFW default policy")
        require("22/tcp (v6) on tailscale0" in status, "IPv6 private rule is missing")


def check_account(final):
    try:
        user = pwd.getpwnam("haven")
    except KeyError:
        require(not final, "Service identity is missing")
        return
    require(0 < user.pw_uid < 1000 and user.pw_dir == "/var/lib/haven"
            and user.pw_shell == "/usr/sbin/nologin"
            and user.pw_gid > 0 and grp.getgrgid(user.pw_gid).gr_name == "haven"
            and set(os.getgrouplist("haven", user.pw_gid)) == {user.pw_gid},
            "Existing haven account needs manual review")
    password = command("/usr/bin/passwd", "-S", "haven").split()
    require(len(password) > 1 and password[1] == "L", "Service password is not locked")
    result = subprocess.run(["/usr/sbin/runuser", "-u", "haven", "--",
                             "/usr/bin/sudo", "-n", "true"], capture_output=True)
    require(result.returncode != 0, "Service identity unexpectedly has sudo")
    require(command("/usr/bin/sudo", "-n", "-l", "-U", "haven").startswith(
        "User haven is not allowed to run sudo on "), "Service sudo policy needs review")
    require(not Path(user.pw_dir).is_symlink(), "Service home must not be a symlink")
    if final:
        home = Path(user.pw_dir).stat()
        require(home.st_uid == user.pw_uid and home.st_gid == user.pw_gid
                and stat.S_IMODE(home.st_mode) == 0o750, "Unexpected service home metadata")


def inspect(mode, admin, connection):
    require(os.geteuid() == 0, "Host checks require administrator sudo")
    require(re.fullmatch(r"[a-z_][a-z0-9_-]*", admin) and admin not in {"root", "haven"},
            "Use a separate non-root administrator")
    release = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines()
                   if "=" in line)
    require(release.get("ID", "").strip('"') == "ubuntu"
            and release.get("VERSION_ID", "").strip('"') == "26.04",
            "This bootstrap is bounded to Ubuntu 26.04")
    for binary in ("ufw", "tailscale", "sshd", "runuser", "sudo"):
        require(shutil.which(binary), "Required host tool is missing")
    client, server = check_tailnet(json.loads(command("tailscale", "status", "--json")),
                                  json.loads(command("tailscale", "debug", "prefs")), connection)
    command("/usr/sbin/sshd", "-t")
    for user in (admin, "root"):
        check_ssh(command("/usr/sbin/sshd", "-T", "-C",
                          f"user={user},host={client},addr={client},laddr={server},lport=22"))
    snippet = Path("/etc/ssh/sshd_config.d/00-haven.conf")
    require(snippet.is_file() and not snippet.is_symlink(), "Manual SSH baseline is required")
    require('IPV6=yes' in Path("/etc/default/ufw").read_text(), "UFW IPv6 must be enabled")
    final = mode == "verify"
    check_firewall(command("ufw", "show", "added"), command("ufw", "status", "verbose"), final)
    check_account(final)
    for unit in ("ssh.socket", "tailscaled.service", "ufw.service"):
        command("systemctl", "is-active", unit)
        if final:
            require(command("systemctl", "is-enabled", unit) == "enabled",
                    "Access service is not enabled at boot")
    return {"result": "passed", "mode": mode, "private_ssh": True,
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "verify"))
    parser.add_argument("--admin", required=True)
    parser.add_argument("--connection", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(inspect(args.mode, args.admin, args.connection)))
    except (ValueError, OSError, KeyError) as error:
        parser.exit(1, f"Host check failed: {error}\n")


if __name__ == "__main__":
    main()
