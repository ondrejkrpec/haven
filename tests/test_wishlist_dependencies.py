"""Dependency artifacts must match the lock and survive inspection without execution."""

import hashlib
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

from scripts import wishlist_dependencies as dependencies
from scripts import render_model


class DependencyTests(unittest.TestCase):
    def test_archive_matches_verified_tree_and_refuses_extra_or_linked_members(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / 'artifact'
            root.mkdir()
            (root / 'module.py').write_text('SYNTHETIC')
            lock = dependencies.ROOT / 'requirements-wishlist.txt'
            manifest = {'schema_version': 1, 'python': f'{sys.version_info.major}.{sys.version_info.minor}',
                        'requirements_sha256': hashlib.sha256(lock.read_bytes()).hexdigest(),
                        'files': dependencies.files(root)}
            (root / dependencies.MANIFEST).write_text(json.dumps(manifest))
            dependencies.bundle(root)
            self.assertEqual(dependencies.verify_bundle(root), manifest)
            archive = Path(str(root) + '.tar.gz')
            for kind in ('extra', 'symlink'):
                with self.subTest(kind=kind), tarfile.open(archive, 'w:gz') as output:
                    output.add(root / 'module.py', arcname='module.py')
                    output.add(root / dependencies.MANIFEST, arcname=dependencies.MANIFEST)
                    entry = tarfile.TarInfo('../outside' if kind == 'extra' else 'module-link')
                    if kind == 'symlink':
                        entry.type = tarfile.SYMTYPE
                        entry.linkname = 'module.py'
                    output.addfile(entry)
                with self.assertRaises(ValueError):
                    dependencies.verify_bundle(root)

    def test_manifest_rejects_corruption_unreviewed_files_links_and_other_python(self):
        for change in ('none', 'changed', 'extra', 'symlink', 'lock', 'python'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                root = base / 'artifact'
                root.mkdir()
                lock = base / 'requirements.txt'
                lock.write_text('synthetic==1\n')
                (root / 'module.py').write_text('SYNTHETIC_PUBLIC_CODE')
                manifest = {'schema_version': 1, 'python': f'{sys.version_info.major}.{sys.version_info.minor}',
                            'requirements_sha256': hashlib.sha256(lock.read_bytes()).hexdigest(),
                            'files': dependencies.files(root)}
                if change == 'python':
                    manifest['python'] = '0.1'
                (root / dependencies.MANIFEST).write_text(json.dumps(manifest))
                if change == 'changed':
                    (root / 'module.py').write_text('CHANGED')
                elif change == 'extra':
                    (root / 'other.py').write_text('UNREVIEWED')
                elif change == 'symlink':
                    (root / 'module.py').unlink()
                    (root / 'module.py').symlink_to(lock)
                elif change == 'lock':
                    lock.write_text('different==2\n')
                if change == 'none':
                    self.assertEqual(dependencies.verify(root, requirements=lock), manifest)
                else:
                    with self.assertRaises(ValueError):
                        dependencies.verify(root, requirements=lock)

    def test_interpreter_render_is_separate_from_enabled_owner_transport(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / 'instance.yaml'
            source.write_text('''schema_version: 1
instance:
  name: synthetic
  timezone: Europe/Prague
host:
  address: synthetic.example.com
model:
  provider: openai-codex
  name: gpt-synthetic
telegram:
  enabled: true
  owner_id: "123"
''')
            native = render_model.render(source, base / 'profile.yaml', interpreter=True)
            self.assertNotIn('platforms', native)
            self.assertEqual(native['fallback_providers'], [])
            self.assertFalse(native['auth']['adopt_external_logins'])
            self.assertEqual((base / 'profile.yaml').stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError):
                render_model.render(source, base / 'invalid.yaml', interpreter=True, service=True)
            self.assertFalse((base / 'invalid.yaml').exists())


if __name__ == '__main__':
    unittest.main()
