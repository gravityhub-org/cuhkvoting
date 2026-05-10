"""Tests for topvoted ordering: vote count, then most recent vote, then id."""

from __future__ import annotations

import copy
import unittest

from cuhkvoting.cli import _latest_vote_timestamp, _topvoted_rows_from_papers


def _paper(
    arxiv_id: str,
    votes: list[dict],
    *,
    selected: bool = False,
    title: str = "t",
    abstract: str = "",
) -> dict:
    return {
        "id": arxiv_id,
        "title": title,
        "abstract": abstract,
        "votes": votes,
        "selected": selected,
    }


def _vote(user: str, voted_at: str) -> dict:
    return {"user": user, "voted_at": voted_at}


class TestLatestVoteTimestamp(unittest.TestCase):
    def test_max_of_votes(self) -> None:
        votes = [
            _vote("a", "2030-01-01T00:00:00Z"),
            _vote("b", "2030-12-31T00:00:00Z"),
            _vote("c", "2030-06-01T00:00:00Z"),
        ]
        self.assertEqual(
            _latest_vote_timestamp(votes),
            _latest_vote_timestamp([_vote("only", "2030-12-31T00:00:00Z")]),
        )

    def test_missing_timestamps(self) -> None:
        self.assertEqual(_latest_vote_timestamp([{"user": "x"}]), 0.0)


class TestTopvotedRowsSort(unittest.TestCase):
    def test_equal_votes_newer_activity_first(self) -> None:
        papers = [
            _paper(
                "1111.0001",
                [_vote("u1", "2030-01-01T00:00:00Z"), _vote("u2", "2030-01-02T00:00:00Z")],
            ),
            _paper(
                "1111.0002",
                [_vote("a", "2030-06-01T00:00:00Z"), _vote("b", "2030-01-01T00:00:00Z")],
            ),
        ]
        rows = _topvoted_rows_from_papers(copy.deepcopy(papers), {})
        ids = [r["id"] for r in rows]
        self.assertEqual(ids, ["1111.0002", "1111.0001"])

    def test_more_votes_beats_more_recent_fewer_votes(self) -> None:
        papers = [
            _paper("2222.0001", [_vote("a", "2030-12-31T00:00:00Z")]),
            _paper(
                "2222.0002",
                [
                    _vote("b", "2030-01-01T00:00:00Z"),
                    _vote("c", "2030-01-02T00:00:00Z"),
                ],
            ),
        ]
        rows = _topvoted_rows_from_papers(copy.deepcopy(papers), {})
        self.assertEqual(rows[0]["id"], "2222.0002")

    def test_tie_on_votes_and_activity_uses_id(self) -> None:
        same_ts = "2030-05-01T12:00:00Z"
        papers = [
            _paper("3333.0002", [_vote("x", same_ts)]),
            _paper("3333.0001", [_vote("y", same_ts)]),
        ]
        rows = _topvoted_rows_from_papers(copy.deepcopy(papers), {})
        self.assertEqual([r["id"] for r in rows], ["3333.0001", "3333.0002"])

    def test_skips_selected_and_zero_votes(self) -> None:
        papers = [
            _paper("4444.0001", [_vote("a", "2030-01-01T00:00:00Z")], selected=True),
            _paper("4444.0002", []),
            _paper("4444.0003", [_vote("b", "2030-02-01T00:00:00Z")]),
        ]
        rows = _topvoted_rows_from_papers(copy.deepcopy(papers), {})
        self.assertEqual([r["id"] for r in rows], ["4444.0003"])


if __name__ == "__main__":
    unittest.main()
