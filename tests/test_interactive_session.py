"""Tests for the pure interactive-mode session model."""

from __future__ import annotations

import unittest

from cuhkvoting.interactive import Session


def _entries(n: int) -> list[dict]:
    return [
        {
            "id": f"2607.{i:05d}",
            "title": f"Title {i}",
            "abstract": f"Abstract {i} about mergers" if i % 2 else f"Abstract {i}",
            "authors": [f"Ann Chan{i}", "Bo Li"],
            "published": "2026-07-15",
            "url": "",
        }
        for i in range(1, n + 1)
    ]


class MovementTests(unittest.TestCase):
    def test_move_clamps(self) -> None:
        s = Session(_entries(5))
        s.move(100)
        self.assertEqual(s.cursor, 4)
        s.move(-100)
        self.assertEqual(s.cursor, 0)

    def test_take_count(self) -> None:
        s = Session(_entries(10))
        s.pending_count = "3"
        s.move(s.take_count())
        self.assertEqual(s.cursor, 3)
        self.assertEqual(s.pending_count, "")
        s.move(s.take_count())  # no pending count -> defaults to 1
        self.assertEqual(s.cursor, 4)

    def test_jump_first_last(self) -> None:
        s = Session(_entries(5))
        s.jump_last()
        self.assertEqual(s.cursor, 4)
        s.jump_first()
        self.assertEqual(s.cursor, 0)

    def test_jump_index(self) -> None:
        s = Session(_entries(5))
        s.jump_index(4)
        self.assertEqual(s.cursor, 3)

    def test_jump_index_out_of_range_sets_feedback(self) -> None:
        s = Session(_entries(5))
        s.jump_index(9)
        self.assertEqual(s.cursor, 0)
        self.assertIn("9", s.feedback)


class VotingListTests(unittest.TestCase):
    def test_add_and_duplicate(self) -> None:
        s = Session(_entries(3))
        s.add_current()
        self.assertEqual(s.voting, ["2607.00001"])
        s.add_current()
        self.assertEqual(s.voting, ["2607.00001"])
        self.assertIn("already", s.feedback)

    def test_remove_and_missing(self) -> None:
        s = Session(_entries(3))
        s.add_current()
        s.remove_current()
        self.assertEqual(s.voting, [])
        s.remove_current()
        self.assertIn("not in the voting list", s.feedback)

    def test_ls_filter_preserves_indices(self) -> None:
        s = Session(_entries(5))
        s.jump_index(2)
        s.add_current()
        s.jump_index(4)
        s.add_current()
        s.enter_ls()
        self.assertTrue(s.ls_only)
        self.assertEqual([p.index for p in s.visible()], [2, 4])
        # the cursor stayed on paper 4, now at filtered position 1
        self.assertEqual(s.cursor, 1)

    def test_ls_empty_refuses(self) -> None:
        s = Session(_entries(3))
        s.enter_ls()
        self.assertFalse(s.ls_only)
        self.assertIn("empty", s.feedback)

    def test_exit_ls_restores_cursor(self) -> None:
        s = Session(_entries(5))
        s.jump_index(2)
        s.add_current()
        s.jump_index(4)
        s.add_current()
        s.enter_ls()
        s.jump_first()  # cursor on paper 2 within the filter
        s.exit_ls()
        self.assertFalse(s.ls_only)
        self.assertEqual(s.visible()[s.cursor].index, 2)

    def test_removing_last_staged_paper_exits_ls(self) -> None:
        s = Session(_entries(3))
        s.add_current()
        s.enter_ls()
        s.remove_current()
        self.assertEqual(s.voting, [])
        self.assertFalse(s.ls_only)


