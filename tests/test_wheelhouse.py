"""Run Online, then Offline in a fresh container with /trial read-only and no network.

Make owns the temporary shared volume; each test owns its temporary working files.
"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest

from packaging.requirements import Requirement
import tomli_w


POOL = Path("/trial/wheels")
LOCK = Path("/trial/wheelhouse.lock")
FIXTURE = Path(__file__).parent / "fixtures" / "pyproject.toml"
TOOL = "/opt/wheelhouse/wheelhouse.py"


def snapshot():
    return {
        str(path.relative_to(POOL)): hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        for path in POOL.rglob("*")
    }


class Commands(unittest.TestCase):
    def execute(self, *arguments):
        environment = {key: value for key, value in os.environ.items() if not key.startswith("UV_")}
        return subprocess.run([str(item) for item in arguments], capture_output=True, text=True, env=environment)

    def succeeds(self, *arguments):
        result = self.execute(*arguments)
        self.assertEqual(result.returncode, 0, f"{arguments}\n{result.stdout}\n{result.stderr}")
        return result

    def tool(self, action, *, lock=LOCK, pool=POOL, project=None, projection=None, error=None):
        arguments = [sys.executable, TOOL, action, "--lock", lock, "--pool", pool]
        if project is not None:
            arguments += ["--project", project]
        if projection is not None:
            arguments += ["--local-lock", projection]
        if error is not None:
            result = self.execute(*arguments)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn(error.lower(), result.stderr.lower())
            return None
        return json.loads(self.succeeds(*arguments).stdout)


class Online(Commands):
    def test_lock_acquire_and_reuse(self):
        POOL.mkdir()
        self.tool("lock", project=FIXTURE)
        self.assertEqual(list(POOL.iterdir()), [], "Lock creation populated the wheel pool")
        acquired = self.tool("acquire")
        self.assertTrue(acquired["artifacts"])
        self.assertCountEqual(acquired["downloaded"], acquired["artifacts"])
        self.assertEqual({path.name for path in POOL.glob("*.whl")}, set(acquired["artifacts"]))
        original_lock, original_pool = LOCK.read_bytes(), snapshot()
        self.assertEqual(self.tool("acquire")["downloaded"], [])
        with tempfile.TemporaryDirectory(prefix="wheelhouse-warm-") as temporary:
            temporary = Path(temporary)
            declaration = tomllib.loads(FIXTURE.read_text())
            declaration["project"]["dependencies"] = [
                Requirement(text).name for text in declaration["project"]["dependencies"]
            ]
            project, warm = temporary / "pyproject.toml", temporary / "warm.lock"
            project.write_text(tomli_w.dumps(declaration))
            self.tool("lock", project=project, lock=warm)
            checked = self.tool("check", lock=warm)
            self.assertCountEqual(checked["artifacts"], acquired["artifacts"])
            self.assertEqual(self.tool("acquire", lock=warm)["downloaded"], [])
        self.assertEqual(LOCK.read_bytes(), original_lock)
        self.assertEqual(snapshot(), original_pool)


class Offline(Commands):
    def test_offline_installation_and_rejection(self):
        self.assertEqual(self.tool("acquire")["downloaded"], [])
        with tempfile.TemporaryDirectory(prefix="wheelhouse-offline-") as temporary:
            temporary = Path(temporary)
            projection, venv = temporary / "pylock.toml", temporary / "venv"
            self.tool("check", projection=projection)
            self.succeeds("uv", "--no-config", "--no-cache", "venv", venv,
                          "--python", sys.executable, "--no-python-downloads")
            python = venv / "bin" / "python"
            self.succeeds("uv", "--no-config", "--no-cache", "pip", "sync", "--offline", "--no-index",
                          "--no-python-downloads", "--only-binary=:all:", "--require-hashes",
                          "--python", python, projection)
            self.succeeds(python, "-I", "-c", "import numpy, requests; assert numpy.arange(3).sum() == 3")
            self.succeeds("uv", "--no-config", "--no-cache", "pip", "check", "--python", python)

            rejected = temporary / "pylock.rejected.toml"
            empty = temporary / "empty"
            empty.mkdir()
            self.tool("check", pool=empty, projection=rejected, error="Missing locked wheels")
            self.assertFalse(rejected.exists())
            original = json.loads(LOCK.read_text())
            for case, evidence in (
                ("absent", {}),
                ("mismatched", dict.fromkeys(original["metadata"], "0" * 64)),
            ):
                with self.subTest(metadata=case):
                    altered = dict(original, metadata=evidence)
                    lock = temporary / f"{case}.lock"
                    lock.write_text(json.dumps(altered))
                    self.tool("check", lock=lock, projection=rejected, error="metadata")
                    self.assertFalse(rejected.exists())
