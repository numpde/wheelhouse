"""Qualify bundle bytes independently of application dependencies on sys.path."""

import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from tools.build_bundle import build


class BundleTests(unittest.TestCase):
    def test_reproducible_bundle_runs_without_site_packages(self):
        source = Path(__file__).resolve().parents[2]
        first = io.BytesIO()
        second = io.BytesIO()

        build(source, first)
        build(source, second)
        self.assertEqual(first.getvalue(), second.getvalue())

        with zipfile.ZipFile(first) as archive:
            self.assertEqual(
                (source / 'wheelhouse.py').read_bytes(),
                archive.read('wheelhouse.py'),
            )

            self.assertIn('packaging/__init__.py', archive.namelist())
            self.assertIn('tomli_w/__init__.py', archive.namelist())

        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary) / 'wheelhouse.pyz'

            bundle.write_bytes(first.getvalue())

            result = subprocess.run(
                [sys.executable, '-I', '-S', str(bundle), '--help'],
                capture_output=True,
                text=True,
                check=True,
            )

            self.assertIn('acquire', result.stdout)
