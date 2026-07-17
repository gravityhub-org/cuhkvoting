"""Tests for VoteResult from the batch-vote paths and the TUI casting flow."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from cuhkvoting import cli, interactive


class BatchVoteSshResultTests(unittest.TestCase):
    def test_new_vs_duplicate(self) -> None:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        papers_dir = Path(tmp) / "papers"
        papers_dir.mkdir()
        dup_file = papers_dir / "2601.00001.json"
        dup_file.write_text(json.dumps({
            "id": "2601.00001", "title": "Dup", "abstract": "", "url": "u",
            "votes": [cli._make_vote_entry("octocat")],
        }))
        original = dup_file.read_text()

        with mock.patch("cuhkvoting.cli._with_repo_checkout", return_value=tmp), \
                mock.patch("cuhkvoting.cli._run_git", return_value=""), \
                mock.patch("cuhkvoting.cli._ensure_commit_identity"), \
                mock.patch("cuhkvoting.cli.shutil.rmtree"), \
                contextlib.redirect_stdout(io.StringIO()):
            result = cli._batch_vote_papers_ssh(
                SimpleNamespace(branch="main"), "octocat",
                [{"paper_id": "2601.00001", "title": "Dup", "url": "u"},
                 {"paper_id": "2601.00002", "title": "New", "url": "u"}],
            )

        self.assertEqual(result.voted, ["2601.00001", "2601.00002"])
        self.assertEqual(result.new, ["2601.00002"])
        self.assertEqual(dup_file.read_text(), original)  # duplicate untouched
        new_paper = json.loads((papers_dir / "2601.00002.json").read_text())
        self.assertEqual(new_paper["votes"][0]["user"], "octocat")

    def test_empty_input(self) -> None:
        result = cli._batch_vote_papers_ssh(SimpleNamespace(branch="main"), "octocat", [])
        self.assertEqual((result.voted, result.new, result.outdated_msg), ([], [], None))


class CastVotesTests(unittest.TestCase):
    @staticmethod
    def _session() -> interactive.Session:
        entries = [
            {"id": f"2601.0000{i}", "title": f"Title {i}", "abstract": "",
             "authors": [], "url": ""}
            for i in (1, 2, 3)
        ]
        session = interactive.Session(entries)
        session.voting = ["2601.00001", "2601.00002", "2601.00003"]
        return session

    def test_skipped_duplicate_and_new_statuses(self) -> None:
        session = self._session()
        batch_result = cli.VoteResult(
            voted=["2601.00001", "2601.00002"], new=["2601.00002"],
            outdated_msg="Note: newer client available",
        )
        with mock.patch("cuhkvoting.cli._get_token", return_value="tok"), \
                mock.patch("cuhkvoting.cli._resolve_repo_config", return_value=object()), \
                mock.patch("cuhkvoting.cli._selected_arxiv_ids", return_value={"2601.00003"}), \
                mock.patch("cuhkvoting.cli._resolve_user", return_value="octocat"), \
                mock.patch("cuhkvoting.cli._warn_if_display_name_changed"), \
                mock.patch("cuhkvoting.cli._has_github_ssh_access", return_value=False), \
                mock.patch("cuhkvoting.cli._batch_vote_papers_api", return_value=batch_result) as batch:
            ok = interactive._cast_votes(session, SimpleNamespace(display_name=""))

        self.assertTrue(ok)
        statuses = {row["id"]: row["status"] for row in session.session_votes}
        self.assertEqual(statuses, {
            "2601.00001": "duplicate", "2601.00002": "new", "2601.00003": "skipped",
        })
        self.assertEqual(session.voting, [])
        self.assertEqual(session.outdated_msg, "Note: newer client available")
        self.assertTrue(any("2601.00003" in w for w in session.warnings))
        metas = batch.call_args[0][3]
        self.assertEqual([m["paper_id"] for m in metas], ["2601.00001", "2601.00002"])

    def test_failure_keeps_votes_staged(self) -> None:
        session = self._session()
        with mock.patch("cuhkvoting.cli._get_token", return_value="tok"), \
                mock.patch("cuhkvoting.cli._resolve_repo_config", return_value=object()), \
                mock.patch("cuhkvoting.cli._selected_arxiv_ids", return_value=set()), \
                mock.patch("cuhkvoting.cli._resolve_user", return_value="octocat"), \
                mock.patch("cuhkvoting.cli._warn_if_display_name_changed"), \
                mock.patch("cuhkvoting.cli._has_github_ssh_access", return_value=False), \
                mock.patch("cuhkvoting.cli._batch_vote_papers_api",
                           side_effect=RuntimeError("boom")):
            ok = interactive._cast_votes(session, SimpleNamespace(display_name=""))

        self.assertFalse(ok)
        self.assertEqual(len(session.voting), 3)
        self.assertEqual(session.session_votes, [])
        self.assertTrue(any("boom" in w for w in session.warnings))

    def test_no_auth_is_reported(self) -> None:
        session = self._session()
        with mock.patch("cuhkvoting.cli._get_token", return_value=None), \
                mock.patch("cuhkvoting.cli._resolve_repo_config", return_value=object()), \
                mock.patch("cuhkvoting.cli._selected_arxiv_ids", return_value=set()), \
                mock.patch("cuhkvoting.cli._resolve_user", return_value="octocat"), \
                mock.patch("cuhkvoting.cli._warn_if_display_name_changed"), \
                mock.patch("cuhkvoting.cli._has_github_ssh_access", return_value=False):
            ok = interactive._cast_votes(session, SimpleNamespace(display_name=""))

        self.assertFalse(ok)
        self.assertEqual(len(session.voting), 3)
        self.assertTrue(any("Voting needs auth" in w for w in session.warnings))


class ExitSummaryTests(unittest.TestCase):
    def _summary(self, session: interactive.Session) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            interactive._print_exit_summary(session)
        return out.getvalue()

    def test_empty_session(self) -> None:
        self.assertEqual(self._summary(interactive.Session([])), "No votes cast this session.\n")

    def test_glyphs_and_legend(self) -> None:
        session = interactive.Session([])
        session.session_votes = [
            {"id": "2601.00001", "title": "Fresh", "status": "new"},
            {"id": "2601.00002", "title": "Again", "status": "duplicate"},
            {"id": "2601.00003", "title": "Gone", "status": "skipped"},
        ]
        session.outdated_msg = "Note: cuhkvoting 9.9.9 is available"
        text = self._summary(session)
        self.assertIn("  2601.00001  Fresh\n", text)
        self.assertIn("  2601.00002  Again  ✗\n", text)
        self.assertIn("  2601.00003  Gone  ⊘\n", text)
        self.assertIn("✗ = duplicated an earlier vote", text)
        self.assertIn("⊘ = skipped", text)
        self.assertIn("9.9.9", text)


if __name__ == "__main__":
    unittest.main()
