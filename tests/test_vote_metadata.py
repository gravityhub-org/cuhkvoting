"""Tests for vote metadata resolution (title must not be empty)."""

from __future__ import annotations

import unittest
import urllib.error
from unittest import mock

from cuhkvoting import cli


class VoteMetadataTests(unittest.TestCase):
    def test_uses_provided_title_without_network(self) -> None:
        # Mock the cache lookup so the url/abstract fallback is deterministic
        # rather than reading whatever the real today/lastweek cache holds.
        with mock.patch("cuhkvoting.cli._lookup_local_cache", return_value=None):
            meta = cli._resolve_vote_metadata("2601.09678", "Known Title")
        self.assertEqual(meta["paper_id"], "2601.09678")
        self.assertEqual(meta["title"], "Known Title")
        self.assertEqual(meta["url"], f"{cli.ARXIV_ABS}2601.09678")

    def test_uses_cache_when_title_missing(self) -> None:
        cached = {"id": "2601.09678", "title": "Cached Title", "url": "u", "abstract": "abs"}
        with mock.patch("cuhkvoting.cli._lookup_local_cache", return_value=cached):
            meta = cli._resolve_vote_metadata("2601.09678")
        self.assertEqual(meta["title"], "Cached Title")
        self.assertEqual(meta["abstract"], "abs")

    def test_fetches_when_title_still_missing(self) -> None:
        entry = {
            "id": "2601.09678",
            "title": "Fetched Title",
            "abstract": "Fetched abstract",
            "url": f"{cli.ARXIV_ABS}2601.09678",
        }
        with mock.patch("cuhkvoting.cli._lookup_local_cache", return_value=None):
            with mock.patch("cuhkvoting.cli._validate_arxiv_entry", return_value=entry) as validate:
                meta = cli._resolve_vote_metadata("2601.09678v2")
        self.assertEqual(validate.call_count, 1)
        self.assertEqual(meta["title"], "Fetched Title")
        self.assertEqual(meta["abstract"], "Fetched abstract")

    def test_apply_paper_metadata_backfills_empty_title(self) -> None:
        paper = {"id": "2601.09678", "title": "", "abstract": "", "url": "", "votes": []}
        cli._apply_paper_metadata(
            paper,
            {
                "paper_id": "2601.09678",
                "title": "Backfilled",
                "url": f"{cli.ARXIV_ABS}2601.09678",
                "abstract": "abs",
            },
        )
        self.assertEqual(paper["title"], "Backfilled")
        self.assertEqual(paper["url"], f"{cli.ARXIV_ABS}2601.09678")
        self.assertEqual(paper["abstract"], "abs")

    def test_apply_paper_metadata_keeps_existing_title(self) -> None:
        paper = {"id": "2601.09678", "title": "Existing", "abstract": "", "url": "u", "votes": []}
        cli._apply_paper_metadata(paper, {"title": "New", "url": "other", "abstract": "abs"})
        self.assertEqual(paper["title"], "Existing")

    def test_backfill_paper_metadata_fetches_missing_title(self) -> None:
        paper = {"id": "2605.11269", "title": "", "abstract": "", "url": "", "votes": [{"user": "u"}]}
        entry = {
            "id": "2605.11269",
            "title": "Fetched",
            "abstract": "abs",
            "url": f"{cli.ARXIV_ABS}2605.11269",
        }
        with mock.patch("cuhkvoting.cli._resolve_vote_metadata", return_value=entry):
            reasons = cli._backfill_paper_metadata(paper)
        self.assertEqual(paper["title"], "Fetched")
        self.assertIn("title backfilled", reasons)

    def test_backfill_paper_metadata_skips_when_complete(self) -> None:
        paper = {"id": "2601.09678", "title": "T", "abstract": "a", "url": "u", "votes": []}
        self.assertEqual(cli._backfill_paper_metadata(paper), [])

    def test_resolve_vote_metadata_raises_titleunresolved_on_not_found(self) -> None:
        # arXiv reachable but id absent: _validate_arxiv_entry raises SystemExit,
        # which _resolve_vote_metadata translates into the typed typo signal.
        with mock.patch("cuhkvoting.cli._lookup_local_cache", return_value=None):
            with mock.patch("cuhkvoting.cli._validate_arxiv_entry", side_effect=SystemExit("nope")):
                with self.assertRaises(cli.TitleUnresolved):
                    cli._resolve_vote_metadata("2601.99999")

    def test_resolve_vote_metadata_raises_titleunresolved_on_empty_title(self) -> None:
        entry = {"id": "2601.09678", "title": "", "url": "u", "abstract": ""}
        with mock.patch("cuhkvoting.cli._lookup_local_cache", return_value=None):
            with mock.patch("cuhkvoting.cli._validate_arxiv_entry", return_value=entry):
                with self.assertRaises(cli.TitleUnresolved):
                    cli._resolve_vote_metadata("2601.09678")

    def test_resolve_vote_metadata_propagates_network_error(self) -> None:
        # arXiv unreachable: the native error must propagate so callers can decide
        # not to block the vote (rather than being swallowed as a typo).
        with mock.patch("cuhkvoting.cli._lookup_local_cache", return_value=None):
            with mock.patch("cuhkvoting.cli._validate_arxiv_entry",
                            side_effect=urllib.error.URLError("down")):
                with self.assertRaises(urllib.error.URLError):
                    cli._resolve_vote_metadata("2601.09678")

    def test_resolve_batch_metadata_isolates_typo_and_outage(self) -> None:
        resolved = [
            ("2601.00001", "Good", None),   # resolves normally
            ("2601.99999", None, None),     # typo → skipped
            ("2601.00003", "Kept", None),   # arXiv outage → kept with known title
        ]

        def fake_resolve(arxiv_id, title=None):
            if arxiv_id == "2601.00001":
                return {"paper_id": arxiv_id, "title": "Good", "url": "u1", "abstract": ""}
            if arxiv_id == "2601.99999":
                raise cli.TitleUnresolved(arxiv_id)
            raise urllib.error.URLError("arxiv down")

        with mock.patch("cuhkvoting.cli._resolve_vote_metadata", side_effect=fake_resolve):
            papers_meta, skipped = cli._resolve_batch_metadata(resolved)

        self.assertEqual(skipped, ["2601.99999"])
        self.assertEqual([m["paper_id"] for m in papers_meta], ["2601.00001", "2601.00003"])
        outage = next(m for m in papers_meta if m["paper_id"] == "2601.00003")
        self.assertEqual(outage["title"], "Kept")  # known title preserved through the outage

    def test_backfill_paper_metadata_survives_network_error(self) -> None:
        paper = {"id": "2605.11269", "title": "", "abstract": "", "url": "", "votes": []}
        with mock.patch("cuhkvoting.cli._resolve_vote_metadata",
                        side_effect=urllib.error.URLError("down")):
            reasons = cli._backfill_paper_metadata(paper)
        self.assertEqual(paper["title"], "")  # unchanged; no crash
        self.assertTrue(any("arXiv unreachable" in r for r in reasons))


if __name__ == "__main__":
    unittest.main()
