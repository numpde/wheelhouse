"""Offline uv uses real private and pooled metadata without publisher requests."""

from unit import test_offline as fixtures
import wheelhouse as w
from packaging.pylock import PylockValidationError
import hashlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile
import tomli_w


class OfflineResolver(fixtures.LocalFixture):
    def setUp(self):
        super().setUp()
        self.project = self.root / 'pyproject.toml'

        self.declaration = {
            'project': {'dependencies': ['fixture==1.0']},
            'tool': {
                'wheelhouse': {
                    'index': 'https://publisher.example/simple',
                    'sources': {
                        'fixture': {'path': str(self.candidate), 'sha256': self.digest},
                    },
                },
            },
        }

        self.write_project()

    def write_project(self):
        self.project.write_text(tomli_w.dumps(self.declaration))

    def wheel(self, name, version='1.0', requires=b''):
        content = io.BytesIO()

        metadata = (
            (b'Metadata-Version: 2.3\n' b'Name: ') +
            name.encode() +
            (b'\n' b'Version: ') +
            version.encode() +
            b'\n' +
            requires +
            b'\n'
        )

        with zipfile.ZipFile(content, 'w') as archive:
            archive.writestr(name + '-' + version + '.dist-info/METADATA', metadata)

        return content.getvalue()

    def test_offline_private_candidate_transitive_pool_dependency(self):
        self.candidate.write_bytes(self.wheel('fixture', requires=b'Requires-Dist: dependency==1.0\n'))
        self.declaration['tool']['wheelhouse']['sources']['fixture']['sha256'] = hashlib.sha256(self.candidate.read_bytes()).hexdigest()

        self.write_project()
        dependency = self.pool / 'dependency-1.0-py3-none-any.whl'

        dependency.write_bytes(self.wheel('dependency'))
        (self.pool / self.candidate.name).write_bytes(b'conflicting pool release')

        with patch.object(w.urllib.request, 'urlopen', side_effect=AssertionError('publisher requested')):
            w.lock_project(self.project, self.pool, self.lock, offline=True)
            w.consume('check', self.lock, self.pool)

        packages = {item['name']:item for item in json.loads(self.lock.read_text())['resolution']['packages']}

        self.assertEqual({'fixture', 'dependency'}, set(packages))
        self.assertEqual(str(self.candidate), packages['fixture']['wheels'][0]['path'])
        self.assertEqual(str(dependency), packages['dependency']['wheels'][0]['path'])
        self.assertTrue(all(('index' not in item) for item in packages.values()))

    def test_offline_missing_transitive_fails_with_locations_and_preserves_lock(self):
        self.candidate.write_bytes(self.wheel('fixture', requires=b'Requires-Dist: missing==1.0\n'))
        self.declaration['tool']['wheelhouse']['sources']['fixture']['sha256'] = hashlib.sha256(self.candidate.read_bytes()).hexdigest()

        self.write_project()
        self.lock.write_bytes(b'previous')

        with patch.object(w.urllib.request, 'urlopen', side_effect=AssertionError('publisher requested')):
            with self.assertRaisesRegex(RuntimeError, 'missing') as caught:
                w.lock_project(self.project, self.pool, self.lock, offline=True)

        self.assertIn(str(self.pool), str(caught.exception))
        self.assertIn(str(self.candidate), str(caught.exception))
        self.assertEqual(b'previous', self.lock.read_bytes())

    def test_offline_remote_binding_does_not_substitute_pool(self):
        (self.pool / self.candidate.name).write_bytes(self.content)
        self.declaration['tool']['wheelhouse']['sources']['fixture'] = {'wheel':'https://publisher.example/' + self.candidate.name, 'sha256':self.digest}

        self.write_project()

        with patch.object(w.urllib.request, 'urlopen', side_effect=AssertionError('publisher requested')):
            with self.assertRaisesRegex(RuntimeError, 'local wheel binding'):
                w.lock_project(self.project, self.pool, self.lock, offline=True)

    def test_wrong_name_local_lock_rejected(self):
        self.envelope['resolution']['packages'][0]['name'] = 'other'

        self.write_lock()

        with self.assertRaisesRegex(PylockValidationError, 'not consistent'):
            w.consume('check', self.lock, self.pool)

if __name__ == '__main__':
    unittest.main()
