"""Complete checked selections produce hashed, relocatable installation inputs."""

import json
from pathlib import Path
import unittest

import wheelhouse
from unit import test_artifacts


class Projection(unittest.TestCase):
    setUp = test_artifacts.Artifacts.setUp

    def prepare(self):
        self.lock.write_text(json.dumps(self.envelope))
        (self.pool / self.name).write_bytes(self.wheel)
        self.requirements = self.root / "install.txt"
        self.map_path = self.root / "destinations.json"

    def check(self):
        return wheelhouse.consume("check", self.lock, self.pool, requirements_output=self.requirements)

    def test_check_emits_complete_evidence_and_requirements(self):
        self.prepare()
        result = self.check()
        entry = result["artifact_entries"][0]
        source = (self.pool / self.name).resolve()

        self.assertEqual([self.name], result["artifacts"])
        self.assertEqual([], result["downloaded"])

        self.assertEqual(
            {
                "package": "fixture",
                "version": "1.0",
                "filename": self.name,
                "sha256": self.digest,
                "source_path": str(source),
            },
            entry,
        )

        self.assertEqual(
            f"{source.as_uri()} --hash=sha256:{self.digest}\n",  # machine-string: pip requirements syntax requires wheel URLs and hashes without expression labels.
            self.requirements.read_text(),
        )

        wheelhouse.consume("check", self.lock, self.pool, self.projection)
        self.assertIn(str(source), self.projection.read_text())

    def test_mapped_paths_encode_spaces_and_preserve_source_evidence(self):
        self.prepare()
        destination = Path("/container/space # percent%") / self.name

        mapping = {
            "artifacts": [
                {
                    "source_path": str((self.pool / self.name).resolve()),
                    "sha256": self.digest,
                    "destination": str(destination),
                },
            ],
        }

        self.map_path.write_text(json.dumps(mapping))

        result = wheelhouse.consume(
            "check",
            self.lock,
            self.pool,
            requirements_output=self.requirements,
            destination_map=self.map_path,
        )

        self.assertEqual(
            f"{destination.as_uri()} --hash=sha256:{self.digest}\n",  # machine-string: pip requirements syntax requires wheel URLs and hashes without expression labels.
            self.requirements.read_text(),
        )

        self.assertEqual(str((self.pool / self.name).resolve()), result["artifact_entries"][0]["source_path"])

    def test_invalid_maps_leave_existing_output_untouched(self):
        self.prepare()

        good = {
            "source_path": str((self.pool / self.name).resolve()),
            "sha256": self.digest,
            "destination": "/container/" + self.name,
        }

        cases = [
            [],
            [good, good],
            [{**good, "sha256": "0" * 64}],
            [{**good, "source_path": "/other/" + self.name}],
            [{**good, "destination": "/container/../" + self.name}],
            [{**good, "destination": "relative/" + self.name}],
            [{**good, "destination": "/container/renamed.whl"}],
        ]

        for mappings in cases:
            with self.subTest(mappings=mappings):
                self.map_path.write_text(json.dumps({"artifacts": mappings}))
                self.requirements.write_text("existing")

                with self.assertRaises(ValueError):
                    wheelhouse.consume(
                        "check",
                        self.lock,
                        self.pool,
                        requirements_output=self.requirements,
                        destination_map=self.map_path,
                    )

                self.assertEqual("existing", self.requirements.read_text())

    def test_corruption_keeps_existing_projection(self):
        self.prepare()
        self.requirements.write_text("existing")
        (self.pool / self.name).write_bytes(b"bad")

        with self.assertRaises(ValueError):
            self.check()

        self.assertEqual("existing", self.requirements.read_text())
        self.assertFalse(self.projection.exists())

    def test_output_cannot_replace_source_map_or_lock(self):
        self.prepare()

        for output in (self.lock, self.pool / self.name):
            with self.subTest(output=output), self.assertRaises(ValueError):
                wheelhouse.consume("check", self.lock, self.pool, requirements_output=output)

        with self.assertRaises(ValueError):
            wheelhouse.consume("check", self.lock, self.pool, self.projection, self.requirements)

        self.assertFalse(self.projection.exists())
        self.assertFalse(self.requirements.exists())

        self.map_path.write_text("{}")

        with self.assertRaises(ValueError):
            wheelhouse.consume(
                "check",
                self.lock,
                self.pool,
                requirements_output=self.map_path,
                destination_map=self.map_path,
            )

    def test_map_rejects_target_collision(self):
        entries = [
            {"source_path": "/a/" + self.name, "sha256": "a" * 64, "filename": self.name},
            {"source_path": "/b/" + self.name, "sha256": "b" * 64, "filename": self.name},
        ]

        mappings = [
            {
                "source_path": entry["source_path"],
                "sha256": entry["sha256"],
                "destination": "/target/" + self.name,
            } for entry in entries
        ]

        with self.assertRaises(ValueError):
            wheelhouse.installation_paths(entries, {"artifacts": mappings})

    def test_output_alias_cannot_overwrite_private_candidate(self):
        private = self.root / "private"
        alias = self.root / "alias"

        private.mkdir()
        alias.symlink_to(private, target_is_directory=True)

        candidate = private / self.name

        candidate.write_bytes(self.wheel)
        package = self.envelope["resolution"]["packages"][0]

        package.pop("index")
        package["wheels"] = [{"path": str(alias / self.name), "hashes": {"sha256": self.digest}}]

        self.lock.write_text(json.dumps(self.envelope))

        with self.assertRaisesRegex(ValueError, "selected wheel"):
            wheelhouse.consume("check", self.lock, self.pool, requirements_output=candidate)

        self.assertEqual(self.wheel, candidate.read_bytes())


if __name__ == "__main__":
    unittest.main()
