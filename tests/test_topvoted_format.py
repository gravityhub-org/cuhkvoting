"""Custom topvoted row formats (plain command and interactive mode)."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from cuhkvoting import cli


def _cfg(fmt: str | None = None) -> cli.Config:
    values = {
        "categories": ["gr-qc"], "today_max_age": 60, "lastweek_max_age": 360,
        "abstract_lines": 0, "abstract_wrap": 80, "confirm_by_number": True,
        "display_name": "", "display_name_overrides": {}, "highlight_authors": [],
        "highlight_keywords": [], "highlight_keyword_count": -1, "highlight_glyph": "★",
    }
    if fmt is not None:
        values["topvoted_format"] = fmt
    return cli.Config(**values)


def _row(index: int, votes: int = 2, days: int | None = 5) -> dict:
    return {
        "id": f"2601.{index:05d}",
        "title": f"Paper {index}",
        "abstract": "",
        "votes": votes,
        "voters": "Ann",
        "expires_in_days": days,
    }


def _plain(rows: list[dict], fmt: str | None, width: int = 60) -> list[str]:
    out = io.StringIO()
    with mock.patch("cuhkvoting.cli._load_config", return_value=_cfg(fmt)), \
            mock.patch("cuhkvoting.cli._topvoted_list",
                       return_value=cli.ListData(rows, [])), \
            mock.patch("cuhkvoting.cli._save_last_list"), \
            mock.patch("cuhkvoting.cli._terminal_width", return_value=width), \
            contextlib.redirect_stdout(out):
        cli.cmd_topvoted(SimpleNamespace(N=None, abstract=0))
    return [cli._strip_ansi(line) for line in out.getvalue().splitlines()]


class DefaultFormatTests(unittest.TestCase):
    def test_default_is_the_historical_layout(self) -> None:
        lines = _plain([_row(1), _row(2)], None)
        self.assertEqual(lines, [
            "1. ┳ 2601.00001  Paper 1  [Ann] ⇒ 2 votes · 5d",
            "2. ┗ 2601.00002  Paper 2  [Ann] ⇒ 2 votes · 5d",
        ])

    def test_days_are_omitted_when_unknown(self) -> None:
        lines = _plain([_row(1, days=None)], None)
        self.assertEqual(lines, ["1. 2601.00001  Paper 1  [Ann] ⇒ 2 votes"])


class CustomFormatTests(unittest.TestCase):
    def test_fields_can_be_reordered_and_omitted(self) -> None:
        lines = _plain([_row(1)], "{arxiv} | {title} | {voters} | {nb_votes} | {days_left}")
        self.assertEqual(lines, ["1. 2601.00001 | Paper 1 | [Ann] | 2 votes | 5d"])

        lines = _plain([_row(1)], "{arxiv}  {title} ⇒ {nb_votes}")
        self.assertEqual(lines, ["1. 2601.00001  Paper 1 ⇒ 2 votes"])

    def test_newline_keeps_the_tie_gutter_unbroken(self) -> None:
        lines = _plain([_row(1), _row(2)], "{arxiv}  {title}\n{voters} ⇒ {nb_votes}")
        self.assertEqual(lines, [
            "1. ┳ 2601.00001  Paper 1",
            "   ┃ [Ann] ⇒ 2 votes",
            "2. ┗ 2601.00002  Paper 2",
            "     [Ann] ⇒ 2 votes",
        ])

    def test_tab_aligns_to_the_next_tab_stop(self) -> None:
        lines = _plain([_row(1)], "{arxiv}\t{title}")
        # No tie box: body starts at column 3; id ends at 13 → tab to 16.
        self.assertEqual(lines, ["1. 2601.00001   Paper 1"])

    def test_braces_can_be_escaped(self) -> None:
        lines = _plain([_row(1)], "{{{arxiv}}}  {title}")
        self.assertEqual(lines, ["1. {2601.00001}  Paper 1"])

    def test_wrapped_continuations_follow_the_title_column(self) -> None:
        row = _row(1)
        row["title"] = " ".join(["word"] * 40)
        lines = _plain([row], "{arxiv} | {title} | {nb_votes}", width=40)
        self.assertTrue(all(cli._visible_width(line) <= 40 for line in lines))
        # Body starts at column 3; "2601.00001 | " puts the title at column 16.
        self.assertTrue(any(line.startswith(" " * 16 + "word") for line in lines))

    def test_invalid_fields_are_rejected(self) -> None:
        self.assertIsNone(cli._parse_topvoted_format("{arxiv} {nope}"))
        self.assertIsNotNone(cli._parse_topvoted_format("{arxiv}  {title}"))


class ConfigLoadTests(unittest.TestCase):
    def _load(self, text: str) -> tuple[cli.Config, str]:
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as tmp:
            tmp.write(text)
            path = Path(tmp.name)
        self.addCleanup(path.unlink, missing_ok=True)
        err = io.StringIO()
        with mock.patch("cuhkvoting.cli.CONFIG_PATH", path), \
                contextlib.redirect_stderr(err):
            cfg = cli._load_config()
        return cfg, err.getvalue()

    def test_reads_the_option(self) -> None:
        cfg, err = self._load('[display]\ntopvoted_format = "{arxiv} | {title}"\n')
        self.assertEqual(cfg.topvoted_format, "{arxiv} | {title}")
        self.assertEqual(err, "")

    def test_invalid_format_warns_and_falls_back(self) -> None:
        cfg, err = self._load('[display]\ntopvoted_format = "{arxiv} {bogus}"\n')
        self.assertEqual(cfg.topvoted_format, cli.DEFAULT_TOPVOTED_FORMAT)
        self.assertIn("invalid topvoted_format", err)


if __name__ == "__main__":
    unittest.main()
