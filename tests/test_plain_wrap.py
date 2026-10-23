"""Normal-mode wrapping: terminal width, aligned continuations, unbroken tie gutters."""

from __future__ import annotations

import contextlib
import io
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from cuhkvoting import cli


def _cfg(**overrides) -> cli.Config:
    values = {
        "categories": ["gr-qc"], "today_max_age": 60, "lastweek_max_age": 360,
        "abstract_lines": 0, "abstract_wrap": 80, "confirm_by_number": True,
        "display_name": "", "display_name_overrides": {}, "highlight_authors": [],
        "highlight_keywords": [], "highlight_keyword_count": -1, "highlight_glyph": "★",
    }
    values.update(overrides)
    return cli.Config(**values)


def _capture(fn, *args, **kwargs) -> list[str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args, **kwargs)
    return buf.getvalue().splitlines()


def _visible(line: str) -> str:
    return cli._strip_ansi(line)


class TerminalWidthTests(unittest.TestCase):
    def test_uses_the_terminal_columns(self) -> None:
        with mock.patch(
            "cuhkvoting.cli.shutil.get_terminal_size",
            return_value=os.terminal_size((123, 40)),
        ):
            self.assertEqual(cli._terminal_width(), 123)

    def test_has_a_small_terminal_floor(self) -> None:
        with mock.patch(
            "cuhkvoting.cli.shutil.get_terminal_size",
            return_value=os.terminal_size((5, 40)),
        ):
            self.assertEqual(cli._terminal_width(), 20)


class WrapAnsiRowTests(unittest.TestCase):
    def test_wraps_under_the_title_and_keeps_ansi(self) -> None:
        blue, reset = "\x1b[94;1m", "\x1b[0m"
        body = ("A long title " * 8) + "  [" + blue + "Chan" + reset + ", Li]"
        prefix = " 1. 2601.00001  "
        cont = " " * cli._visible_width(prefix)
        lines = cli._wrap_ansi_row(prefix, body, cont, 60)

        self.assertGreater(len(lines), 1)
        self.assertTrue(all(cli._visible_width(line) <= 60 for line in lines))
        self.assertTrue(lines[0].startswith(prefix))
        self.assertTrue(all(line.startswith(cont) for line in lines[1:]))
        self.assertIn(blue, "\n".join(lines))
        self.assertIn("Chan", " ".join(_visible(line).strip() for line in lines))

    def test_drops_break_spaces_and_hard_breaks_long_words(self) -> None:
        lines = cli._wrap_ansi_row(" 1. x  ", "abc" * 20, " " * 8, 20)
        self.assertTrue(all(cli._visible_width(line) <= 20 for line in lines))
        self.assertTrue(all(line.startswith("        ") for line in lines[1:]))
        content = _visible(lines[0])[len(" 1. x  "):] + "".join(
            _visible(line).strip() for line in lines[1:])
        self.assertEqual(content, "abc" * 20)

    def test_resets_are_kept_and_openers_follow_the_prefix(self) -> None:
        cyan, reset = "\x1b[36m", "\x1b[0m"
        body = ("word " * 8) + cyan + "[Ann] ⇒ 2 votes" + reset
        lines = cli._wrap_ansi_row("1. ┳ 2601.00001  ", body, "   ┃ " + " " * 12, 30)
        raw = "\n".join(lines)
        self.assertEqual(raw.count(cyan), 1)
        self.assertEqual(raw.count(reset), 1)
        styled = [line for line in lines if cyan in line]
        self.assertTrue(styled)
        for line in styled:
            # The gutter itself stays uncolored: the opener comes after it.
            self.assertTrue(line.startswith("   ┃ " + " " * 12 + cyan))


class AbstractWrapTests(unittest.TestCase):
    def test_abstract_wrap_is_a_maximum(self) -> None:
        abstract = " ".join(["word"] * 60)
        text = cli._format_abstract(abstract, -1, 30, width=200)
        lines = text.splitlines()
        self.assertTrue(lines)
        self.assertLessEqual(max(len(line) - 6 for line in lines), 30)

    def test_terminal_width_caps_the_abstract(self) -> None:
        abstract = " ".join(["word"] * 60)
        lines = cli._format_abstract(abstract, -1, 80, width=50).splitlines()
        self.assertTrue(lines)
        self.assertTrue(all(cli._visible_width(line) <= 50 for line in lines))
        self.assertTrue(any(len(line) > 6 for line in lines))


