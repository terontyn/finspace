from __future__ import annotations

import io
import subprocess
import sys
import unittest
from unittest.mock import patch

from scripts import validate_compose_topology as validator


class ComposeVersionTest(unittest.TestCase):
    def test_parse_compose_version(self) -> None:
        cases = [
            ("2.40.3", (2, 40, 3)),
            ("v2.40.3", (2, 40, 3)),
            ("2.24.4-desktop.1", (2, 24, 4)),
        ]
        for raw_version, expected in cases:
            with self.subTest(raw_version=raw_version):
                self.assertEqual(validator.parse_compose_version(raw_version), expected)

    def test_compose_version_below_minimum_fails(self) -> None:
        with self.assertRaisesRegex(
            validator.TopologyError,
            r"Docker Compose >= 2\.24\.4 is required for !override",
        ):
            validator.require_supported_compose_version("2.24.3")

    def test_compose_version_at_or_above_minimum_passes(self) -> None:
        for raw_version in ("2.24.4", "2.40.3"):
            with self.subTest(raw_version=raw_version):
                validator.require_supported_compose_version(raw_version)

    def test_unknown_compose_version_fails(self) -> None:
        for raw_version in ("", "Docker Compose version unknown", "2.24"):
            with self.subTest(raw_version=raw_version):
                with self.assertRaisesRegex(
                    validator.TopologyError,
                    "Unable to determine Docker Compose version",
                ):
                    validator.parse_compose_version(raw_version)

    def test_compose_cli_failure_fails_closed(self) -> None:
        result = subprocess.CompletedProcess(
            args=["docker", "compose", "version", "--short"],
            returncode=1,
            stdout="",
            stderr="Docker Compose is unavailable",
        )
        with (
            patch.object(validator.subprocess, "run", return_value=result),
            self.assertRaisesRegex(
                validator.TopologyError,
                "Unable to determine Docker Compose version",
            ),
        ):
            validator._check_compose_version()

    def test_stdin_mode_does_not_check_compose_version(self) -> None:
        with (
            patch.object(sys, "argv", ["validate_compose_topology.py", "production", "--stdin"]),
            patch.object(sys, "stdin", io.StringIO("{}")),
            patch.object(validator, "validate_production"),
            patch.object(
                validator,
                "_check_compose_version",
                side_effect=AssertionError("version check must not run"),
            ),
        ):
            self.assertEqual(validator.main(), 0)


if __name__ == "__main__":
    unittest.main()