class FoldTests(unittest.TestCase):
    def test_toggle_open_close(self) -> None:
        s = Session(_entries(3))
        s.toggle_abstract()
        self.assertTrue(s.papers[0].open)
        s.open_abstract()  # spec: no-op when already open
        self.assertTrue(s.papers[0].open)
        s.close_abstract()
        self.assertFalse(s.papers[0].open)
        s.toggle_abstract()
        s.toggle_abstract()
        self.assertFalse(s.papers[0].open)

    def test_open_close_all(self) -> None:
        s = Session(_entries(3))
        s.open_all()
        self.assertTrue(all(p.open for p in s.papers))
        s.close_all()
        self.assertFalse(any(p.open for p in s.papers))


class FollowModeTests(unittest.TestCase):
    def test_toggle(self) -> None:
        s = Session(_entries(3))
        s.toggle_follow()
        self.assertTrue(s.follow)
        self.assertIn("Follow mode", s.feedback)
        s.toggle_follow()
        self.assertFalse(s.follow)

    def test_overlay_follows_cursor_without_touching_flags(self) -> None:
        s = Session(_entries(3))
        s.toggle_follow()
        self.assertTrue(s.abstract_open(s.papers[0]))
        self.assertFalse(s.abstract_open(s.papers[1]))
        s.move(1)
        self.assertFalse(s.abstract_open(s.papers[0]))  # back to its own state
        self.assertTrue(s.abstract_open(s.papers[1]))
        self.assertFalse(any(p.open for p in s.papers))  # flags never touched

    def test_explicitly_opened_paper_keeps_its_state(self) -> None:
        s = Session(_entries(3))
        s.toggle_abstract()  # za on paper 1
        s.toggle_follow()
        s.move(1)
        self.assertTrue(s.abstract_open(s.papers[0]))  # za state survives
        self.assertTrue(s.abstract_open(s.papers[1]))  # followed

    def test_off_restores_view(self) -> None:
        s = Session(_entries(3))
        s.toggle_follow()
        s.toggle_follow()
        self.assertFalse(s.abstract_open(s.papers[0]))


class SearchTests(unittest.TestCase):
    def test_forward_search_and_wrap(self) -> None:
        s = Session(_entries(4))  # papers 1 and 3 have "mergers" in the abstract
        s.search("mergers", +1)
        self.assertEqual(s.cursor, 2)  # paper 3: first match after the cursor
        s.next_match()
        self.assertEqual(s.cursor, 0)  # wraps back to paper 1
        self.assertIn("match 1/2", s.feedback)

    def test_backward_search(self) -> None:
        s = Session(_entries(4))
        s.search("mergers", -1)
        self.assertEqual(s.cursor, 2)  # first match going backwards (wraps)

    def test_reverse_of_backward_goes_forward(self) -> None:
        s = Session(_entries(4))
        s.search("mergers", -1)
        s.next_match(reverse=True)
        self.assertEqual(s.cursor, 0)

    def test_search_matches_authors_and_title(self) -> None:
        s = Session(_entries(4))
        s.search("chan3", +1)
        self.assertEqual(s.cursor, 2)
        s.search("title 2", +1)
        self.assertEqual(s.cursor, 1)

    def test_not_found(self) -> None:
        s = Session(_entries(3))
        s.search("axion", +1)
        self.assertEqual(s.cursor, 0)
        self.assertIn("not found", s.feedback)

    def test_n_without_search(self) -> None:
        s = Session(_entries(3))
        s.next_match()
        self.assertIn("No previous search", s.feedback)


class StatusLineTests(unittest.TestCase):
    def test_spec_example(self) -> None:
        # The spec's `| NOR ... 3 sel  23:34 |`: 3 staged, cursor on 23 of 34.
        s = Session(_entries(34))
        s.voting = ["2607.00001", "2607.00002", "2607.00003"]
        s.cursor = 22
        self.assertEqual(s.status_right(), "3 sel  23:34")

    def test_empty_list(self) -> None:
        s = Session([])
        self.assertEqual(s.status_right(), "0 sel  0:0")


if __name__ == "__main__":
    unittest.main()
