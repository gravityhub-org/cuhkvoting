"""Interactive day rows: section grouping, inline rules, and the sticky header."""

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


def _entry(index: int, day: str = "") -> dict:
    entry = {
        "id": f"2601.{index:05d}",
        "title": f"Paper {index}",
        "abstract": "abs",
        "authors": ["Ann Chan"],
        "url": "u",
    }
    if day:
        entry["published"] = day
    return entry


def _session(days: list[str], *, group: bool = True) -> interactive.Session:
    session = interactive.Session([_entry(i + 1, day) for i, day in enumerate(days)])
    session.group_by_day = group
    return session


def _tui(session: interactive.Session):
    from prompt_toolkit.input import DummyInput
    from prompt_toolkit.output import DummyOutput

    keymap, _ = interactive._effective_keymap({})
    return interactive.Tui(
        session, _cfg(), interactive.InteractiveConfig(), keymap,
        input=DummyInput(), output=DummyOutput(),
    )


def _text(frags: list) -> str:
    return "".join(text for _style, text in frags)


class DisplayItemsTests(unittest.TestCase):
    def test_multi_day_list_gets_day_rows(self) -> None:
        session = _session(["2026-10-15", "2026-10-15", "2026-10-16"])
        items = interactive._display_items(session)
        kinds = [kind for kind, _payload in items]
        self.assertEqual(kinds, ["day", "paper", "paper", "day", "paper"])
        self.assertEqual(items[0], ("day", "2026-10-15"))
        self.assertEqual(items[3], ("day", "2026-10-16"))

    def test_single_day_list_has_no_day_rows(self) -> None:
        session = _session(["2026-10-15", "2026-10-15"])
        self.assertEqual(
            [kind for kind, _payload in interactive._display_items(session)],
            ["paper", "paper"],
        )

    def test_missing_date_inherits_previous_day(self) -> None:
        session = _session(["2026-10-15", "", "2026-10-15", "2026-10-16"])
        items = interactive._display_items(session)
        self.assertEqual(
            [kind for kind, _payload in items],
            ["day", "paper", "paper", "paper", "day", "paper"],
        )

    def test_grouping_disabled_returns_plain_papers(self) -> None:
        session = _session(["2026-10-15", "2026-10-16"], group=False)
        self.assertEqual(
            [kind for kind, _payload in interactive._display_items(session)],
            ["paper", "paper"],
        )

    def test_ls_filter_regroups_the_visible_papers(self) -> None:
        session = _session(["2026-10-15", "2026-10-15", "2026-10-16"])
        session.voting = ["2601.00001", "2601.00003"]
        session.enter_ls()
        self.assertEqual(
            [kind for kind, _payload in interactive._display_items(session)],
            ["day", "paper", "day", "paper"],
        )


class LoadListTests(unittest.TestCase):
    def test_returns_the_dispatched_loader_for_day_grouping(self) -> None:
        from unittest import mock

        def fake_loader(args, cfg):
            return cli.ListData([], [])

        with mock.patch("cuhkvoting.interactive._dispatch", return_value=(fake_loader, object())):
            _data, _captured, loader = interactive._load_list(["today"], _cfg())
        self.assertIs(loader, fake_loader)


class DayRenderTests(unittest.TestCase):
    def test_rules_and_line_ownership(self) -> None:
        session = _session(["2026-10-15", "2026-10-16"])
        tui = _tui(session)
        lines = _text(tui.render_body()).splitlines()
        self.assertTrue(lines[0].startswith("── 2026-10-15 "))
        self.assertEqual(tui._line_owner[0], ("day", "2026-10-15"))
        self.assertEqual(tui._line_owner[1], ("paper", 1))
        self.assertEqual(tui._paper_spans[1], (1, 2))
        self.assertEqual(tui._paper_days[2], "2026-10-16")

    def test_cursor_line_points_at_the_selected_row(self) -> None:
        session = _session(["2026-10-15", "2026-10-16"])
        session.cursor = 1  # second visible paper, after its day rule
        tui = _tui(session)
        tui.render_body()
        self.assertEqual(tui._last_point.y, tui._paper_spans[2][0])

    def test_rules_are_not_selectable(self) -> None:
        session = _session(["2026-10-15", "2026-10-16", "2026-10-17"])
        session.jump_index(3)  # full-list index is unaffected by the day rules
        self.assertIs(session.current(), session.papers[2])


class _FakeInfo:
    def __init__(self, first: int) -> None:
        self._first = first

    def first_visible_line(self, after_scroll_offset: bool = False) -> int:
        return self._first


class StickyHeaderTests(unittest.TestCase):
    def test_shown_when_the_day_row_scrolled_off(self) -> None:
        session = _session(["2026-10-15", "2026-10-16"])
        tui = _tui(session)
        tui.render_body()
        tui.body_window.render_info = _FakeInfo(tui._paper_spans[2][0])  # a paper line
        sticky = _text(tui.render_sticky())
        self.assertIn("2026-10-16", sticky)

    def test_hidden_while_the_inline_day_row_is_visible(self) -> None:
        session = _session(["2026-10-15", "2026-10-16"])
        tui = _tui(session)
        tui.render_body()
        tui.body_window.render_info = _FakeInfo(0)  # the first day rule itself
        self.assertEqual(tui.render_sticky(), [])

    def test_hidden_without_day_grouping(self) -> None:
        session = _session(["2026-10-15", "2026-10-16"], group=False)
        tui = _tui(session)
        tui.render_body()
        tui.body_window.render_info = _FakeInfo(0)
        self.assertEqual(tui.render_sticky(), [])

    def test_hidden_before_any_render(self) -> None:
        session = _session(["2026-10-15", "2026-10-16"])
        tui = _tui(session)
        tui.body_window.render_info = None
        self.assertEqual(tui.render_sticky(), [])


@unittest.skipUnless(PT_OK, "prompt_toolkit not installed")
class StickySmokeTests(unittest.TestCase):
    def test_sticky_day_appears_after_scrolling(self) -> None:
        from prompt_toolkit.data_structures import Size
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        class ShortOutput(DummyOutput):
            def get_size(self):
                return Size(rows=12, columns=60)

        days = ["2026-10-15"] * 6 + ["2026-10-16"] * 5 + ["2026-10-17"] * 5
        session = _session(days)
        keymap, _ = interactive._effective_keymap({})
        with create_pipe_input() as pipe:
            tui = interactive.Tui(
                session, _cfg(), interactive.InteractiveConfig(), keymap,
                input=pipe, output=ShortOutput(),
            )
            pipe.send_text("G:q!\r")
            tui.app.run()
        first = tui.body_window.render_info.first_visible_line()
        self.assertEqual(tui._line_owner[first][0], "paper")
        sticky = _text(tui.render_sticky())
        self.assertIn("2026-10-", sticky)


if __name__ == "__main__":
    unittest.main()
