"""Private candidates are verified in place and never substituted by pooled releases."""

import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import wheelhouse as w


class LocalFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()

        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.pool = self.root / 'pool'

        self.pool.mkdir()
        self.candidate = self.root / 'fixture-1.0-py3-none-any.whl'

        self.metadata = (
            b'Metadata-Version: 2.3\n'
            b'Name: fixture\n'
            b'Version: 1.0\n'
            b'\n'
        )

        content = io.BytesIO()

        with zipfile.ZipFile(content, 'w') as archive:
            archive.writestr('fixture-1.0.dist-info/METADATA', self.metadata)

        self.content = content.getvalue()

        self.candidate.write_bytes(self.content)
        self.digest = hashlib.sha256(self.content).hexdigest()
        self.lock = self.root / 'lock.json'

        self.envelope = {
            'wheelhouse-lock': 1,
            'target': w.target_environment(),
            'metadata': {self.digest: hashlib.sha256(self.metadata).hexdigest()},
            'resolution': {
                'lock-version': '1.0',
                'created-by': 'test',
                'packages': [
                    {
                        'name': 'fixture',
                        'version': '1.0',
                        'wheels': [
                            {'path': str(self.candidate), 'hashes': {'sha256': self.digest}},
                        ],
                    },
                ],
            },
        }

        self.write_lock()

    def write_lock(self):
        self.lock.write_text(json.dumps(self.envelope))


class LocalLocks(LocalFixture):
    def test_private_candidate_check_and_acquire_use_no_pool_copy(self):
        (self.pool / self.candidate.name).write_bytes(b'different release bytes')

        with patch.object(w.urllib.request, 'urlopen', side_effect=AssertionError('network used')):
            for action in ('check', 'acquire'):
                result = w.consume(action, self.lock, self.pool)

                self.assertEqual([], result['downloaded'])

        self.assertEqual(b'different release bytes', (self.pool / self.candidate.name).read_bytes())

    def test_missing_bound_candidate_does_not_use_identical_pool_artifact(self):
        (self.pool / self.candidate.name).write_bytes(self.content)
        self.candidate.unlink()

        for action in ('check', 'acquire'):
            with self.subTest(action=action), patch.object(w.urllib.request, 'urlopen') as request:
                with self.assertRaises(FileNotFoundError):
                    w.consume(action, self.lock, self.pool)

                request.assert_not_called()

    def test_corrupt_bound_candidate_does_not_use_identical_pool_artifact(self):
        (self.pool / self.candidate.name).write_bytes(self.content)
        self.candidate.write_bytes(self.content + b'changed')

        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            w.consume('acquire', self.lock, self.pool)

    def test_local_metadata_evidence_and_target_are_checked(self):
        self.envelope['metadata'][self.digest] = '0' * 64

        self.write_lock()

        with self.assertRaisesRegex(ValueError, 'METADATA'):
            w.consume('check', self.lock, self.pool)

        self.envelope['target']['python_version'] = '0.0'

        self.write_lock()

        with self.assertRaisesRegex(ValueError, 'different target'):
            w.load_locked(self.lock)

    def test_relative_local_origin_is_rejected(self):
        self.envelope['resolution']['packages'][0]['wheels'][0]['path'] = self.candidate.name

        self.write_lock()

        with self.assertRaisesRegex(ValueError, 'absolute path'):
            w.load_locked(self.lock)

    def test_local_provenance_cannot_claim_a_publisher(self):
        self.envelope['resolution']['packages'][0]['index'] = 'https://example.com/simple/'

        self.write_lock()

        with self.assertRaisesRegex(ValueError, 'local provenance'):
            w.load_locked(self.lock)

if __name__ == '__main__':
    unittest.main()
