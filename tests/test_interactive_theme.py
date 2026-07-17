"""Tests for interactive-mode themes and the config.toml text-edit persistence."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cuhkvoting import cli, interactive
from cuhkvoting.interactive import THEMES, _set_theme_in_toml


class SetThemeInTomlTests(unittest.TestCase):
    def test_empty_file_creates_section(self) -> None:
        self.assertEqual(_set_theme_in_toml("", "nord"), '[interactive]\ntheme = "nord"\n')

    def test_appends_after_existing_content(self) -> None:
        text = 'categories = ["gr-qc"]\n\n[display]\nabstract_lines = 0\n'
        out = _set_theme_in_toml(text, "nord")
        self.assertTrue(out.startswith(text))
        self.assertIn('\n[interactive]\ntheme = "nord"\n', out)

    def test_inserts_into_existing_section(self) -> None:
        text = "[interactive]\nkey_hints = false\n"
        out = _set_theme_in_toml(text, "gruvbox")
        self.assertEqual(out, '[interactive]\ntheme = "gruvbox"\nkey_hints = false\n')

    def test_replaces_existing_theme(self) -> None:
        text = '[interactive]\ntheme = "nord"\nkey_hints = false\n'
        out = _set_theme_in_toml(text, "onedark")
        self.assertEqual(out, '[interactive]\ntheme = "onedark"\nkey_hints = false\n')

    def test_keys_subsection_untouched(self) -> None:
        text = '[interactive]\ntheme = "nord"\n\n[interactive.keys]\nadd = "x"\n'
        out = _set_theme_in_toml(text, "onedark")
        self.assertIn('theme = "onedark"', out)
        self.assertIn('[interactive.keys]\nadd = "x"\n', out)
        self.assertNotIn("nord", out)

    def test_keys_only_file_gets_its_own_section(self) -> None:
        # [interactive.keys] alone must not be mistaken for [interactive].
        text = '[interactive.keys]\nadd = "x"\n'
        out = _set_theme_in_toml(text, "nord")
        self.assertIn('add = "x"', out)
        self.assertIn('[interactive]\ntheme = "nord"\n', out)

    def test_idempotent(self) -> None:
        once = _set_theme_in_toml("", "nord")
        self.assertEqual(_set_theme_in_toml(once, "nord"), once)

    def test_comments_and_sections_preserved(self) -> None:
        text = (
            "# top comment\n"
            'categories = ["gr-qc"]\n'
            "\n"
            "[interactive]\n"
            "# inner comment\n"
            'theme = "nord"\n'
            "\n"
            "[vote]\n"
            "confirm_by_number = true\n"
        )
        out = _set_theme_in_toml(text, "gruvbox")
        self.assertIn("# top comment", out)
        self.assertIn("# inner comment", out)
        self.assertIn('theme = "gruvbox"', out)
        self.assertIn("[vote]\nconfirm_by_number = true", out)


class LoadInteractiveConfigTests(unittest.TestCase):
    def _load(self, text: str | None) -> interactive.InteractiveConfig:
        tmp = Path(tempfile.mkdtemp()) / "config.toml"
        if text is not None:
            tmp.write_text(text, encoding="utf-8")
        with mock.patch.object(cli, "CONFIG_PATH", tmp):
            return interactive._load_interactive_config()

    def test_defaults_when_missing(self) -> None:
        icfg = self._load(None)
        self.assertEqual(icfg.theme, "default")
        self.assertTrue(icfg.key_hints)
        self.assertFalse(icfg.follow)
        self.assertEqual(icfg.keys, {})

    def test_reads_section(self) -> None:
        icfg = self._load(
            '[interactive]\ntheme = "nord"\nkey_hints = false\nfollow = true\n\n'
            '[interactive.keys]\nadd = "x"\n'
        )
        self.assertEqual(icfg.theme, "nord")
        self.assertFalse(icfg.key_hints)
        self.assertTrue(icfg.follow)
        self.assertEqual(icfg.keys, {"add": "x"})

    def test_broken_toml_falls_back_to_defaults(self) -> None:
        icfg = self._load("[interactive\noops")
        self.assertEqual(icfg.theme, "default")


@unittest.skipUnless(interactive.PROMPT_TOOLKIT_OK, "prompt_toolkit not installed")
class ThemeCompileTests(unittest.TestCase):
    def test_every_theme_compiles(self) -> None:
        from prompt_toolkit.styles import Style

        for name, theme in THEMES.items():
            Style.from_dict(theme)  # raises on malformed style strings
        self.assertIn("default", THEMES)


if __name__ == "__main__":
    unittest.main()
