"""Validate the proposed wire shape; this does not implement authentication."""

from copy import deepcopy
import hashlib
import unittest

from jsonschema import Draft202012Validator

from scripts.wishlist_contract import COMMAND_VALIDATOR, REQUEST_VALIDATOR, SCHEMA
ENTRY = "wi_" + "0123456789abcdef" * 2


def request(operation, arguments):
    return {"protocol_version": 1, "request_id": hashlib.sha256(b"synthetic-request").hexdigest(),
            "source": {"platform": "telegram", "user_id": "100001", "chat_id": "100001", "message_id": "1",
                       "text_sha256": hashlib.sha256(b"synthetic-message").hexdigest()},
            "command": {"operation": operation, "arguments": arguments}}


class WishlistContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Draft202012Validator.check_schema(SCHEMA)
        cls.validator = REQUEST_VALIDATOR
        cls.command_validator = COMMAND_VALIDATOR

    def test_all_first_release_commands_and_incomplete_intake_are_representable(self):
        ref = {"entry_id": ENTRY, "expected_version": 1}
        cases = [
            ("wishlist.add", {"url": "https://shop.example/product?variant=2"}),
            ("wishlist.add", {"url": "https://shop.example/product", "details": {"purpose": "gift"}}),
            ("wishlist.list", {}), ("wishlist.get", {"entry_id": ENTRY}),
            ("wishlist.update", {**ref, "patch": {"note": None}}),
            ("wishlist.record_purchase", ref),
            ("wishlist.pause", {**ref, "paused_until": "2026-12-06T01:00:00+01:00"}),
            ("wishlist.resume", ref),
        ]
        for operation, arguments in cases:
            with self.subTest(operation=operation):
                self.validator.validate(request(operation, arguments))

    def test_model_command_has_no_identity_or_execution_escape_fields(self):
        command = request("wishlist.list", {})["command"]
        self.command_validator.validate(command)
        for field in ("owner_id", "source", "request_id", "shell", "sql"):
            bad = deepcopy(command)
            bad[field] = "synthetic"
            self.assertFalse(self.command_validator.is_valid(bad))
            bad = deepcopy(command)
            bad["arguments"][field] = "synthetic"
            self.assertFalse(self.command_validator.is_valid(bad))

    def test_unknown_operations_fields_and_unsafe_shapes_are_rejected(self):
        cases = [request("system.shell", {"command": "synthetic"}),
                 request("wishlist.list", {"limit": 100000}),
                 request("wishlist.add", {"url": "file:///synthetic"}),
                 request("wishlist.add", {"url": "https://shop.example/x", "price": "1"}),
                 request("wishlist.update", {"entry_id": ENTRY, "patch": {"note": "x"}}),
                 request("wishlist.pause", {"entry_id": ENTRY, "expected_version": 1, "paused_until": "tomorrow"})]
        for item in cases:
            with self.subTest(item=item):
                self.assertFalse(self.validator.is_valid(item))

    def test_shape_validation_does_not_establish_owner_or_url_authorization(self):
        item = request("wishlist.add", {"url": "https://127.0.0.1/private"})
        item["source"]["chat_id"] = "100002"
        # Both are structurally valid. The future service must deny these independently.
        self.validator.validate(item)

    def test_calendar_validity_and_explicit_offsets_do_not_need_optional_format_packages(self):
        for value in ("2026-02-30T12:00:00Z", "2026-11-06T12:00:00", "2026-11-06", "tomorrow"):
            item = request("wishlist.pause", {"entry_id": ENTRY, "expected_version": 1, "paused_until": value})
            with self.subTest(value=value):
                self.assertFalse(self.validator.is_valid(item))


if __name__ == "__main__":
    unittest.main()
