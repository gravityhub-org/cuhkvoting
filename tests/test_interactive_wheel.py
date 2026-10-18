"""Interactive mouse wheel: viewport scrolling and selection drag at the edges."""

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


def _entry(index: int) -> dict:
    return {
        "id": f"2601.{index:05d}",
        "title": f"Paper {index}",
        "abstract": "abs",
        "authors": ["Ann Chan"],
        "published": f"2026-10-{1 + (index - 1) // 4:02d}",
        "url": "u",
    }


@unittest.skipUnless(interactive.PROMPT_TOOLKIT_OK, "prompt_toolkit not installed")
class WheelTests(unittest.TestCase):
    def _boot(self, n: int = 40, rows: int = 12, follow: bool = False):
        from prompt_toolkit.data_structures import Size
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        class SizedOutput(DummyOutput):
            def get_size(self):
                return Size(rows=rows, columns=60)

        session = interactive.Session([_entry(i + 1) for i in range(n)])
        session.group_by_day = True
        session.follow = follow
        keymap, _ = interactive._effective_keymap({})
        ctx = create_pipe_input()
        pipe = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        tui = interactive.Tui(
            session, _cfg(), interactive.InteractiveConfig(), keymap,
            input=pipe, output=SizedOutput(),
        )
        pipe.send_text(":q!\r")
        tui.app.run()
        return session, tui

    def test_first_tick_scrolls_then_the_selection_is_dragged(self) -> None:
        session, tui = self._boot()
        tui.wheel(1)  # cursor starts at row 1 (the day rule owns row 0)
        self.assertEqual(session.cursor, 0)
        self.assertEqual(tui.body_window.vertical_scroll, 1)
        tui.wheel(1)  # selection reached the top edge: drag it along
        self.assertEqual(session.cursor, 1)
        self.assertEqual(tui.body_window.vertical_scroll, tui._paper_spans[2][0])
        tui.wheel(1)
        self.assertEqual(session.cursor, 2)
        self.assertEqual(tui.body_window.vertical_scroll, tui._paper_spans[3][0])

    def test_wheel_up_scrolls_the_viewport_without_moving_the_selection(self) -> None:
        session, tui = self._boot()
        for _ in range(6):
            tui.wheel(1)
        self.assertEqual(session.cursor, 5)
        scroll = tui.body_window.vertical_scroll
        tui.wheel(-1)
        self.assertEqual(session.cursor, 5)
        self.assertEqual(tui.body_window.vertical_scroll, scroll - 1)

    def test_wheel_up_at_the_bottom_edge_drags_the_selection(self) -> None:
        session, tui = self._boot()
        for _ in range(12):
            tui.wheel(1)
        height = tui.body_window.render_info.window_height
        span = tui._paper_spans[session.current().index]
        tui.body_window.vertical_scroll = span[0] - (height - 1)  # selection on the last row
        before = session.cursor
        tui.wheel(-1)
        self.assertEqual(session.cursor, before - 1)
        new_span = tui._paper_spans[session.current().index]
        self.assertEqual(tui.body_window.vertical_scroll, max(0, new_span[1] - height))

    def test_no_drag_at_the_last_paper(self) -> None:
        session, tui = self._boot(n=8, rows=40)  # the whole list fits; nothing to scroll
        session.jump_last()
        cursor = session.cursor
        tui.wheel(1)
        self.assertEqual(session.cursor, cursor)
        self.assertEqual(tui.body_window.vertical_scroll, 0)

    def test_follow_mode_still_drags_the_selection(self) -> None:
        session, tui = self._boot(follow=True)
        tui.wheel(1)
        tui.wheel(1)
        self.assertEqual(session.cursor, 1)  # stale spans are not used for pinning
        self.assertTrue(session.abstract_open(session.current()))

    def test_wheel_is_ignored_outside_the_list_view(self) -> None:
        session, tui = self._boot()
        session.view = "help"
        tui.wheel(1)
        self.assertEqual(session.cursor, 0)
        self.assertEqual(tui.body_window.vertical_scroll, 0)

    def test_wheel_window_routes_mouse_events(self) -> None:
        from prompt_toolkit.data_structures import Point
        from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

        def event(kind):
            return MouseEvent(position=Point(x=0, y=0), event_type=kind,
                              button=MouseButton.NONE, modifiers=frozenset())

        _session, tui = self._boot()
        tui.body_window._mouse_handler(event(MouseEventType.SCROLL_DOWN))
        self.assertEqual(tui.body_window.vertical_scroll, 1)
        tui.body_window._mouse_handler(event(MouseEventType.SCROLL_UP))
        self.assertEqual(tui.body_window.vertical_scroll, 0)
        self.assertIs(
            tui.body_window._mouse_handler(event(MouseEventType.MOUSE_DOWN)), NotImplemented,
        )


if __name__ == "__main__":
    unittest.main()
