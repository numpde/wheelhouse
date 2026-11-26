"""Explicit package pages resolve through uv without fetching wheel bytes."""

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


class PackagePages(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="wheelhouse-pages-")

        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pool = self.root / "wheels"

        self.pool.mkdir()
        self.project = self.root / "pyproject.toml"
        self.lock = self.root / "wheelhouse.lock"
        self.page = "https://raw.example/commit/simple/fixture.html"
        self.resources = {}
        self.requests = []

        self.declaration = {
            "project": {"dependencies": ["fixture[api]==1.0"]},
            "tool": {
                "wheelhouse": {
                    "index": "https://publisher.example/simple/",
                    "sources": {"FiXtUrE": {"page": self.page}},
                },
            },
        }

        self.add_wheel(
            "fixture",
            self.page,
            b"Provides-Extra: api\n"
            b"Requires-Dist: dependency==1.0; extra == 'api'\n",
        )

        self.add_wheel("dependency", "https://publisher.example/simple/dependency/")
        self.project.write_text(tomli_w.dumps(self.declaration))

    def add_wheel(self, name, page, extra_metadata=b""):
        filename = name + "-1.0-py3-none-any.whl"
        url = "https://releases.example/v1.0/" + filename
        metadata = b"\n".join((b"Metadata-Version: 2.3", b"Name: " + name.encode(), b"Version: 1.0", extra_metadata, b""))
        content = io.BytesIO()

        with zipfile.ZipFile(content, "w") as archive:
            archive.writestr(name + "-1.0.dist-info/METADATA", metadata)

        wheel = content.getvalue()
        wheel_hash = hashlib.sha256(wheel).hexdigest()
        metadata_hash = hashlib.sha256(metadata).hexdigest()
        self.resources[url] = wheel
        self.resources[url + ".metadata"] = metadata

        self.resources[page] = (
            '<a href="' + url + '#sha256=' + wheel_hash + '" data-core-metadata="sha256=' + metadata_hash + '">'
            + filename + '</a>'
        ).encode()

    def fetch(self, request, **options):
        url = request.full_url if isinstance(request, wheelhouse.urllib.request.Request) else request

        self.requests.append(url)

        if url not in self.resources:
            raise wheelhouse.urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        response = io.BytesIO(self.resources[url])
        response.geturl = lambda: url

        return response

    def test_page_extras_and_cached_metadata(self):
        with patch("wheelhouse.urllib.request.urlopen", side_effect=self.fetch):
            wheelhouse.lock_project(self.project, self.pool, self.lock)

            self.assertIn(self.page, self.requests)
            self.assertIn("https://publisher.example/simple/dependency/", self.requests)
            self.assertFalse(any(url.endswith(".whl") for url in self.requests))
            self.assertFalse(list(self.pool.glob("*.whl")))
            document = json.loads(self.lock.read_text())
            packages = {item["name"]: item for item in document["resolution"]["packages"]}

            self.assertEqual(self.page, packages["fixture"]["index"])
            self.assertEqual({"fixture", "dependency"}, set(packages))
            acquired = wheelhouse.consume("acquire", self.lock, self.pool)

            self.assertEqual(2, len(acquired["downloaded"]))
            self.requests.clear()
            wheelhouse.lock_project(self.project, self.pool, self.lock)

            self.assertFalse(any((url.endswith(".metadata") or url.endswith(".whl")) for url in self.requests))

        with patch("wheelhouse.urllib.request.urlopen") as request:
            wheelhouse.consume("check", self.lock, self.pool, self.root / "pylock.toml")

            request.assert_not_called()

    def test_bad_sidecar_preserves_existing_lock(self):
        self.lock.write_bytes(b"previous lock")
        self.resources["https://releases.example/v1.0/fixture-1.0-py3-none-any.whl.metadata"] += b"changed"

        with patch("wheelhouse.urllib.request.urlopen", side_effect=self.fetch):
            with self.assertRaisesRegex(RuntimeError, "metadata hash mismatch"):
                wheelhouse.lock_project(self.project, self.pool, self.lock)

        self.assertEqual(b"previous lock", self.lock.read_bytes())
        self.assertFalse(list(self.pool.glob("*.whl")))

    def test_missing_page_does_not_fall_back_to_default_index(self):
        default_page = "https://publisher.example/simple/fixture/"
        self.resources[default_page] = self.resources.pop(self.page)

        with patch("wheelhouse.urllib.request.urlopen", side_effect=self.fetch):
            with self.assertRaisesRegex(RuntimeError, "resolve"):
                wheelhouse.lock_project(self.project, self.pool, self.lock)

        self.assertIn(self.page, self.requests)
        self.assertNotIn(default_page, self.requests)
        self.assertFalse(self.lock.exists())

    def test_ambiguous_source_fails_before_resolution(self):
        self.declaration["tool"]["wheelhouse"]["sources"]["FiXtUrE"]["index"] = "https://publisher.example/simple/"

        self.project.write_text(tomli_w.dumps(self.declaration))

        with self.assertRaisesRegex(ValueError, "table containing only"):
            wheelhouse.read_project(self.project)
