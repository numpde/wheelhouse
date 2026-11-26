"""Make runs Online, then Offline with /trial read-only and networking disabled."""

import hashlib
import json
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
UV = ("uv", "--no-config", "--no-cache")


def snapshot():
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in POOL.iterdir()}


class Commands(unittest.TestCase):
    def command(self, *arguments, error=None):
        result = subprocess.run([str(item) for item in arguments], capture_output=True, text=True)
        if error is None:
            self.assertEqual(result.returncode, 0, f"{arguments}\n{result.stdout}\n{result.stderr}")
        else:
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(error.lower(), result.stderr.lower())
        return result.stdout

    def tool(self, action, *, error=None, **options):
        arguments = [sys.executable, "/opt/wheelhouse/wheelhouse.py", action]
        for name, value in ({"lock": LOCK, "pool": POOL} | options).items():
            arguments += ["--" + name.replace("_", "-"), value]
        output = self.command(*arguments, error=error)
        return json.loads(output) if error is None else None


class Online(Commands):
    def test_lock_acquire_and_reuse(self):
        POOL.mkdir()
        self.tool("lock", project=FIXTURE)
        self.assertEqual(list(POOL.iterdir()), [], "Lock creation populated the wheel pool")
        acquired = self.tool("acquire")
        self.assertCountEqual(acquired["downloaded"], acquired["artifacts"])
        original_lock, original_pool = LOCK.read_bytes(), snapshot()
        with tempfile.TemporaryDirectory(prefix="wheelhouse-warm-") as temporary:
            temporary = Path(temporary)
            declaration = tomllib.loads(FIXTURE.read_text())
            declaration["project"]["dependencies"] = [
                Requirement(text).name for text in declaration["project"]["dependencies"]
            ]
            project, warm = temporary / "pyproject.toml", temporary / "warm.lock"
            project.write_text(tomli_w.dumps(declaration))
            self.tool("lock", project=project, lock=warm)
            reused = self.tool("acquire", lock=warm)
            self.assertCountEqual(reused["artifacts"], acquired["artifacts"])
            self.assertEqual(reused["downloaded"], [])
        self.assertEqual(LOCK.read_bytes(), original_lock)
        self.assertEqual(snapshot(), original_pool)


class Offline(Commands):
    def test_offline_installation(self):
        self.assertEqual(self.tool("acquire")["downloaded"], [])
        with tempfile.TemporaryDirectory(prefix="wheelhouse-offline-") as temporary:
            temporary = Path(temporary)
            projection, venv = temporary / "pylock.toml", temporary / "venv"
            self.tool("check", local_lock=projection)
            self.command(*UV, "venv", venv, "--python", sys.executable, "--no-python-downloads")
            python = venv / "bin" / "python"
            self.command(*UV, "pip", "sync", "--offline", "--no-index",
                         "--no-python-downloads", "--only-binary=:all:", "--require-hashes",
                         "--python", python, projection)
            self.command(python, "-I", "-c", "import numpy, requests; assert numpy.arange(3).sum() == 3")
            self.command(*UV, "pip", "check", "--python", python)

            rejected = temporary / "pylock.rejected.toml"
            empty = temporary / "empty"
            empty.mkdir()
            self.tool("check", pool=empty, local_lock=rejected, error="Missing locked wheels")
            altered = json.loads(LOCK.read_text())
            altered["metadata"] = {}
            lock = temporary / "missing-metadata.lock"
            lock.write_text(json.dumps(altered))
            self.tool("check", lock=lock, local_lock=rejected, error="metadata")
            self.assertFalse(rejected.exists())