class PlainListWrapTests(unittest.TestCase):
    def test_entry_list_wraps_titles_and_abstracts(self) -> None:
        entry = {
            "id": "2601.00001",
            "title": " ".join(["word"] * 40),
            "authors": ["Ann Chan"],
            "abstract": " ".join(["abstract"] * 40),
        }
        with mock.patch("cuhkvoting.cli._terminal_width", return_value=50):
            lines = _capture(
                cli._print_entry_list, [entry], _cfg(abstract_wrap=10), -1, -1,
            )

        self.assertTrue(lines)
        self.assertTrue(all(cli._visible_width(line) <= 50 for line in lines))
        # Title continuations align under the title (index + id + two spaces)
        # and wrap at the terminal width, not at abstract_wrap.
        title_col = cli._visible_width(" 1. 2601.00001  ")
        title_lines = [line for line in lines if line.startswith(" " * title_col)]
        self.assertTrue(any(len(line) > title_col + 10 for line in title_lines))
        # Abstracts keep their six-space indent and are capped at 10 columns.
        abstract = [
            line for line in lines
            if len(line) - len(line.lstrip(" ")) == 6
        ]
        self.assertTrue(abstract)
        self.assertLessEqual(max(len(line) for line in abstract), 6 + 10)

    def test_show_wraps_the_title_and_caps_the_abstract(self) -> None:
        entry = {
            "id": "2601.00001",
            "title": " ".join(["word"] * 40),
            "authors": ["Ann Chan"],
            "abstract": " ".join(["abstract"] * 40),
        }
        cfg = _cfg(abstract_wrap=80)
        args = SimpleNamespace(paper_ids=["2601.00001"])
        with mock.patch("cuhkvoting.cli._load_config", return_value=cfg), \
                mock.patch("cuhkvoting.cli._resolve_paper_ids",
                           return_value=[("2601.00001", "", None)]), \
                mock.patch("cuhkvoting.cli._lookup_local_cache", return_value=entry), \
                mock.patch("cuhkvoting.cli._terminal_width", return_value=50):
            lines = _capture(cli.cmd_show, args)

        self.assertTrue(lines)
        self.assertTrue(all(cli._visible_width(line) <= 50 for line in lines))
        self.assertTrue(any(line.startswith("            word") for line in lines))
        self.assertTrue(any(line.startswith("      abstract") for line in lines))

    def test_search_wraps_titles(self) -> None:
        data = cli.ListData([{
            "id": "2601.00001",
            "title": " ".join(["gamma"] * 40),
        }], [])
        args = SimpleNamespace(query=["x"])
        with mock.patch("cuhkvoting.cli._search_list", return_value=data), \
                mock.patch("cuhkvoting.cli._save_last_list"), \
                mock.patch("cuhkvoting.cli._terminal_width", return_value=50):
            lines = _capture(cli.cmd_search, args)

        self.assertTrue(lines)
        self.assertTrue(all(cli._visible_width(line) <= 50 for line in lines))
        title_col = cli._visible_width(" 1. 2601.00001  ")
        self.assertTrue(any(line.startswith(" " * title_col + "gamma") for line in lines))


class TopvotedWrapTests(unittest.TestCase):
    def _rows(self) -> list[dict]:
        return [
            {
                "id": "2601.00001", "title": " ".join(["alpha"] * 30),
                "abstract": " ".join(["abstract"] * 40), "votes": 2,
                "voters": "Ann", "expires_in_days": 5,
            },
            {
                "id": "2601.00002", "title": " ".join(["beta"] * 30),
                "abstract": " ".join(["abstract"] * 40), "votes": 2,
                "voters": "Bo", "expires_in_days": 5,
            },
            {
                "id": "2601.00003", "title": "short title", "abstract": "",
                "votes": 1, "voters": "Cy", "expires_in_days": 4,
            },
        ]

    def _render(self, width: int = 50) -> list[str]:
        cfg = _cfg(abstract_wrap=30)
        args = SimpleNamespace(N=None, abstract=-1)
        with mock.patch("cuhkvoting.cli._load_config", return_value=cfg), \
                mock.patch("cuhkvoting.cli._topvoted_list",
                           return_value=cli.ListData(self._rows(), [])), \
                mock.patch("cuhkvoting.cli._save_last_list"), \
                mock.patch("cuhkvoting.cli._terminal_width", return_value=width):
            return _capture(cli.cmd_topvoted, args)

    def test_wrapped_rows_fit_the_terminal(self) -> None:
        lines = self._render()
        self.assertTrue(lines)
        self.assertTrue(all(cli._visible_width(line) <= 50 for line in lines))

    def test_tie_gutter_stays_unbroken_on_continuations(self) -> None:
        lines = self._render()
        visible = [_visible(line) for line in lines]
        # w=1 → "1." then a space, so the tie glyph column is index 3.
        self.assertEqual({line.index("┃") for line in visible if "┃" in line}, {3})
        # The last tie member closes the gutter: its wrapped title carries no ┃.
        beta_lines = [line for line in visible if "beta" in line]
        self.assertTrue(beta_lines)
        self.assertFalse(any("┃" in line for line in beta_lines))

    def test_tie_gutter_continues_through_the_abstract(self) -> None:
        lines = [_visible(line) for line in self._render()]
        # Row 1's abstract lines are indented to the gutter column.
        abstract_lines = [line for line in lines if "abstract" in line]
        self.assertTrue(abstract_lines)
        self.assertTrue(any(line[3] == "┃" for line in abstract_lines))
        # abstract_wrap=30 is the text cap; the prefix adds a gutter.
        self.assertLessEqual(max(len(line) for line in abstract_lines), 5 + 30)
