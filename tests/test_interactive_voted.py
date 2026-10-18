"""Session-local "already voted" marks: tick + green tint after :w."""

from __future__ import annotations

import unittest
from unittest import mock

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
        "url": "u",
    }


class CastVotesMarksSessionTest(unittest.TestCase):
    def _cast(self, session: interactive.Session, result) -> tuple[bool, list[list[dict]]]:
        metas_calls: list[list[dict]] = []

        def fake_batch(*args, **_kwargs):
            metas_calls.append(args[2] if len(args) > 2 else [])
            if isinstance(result, Exception):
                raise result
            return result

        with mock.patch("cuhkvoting.cli._get_token", return_value="tok"), \
             mock.patch("cuhkvoting.cli._resolve_repo_config", return_value=object()), \
             mock.patch("cuhkvoting.cli._prefer_api_vote", return_value=False), \
             mock.patch("cuhkvoting.cli._has_github_ssh_access", return_value=True), \
             mock.patch("cuhkvoting.cli._resolve_user", return_value="tester"), \
             mock.patch("cuhkvoting.cli._warn_if_display_name_changed"), \
             mock.patch("cuhkvoting.cli._batch_vote_papers_ssh", side_effect=fake_batch):
            ok = interactive._cast_votes(session, _cfg())
        return ok, metas_calls

    def test_new_and_duplicate_votes_get_marked(self) -> None:
        session = interactive.Session([_entry(1), _entry(2)])
        session.voting = ["2601.00001", "2601.00002"]
        result = cli.VoteResult(["2601.00001", "2601.00002"], ["2601.00001"], None)
        ok, _calls = self._cast(session, result)
        self.assertTrue(ok)
        self.assertEqual(session.voted, {"2601.00001", "2601.00002"})
        self.assertEqual(session.voting, [])
        self.assertEqual(
            [row["status"] for row in session.session_votes], ["new", "duplicate"],
        )

    def test_failed_cast_marks_nothing(self) -> None:
        session = interactive.Session([_entry(1)])
        session.voting = ["2601.00001"]
        ok, _calls = self._cast(session, RuntimeError("boom"))
        self.assertFalse(ok)
        self.assertEqual(session.voted, set())
        self.assertEqual(session.voting, ["2601.00001"])

    def test_skipped_papers_are_not_marked(self) -> None:
        session = interactive.Session([_entry(1)])
        session.voting = ["2601.00001"]
        result = cli.VoteResult([], [], None, ["2601.00001"])
        ok, _calls = self._cast(session, result)
        self.assertTrue(ok)
        self.assertEqual(session.voted, set())
        self.assertEqual(session.voting, [])

    def test_marks_accumulate_across_writes(self) -> None:
        session = interactive.Session([_entry(1), _entry(2)])
        session.voting = ["2601.00001"]
        self._cast(session, cli.VoteResult(["2601.00001"], ["2601.00001"], None))
        session.voting = ["2601.00002"]
        self._cast(session, cli.VoteResult(["2601.00002"], ["2601.00002"], None))
        self.assertEqual(session.voted, {"2601.00001", "2601.00002"})

    def test_cast_passes_the_abstract_to_the_batch(self) -> None:
        session = interactive.Session([_entry(1)])
        session.voting = ["2601.00001"]
        _ok, calls = self._cast(session, cli.VoteResult(["2601.00001"], ["2601.00001"], None))
        self.assertEqual(calls[0][0]["abstract"], "abs")
        self.assertEqual(calls[0][0]["title"], "Paper 1")
        self.assertEqual(calls[0][0]["paper_id"], "2601.00001")

    def test_abstract_whitespace_is_normalized(self) -> None:
        entry = dict(_entry(1), abstract="first line\n  second\tline")
        session = interactive.Session([entry])
        session.voting = ["2601.00001"]
        _ok, calls = self._cast(session, cli.VoteResult(["2601.00001"], ["2601.00001"], None))
        self.assertEqual(calls[0][0]["abstract"], "first line second line")


class VotedFragmentsTest(unittest.TestCase):
    def _frags(self, *, voted: bool, open_abstract: bool = False, cursor: bool = False):
        session = interactive.Session([_entry(1)])
        if voted:
            session.voted.add("2601.00001")
        session.papers[0].open = open_abstract
        if not cursor:
            session.cursor = len(session.papers)  # no current row
        return interactive._entry_fragments(session.papers[0], session, _cfg(), 80)

    def _text(self, frags: list) -> str:
        return "".join(text for _style, text in frags)

    def _styles(self, frags: list) -> str:
        return " ".join(style for style, _text in frags)

    def test_tick_and_green_tint(self) -> None:
        frags = self._frags(voted=True)
        self.assertIn("✓", self._text(frags))
        styles = self._styles(frags)
        self.assertIn("class:title class:voted", styles)
        self.assertIn("class:id class:voted", styles)

    def test_abstract_is_tinted_too(self) -> None:
        styles = self._styles(self._frags(voted=True, open_abstract=True))
        self.assertIn("class:abstract class:voted", styles)

    def test_cursor_and_voted_compose(self) -> None:
        styles = self._styles(self._frags(voted=True, cursor=True))
        self.assertIn("class:cursor class:voted", styles)

    def test_plain_rows_have_no_tick_or_tint(self) -> None:
        frags = self._frags(voted=False)
        self.assertNotIn("✓", self._text(frags))
        self.assertNotIn("class:voted", self._styles(frags))


@unittest.skipUnless(interactive.PROMPT_TOOLKIT_OK, "prompt_toolkit not installed")
class VotedRenderTests(unittest.TestCase):
    def test_render_body_picks_up_votes_without_reload(self) -> None:
        from prompt_toolkit.input import DummyInput
        from prompt_toolkit.output import DummyOutput

        session = interactive.Session([_entry(1), _entry(2)])
        keymap, _ = interactive._effective_keymap({})
        tui = interactive.Tui(
            session, _cfg(), interactive.InteractiveConfig(), keymap,
            input=DummyInput(), output=DummyOutput(),
        )
        before = "".join(text for _style, text in tui.render_body())
        self.assertNotIn("✓", before)
        session.voted.add("2601.00002")
        after = "".join(text for _style, text in tui.render_body())
        self.assertIn("✓", after)


if __name__ == "__main__":
    unittest.main()
