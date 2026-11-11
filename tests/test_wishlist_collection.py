"""W3 transactions, lease/replay races, migration and recovery; no production data."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from scripts.wishlist_collection import Collection, DAY, LEASE
from scripts.wishlist_collector import CollectionServer, collect, request
from scripts.wishlist_domain import Wishlist, stamp
from scripts.wishlist_fetch import failure
from scripts.wishlist_store import MIGRATIONS, Store
from scripts.wishlist_operations import configurations
from scripts.wishlist_service import load_service_config
from tests import test_wishlist_domain as domain_tests

SUCCESS = {"status": "ok", "reason": "structured_offer_candidate", "http_status": 200, "retry_after": 0,
           "offer": {"name": "Synthetic product", "sku": "SYNTHETIC-2", "price": "249.90", "currency": "CZK",
                     "availability": "in_stock", "conditions_verification": "not_verified"}}


class CollectionTests(unittest.TestCase):
    open = domain_tests.WishlistTests.open
    request = domain_tests.WishlistTests.request
    send = domain_tests.WishlistTests.send
    add = domain_tests.WishlistTests.add
    change = domain_tests.WishlistTests.change

    def setUp(self):
        domain_tests.WishlistTests.setUp(self)
        self.collection = Collection(self.domain)
        self.now = datetime(2026, 11, 11, 12, tzinfo=UTC)
        self.domain.clock = lambda: self.now

    def claim(self, claim_id=None):
        return self.collection.handle({"protocol_version": 1, "operation": "collection.claim",
                                       "claim_id": claim_id or uuid4().hex})

    def finish(self, admission, result=None):
        return self.collection.handle({"protocol_version": 1, "operation": "collection.complete",
                                       "job": admission["job"], "result": deepcopy(result or SUCCESS)})

    def supported(self, path="synthetic"):
        return self.add(url="https://www.fabini.cz/products/" + path)

    def test_daily_history_is_atomic_idempotent_and_visible_to_owner(self):
        entry = self.supported()
        job = self.claim()
        self.assertEqual(self.claim()["code"], "idle")
        self.assertEqual(self.finish(job)["code"], "ok")
        self.assertEqual(self.finish(job)["code"], "ok")
        changed = deepcopy(SUCCESS)
        changed["offer"]["price"] = "1.00"
        self.assertEqual(self.finish(job, changed)["code"], "conflict")
        self.assertEqual(self.claim()["code"], "idle")
        detail = self.send("wishlist.get", {"entry_id": entry["entry_id"]})["data"]
        self.assertEqual(len(detail["observations"]), 1)
        self.assertEqual(detail["entry"]["latest_observation"]["price"], "249.90")
        self.assertEqual(detail["entry"]["collection_status"], "ok")
        self.now += timedelta(seconds=DAY)
        second = self.claim()
        self.assertNotEqual(job["job"]["job_id"], second["job"]["job_id"])
        self.assertEqual(self.finish(second)["code"], "ok")
        self.assertEqual(self.domain.store.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 2)

    def test_failure_preserves_price_timestamp_and_obeys_retry_after(self):
        entry = self.supported()
        self.finish(self.claim())
        observed = stamp(self.now)
        self.now += timedelta(days=1)
        self.finish(self.claim(), failure("source_http_429", status="blocked", http_status=429, retry_after=3 * DAY))
        item = self.domain._entry(self.domain.store.db, entry["entry_id"])
        self.assertEqual(item["latest_observation"]["observed_at"], observed)
        self.assertEqual(item["last_observed_at"], observed)
        self.assertEqual(item["collection_status"], "blocked")
        self.assertEqual(item["last_http_status"], 429)
        self.now += timedelta(days=2)
        self.assertEqual(self.claim()["code"], "idle")
        self.now += timedelta(days=1)
        self.assertEqual(self.claim()["code"], "ok")

    def test_stale_result_cannot_reactivate_changed_purchased_paused_or_removed_entry(self):
        for operation, extra in (("wishlist.record_purchase", {}),
                                 ("wishlist.pause", {"paused_until": "2026-12-11T12:00:00Z"}),
                                 ("wishlist.remove", {}), ("wishlist.update", {"patch": {"label": "Changed"}})):
            with self.subTest(operation=operation):
                entry = self.supported(uuid4().hex)
                job = self.claim()
                self.change(operation, entry, **extra)
                self.assertEqual(self.finish(job)["code"], "stale")
                self.assertIsNone(self.domain._entry(self.domain.store.db, entry["entry_id"])["latest_observation"])
                # Drain the still-active edited item so later cases select their own entry.
                if operation == "wishlist.update":
                    self.finish(self.claim())

    def test_expired_lease_survives_restart_and_rejects_old_attempt(self):
        self.supported()
        first = self.claim()
        self.domain.close()
        self.domain = Wishlist(self.path, bot_id=self.domain.bot_id, owner_id=self.domain.owner_id,
                               clock=lambda: self.now)
        self.collection = Collection(self.domain)
        self.now += timedelta(seconds=LEASE + 1)
        self.assertEqual(self.claim()["code"], "idle")
        self.now += timedelta(hours=1)
        second = self.claim()
        self.assertEqual(self.finish(first)["code"], "stale")
        self.assertEqual(self.finish(second)["code"], "ok")

    def test_duplicate_claim_and_altered_job_do_not_grant_new_work(self):
        self.supported()
        claim_id = uuid4().hex
        first = self.claim(claim_id)
        self.assertEqual(self.claim(claim_id), first)
        for field in first["job"]:
            changed = deepcopy(first)
            changed["job"][field] = "wrong" if isinstance(changed["job"][field], str) else 99
            self.assertEqual(self.finish(changed)["code"], "denied")
        self.finish(first)
        self.assertEqual(self.claim(claim_id)["code"], "idle")

    def test_resume_expired_recurring_pause_with_a_new_generation(self):
        entry = self.add(url="https://www.fabini.cz/products/repeat", details={
            "purpose": "self", "purchase_mode": "recurring"})
        self.change("wishlist.record_purchase", entry)
        self.assertEqual(self.claim()["code"], "idle")
        self.now = datetime(2026, 12, 11, 12, tzinfo=UTC)
        job = self.claim()
        self.assertEqual(job["code"], "ok")
        current = self.domain._entry(self.domain.store.db, entry["entry_id"])
        self.assertEqual(current["state"], "active")
        self.assertGreater(current["version"], entry["version"])
        self.assertEqual(self.finish(job)["code"], "ok")

    def test_changed_sku_and_invalid_price_do_not_replace_history(self):
        self.supported()
        self.finish(self.claim())
        self.now += timedelta(days=1)
        job = self.claim()
        for change in ({"price": "0.00"}, {"price": "NaN"}, {"currency": "EUR"},
                       {"conditions_verification": "verified"}, {"name": "x" * 201}):
            invalid = deepcopy(SUCCESS)
            invalid["offer"].update(change)
            self.assertEqual(self.finish(job, invalid)["code"], "invalid_request")
        changed = deepcopy(SUCCESS)
        changed["offer"]["sku"] = "OTHER-VARIANT"
        self.assertEqual(self.finish(job, changed)["code"], "ok")
        self.assertEqual(self.domain.store.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 1)
        self.assertEqual(self.domain.store.db.execute("SELECT collection_reason FROM offers").fetchone()[0], "offer_unverified")
        self.assertEqual(self.finish(job, changed)["code"], "ok")

    def test_unknown_shop_is_saved_and_unsupported_without_losing_entry(self):
        entry = self.add()
        job = self.claim()
        self.assertEqual(job["job"]["adapter"], "unsupported")
        self.assertEqual(self.finish(job)["code"], "invalid_request")
        self.assertEqual(self.finish(job, failure("unsupported_shop", status="unsupported"))["code"], "ok")
        self.assertEqual(self.domain._entry(self.domain.store.db, entry["entry_id"])["collection_status"], "unsupported")

    def test_observation_and_job_completion_rollback_together(self):
        self.supported()
        job = self.claim()
        self.domain.store.db.execute("""CREATE TRIGGER fail_completion BEFORE UPDATE ON collection_jobs
            WHEN NEW.status='completed' BEGIN SELECT RAISE(ABORT, 'synthetic'); END""")
        self.assertEqual(self.finish(job)["code"], "unavailable")
        self.assertEqual(self.domain.store.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 0)
        self.assertEqual(self.domain.store.db.execute("SELECT status FROM collection_jobs").fetchone()[0], "running")
        self.domain.store.db.execute("DROP TRIGGER fail_completion")
        self.assertEqual(self.finish(job)["code"], "ok")

    def test_two_domain_connections_cannot_claim_the_same_offer(self):
        self.supported()
        def claim():
            domain = Wishlist(self.path, bot_id=self.domain.bot_id, owner_id=self.domain.owner_id, clock=lambda: self.now)
            try:
                return Collection(domain).handle({"protocol_version": 1, "operation": "collection.claim", "claim_id": uuid4().hex})
            finally:
                domain.close()
        with ThreadPoolExecutor(max_workers=2) as workers:
            responses = list(workers.map(lambda _: claim(), range(2)))
        self.assertEqual(sorted(row["code"] for row in responses), ["idle", "ok"])

    def test_host_rate_limit_delays_other_offers_but_not_another_shop(self):
        self.supported("first")
        self.supported("second")
        self.add(url="https://www.momenti.cz/synthetic")
        job = self.claim()
        self.assertEqual(self.claim()["code"], "idle")
        self.finish(job, failure("source_http_429", status="blocked", http_status=429, retry_after=DAY * 2))
        other = self.claim()
        self.assertEqual(other["job"]["adapter"], "momenti-microdata-v1")
        self.finish(other)
        self.assertEqual(self.claim()["code"], "idle")
        self.now += timedelta(days=2)
        self.assertEqual(self.claim()["job"]["adapter"], "fabini-jsonld-v1")

    def test_missing_daily_scan_is_stale_without_changing_old_price(self):
        entry = self.supported()
        self.finish(self.claim())
        observed = stamp(self.now)
        self.now += timedelta(hours=26)
        current = self.domain._entry(self.domain.store.db, entry["entry_id"])
        self.assertEqual(current["collection_status"], "stale")
        self.assertEqual(current["collection_reason"], "schedule_overdue")
        self.assertEqual(current["latest_observation"]["observed_at"], observed)

    def test_restored_database_preserves_observation_and_completion_replay(self):
        self.supported()
        job = self.claim()
        self.finish(job)
        target = self.path.with_name("restored.sqlite3")
        with closing(sqlite3.connect(target)) as destination:
            self.domain.store.db.backup(destination)
        restored = Wishlist(target, bot_id=self.domain.bot_id, owner_id=self.domain.owner_id, clock=lambda: self.now)
        try:
            response = Collection(restored).handle({"protocol_version": 1, "operation": "collection.complete",
                                                   "job": job["job"], "result": SUCCESS})
            self.assertEqual(response["code"], "ok")
            self.assertEqual(restored.store.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 1)
            self.assertEqual(restored.store.db.execute("SELECT price FROM observations").fetchone()[0], "249.90")
        finally:
            restored.close()


class MigrationTests(unittest.TestCase):
    def test_collector_configuration_requires_distinct_identities_and_socket_directory(self):
        args = dict(bot_id="123", owner_id="456", timezone="Europe/Prague", ingress_uid=1001,
                    domain_uid=1002, language_uid=1003, ingress_gid=1010, language_gid=1011,
                    collector_uid=1004, collection_gid=1012)
        config = configurations(**args)
        self.assertEqual(config["collector"]["service_uid"], args["domain_uid"])
        for changes in ({"collector_uid": 1001}, {"collector_uid": 1002}, {"collector_uid": 1003},
                        {"collector_uid": True}, {"collection_gid": None}, {"collection_gid": 1011}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                configurations(**{**args, **changes})
        import json
        with tempfile.TemporaryDirectory(prefix="hw3-cfg-") as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(config["domain"]))
            self.assertEqual(load_service_config(path), config["domain"])
            for changes in ({"client_uid": os.geteuid()}, {"client_uid": args["ingress_uid"]},
                            {"client_uid": args["language_uid"]}, {"socket_gid": args["ingress_gid"]},
                            {"socket": "/run/haven-wishlist/collector.sock"},
                            {"socket": "/run/haven-wishlist-language/collector.sock"}):
                invalid = deepcopy(config["domain"])
                invalid["collector"].update(changes)
                path.write_text(json.dumps(invalid))
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    load_service_config(path)

    def test_schema_four_migrates_without_erasing_data_and_old_binary_refuses_schema_five(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.sqlite3"
            args = dict(bot_id="123", owner_id="456", timezone="Europe/Prague")
            with patch("scripts.wishlist_store.MIGRATIONS", MIGRATIONS[:4]):
                store = Store(path, **args)
                store.db.execute("INSERT INTO language_sessions VALUES ('456','456',1,'{}','2030-01-01')")
                store.close()
            store = Store(path, **args)
            self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM language_sessions").fetchone()[0], 1)
            store.close()
            before = path.read_bytes()
            with patch("scripts.wishlist_store.MIGRATIONS", MIGRATIONS[:4]), self.assertRaises(ValueError):
                Store(path, **args)
            self.assertEqual(path.read_bytes(), before)


class SocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_peer_socket_drives_collection_and_denies_domain_operations(self):
        with tempfile.TemporaryDirectory(prefix="hw3-") as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            domain = Wishlist(root / "db", bot_id="123", owner_id="456")
            # No native poller or model; trusted ingress creates the synthetic entry directly.
            db = domain.store.db
            db.execute("""INSERT INTO entries (entry_id,owner_id,version,state,classification,created_at,updated_at)
                VALUES ('wi_test','456',1,'active','needs_details','2026-01-01','2026-01-01')""")
            import hashlib
            url = "https://www.fabini.cz/products/synthetic"
            db.execute("""INSERT INTO offers (offer_id,entry_id,owner_id,url,shop_host,url_sha256,collection_status,collection_generation)
                VALUES ('wo_test','wi_test','456',?,'www.fabini.cz',?,'pending',1)""", (url, hashlib.sha256(url.encode()).hexdigest()))
            server = CollectionServer(domain, root / "collector.sock", ingress_uid=os.getuid())
            endpoint = {"socket": str(server.path), "service_uid": os.getuid()}
            await server.start()
            async def fetcher(admission):
                await asyncio.sleep(0)
                return SUCCESS
            try:
                denied = await request(endpoint, {"protocol_version": 1, "operation": "wishlist.list"})
                self.assertEqual(denied["code"], "invalid_request")
                self.assertEqual(await collect(endpoint, fetcher=fetcher), 1)
                self.assertEqual(await collect(endpoint, fetcher=fetcher), 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 1)
                with self.assertRaises(PermissionError):
                    await request({**endpoint, "service_uid": os.getuid() + 1}, {})
            finally:
                await server.close()
            denied_server = CollectionServer(domain, root / "denied.sock", ingress_uid=os.getuid() + 1)
            await denied_server.start()
            try:
                response = await request({**endpoint, "socket": str(denied_server.path)},
                                         {"protocol_version": 1, "operation": "collection.claim", "claim_id": uuid4().hex})
                self.assertEqual(response["code"], "denied")
            finally:
                await denied_server.close()
                domain.close()


if __name__ == "__main__":
    unittest.main()
