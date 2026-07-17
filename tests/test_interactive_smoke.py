"""End-to-end smoke tests: real Application driven through a pipe input."""

from __future__ import annotations

import unittest

from cuhkvoting import cli, interactive


def _cfg() -> cli.Config:
    return cli.Config(
        categories=["gr-qc"], today_max_age=60, lastweek_max_age=360,
        abstract_lines=0, abstract_wrap=80, confirm_by_number=True,
        display_name="", display_name_overrides={}, highlight_authors=[],
        highlight_keywords=["kilonova"], highlight_keyword_count=-1,
        highlight_glyph="★",
    )


def _entries(n: int) -> list[dict]:
    return [
        {
            "id": f"2607.{i:05d}",
            "title": f"Paper number {i}",
            "abstract": "About a kilonova." if i == 2 else "About mergers.",
            "authors": ["Ann Chan", "Bo Li"],
            "published": "2026-07-15",
            "url": f"https://arxiv.org/abs/2607.{i:05d}",
        }
        for i in range(1, n + 1)
    ]


@unittest.skipUnless(interactive.PROMPT_TOOLKIT_OK, "prompt_toolkit not installed")
class SmokeTests(unittest.TestCase):
    def _run_keys(self, keys: str, n: int = 5):
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        session = interactive.Session(_entries(n))
        keymap, _problems = interactive._effective_keymap({})
        with create_pipe_input() as pipe:
            tui = interactive.Tui(
                session, _cfg(), interactive.InteractiveConfig(), keymap,
                input=pipe, output=DummyOutput(),
            )
            pipe.send_text(keys)
            tui.app.run()
        return session, tui

    def test_counts_folds_search_jump(self) -> None:
        session, _tui = self._run_keys("3jzagg/kilonova\rG:2\r:q!\r")
        self.assertTrue(session.papers[3].open)      # za after 3j
        self.assertEqual(session.search_pat, "kilonova")
        self.assertEqual(session.cursor, 1)          # :2 jump was the last move

    def test_stage_quit_refusal_then_unstage(self) -> None:
        # :q must refuse while a vote is staged; d unstages; the second :q exits.
        # Reaching the end of the sequence proves the refusal (an early exit
        # would leave the paper staged).
        session, _tui = self._run_keys("v:q\rd:q\r")
        self.assertEqual(session.voting, [])
        self.assertEqual(session.session_votes, [])

    def test_render_body_produces_styled_fragments(self) -> None:
        session, tui = self._run_keys("jza:q!\r")
        fragments = tui.render_body()
        styles = " ".join(style for style, _text in fragments)
        text = "".join(text for _style, text in fragments)
        self.assertIn("class:id", styles)
        self.assertIn("class:cursor", styles)
        self.assertIn("class:abstract", styles)      # paper 2's fold is open
        self.assertIn("class:kw", styles)            # kilonova keyword suffix
        self.assertIn("2607.00002", text)

    def test_follow_mode_tracks_cursor(self) -> None:
        session, _tui = self._run_keys("zij:q!\r")
        self.assertTrue(session.follow)
        self.assertFalse(session.abstract_open(session.papers[0]))
        self.assertTrue(session.abstract_open(session.papers[1]))
        self.assertFalse(any(p.open for p in session.papers))

    def test_help_and_confirm_views_render(self) -> None:
        session, tui = self._run_keys(":q!\r")
        session.view = "help"
        help_text = "".join(t for _s, t in tui.render_body())
        self.assertIn(":theme <name>", help_text)
        session.view = "confirm"
        session.voting = ["2607.00001"]
        confirm_text = "".join(t for _s, t in tui.render_body())
        self.assertIn("Cast 1 vote?", confirm_text)
        self.assertIn("2607.00001", confirm_text)


if __name__ == "__main__":
    unittest.main()
