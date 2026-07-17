"""Tests for interactive-mode key parsing, overrides, and the idle hint strip."""

from __future__ import annotations

import unittest

from cuhkvoting import interactive
from cuhkvoting.interactive import (
    DEFAULT_KEYS, _effective_keymap, _hint_strip, _key_tokens,
)


class KeyTokenTests(unittest.TestCase):
    def test_single_char(self) -> None:
        self.assertEqual(_key_tokens("j"), ("j",))

    def test_sequence(self) -> None:
        self.assertEqual(_key_tokens("g g"), ("g", "g"))

    def test_rejects_non_strings_and_empty(self) -> None:
        for bad in ("", "   ", 123, None, ["j"]):
            self.assertIsNone(_key_tokens(bad), bad)

    @unittest.skipUnless(interactive.PROMPT_TOOLKIT_OK, "prompt_toolkit not installed")
    def test_named_keys_and_aliases(self) -> None:
        self.assertEqual(_key_tokens("pagedown"), ("pagedown",))
        self.assertEqual(_key_tokens("enter"), ("enter",))  # alias, not in ALL_KEYS
        self.assertEqual(_key_tokens("c-f"), ("c-f",))

    @unittest.skipUnless(interactive.PROMPT_TOOLKIT_OK, "prompt_toolkit not installed")
    def test_unknown_name_rejected(self) -> None:
        self.assertIsNone(_key_tokens("notakey"))


class EffectiveKeymapTests(unittest.TestCase):
    def test_defaults_pass_through(self) -> None:
        keymap, problems = _effective_keymap({})
        self.assertEqual(keymap, DEFAULT_KEYS)
        self.assertIsNot(keymap["add"], DEFAULT_KEYS["add"])  # a copy, not the original
        self.assertEqual(problems, [])

    def test_string_override(self) -> None:
        keymap, problems = _effective_keymap({"add": "x"})
        self.assertEqual(keymap["add"], ["x"])
        self.assertEqual(problems, [])

    def test_list_override(self) -> None:
        keymap, _problems = _effective_keymap({"add": ["x", "y"]})
        self.assertEqual(keymap["add"], ["x", "y"])

    def test_unknown_action_reported(self) -> None:
        keymap, problems = _effective_keymap({"fly": "x"})
        self.assertEqual(keymap, DEFAULT_KEYS)
        self.assertEqual(len(problems), 1)
        self.assertIn("fly", problems[0])

    def test_invalid_spec_keeps_default(self) -> None:
        keymap, problems = _effective_keymap({"add": ""})
        self.assertEqual(keymap["add"], DEFAULT_KEYS["add"])
        self.assertEqual(len(problems), 1)
        self.assertIn("add", problems[0])


class HintStripTests(unittest.TestCase):
    def test_default_strip(self) -> None:
        keymap, _ = _effective_keymap({})
        strip = _hint_strip(keymap, width=200)
        self.assertTrue(strip.startswith("v add  d del  o open  za abs"))
        self.assertIn(":w vote", strip)
        self.assertIn(":h help", strip)

    def test_reflects_overrides(self) -> None:
        keymap, _ = _effective_keymap({"add": "x"})
        strip = _hint_strip(keymap, width=200)
        self.assertTrue(strip.startswith("x add"))

    def test_truncates_whole_items(self) -> None:
        keymap, _ = _effective_keymap({})
        self.assertEqual(_hint_strip(keymap, width=12), "v add  d del")
        self.assertEqual(_hint_strip(keymap, width=3), "")


if __name__ == "__main__":
    unittest.main()
