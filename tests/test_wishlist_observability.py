"""Real SQLite retention/replay, aggregate privacy and independent health decisions."""

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from scripts import backup_runtime as runtime
from scripts import wishlist_observability as metrics
from scripts.wishlist_collection import Collection
from scripts.wishlist_domain import IngressContext, Wishlist, canonical, stamp
from scripts.dashboard_snapshot import render
from scripts.wishlist_language import LanguageFlow
from scripts.wishlist_retention import BATCH, EXPIRED_REPLY, compact
from scripts.wishlist_telegram import TelegramApplication
from tests.test_wishlist_collection import SUCCESS
from tests.test_wishlist_service import BOT, OWNER, event


class Fixture:
    @contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'wishlist.sqlite3'
            self.now = datetime(2026, 11, 15, 18, tzinfo=UTC)
            domain = Wishlist(path, bot_id=BOT, owner_id=OWNER, clock=lambda: self.now)
            try:
                yield domain, path
            finally:
                domain.close()

    def scan(self, domain):
        return Collection(domain).handle({'protocol_version': 1, 'operation': 'collection.claim', 'claim_id': uuid4().hex})


class ObservabilityTests(Fixture, unittest.TestCase):
    def test_atomic_projection_roundtrip_and_dashboard_show_separate_unknown_usage(self):
        with self.fixture() as (domain, path):
            self.scan(domain)
            compact(domain)
            value = metrics.collect(path, now=self.now)
            output = path.with_name('snapshot.json')
            metrics.write_snapshot(value, output)
            self.assertEqual(metrics.read_snapshot(output), value)
            self.assertEqual(output.stat().st_mode & 0o777, 0o640)
            page = render({'service': {}, 'usage': {'status': 'unavailable'}, 'tasks': {'status': 'unavailable'},
                           'generated_at_utc': stamp(self.now), 'wishlist': value})
            self.assertIn('Spotřeba wishlistového interpretu', page)
            self.assertIn('Pokusy bez měření tokenů', page)
            self.assertIn('Zbývající kvóta</th><td>nezjištěno', page)

    def test_no_due_work_is_healthy_but_missing_scan_stale_snapshot_and_future_clock_are_not(self):
        with self.fixture() as (domain, path):
            compact(domain)
            self.assertIn('scan_missing_or_stale', metrics.health(metrics.collect(path, now=self.now), self.now))
            self.assertEqual(self.scan(domain)['code'], 'idle')
            before = path.read_bytes()
            snapshot = metrics.collect(path, now=self.now)
            self.assertEqual(metrics.health(snapshot, self.now), [])
            self.assertIsNone(snapshot['usage']['input_tokens'])
            self.assertEqual(snapshot['sources'], [])
            self.assertEqual(before, path.read_bytes())
            self.assertIn('snapshot_stale', metrics.health(snapshot, self.now + timedelta(seconds=121)))
            self.assertIn('snapshot_stale', metrics.health(snapshot, self.now - timedelta(seconds=1)))
            self.now += timedelta(minutes=31)
            self.assertIn('scan_missing_or_stale', metrics.health(metrics.collect(path, now=self.now), self.now))

    def test_prices_are_private_and_deferred_source_is_visible_but_does_not_suppress_other_failures(self):
        with self.fixture() as (domain, path):
            application = TelegramApplication(domain)
            application.handle(event('https://www.alza.cz/PRIVATE_PRODUCT.htm', '1'))
            job = self.scan(domain)
            Collection(domain).handle({'protocol_version': 1, 'operation': 'collection.complete', 'job': job['job'],
                                      'result': {'status': 'blocked', 'reason': 'source_http_403', 'http_status': 403, 'retry_after': 0}})
            compact(domain)
            snapshot = metrics.collect(path, deferred_hosts=['www.alza.cz'], now=self.now)
            self.assertEqual(metrics.health(snapshot, self.now), [])
            self.assertTrue(snapshot['sources'][0]['deferred'])
            self.assertNotIn('PRIVATE_', json.dumps(snapshot))
            application.handle(event('https://www.fabini.cz/products/PRIVATE_SECOND', '2'))
            job = self.scan(domain)
            Collection(domain).handle({'protocol_version': 1, 'operation': 'collection.complete', 'job': job['job'],
                                      'result': {'status': 'blocked', 'reason': 'source_http_429', 'http_status': 429, 'retry_after': 0}})
            snapshot = metrics.collect(path, deferred_hosts=['www.alza.cz'], now=self.now)
            self.assertIn('source_failed', metrics.health(snapshot, self.now))
            # A server's future retry gate is not overdue work.
            self.assertEqual(snapshot['overdue_offers'], 0)

    def test_overdue_work_and_expired_lease_fail_even_with_fresh_scan(self):
        with self.fixture() as (domain, path):
            TelegramApplication(domain).handle(event('https://www.fabini.cz/products/private', '1'))
            self.scan(domain)
            self.now += timedelta(hours=2)
            compact(domain)
            # Simulate a stalled collector. Refreshing the export is not progress.
            domain.store.db.execute("INSERT OR REPLACE INTO metadata VALUES ('collection_last_scan',?)", (stamp(self.now),))
            snapshot = metrics.collect(path, now=self.now)
            self.assertIn('lease_expired', metrics.health(snapshot, self.now))
            self.assertIn('collection_overdue', metrics.health(snapshot, self.now))

    def test_usage_counts_unknown_and_rejected_attempts_without_inventing_tokens_or_cost(self):
        with self.fixture() as (domain, path):
            class Interpreter:
                async def interpret(self, prepared):
                    return {'request_id': prepared['request_id'], 'attempt_id': prepared['attempt_id'], 'code': 'ok',
                            'decision': {'kind': 'INVALID_PRIVATE_OUTPUT'}, 'usage': {'input_tokens': 17, 'output_tokens': 9}}
            flow = LanguageFlow(domain, TelegramApplication(domain), Interpreter())
            self.assertEqual(asyncio.run(flow.handle(event('PRIVATE_MESSAGE', '1')))['code'], 'unavailable')
            db = domain.store.db
            run = db.execute('SELECT request_id FROM language_runs').fetchone()[0]
            for value in (None, 'null', '{broken', '{"input_tokens":true,"output_tokens":2}', '{"input_tokens":-1,"output_tokens":2}'):
                db.execute('INSERT INTO language_attempts VALUES (?,?,?, ?,?)', (uuid4().hex, run, stamp(self.now), 'unavailable', value))
            snapshot = metrics.collect(path, now=self.now)
            self.assertEqual(snapshot['usage']['interpretation_attempts'], 6)
            self.assertEqual(snapshot['usage']['measured_attempts'], 1)
            self.assertEqual(snapshot['usage']['unknown_usage_attempts'], 5)
            self.assertEqual(snapshot['usage']['input_tokens'], 17)
            self.assertIsNone(snapshot['usage']['estimated_cost_usd'])
            self.assertNotIn('PRIVATE_', json.dumps(snapshot))

    def test_missing_corrupt_newer_and_linked_databases_are_unavailable_without_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'missing.sqlite3'
            self.assertEqual(metrics.collect(path)['status'], 'unavailable')
            self.assertFalse(path.exists())
            path.write_bytes(b'corrupt')
            self.assertEqual(metrics.collect(path)['status'], 'unavailable')
            other = path.with_name('link')
            other.symlink_to(path)
            self.assertEqual(metrics.collect(other)['status'], 'unavailable')
        with self.fixture() as (domain, path):
            domain.store.db.execute('PRAGMA user_version=99')
            self.assertEqual(metrics.collect(path)['status'], 'unavailable')

    def test_monitor_never_pings_success_for_missing_stale_or_failed_collection(self):
        with self.fixture() as (domain, path), tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime.save(root / 'accepted.json', {'restored_and_verified': True, 'captured_at_utc': stamp(self.now)})
            self.scan(domain)
            compact(domain)
            snapshot = metrics.collect(path, now=self.now)
            dashboard = root / 'dashboard.json'
            dashboard.write_text(json.dumps({'generated_at_utc': stamp(self.now)}))
            with patch.object(runtime, 'SystemController') as controller, patch.object(runtime, 'ping') as ping, \
                    patch.object(runtime.time, 'time', return_value=self.now.timestamp()), \
                    patch.object(runtime, 'Path', return_value=dashboard), \
                    patch.object(metrics, 'read_snapshot', return_value=snapshot), \
                    patch.object(metrics, 'health') as checked_health:
                # Use fixed health decisions here; actual time boundaries above
                # use real snapshots. This test covers success-ping admission.
                controller.return_value.healthy.return_value = True
                for problems in (['snapshot_stale'], ['scan_missing_or_stale'], ['source_failed']):
                    checked_health.return_value = problems
                    with self.assertRaisesRegex(Exception, 'wishlist_collection_unhealthy'):
                        runtime.monitor({'wishlist_collection_enabled': True}, root, root)
                ping.assert_not_called()
                checked_health.return_value = []
                runtime.monitor({'wishlist_collection_enabled': True}, root, root)
                ping.assert_called_once_with(root, 'service')


