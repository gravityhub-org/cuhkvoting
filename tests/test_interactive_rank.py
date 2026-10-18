"""Interactive topvoted tie gutter: group roles and rendered row fragments."""

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


def _entry(index: int, votes: int, *, abstract: str = "abs") -> dict:
    return {
        "id": f"2601.{index:05d}",
        "title": f"Paper {index}",
        "abstract": abstract,
        "votes": votes,
        "voters": "Ann",
        "url": "u",
    }


def _text(frags: list) -> str:
    return "".join(text for _style, text in frags)


def _styles(frags: list) -> str:
    return " ".join(style for style, _text in frags)


class RankRolesTests(unittest.TestCase):
    def test_groups_and_roles(self) -> None:
        papers = [
            interactive.Paper(1, _entry(1, 3)),
            interactive.Paper(2, _entry(2, 3)),
            interactive.Paper(3, _entry(3, 3)),
            interactive.Paper(4, _entry(4, 1)),
            interactive.Paper(5, _entry(5, 2)),
            interactive.Paper(6, _entry(6, 2)),
        ]
        has_ties, roles = interactive._rank_roles(papers)
        self.assertTrue(has_ties)
        self.assertEqual(
            roles, {1: "top", 2: "mid", 3: "bottom", 4: "single", 5: "top", 6: "bottom"},
        )

    def test_no_ties_means_plain_rows(self) -> None:
        papers = [interactive.Paper(1, _entry(1, 1)), interactive.Paper(2, _entry(2, 2))]
        self.assertEqual(interactive._rank_roles(papers), (False, {}))

    def test_non_ranked_entries_have_no_roles(self) -> None:
        papers = [interactive.Paper(1, {"id": "2601.00001", "title": "T"})]
        self.assertEqual(interactive._rank_roles(papers), (False, {}))

    def test_empty_list(self) -> None:
        self.assertEqual(interactive._rank_roles([]), (False, {}))


class RankFragmentTests(unittest.TestCase):
    def _session(self) -> interactive.Session:
        return interactive.Session([
            _entry(1, 3),
            _entry(2, 3, abstract="A long abstract text"),
            _entry(3, 3),
        ])

    def test_tie_gutter_glyphs_and_dim_index(self) -> None:
        session = self._session()
        mid = interactive._entry_fragments(session.papers[1], session, _cfg(), 80, role="mid")
        self.assertIn("┣", _text(mid))
        self.assertIn("class:index.dim", _styles(mid))
        bottom = interactive._entry_fragments(session.papers[2], session, _cfg(), 80, role="bottom")
        self.assertIn("┗", _text(bottom))
        self.assertIn("class:index.dim", _styles(bottom))

    def test_top_member_keeps_the_normal_index(self) -> None:
        session = self._session()
        top = interactive._entry_fragments(session.papers[0], session, _cfg(), 80, role="top")
        self.assertIn("┳", _text(top))
        self.assertNotIn("class:index.dim", _styles(top))

    def test_no_role_means_no_gutter(self) -> None:
        session = self._session()
        plain = interactive._entry_fragments(session.papers[0], session, _cfg(), 80)
        for glyph in "┳┣┗":
            self.assertNotIn(glyph, _text(plain))

    def test_wrapped_tie_row_keeps_the_gutter(self) -> None:
        entries = [_entry(1, 3), _entry(2, 3)]
        entries[0]["title"] = " ".join(["wording"] * 20)
        session = interactive.Session(entries)
        lines = _text(
            interactive._entry_fragments(session.papers[0], session, _cfg(), 40, role="top")
        ).splitlines()
        self.assertGreater(len(lines), 1)  # the title wrapped
        for line in lines[1:]:
            self.assertEqual(line[5], "┃")          # pipe sits under the ┳ column
            self.assertEqual(line[:19].replace(" ", ""), "┃")
            self.assertTrue(line[19:].strip())      # text re-aligns under the title

    def test_wrapped_last_tie_row_has_no_pipe(self) -> None:
        entries = [_entry(1, 3), _entry(2, 3)]
        entries[1]["title"] = " ".join(["wording"] * 20)
        session = interactive.Session(entries)
        text = _text(
            interactive._entry_fragments(session.papers[1], session, _cfg(), 40, role="bottom")
        )
        self.assertGreater(len(text.splitlines()), 1)
        self.assertNotIn("┃", text)  # nothing below the last member: the tie ends

    def test_abstract_pipe_for_non_last_tie_member(self) -> None:
        session = self._session()
        session.papers[1].open = True
        text = _text(interactive._entry_fragments(session.papers[1], session, _cfg(), 80, role="mid"))
        self.assertTrue(any(
            line.startswith("     ┃ ") and "abstract" in line
            for line in text.splitlines()
        ))

    def test_abstract_of_last_tie_member_has_no_pipe(self) -> None:
        session = self._session()
        session.papers[2].open = True
        text = _text(interactive._entry_fragments(session.papers[2], session, _cfg(), 80, role="bottom"))
        self.assertNotIn("┃", text)
        self.assertTrue(any(line.startswith("       ") for line in text.splitlines()))


@unittest.skipUnless(interactive.PROMPT_TOOLKIT_OK, "prompt_toolkit not installed")
class RankRenderTests(unittest.TestCase):
    def test_render_body_draws_the_gutter_for_tied_rows(self) -> None:
        from prompt_toolkit.input import DummyInput
        from prompt_toolkit.output import DummyOutput

        session = interactive.Session([_entry(1, 3), _entry(2, 3), _entry(3, 1)])
        keymap, _ = interactive._effective_keymap({})
        tui = interactive.Tui(
            session, _cfg(), interactive.InteractiveConfig(), keymap,
            input=DummyInput(), output=DummyOutput(),
        )
        frags = tui.render_body()
        text = _text(frags)
        self.assertIn("┳", text)
        self.assertIn("┗", text)  # the single row gets a blank gutter instead
        self.assertIn("class:index.dim", _styles(frags))


if __name__ == "__main__":
    unittest.main()
