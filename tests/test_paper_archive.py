"""Paper-level archive expiry: keep individual votes; archive when newest vote is stale."""

from __future__ import annotations

import copy
import datetime as dt
import unittest
from unittest import mock

from cuhkvoting.cli import (
    VOTE_EXPIRY_DAYS,
    _archive_expired_paper,
    _paper_is_expired,
    _prune_expired_votes,
    _register_vote,
    _topvoted_rows_from_papers,
    _unarchive_paper,
)


def _vote(user: str, voted_at: str) -> dict:
    return {"user": user, "voted_at": voted_at}


def _paper(arxiv_id: str, votes: list[dict], **extra) -> dict:
    doc = {
        "id": arxiv_id,
        "title": "t",
        "abstract": "",
        "url": f"https://arxiv.org/abs/{arxiv_id}",
        "votes": votes,
    }
    doc.update(extra)
    return doc


class TestPaperExpiry(unittest.TestCase):
    def test_expired_when_newest_vote_past_cutoff(self) -> None:
        now = dt.datetime(2030, 6, 15, tzinfo=dt.timezone.utc)
        paper = _paper(
            "1111.0001",
            [
                _vote("a", "2030-04-01T00:00:00Z"),
                _vote("b", "2030-05-01T00:00:00Z"),  # 45 days before now
            ],
        )
        self.assertTrue(_paper_is_expired(paper, now=now))

    def test_active_when_any_vote_within_window(self) -> None:
        now = dt.datetime(2030, 6, 15, tzinfo=dt.timezone.utc)
        paper = _paper(
            "1111.0002",
            [
                _vote("a", "2030-01-01T00:00:00Z"),
                _vote("b", "2030-06-01T00:00:00Z"),  # 14 days before now
            ],
        )
        self.assertFalse(_paper_is_expired(paper, now=now))


class TestArchiveHelpers(unittest.TestCase):
    def test_archive_sets_flag_keeps_votes(self) -> None:
        now = dt.datetime(2030, 6, 15, tzinfo=dt.timezone.utc)
        votes = [
            _vote("a", "2030-04-01T00:00:00Z"),
            _vote("b", "2030-05-01T00:00:00Z"),
        ]
        paper = _paper("2222.0001", copy.deepcopy(votes))
        self.assertTrue(_archive_expired_paper(paper, now=now))
        self.assertTrue(paper["archived"])
        self.assertEqual(paper["archived_at"], "2030-06-15T00:00:00Z")
        self.assertEqual(paper["votes"], votes)

    def test_archive_noop_when_active_or_already_archived(self) -> None:
        now = dt.datetime(2030, 6, 15, tzinfo=dt.timezone.utc)
        active = _paper("2222.0002", [_vote("a", "2030-06-10T00:00:00Z")])
        self.assertFalse(_archive_expired_paper(active, now=now))
        self.assertNotIn("archived", active)

        already = _paper(
            "2222.0003",
            [_vote("a", "2030-01-01T00:00:00Z")],
            archived=True,
            archived_at="2030-02-01T00:00:00Z",
        )
        self.assertFalse(_archive_expired_paper(already, now=now))
        self.assertEqual(already["archived_at"], "2030-02-01T00:00:00Z")

    def test_prune_shim_archives_without_stripping_votes(self) -> None:
        votes = [_vote("a", "2020-01-01T00:00:00Z"), _vote("b", "2020-01-02T00:00:00Z")]
        paper = _paper("2222.0004", copy.deepcopy(votes))
        self.assertEqual(_prune_expired_votes(paper), 1)
        self.assertTrue(paper["archived"])
        self.assertEqual(paper["votes"], votes)

    def test_unarchive_clears_fields(self) -> None:
        paper = _paper(
            "2222.0005",
            [_vote("a", "2020-01-01T00:00:00Z")],
            archived=True,
            archived_at="2020-02-01T00:00:00Z",
        )
        _unarchive_paper(paper)
        self.assertNotIn("archived", paper)
        self.assertNotIn("archived_at", paper)


