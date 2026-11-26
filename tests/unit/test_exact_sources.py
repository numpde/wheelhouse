"""Exact declarations prepare verified wheels and retain exclusive source identity."""

import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import tomli_w

import wheelhouse


class ExactSources(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix = "wheelhouse-exact-")

        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pool = self.root / "pool"

        self.pool.mkdir()
        self.project = self.root / "pyproject.toml"
        self.lock = self.root / "wheelhouse.lock"
        self.filename = "fixture-1.0-py3-none-any.whl"
        self.url = "https://releases.example/" + self.filename

        self.metadata = (
            b"Metadata-Version: 2.3\n"
            b"Name: fixture\n"
            b"Version: 1.0\n"
            b"\n"
        )

        content = io.BytesIO()

        with zipfile.ZipFile(content, "w") as archive:
            archive.writestr("fixture-1.0.dist-info/METADATA", self.metadata)

        self.content = content.getvalue()
        self.digest = hashlib.sha256(self.content).hexdigest()

        self.declaration = {
            "project": {"dependencies": ["fixture==1.0"]},
            "tool": {"wheelhouse": {"index": "https://publisher.example/simple/", "sources": {}}},
        }

    def declare(self, source):
        self.declaration["tool"]["wheelhouse"]["sources"] = {"FiXtUrE": source}

        self.project.write_text(tomli_w.dumps(self.declaration))

    def local(self):
        private = self.root / "private" / self.filename

        private.parent.mkdir()
        private.write_bytes(self.content)
        self.declare({"path": "private/" + self.filename, "sha256": self.digest})

        return private

    def test_remote_preparation_without_sidecar_and_warm_reuse(self):
        self.declare({"wheel": self.url, "sha256": self.digest})

        with patch("wheelhouse.urllib.request.urlopen", return_value=io.BytesIO(self.content)) as fetch:
            result = wheelhouse.prepare_package(self.project, "FIXTURE", self.pool)

            fetch.assert_called_once_with(self.url, timeout=60)
            self.assertEqual([self.filename], result["downloaded"])
            self.assertEqual(self.content, (self.pool / self.filename).read_bytes())

        with patch("wheelhouse.urllib.request.urlopen") as fetch:
            result = wheelhouse.prepare_package(self.project, "fixture", self.pool)

            fetch.assert_not_called()
            self.assertEqual([], result["downloaded"])

    def test_local_preparation_stays_private_and_checks_declared_bytes(self):
        private = self.local()
        released = self.pool / self.filename

        released.write_bytes(b"different released bytes")

        with patch("wheelhouse.urllib.request.urlopen") as fetch:
            result = wheelhouse.prepare_package(self.project, "fixture", self.pool)

            fetch.assert_not_called()
            self.assertEqual(str(private), result["path"])
            self.assertEqual(b"different released bytes", released.read_bytes())
            private.write_bytes(b"changed private bytes")

            with self.assertRaisesRegex(ValueError, "SHA-256"):
                wheelhouse.prepare_package(self.project, "fixture", self.pool)

    def test_exact_remote_requires_preparation_during_metadata_only_lock(self):
        self.declare({"wheel": self.url, "sha256": self.digest})

        with patch("wheelhouse.urllib.request.urlopen") as fetch:
            with self.assertRaisesRegex(RuntimeError, "wheel downloads are forbidden"):
                wheelhouse.lock_project(self.project, self.pool, self.lock)

            fetch.assert_not_called()

        self.assertFalse(self.lock.exists())

    def test_local_lock_excludes_same_filename_pool_candidate(self):
        private = self.local()

        (self.pool / self.filename).write_bytes(b"released bytes")

        with patch("wheelhouse.urllib.request.urlopen") as fetch:
            wheelhouse.lock_project(self.project, self.pool, self.lock)

            fetch.assert_not_called()

        package = json.loads(self.lock.read_text())["resolution"]["packages"][0]

        self.assertEqual(str(private), package["wheels"][0]["path"])
        self.assertNotIn("url", package["wheels"][0])
        private.unlink()

        with patch("wheelhouse.urllib.request.urlopen") as fetch:
            with self.assertRaisesRegex(RuntimeError, "Metadata"):
                wheelhouse.lock_project(self.project, self.pool, self.lock)

            fetch.assert_not_called()

    def test_source_validation_precedes_network(self):
        cases = (
            ({"wheel": self.url, "sha256": "invalid"}, "SHA-256"),
            ({"wheel": self.url.replace("fixture-", "different-"), "sha256": self.digest}, "does not match"),
            ({"wheel": self.url.replace("py3-none-any", "cp20-cp20-win32"), "sha256": self.digest}, "incompatible"),
            ({"wheel": self.url, "path": self.filename, "sha256": self.digest}, "table containing only"),
        )

        with patch("wheelhouse.urllib.request.urlopen") as fetch:
            for (source, message) in cases:
                with self.subTest(source=source):
                    self.declare(source)

                    with self.assertRaisesRegex(ValueError, message):
                        wheelhouse.read_project(self.project)

            fetch.assert_not_called()

    def test_non_exact_preparation_and_corrupt_download_do_not_publish(self):
        self.declare("https://publisher.example/simple/")

        with self.assertRaisesRegex(ValueError, "exact wheel"):
            wheelhouse.prepare_package(self.project, "fixture", self.pool)

        self.declare({"wheel": self.url, "sha256": self.digest})

        with patch("wheelhouse.urllib.request.urlopen", return_value=io.BytesIO(b"corrupt")):
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                wheelhouse.prepare_package(self.project, "fixture", self.pool)

        self.assertFalse(list(self.pool.glob("*.whl")))

    def test_preparation_rejects_wrong_metadata_and_python_target(self):
        for metadata in (
            self.metadata.replace(b"Name: fixture", b"Name: different"),
            self.metadata.replace(
                (
                    b"\n"
                    b"\n"
                ),
                (
                    b"\n"
                    b"Requires-Python: <2\n"
                    b"\n"
                ),
            ),
        ):
            with self.subTest(metadata=metadata):
                content = io.BytesIO()

                with zipfile.ZipFile(content, "w") as archive:
                    archive.writestr("fixture-1.0.dist-info/METADATA", metadata)

                data = content.getvalue()

                self.declare({"wheel": self.url, "sha256": hashlib.sha256(data).hexdigest()})

                with patch("wheelhouse.urllib.request.urlopen", return_value=io.BytesIO(data)):
                    with self.assertRaises(ValueError):
                        wheelhouse.prepare_package(self.project, "fixture", self.pool)

                self.assertFalse(list(self.pool.glob("*.whl")))

    def test_existing_conflicting_remote_bytes_are_never_overwritten(self):
        self.declare({"wheel": self.url, "sha256": self.digest})
        existing = self.pool / self.filename

        existing.write_bytes(b"existing conflicting bytes")

        with patch("wheelhouse.urllib.request.urlopen") as fetch:
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                wheelhouse.prepare_package(self.project, "fixture", self.pool)

            fetch.assert_not_called()

        self.assertEqual(b"existing conflicting bytes", existing.read_bytes())

    def test_remote_encoded_control_character_is_rejected_before_preparation(self):
        self.declare({"wheel": "https://releases.example/fixture-1.0-1%0A-py3-none-any.whl", "sha256": self.digest})

        with patch("wheelhouse.urllib.request.urlopen") as fetch:
            with self.assertRaisesRegex(ValueError, "Unsafe wheel filename"):
                wheelhouse.prepare_package(self.project, "fixture", self.pool)

            fetch.assert_not_called()

        self.assertFalse(list(self.pool.iterdir()))

    def test_local_control_character_is_rejected_before_preparation(self):
        self.declare({"path": "private/fixture-1.0-1\t-py3-none-any.whl", "sha256": self.digest})

        with patch("wheelhouse.urllib.request.urlopen") as fetch:
            with self.assertRaisesRegex(ValueError, "Unsafe wheel filename"):
                wheelhouse.prepare_package(self.project, "fixture", self.pool)

            fetch.assert_not_called()

        self.assertFalse(list(self.pool.iterdir()))

    def test_lock_output_cannot_overwrite_project_or_private_wheel(self):
        private = self.local()
        alias = self.root / "candidate-link"

        alias.symlink_to(private)

        with patch("wheelhouse.subprocess.run") as resolve:
            for output in (self.project, private, alias):
                with self.subTest(output=output):
                    original = output.read_bytes()

                    with self.assertRaisesRegex(ValueError, "separately"):
                        wheelhouse.lock_project(self.project, self.pool, output)

                    self.assertEqual(original, output.read_bytes())

            resolve.assert_not_called()

    def test_vendored_metadata_does_not_replace_the_wheel_metadata(self):
        content = io.BytesIO()

        with zipfile.ZipFile(content, "w") as archive:
            archive.writestr("fixture-1.0.dist-info/METADATA", self.metadata)
            archive.writestr("fixture/vendor/other-1.0.dist-info/METADATA", b"vendored metadata")

        content.seek(0)
        self.assertEqual(self.metadata, wheelhouse.wheel_metadata(content))

        with zipfile.ZipFile(content, "a") as archive:
            archive.writestr("other-1.0.dist-info/METADATA", b"second root")

        content.seek(0)

        with self.assertRaisesRegex(ValueError, "exactly one root"):
            wheelhouse.wheel_metadata(content)
