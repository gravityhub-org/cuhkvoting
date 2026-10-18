"""Interactive-mode reflow: styled fragment wrapping and terminal width tracking."""

from __future__ import annotations

import unittest

from cuhkvoting import cli, interactive

PT_OK = interactive.PROMPT_TOOLKIT_OK


def _cfg() -> cli.Config:
    return cli.Config(
        categories=["gr-qc"], today_max_age=60, lastweek_max_age=360,
        abstract_lines=0, abstract_wrap=80, confirm_by_number=True,
        display_name="", display_name_overrides={}, highlight_authors=[],
        highlight_keywords=[], highlight_keyword_count=-1, highlight_glyph="★",
    )


def _lines(frags: list) -> list[str]:
    return "".join(text for _style, text in frags).split("\n")[:-1]


class WrapStyledTests(unittest.TestCase):
    def test_wraps_at_width_and_keeps_styles(self) -> None:
        frags = [("class:title", "hello world this is a long title")]
        wrapped = interactive._wrap_styled(frags, 12)
        self.assertEqual(_lines(wrapped), ["hello world", "this is a", "long title"])
        self.assertIn("class:title", {style for style, _text in wrapped})

    def test_indent_applies_to_continuation_lines_only(self) -> None:
        wrapped = interactive._wrap_styled(
            [("a", "alpha beta gamma")], 12, indent="    ", indent_style="class:x",
        )
        self.assertEqual(_lines(wrapped), ["alpha beta", "    gamma"])

    def test_indent_first_applies_to_every_line(self) -> None:
        wrapped = interactive._wrap_styled(
            [("a", "alpha beta gamma")], 10,
            indent="  ", indent_style="class:x", indent_first=True,
        )
        self.assertEqual(_lines(wrapped), ["  alpha", "  beta", "  gamma"])

    def test_hard_breaks_overlong_words(self) -> None:
        wrapped = interactive._wrap_styled([("a", "abcdefghijkl")], 8)
        self.assertEqual(_lines(wrapped), ["abcdefgh", "ijkl"])

    def test_preserves_space_runs_on_a_line(self) -> None:
        wrapped = interactive._wrap_styled([("a", "one  two   three")], 30)
        self.assertEqual(_lines(wrapped), ["one  two   three"])

    def test_leading_spaces_stay_on_the_first_line(self) -> None:
        wrapped = interactive._wrap_styled([("a", "   1. word")], 20)
        self.assertEqual(_lines(wrapped), ["   1. word"])

    def test_wide_space_run_moves_the_word_to_a_new_line(self) -> None:
        wrapped = interactive._wrap_styled([("a", "aaaa     bbbb")], 10)
        self.assertEqual(_lines(wrapped), ["aaaa", "bbbb"])

    def test_styles_of_space_runs_survive(self) -> None:
        wrapped = interactive._wrap_styled([("a", "x"), ("b", "  y")], 10)
        styles = [(style, text) for style, text in wrapped if text.strip()]
        self.assertIn(("b", "  y"), styles)

    def test_empty_input_renders_nothing(self) -> None:
        self.assertEqual(interactive._wrap_styled([("a", "")], 10), [])


class EntryReflowTests(unittest.TestCase):
    def _session(self) -> interactive.Session:
        entry = {
            "id": "2601.00001",
            "title": " ".join(["wording"] * 30),
            "abstract": " ".join(["abstract"] * 60),
            "authors": ["Ann Chan", "Bo Li"],
            "published": "2026-10-15",
            "url": "u",
        }
        return interactive.Session([entry])

    def test_every_line_fits_the_width(self) -> None:
        session = self._session()
        session.papers[0].open = True
        for width in (30, 45, 80):
            with self.subTest(width=width):
                frags = interactive._entry_fragments(session.papers[0], session, _cfg(), width)
                self.assertLessEqual(max(len(line) for line in _lines(frags)), width)

    def test_abstract_lines_are_indented(self) -> None:
        session = self._session()
        session.papers[0].open = True
        frags = interactive._entry_fragments(session.papers[0], session, _cfg(), 50)
        abstract_lines = [line for line in _lines(frags) if line.startswith("      ")]
        self.assertGreaterEqual(len(abstract_lines), 2)

    def test_open_abstract_reflows_to_width(self) -> None:
        session = self._session()
        session.papers[0].open = True
        wide = _lines(interactive._entry_fragments(session.papers[0], session, _cfg(), 80))
        narrow = _lines(interactive._entry_fragments(session.papers[0], session, _cfg(), 40))
        self.assertGreater(len(narrow), len(wide))


@unittest.skipUnless(PT_OK, "prompt_toolkit not installed")
class RenderResizeTests(unittest.TestCase):
    def test_resize_rewraps_cached_rows(self) -> None:
        from prompt_toolkit.data_structures import Size
        from prompt_toolkit.input import DummyInput
        from prompt_toolkit.output import DummyOutput

        class SizedOutput(DummyOutput):
            def __init__(self, columns: int) -> None:
                self._size = Size(rows=40, columns=columns)

            def get_size(self):
                return self._size

        entry = {
            "id": "2601.00001",
            "title": " ".join(["wording"] * 30),
            "abstract": " ".join(["abstract"] * 60),
            "authors": ["Ann Chan"],
            "published": "2026-10-15",
            "url": "u",
        }
        session = interactive.Session([entry])
        session.papers[0].open = True
        keymap, _ = interactive._effective_keymap({})
        output = SizedOutput(80)
        tui = interactive.Tui(
            session, _cfg(), interactive.InteractiveConfig(), keymap,
            input=DummyInput(), output=output,
        )

        wide = _lines(tui.render_body())
        self.assertLessEqual(max(len(line) for line in wide), 80)
        output._size = Size(rows=40, columns=40)
        narrow = _lines(tui.render_body())
        self.assertLessEqual(max(len(line) for line in narrow), 40)
        self.assertGreater(len(narrow), len(wide))


if __name__ == "__main__":
    unittest.main()
