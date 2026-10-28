"""Fail-closed checks for the network and identity boundary before host writes."""

import copy
import unittest

from scripts.host_checks import PRIVATE_RULE, SSH_VALUES, check_firewall, check_ssh, check_tailnet


class HostChecksTests(unittest.TestCase):
    def setUp(self):
        self.status = {"BackendState": "Running", "Self": {
            "TailscaleIPs": ["100.101.102.103", "fd7a:115c:a1e0::1234"], "Tags": ["tag:haven"]}}
        self.prefs = {"WantRunning": True, "RunSSH": False, "CorpDNS": False,
                      "RouteAll": False, "AdvertiseRoutes": None, "NetfilterMode": 2}
        self.connection = "100.101.102.104 54321 100.101.102.103 22"
        self.firewall = ("Status: active\nDefault: deny (incoming), allow (outgoing)\n"
                         "22/tcp on tailscale0\n22/tcp (v6) on tailscale0")

    def test_private_ipv4_and_ipv6_connections_are_accepted(self):
        check_tailnet(self.status, self.prefs, self.connection)
        check_tailnet(self.status, self.prefs,
                      "fd7a:115c:a1e0::2345 54321 fd7a:115c:a1e0::1234 22")

    def test_public_or_missing_connection_cannot_authorize_writes(self):
        for connection in ("", "192.0.2.2 54321 192.0.2.1 22", "100.101.102.104 54321 100.101.102.103 2222"):
            with self.subTest(connection=connection), self.assertRaises(ValueError):
                check_tailnet(self.status, self.prefs, connection)

    def test_stopped_or_untagged_node_is_rejected(self):
        for change in ({"BackendState": "NeedsLogin"}, {"Self": {"TailscaleIPs": ["100.101.102.103"]}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                check_tailnet({**self.status, **change}, self.prefs, self.connection)

    def test_ssh_dns_routing_and_firewall_changes_need_review(self):
        for change in ({"RunSSH": True}, {"CorpDNS": True}, {"RouteAll": True},
                       {"WantRunning": False}, {"AdvertiseRoutes": ["0.0.0.0/0"]},
                       {"NetfilterMode": 0}, {"CorpDNS": None}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                check_tailnet(self.status, {**self.prefs, **change}, self.connection)

    def test_changed_authentication_and_missing_values_are_rejected(self):
        for key in SSH_VALUES:
            for value in (None, "unexpected"):
                values = copy.copy(SSH_VALUES)
                if value is None:
                    del values[key]
                else:
                    values[key] = value
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    check_ssh("\n".join(f"{k} {v}" for k, v in values.items()))
        check_ssh("\n".join(f"{k} {v}" for k, v in SSH_VALUES.items()))
        with self.assertRaises(ValueError):
            check_ssh("port 2222\n" + "\n".join(f"{k} {v}" for k, v in SSH_VALUES.items()))

    def test_public_rule_is_allowed_only_before_cutover(self):
        added = f"{PRIVATE_RULE}\nufw allow 22/tcp"
        check_firewall(added, self.firewall, final=False)
        with self.assertRaises(ValueError):
            check_firewall(added, self.firewall, final=True)
        check_firewall(PRIVATE_RULE, self.firewall, final=True)

    def test_unknown_rules_missing_ipv6_and_inactive_firewall_fail(self):
        for added, status in ((f"{PRIVATE_RULE}\nufw allow 443/tcp", self.firewall),
                              (PRIVATE_RULE, "Status: inactive"),
                              (PRIVATE_RULE, self.firewall.replace("22/tcp (v6) on tailscale0", "")),
                              ("", self.firewall)):
            with self.subTest(added=added, status=status), self.assertRaises(ValueError):
                check_firewall(added, status, final=True)


if __name__ == "__main__":
    unittest.main()