class RetentionTests(Fixture, unittest.TestCase):
    def test_purchase_domain_receipt_survives_as_replay_barrier_and_failure_rolls_back(self):
        with self.fixture() as (domain, _):
            def request(number, operation, arguments):
                command = {'operation': operation, 'arguments': arguments}
                context = IngressContext(BOT, OWNER, OWNER, str(number), hashlib.sha256(canonical(command).encode()).hexdigest())
                return {'protocol_version': 1, 'request_id': context.request_id, 'source': context.source(), 'command': command}, context
            added = domain.handle(*request(1, 'wishlist.add', {'url': 'https://www.fabini.cz/products/private',
                                                            'details': {'purpose': 'self', 'purchase_mode': 'once'}}))
            entry = added['data']['entry']
            purchase = request(2, 'wishlist.record_purchase', {'entry_id': entry['entry_id'], 'expected_version': entry['version']})
            domain.handle(*purchase)
            self.now += timedelta(days=31)
            db = domain.store.db
            before = [tuple(row) for row in db.execute('SELECT * FROM receipts')]
            db.execute("CREATE TRIGGER reject_retention BEFORE UPDATE ON receipts BEGIN SELECT RAISE(ABORT, 'synthetic'); END")
            with self.assertRaises(Exception):
                compact(domain)
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM receipts')], before)
            db.execute('DROP TRIGGER reject_retention')
            compact(domain)
            self.assertEqual(domain.handle(*purchase)['code'], 'unavailable')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM purchases').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 2)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_old_reply_compaction_preserves_replay_conflict_prices_and_purchase_history(self):
        with self.fixture() as (domain, path):
            application = TelegramApplication(domain)
            message = event('https://www.fabini.cz/products/PRIVATE_ITEM', '1')
            original = application.handle(message)
            job = self.scan(domain)
            Collection(domain).handle({'protocol_version': 1, 'operation': 'collection.complete', 'job': job['job'], 'result': SUCCESS})
            self.now += timedelta(days=31)
            usage_before = metrics.collect(path, now=self.now)['usage']
            self.assertGreater(compact(domain), 0)
            result = application.handle(message)
            self.assertEqual(result['reply'], EXPIRED_REPLY)
            self.assertFalse(result['committed'])
            self.assertEqual(application.handle({**message, 'text': 'changed'})['code'], 'conflict')
            db = domain.store.db
            self.assertEqual(db.execute('SELECT COUNT(*) FROM entries').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 1)
            self.assertIsNone(db.execute('SELECT result FROM collection_jobs').fetchone()[0])
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(metrics.collect(path, now=self.now)['usage'], usage_before)
            self.assertEqual(compact(domain), 0)
            self.assertTrue(original['committed'])

    def test_unfinished_language_request_cannot_run_again_after_payload_compaction(self):
        with self.fixture() as (domain, _):
            class Interpreter:
                calls = 0
                async def interpret(self, prepared):
                    self.calls += 1
                    raise ValueError('unavailable')
            interpreter = Interpreter()
            flow = LanguageFlow(domain, TelegramApplication(domain), interpreter)
            message = event('PRIVATE_PROMPT', '1')
            asyncio.run(flow.handle(message))
            self.now += timedelta(days=31)
            compact(domain)
            reply = asyncio.run(flow.handle(message))
            self.assertEqual(reply['reply'], EXPIRED_REPLY)
            self.assertEqual(interpreter.calls, 1)
            self.assertEqual(domain.store.db.execute('SELECT prepared FROM language_runs').fetchone()[0], '{}')
            self.assertNotIn('PRIVATE_', str([tuple(r) for r in domain.store.db.execute('SELECT * FROM language_runs')]))

    def test_batch_limit_and_active_requests_survive(self):
        with self.fixture() as (domain, _):
            db = domain.store.db
            old = stamp(self.now - timedelta(days=31))
            for _ in range(BATCH + 1):
                key = uuid4().hex
                db.execute('INSERT INTO receipts VALUES (?,?,?,?,?)', (key, OWNER, 'f', '{"PRIVATE_PAYLOAD":1}', old))
            compact(domain)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM receipts WHERE response LIKE '%PRIVATE_PAYLOAD%'").fetchone()[0], 1)
            compact(domain)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM receipts WHERE response LIKE '%PRIVATE_PAYLOAD%'").fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0], BATCH + 1)
