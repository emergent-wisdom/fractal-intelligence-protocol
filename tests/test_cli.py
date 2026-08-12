from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fractal_protocol.cli import _is_loopback_host, _serve, build_parser


class CLITests(unittest.TestCase):
    def test_loopback_detection(self) -> None:
        self.assertTrue(_is_loopback_host("localhost"))
        self.assertTrue(_is_loopback_host("127.0.0.1"))
        self.assertTrue(_is_loopback_host("::1"))
        self.assertFalse(_is_loopback_host("0.0.0.0"))
        self.assertFalse(_is_loopback_host("coordinator.example"))

    def test_non_loopback_plaintext_is_refused_before_database_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "should-not-exist.db"
            args = build_parser().parse_args(
                [
                    "serve",
                    "--host",
                    "0.0.0.0",
                    "--database",
                    str(database),
                    "--admin-token",
                    "test-admin",
                ]
            )
            with self.assertRaisesRegex(SystemExit, "Refusing non-loopback"):
                _serve(args)
            self.assertFalse(database.exists())

    def test_non_ascii_admin_token_is_refused_before_database_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "should-not-exist.db"
            args = build_parser().parse_args(
                [
                    "serve",
                    "--database",
                    str(database),
                    "--admin-token",
                    "not-ascii-å",
                ]
            )
            with self.assertRaisesRegex(SystemExit, "ASCII"):
                _serve(args)
            self.assertFalse(database.exists())


if __name__ == "__main__":
    unittest.main()
