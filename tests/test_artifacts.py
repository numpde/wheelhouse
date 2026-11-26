"""Acquisition boundaries, using a tiny wheel and controlled HTTP responses."""

import hashlib
import io
import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch
import zipfile

import wheelhouse


class Response(io.BytesIO):
    def __init__(self, content, length=None):
        super().__init__(content)
        self.headers = {"Content-Length": str(len(content) if length is None else length)}


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
        self.url = f"https://publisher.example/wheels/{self.name}"
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
                    "wheels": [{"url": self.url, "hashes": {"sha256": self.digest}}],
                }],
            },
            "metadata": {self.digest: self.metadata_digest},
        }
        self.write_lock()

    def write_lock(self):
        self.lock.write_text(json.dumps(self.envelope))
        self.original_lock = self.lock.read_bytes()

    def consume(self, action="acquire"):
        return wheelhouse.consume(action, self.lock, self.pool, self.projection)

    def test_download_reuse_and_local_projection(self):
        incompatible = "https://publisher.example/wheels/fixture-1.0-cp27-cp27m-win32.whl"
        self.envelope["resolution"]["packages"][0]["wheels"].insert(
            0, {"url": incompatible, "hashes": {"sha256": "0" * 64}}
        )
        self.envelope["metadata"]["0" * 64] = self.envelope["metadata"][self.digest]
        self.write_lock()
        with patch("wheelhouse.urllib.request.urlopen", side_effect=lambda *a, **k: Response(self.wheel)) as request:
            acquired = self.consume()
            self.assertEqual(acquired, {"artifacts": [self.name], "downloaded": [self.name]})
            self.assertEqual(self.consume()["downloaded"], [])
            self.assertEqual(self.consume("check")["artifacts"], [self.name])
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0], self.url)
        self.assertEqual((self.pool / self.name).read_bytes(), self.wheel)
        projected = tomllib.loads(self.projection.read_text())["packages"][0]["wheels"][0]
        self.assertEqual(projected["path"], str(self.pool / self.name))
        self.assertEqual(projected["hashes"]["sha256"], self.digest)
        self.assertNotIn("url", projected)
        self.assertEqual(self.lock.read_bytes(), self.original_lock)

    def test_interrupted_download_retries_from_clean_file(self):
        class Interrupted(Response):
            def read(self, size=-1):
                if self.tell():
                    raise ConnectionResetError("connection lost during transfer")
                return super().read(17)

        responses = [Interrupted(self.wheel), Response(self.wheel)]
        with patch("wheelhouse.time.sleep"), patch(
            "wheelhouse.urllib.request.urlopen", side_effect=responses
        ) as request:
            acquired = self.consume()
        self.assertEqual(request.call_count, 2)
        self.assertEqual(acquired["downloaded"], [self.name])
        self.assertEqual((self.pool / self.name).read_bytes(), self.wheel)
        self.assertCountEqual(
            [path.name for path in self.pool.iterdir()], [self.name, ".wheelhouse-acquire.lock"]
        )
        self.assertEqual(self.lock.read_bytes(), self.original_lock)

    def test_failed_downloads_leave_no_artifact(self):
        for case in ("hash", "metadata", "incomplete"):
            with self.subTest(case=case):
                content = self.wheel + b"corrupt" if case == "hash" else self.wheel
                length = len(content) + 1 if case == "incomplete" else len(content)
                self.envelope["metadata"][self.digest] = "0" * 64 if case == "metadata" else self.metadata_digest
                self.write_lock()
                with (
                    patch("wheelhouse.urllib.request.urlopen", side_effect=lambda *a, **k: Response(content, length)) as request,
                    patch("wheelhouse.time.sleep"),
                ):
                    with self.assertRaises(ValueError):
                        self.consume()
                self.assertEqual(request.call_count, 3 if case == "incomplete" else 1)
                self.assertFalse(self.projection.exists())
                self.assertEqual(
                    [path.name for path in self.pool.iterdir() if path.name != ".wheelhouse-acquire.lock"], []
                )
                self.assertEqual(self.lock.read_bytes(), self.original_lock)

    def test_cached_conflicts_preserve_files(self):
        external = self.root / "original.whl"
        external.write_bytes(self.wheel)
        cached = self.pool / self.name
        for case in ("hash", "symlink", "metadata"):
            with self.subTest(case=case):
                if case == "symlink":
                    cached.symlink_to(external)
                else:
                    cached.write_bytes(b"conflicting bytes" if case == "hash" else self.wheel)
                self.envelope["metadata"][self.digest] = "0" * 64 if case == "metadata" else self.metadata_digest
                self.write_lock()
                original = cached.read_bytes()
                with patch("wheelhouse.urllib.request.urlopen") as request:
                    for action in ("check", "acquire"):
                        with self.subTest(action=action), self.assertRaises(ValueError):
                            self.consume(action)
                request.assert_not_called()
                self.assertEqual(cached.read_bytes(), original)
                self.assertEqual(cached.is_symlink(), case == "symlink")
                self.assertEqual(external.read_bytes(), self.wheel)
                self.assertEqual(list(self.pool.iterdir()), [cached])
                self.assertFalse(self.projection.exists())
                self.assertEqual(self.lock.read_bytes(), self.original_lock)
                cached.unlink()