class TestRegisterVoteUnarchives(unittest.TestCase):
    def test_new_vote_clears_archive_keeps_prior_votes(self) -> None:
        prior = [_vote("alice", "2020-01-01T00:00:00Z"), _vote("bob", "2020-01-15T00:00:00Z")]
        paper = _paper(
            "3333.0001",
            copy.deepcopy(prior),
            archived=True,
            archived_at="2020-03-01T00:00:00Z",
        )
        meta = {"title": "t", "url": "https://arxiv.org/abs/3333.0001", "abstract": ""}
        with mock.patch(
            "cuhkvoting.cli._make_vote_entry",
            return_value=_vote("carol", "2030-06-15T12:00:00Z"),
        ):
            added = _register_vote(paper, "3333.0001", "carol", meta)
        self.assertTrue(added)
        self.assertNotIn("archived", paper)
        self.assertNotIn("archived_at", paper)
        self.assertEqual(len(paper["votes"]), 3)
        self.assertEqual(paper["votes"][0]["user"], "alice")
        self.assertEqual(paper["votes"][1]["user"], "bob")
        self.assertEqual(paper["votes"][2]["user"], "carol")

    def test_already_voted_does_not_unarchive(self) -> None:
        paper = _paper(
            "3333.0002",
            [_vote("alice", "2020-01-01T00:00:00Z")],
            archived=True,
            archived_at="2020-03-01T00:00:00Z",
        )
        added = _register_vote(
            paper,
            "3333.0002",
            "alice",
            {"title": "t", "url": "u", "abstract": ""},
        )
        self.assertFalse(added)
        self.assertTrue(paper["archived"])
        self.assertEqual(len(paper["votes"]), 1)


class TestTopvotedArchiveExpiry(unittest.TestCase):
    def test_keeps_all_votes_when_paper_has_recent_activity(self) -> None:
        # Relative to real now: one ancient vote + one far-future vote → active, count=2
        papers = [
            _paper(
                "4444.0001",
                [
                    _vote("old", "2020-01-01T00:00:00Z"),
                    _vote("new", "2030-06-01T00:00:00Z"),
                ],
            )
        ]
        rows = _topvoted_rows_from_papers(copy.deepcopy(papers), {})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["votes"], 2)

    def test_omits_expired_paper_without_archived_flag(self) -> None:
        papers = [
            _paper("4444.0002", [_vote("a", "2020-01-01T00:00:00Z")]),
            _paper("4444.0003", [_vote("b", "2030-06-01T00:00:00Z")]),
        ]
        rows = _topvoted_rows_from_papers(copy.deepcopy(papers), {})
        self.assertEqual([r["id"] for r in rows], ["4444.0003"])

    def test_omits_archived_flag_even_if_votes_look_fresh(self) -> None:
        # archived flag is authoritative for topvoted once set
        papers = [
            _paper(
                "4444.0004",
                [_vote("a", "2030-06-01T00:00:00Z")],
                archived=True,
                archived_at="2030-06-10T00:00:00Z",
            )
        ]
        rows = _topvoted_rows_from_papers(copy.deepcopy(papers), {})
        self.assertEqual(rows, [])

    def test_does_not_strip_votes_on_topvoted(self) -> None:
        source = _paper(
            "4444.0005",
            [
                _vote("old", "2020-01-01T00:00:00Z"),
                _vote("new", "2030-06-01T00:00:00Z"),
            ],
        )
        papers = [copy.deepcopy(source)]
        _ = _topvoted_rows_from_papers(papers, {})
        self.assertEqual(len(papers[0]["votes"]), 2)


class TestExpiryConstant(unittest.TestCase):
    def test_vote_expiry_days_positive(self) -> None:
        self.assertGreater(VOTE_EXPIRY_DAYS, 0)


if __name__ == "__main__":
    unittest.main()
