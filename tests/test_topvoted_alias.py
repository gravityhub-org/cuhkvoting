"""`topvoted` count option: `-n` must behave like the long `--N`/`--n` forms."""

from __future__ import annotations

import unittest
from unittest import mock

from typer.testing import CliRunner

from cuhkvoting import cli


class TopvotedCountAliasTest(unittest.TestCase):
    def _invoke(self, argv: list[str]) -> tuple[int, int | None]:
        """Run `topvoted` with its list loader stubbed; return (exit code, args.N)."""
        seen: list[int | None] = []

        def fake_list(args, _cfg):
            seen.append(args.N)
            return cli.ListData([], [], empty_msg="No voted papers yet.")

        with mock.patch("cuhkvoting.cli._topvoted_list", side_effect=fake_list):
            result = CliRunner().invoke(cli.app, argv)
        return result.exit_code, (seen[0] if seen else None)

    def test_short_alias_parses(self) -> None:
        code, n = self._invoke(["topvoted", "-n", "3"])
        self.assertEqual(code, 0)
        self.assertEqual(n, 3)

    def test_short_alias_attached_value(self) -> None:
        code, n = self._invoke(["topvoted", "-n7"])
        self.assertEqual(code, 0)
        self.assertEqual(n, 7)

    def test_long_aliases_still_parse(self) -> None:
        for flag in ("--N", "--n"):
            with self.subTest(flag=flag):
                code, n = self._invoke(["topvoted", flag, "5"])
                self.assertEqual(code, 0)
                self.assertEqual(n, 5)

    def test_default_is_ten(self) -> None:
        code, n = self._invoke(["topvoted"])
        self.assertEqual(code, 0)
        self.assertEqual(n, 10)


if __name__ == "__main__":
    unittest.main()
