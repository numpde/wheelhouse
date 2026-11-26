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

UV = (
    "uv",  # Exercise the resolver and installer supplied by the proof image.
    "--no-config",  # Exclude ambient uv configuration from the proof.
    "--no-cache",  # Disable uv's persistent cache for each invocation.
)


def snapshot():
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in POOL.iterdir()}


class Commands(unittest.TestCase):
    def command(self, *arguments, error=None):
        result = subprocess.run([str(item) for item in arguments], capture_output=True, text=True)

        if error is None:
            self.assertEqual(
                0,
                result.returncode,
                msg=f"{arguments=!s}\n"
                f"{result.stdout=!s}\n"
                f"{result.stderr=!s}",
            )
        else:
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(error.lower(), result.stderr.lower())

        return result.stdout

    def tool(self, action, *, error=None, **options):
        arguments = [
            sys.executable,  # Use the proof image's Python interpreter.
            "/opt/wheelhouse/wheelhouse.py",  # Exercise the implementation baked into the image.
            action,  # Select lock creation, acquisition, or offline verification.
        ]

        for (name, value) in ({"lock": LOCK, "pool": POOL} | options).items():
            arguments += [
                "--" + name.replace("_", "-"),  # Translate test keyword names to CLI option names.
                value,  # Pass the caller's path; command() converts it to text.
            ]

        output = self.command(*arguments, error=error)

        return json.loads(output) if error is None else None


class Online(Commands):
    def test_lock_acquire_and_reuse(self):
        POOL.mkdir()
        self.tool("lock", project=FIXTURE)
        self.assertEqual([], list(POOL.iterdir()), msg="Lock creation populated the wheel pool")
        acquired = self.tool("acquire")

        self.assertCountEqual(acquired["artifacts"], acquired["downloaded"])
        (original_lock, original_pool) = (LOCK.read_bytes(), snapshot())

        with tempfile.TemporaryDirectory(prefix="wheelhouse-warm-") as temporary:
            temporary = Path(temporary)
            declaration = tomllib.loads(FIXTURE.read_text())

            declaration["project"]["dependencies"] = [
                Requirement(text).name for text in declaration["project"]["dependencies"]
            ]

            (project, warm) = (temporary / "pyproject.toml", temporary / "warm.lock")

            project.write_text(tomli_w.dumps(declaration))
            self.tool("lock", project=project, lock=warm)
            reused = self.tool("acquire", lock=warm)

            self.assertCountEqual(acquired["artifacts"], reused["artifacts"])
            self.assertEqual([], reused["downloaded"])

        self.assertEqual(original_lock, LOCK.read_bytes())
        self.assertEqual(original_pool, snapshot())


class Offline(Commands):
    def test_offline_installation(self):
        self.assertEqual([], self.tool("acquire")["downloaded"])

        with tempfile.TemporaryDirectory(prefix="wheelhouse-offline-") as temporary:
            temporary = Path(temporary)
            (projection, venv) = (temporary / "pylock.toml", temporary / "venv")

            self.tool("check", local_lock=projection)

            self.command(
                *UV,  # Apply the shared configuration and cache isolation flags.
                "venv",  # Create an empty environment for the offline installation.
                venv,  # Keep the environment inside this disposable test directory.
                "--python",  # Use the same target interpreter as lock creation.
                sys.executable,  # Select the interpreter supplied by the proof image.
                "--no-python-downloads",  # Forbid interpreter acquisition during the offline proof.
            )

            python = venv / "bin" / "python"

            self.command(
                *UV,  # Apply the shared configuration and cache isolation flags.
                "pip",  # Select uv's package installation interface.
                "sync",  # Make the empty environment match the projected lock exactly.
                "--offline",  # Reject any attempt to acquire remote artifacts.
                "--no-index",  # Exclude registry lookups even if network access returns.
                "--no-python-downloads",  # Forbid acquiring an interpreter during installation.
                "--only-binary=:all:",  # Exclude source builds from the wheel-only proof.
                "--require-hashes",  # Verify each installed artifact against the projected lock.
                "--python",  # Install into the disposable environment, not the proof image.
                python,  # Use the interpreter created by the preceding venv command.
                projection,  # Read the checked lock containing local wheel paths.
            )

            self.command(
                python,  # Exercise imports from the newly installed environment.
                "-I",  # Exclude ambient Python paths and user-site packages.
                "-c",  # Run the installation smoke check without a script file.
                "import numpy, requests; assert numpy.arange(3).sum() == 3",  # Check imports and native NumPy execution.
            )

            self.command(
                *UV,  # Apply the shared configuration and cache isolation flags.
                "pip",  # Select uv's package inspection interface.
                "check",  # Verify that installed dependency requirements are satisfied.
                "--python",  # Inspect the disposable environment.
                python,  # Select the interpreter whose packages were just installed.
            )

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
