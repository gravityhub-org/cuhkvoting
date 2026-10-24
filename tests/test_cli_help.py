"""Every CLI command (including nested groups) must describe itself in --help."""

from __future__ import annotations

import unittest

import typer

from cuhkvoting import cli


class CommandHelpTests(unittest.TestCase):
    def test_every_command_has_a_description(self) -> None:
        root = typer.main.get_command(cli.app)
        missing: list[str] = []

        def walk(command, path: str) -> None:
            if not (command.help or "").strip():
                missing.append(path)
            for name, sub in getattr(command, "commands", {}).items():
                walk(sub, f"{path} {name}")

        walk(root, "cuhkvoting")
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
