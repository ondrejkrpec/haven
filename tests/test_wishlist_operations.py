"""W4 backup/recovery with real databases and bounded, offline service controls."""

from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import backup_capture as capture
from scripts import backup_restore as restore
from scripts import backup_runtime as runtime
from scripts import wishlist_operations as operations
from scripts.wishlist_store import MIGRATIONS, Store


class OperationsTests(unittest.TestCase):
    @contextmanager
    def bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, domain, language = (root / name for name in ('home', 'domain', 'language'))
            for path in (home, domain, language):
                path.mkdir(mode=0o700)
            (domain / 'state').mkdir(mode=0o700)
            (domain / '.ansible').mkdir(mode=0o700)
            (domain / '.ansible/transient.py').write_text('INSTALLATION_ONLY')
            (domain / 'config.json').write_text('{}')
            (home / 'config.yaml').write_text('synthetic: true')
            for name in ('config.yaml', 'auth.json'):
                (language / name).write_text('{}')
                (language / name).chmod(0o600)
            for name in ('cron', 'hooks', 'memories', 'pairing', 'sessions', 'skills', 'backups'):
                (language / name).mkdir(mode=0o700)
            (language / 'SOUL.md').write_text('SYNTHETIC_BOOTSTRAP')
            database = domain / 'state/wishlist.sqlite3'
            store = Store(database, bot_id='123', owner_id='456', timezone='Europe/Prague')
            store.db.execute("INSERT INTO language_sessions VALUES ('456','456',1,'{}','2030-01-01')")
            store.close()
            database.chmod(0o600)
            payload = root / 'payload'
            payload.mkdir(mode=0o700)
            home_policy = {'schema_version': 1, 'include': ['config.yaml'], 'required': ['config.yaml'],
                           'exclude': [], 'sqlite': []}
            controller, journal = Mock(), Mock()
            controller.healthy.return_value = controller.stopped.return_value = True
            journal.pending.return_value = False
            receipt = capture.capture_with_pause(controller, journal, home, payload / 'home', home_policy,
                components={'wishlist': {'source': domain, 'policy': operations.DOMAIN_POLICY},
                            'language': {'source': language, 'policy': operations.LANGUAGE_POLICY}})
            manifest = {'schema_version': 1, 'run_id': 'a' * 32, 'policy': home_policy, 'capture': receipt,
                        'additional': receipt.pop('additional')}
            runtime.save(payload / 'restore-manifest.json', manifest)
            digest = hashlib.sha256((payload / 'restore-manifest.json').read_bytes()).hexdigest()
            yield root, payload, digest, controller, journal

    def test_all_components_restore_with_conversation_and_database_version_intact(self):
        with self.bundle() as (root, payload, digest, controller, journal):
            manifest = runtime.validate_payload(payload, digest)
            self.assertEqual(set(manifest['additional']), {'wishlist', 'language'})
            self.assertIn('.ansible', manifest['additional']['wishlist']['capture']['excluded'])
            owners = {'uids': {str(os.getuid()): os.getuid()}, 'gids': {str(os.getgid()): os.getgid()}}
            restore.rebuild(payload, root / 'recovered', digest, owners, component='wishlist')
            db = root / 'recovered/state/wishlist.sqlite3'
            self.assertEqual(operations.database_status(db)['schema_version'], len(MIGRATIONS))
            with closing(sqlite3.connect(db)) as connection:
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM language_sessions').fetchone()[0], 1)
            restore.rebuild(payload, root / 'model', digest, owners, component='language')
            self.assertEqual((root / 'model/auth.json').stat().st_mode & 0o777, 0o600)
            controller.stop.assert_called_once()
            controller.start.assert_called_once()
            journal.disarm.assert_called_once()
            with self.assertRaises(Exception):
                restore.rebuild(payload, root / 'recovered', digest, owners, component='wishlist')
            self.assertEqual(operations.database_status(db)['schema_version'], len(MIGRATIONS))

    def test_additional_corruption_and_missing_components_cannot_be_accepted(self):
        for change in ('database', 'auth', 'missing', 'unexpected'):
            with self.subTest(change=change), self.bundle() as (_, payload, digest, _, _):
                if change == 'database':
                    (payload / 'wishlist/state/wishlist.sqlite3').write_bytes(b'broken')
                elif change == 'auth':
                    (payload / 'language/auth.json').write_text('changed')
                elif change == 'missing':
                    (payload / 'language/auth.json').unlink()
                else:
                    (payload / 'wishlist/extra').write_text('unreviewed')
                with self.assertRaises(Exception):
                    runtime.validate_payload(payload, digest)

    def test_read_only_compatibility_refuses_older_binary_without_replacing_newer_history(self):
        with self.bundle() as (_, payload, _, _, _):
            path = payload / 'wishlist/state/wishlist.sqlite3'
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, 'version'):
                operations.database_status(path, maximum_version=3)
            self.assertEqual(before, path.read_bytes())
            self.assertFalse(operations.database_status(path)['writes'])

    def test_failed_additional_capture_resumes_services_and_cannot_return_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            source = root / 'source'
            source.mkdir(mode=0o700)
            (source / 'config.yaml').write_text('{}')
            policy = {'schema_version': 1, 'include': ['config.yaml'], 'required': ['config.yaml'],
                      'exclude': [], 'sqlite': []}
            controller, journal = Mock(), Mock()
            controller.healthy.return_value = controller.stopped.return_value = True
            journal.pending.return_value = False
            with self.assertRaises(FileNotFoundError):
                capture.capture_with_pause(controller, journal, source, root / 'staged', policy,
                    components={'wishlist': {'source': root / 'absent', 'policy': operations.DOMAIN_POLICY}})
            controller.start.assert_called_once()
            journal.disarm.assert_called_once()

    def test_multiunit_resume_intent_survives_configuration_change_and_failed_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            state.chmod(0o700)
            runtime.PauseJournal(state, operations.UNITS).arm()
            with patch.object(runtime, 'SystemController') as constructor:
                constructor.return_value.healthy.return_value = False
                with self.assertRaises(capture.CaptureError):
                    runtime.recover({}, state)
                self.assertEqual(constructor.return_value.units, operations.UNITS)
                self.assertTrue(runtime.PauseJournal(state).pending())
                constructor.return_value.healthy.return_value = True
                runtime.recover({}, state)
                self.assertFalse(runtime.PauseJournal(state).pending())

    def test_writers_stop_before_readers_and_start_in_reverse_order(self):
        with patch.object(runtime, 'command', return_value=b'') as command:
            controller = runtime.SystemController({'wishlist_enabled': True})
            with patch.object(controller, 'healthy', return_value=True):
                controller.stop()
                controller.start()
            calls = [call.args[0] for call in command.call_args_list]
            self.assertEqual(calls[:3], [['/usr/bin/systemctl', 'stop', unit] for unit in operations.UNITS])
            self.assertEqual(calls[3:], [['/usr/bin/systemctl', 'start', unit] for unit in reversed(operations.UNITS)])

    def test_legacy_journal_remains_readable(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            state.chmod(0o700)
            journal = runtime.PauseJournal(state)
            journal.arm()
            self.assertEqual(json.loads(journal.path.read_text()), {'schema_version': 1, 'resume': 'haven.service'})
            self.assertTrue(journal.pending())
            self.assertEqual(journal.units, ('haven.service',))

    def test_identity_contract_refuses_shared_users_and_groups(self):
        args = dict(bot_id='123', owner_id='456', timezone='Europe/Prague', ingress_uid=1001,
                    domain_uid=1002, language_uid=1003, ingress_gid=1010, language_gid=1011)
        config = operations.configurations(**args)
        self.assertEqual(config['domain']['language']['service_uid'], 1003)
        for changed in ({'domain_uid': 1001}, {'language_uid': 1002}, {'language_gid': 1010}, {'bot_id': 'invalid'}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                operations.configurations(**{**args, **changed})

    def test_systemd_verifies_the_rendered_units(self):
        # Substitute only the executable path; the real verifier parses all hardening directives.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / 'python'
            executable.write_text('#!/bin/sh\nexit 0\n')
            executable.chmod(0o755)
            units = []
            for name in (*operations.UNITS[1:], 'haven-wishlist-collector.service', 'haven-wishlist-collector.timer',
                         'haven-wishlist-browser.service', 'haven-wishlist-egress.service'):
                source = Path('ops/templates') / (name + '.j2')
                unit = root / name
                unit.write_text(source.read_text().replace('/opt/haven-wishlist/native/venv/bin/python', str(executable))
                                .replace('/opt/haven-wishlist-browser/venv/bin/python', str(executable)))
                units.append(str(unit))
            result = subprocess.run(['systemd-analyze', 'verify', *units], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            # Unknown directives are warnings with exit code zero, so they must
            # fail acceptance explicitly instead of silently losing a guard.
            self.assertNotRegex(result.stderr, r"Unknown (?:key|lvalue)", result.stderr)


if __name__ == '__main__':
    unittest.main()
