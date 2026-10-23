"""Interactive scrolling: reveal expanded abstracts and read long ones in place."""

from __future__ import annotations

import unittest

from cuhkvoting import cli, interactive


def _cfg() -> cli.Config:
    return cli.Config(
        categories=["gr-qc"], today_max_age=60, lastweek_max_age=360,
        abstract_lines=0, abstract_wrap=80, confirm_by_number=True,
        display_name="", display_name_overrides={}, highlight_authors=[],
        highlight_keywords=[], highlight_keyword_count=-1, highlight_glyph="★",
    )


def _entry(index: int, abstract: str) -> dict:
    return {
        "id": f"2601.{index:05d}",
        "title": f"Paper {index}",
        "abstract": abstract,
        "authors": ["Ann Chan"],
        "published": "2026-10-15",
        "url": "u",
    }


@unittest.skipUnless(interactive.PROMPT_TOOLKIT_OK, "prompt_toolkit not installed")
class RevealTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from prompt_toolkit.data_structures import Size
        from prompt_toolkit.output import DummyOutput

        class SizedOutput(DummyOutput):
            def get_size(self):
                return Size(rows=10, columns=60)

        cls.output = SizedOutput

    def _boot(self, keys: str, *, abstracts: list[str], n: int = 20,
              group_by_day: bool = False, follow: bool = False):
        from prompt_toolkit.input import create_pipe_input

        entries = [_entry(i + 1, abstracts[i]) for i in range(n)]
        if group_by_day:
            for i, entry in enumerate(entries):
                entry["published"] = "2026-10-15" if i < n // 2 else "2026-10-16"
        session = interactive.Session(entries)
        session.group_by_day = group_by_day
        session.follow = follow
        keymap, _ = interactive._effective_keymap({})
        ctx = create_pipe_input()
        pipe = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        tui = interactive.Tui(
            session, _cfg(), interactive.InteractiveConfig(), keymap,
            input=pipe, output=self.output(),
        )
        pipe.send_text(keys)
        tui.app.run()
        return session, tui

    def _span(self, tui, session, index: int):
        return tui._paper_spans[index]

    def test_opening_the_last_abstract_reveals_it(self) -> None:
        abstracts = ["short abs"] * 19 + ["abs " * 3]
        session, tui = self._boot("Gza:q!\r", abstracts=abstracts)
        height = tui.body_window.render_info.window_height
        start, end = self._span(tui, session, 20)
        self.assertEqual(tui.body_window.vertical_scroll, end - height)
        self.assertLessEqual(tui.body_window.vertical_scroll, start)
        self.assertLessEqual(end, tui.body_window.vertical_scroll + height)

    def test_long_last_abstract_pins_the_title_at_the_top(self) -> None:
        abstracts = ["short abs"] * 19 + ["long " * 80]
        session, tui = self._boot("Gza:q!\r", abstracts=abstracts)
        height = tui.body_window.render_info.window_height
        start, end = self._span(tui, session, 20)
        self.assertEqual(tui.body_window.vertical_scroll, start)
        self.assertGreater(end - start, height)

    def test_down_reads_a_clipped_abstract_before_moving(self) -> None:
        abstracts = ["short abs"] * 20
        abstracts[4] = "long " * 80
        session, tui = self._boot(":5\rza j:q!\r", abstracts=abstracts)
        start, _end = self._span(tui, session, 5)
        self.assertEqual(session.cursor, 4)  # still paper 5
        self.assertEqual(tui.body_window.vertical_scroll, start + 1)
        self.assertEqual(tui._scroll_anchor, 1)

        session, tui = self._boot(":5\rza jj:q!\r", abstracts=abstracts)
        height = tui.body_window.render_info.window_height
        _start, _end = self._span(tui, session, 5)
        next_start = self._span(tui, session, 6)[0]
        self.assertEqual(session.cursor, 4)  # not moved yet
        self.assertLess(next_start, tui.body_window.vertical_scroll + height)

        session, tui = self._boot(":5\rza jjj:q!\r", abstracts=abstracts)
        self.assertEqual(session.cursor, 5)  # next paper after the abstract is visible

    def test_down_moves_on_when_the_abstract_fits(self) -> None:
        session, _tui = self._boot(
            ":5\rza j:q!\r", abstracts=["short abs"] * 20,
        )
        self.assertEqual(session.cursor, 5)  # paper 6, as usual

    def test_up_rewinds_through_the_abstract_before_moving(self) -> None:
        abstracts = ["short abs"] * 20
        abstracts[4] = "long " * 80

        session, tui = self._boot(":5\rza jj k:q!\r", abstracts=abstracts)
        start, _end = self._span(tui, session, 5)
        self.assertEqual(session.cursor, 4)
        self.assertEqual(tui.body_window.vertical_scroll, start + 1)

        session, tui = self._boot(":5\rza jj kk:q!\r", abstracts=abstracts)
        start, _end = self._span(tui, session, 5)
        self.assertEqual(session.cursor, 4)
        self.assertEqual(tui.body_window.vertical_scroll, start)

        session, tui = self._boot(":5\rza jj kkk:q!\r", abstracts=abstracts)
        self.assertEqual(session.cursor, 3)  # now the selection moves up

    def test_closing_the_abstract_resets_the_scroll_anchor(self) -> None:
        abstracts = ["short abs"] * 20
        abstracts[4] = "long " * 80
        session, tui = self._boot(":5\rza jjza:q!\r", abstracts=abstracts)
        self.assertFalse(session.papers[4].open)
        self.assertEqual(tui._scroll_anchor, 0)

    def test_no_scroll_when_the_abstract_is_already_visible(self) -> None:
        session, tui = self._boot("za:q!\r", abstracts=["short abs"] * 20)
        self.assertTrue(session.papers[0].open)
        self.assertEqual(tui.body_window.vertical_scroll, 0)

    def test_grouped_list_leaves_room_for_the_sticky_day_header(self) -> None:
        abstracts = ["short abs"] * 19 + ["long " * 80]
        session, tui = self._boot(
            "Gza:q!\r", abstracts=abstracts, group_by_day=True,
        )
        start, _end = self._span(tui, session, 20)
        self.assertGreaterEqual(start - tui.body_window.vertical_scroll, 1)

    def test_follow_mode_reveals_the_new_abstract(self) -> None:
        session, tui = self._boot(
            "jj:q!\r", abstracts=["short abs"] * 20, follow=True,
        )
        height = tui.body_window.render_info.window_height
        start, end = self._span(tui, session, 3)
        self.assertEqual(session.cursor, 2)
        self.assertTrue(session.abstract_open(session.current()))
        self.assertLessEqual(tui.body_window.vertical_scroll, start)
        self.assertLessEqual(end, tui.body_window.vertical_scroll + height)


if __name__ == "__main__":
    unittest.main()
