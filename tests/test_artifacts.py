"""Rejected wheels must not be published or replace an existing pool file."""

import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import wheelhouse


class Artifacts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="wheelhouse-artifacts-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pool = self.root / "wheels"
        self.pool.mkdir()
        self.lock = self.root / "wheelhouse.lock"
        self.projection = self.root / "pylock.toml"
        self.name = "fixture-1.0-py3-none-any.whl"
        metadata = b"Metadata-Version: 2.3\nName: fixture\nVersion: 1.0\n\n"
        content = io.BytesIO()
        with zipfile.ZipFile(content, "w") as archive:
            archive.writestr("fixture-1.0.dist-info/METADATA", metadata)
        self.wheel = content.getvalue()
        self.digest = hashlib.sha256(self.wheel).hexdigest()
        self.metadata_digest = hashlib.sha256(metadata).hexdigest()
        self.envelope = {
            "wheelhouse-lock": 1,
            "target": wheelhouse.target_environment(),
            "resolution": {
                "lock-version": "1.0",
                "created-by": "wheelhouse-tests",
                "packages": [{
                    "name": "fixture", "version": "1.0",
                    "index": "https://publisher.example/simple",
                    "wheels": [{
                        "url": f"https://publisher.example/{self.name}",
                        "hashes": {"sha256": self.digest},
                    }],
                }],
            },
            "metadata": {self.digest: self.metadata_digest},
        }

    def reject(self):
        original = json.dumps(self.envelope)
        self.lock.write_text(original)
        with self.assertRaises(ValueError):
            wheelhouse.consume("acquire", self.lock, self.pool, self.projection)
        self.assertEqual(self.lock.read_text(), original)
        self.assertFalse(self.projection.exists())

    def test_bad_downloads_are_not_published(self):
        for case in ("hash", "metadata"):
            with self.subTest(case=case):
                content = self.wheel + b"corrupt" if case == "hash" else self.wheel
                self.envelope["metadata"][self.digest] = "0" * 64 if case == "metadata" else self.metadata_digest
                with patch("wheelhouse.urllib.request.urlopen", side_effect=lambda *a, **k: io.BytesIO(content)):
                    self.reject()
                self.assertFalse(list(self.pool.glob("*.whl")))

    def test_cached_conflicts_are_preserved(self):
        cached = self.pool / self.name
        for case in ("hash", "metadata"):
            with self.subTest(case=case):
                content = b"conflicting bytes" if case == "hash" else self.wheel
                cached.write_bytes(content)
                self.envelope["metadata"][self.digest] = "0" * 64 if case == "metadata" else self.metadata_digest
                with patch("wheelhouse.urllib.request.urlopen") as request:
                    self.reject()
                request.assert_not_called()
                self.assertEqual(cached.read_bytes(), content)
