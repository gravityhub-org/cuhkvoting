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
        self.assertEqual(result.skipped_selected, [])
        self.assertEqual(dup_file.read_text(), original)  # duplicate untouched
        new_paper = json.loads((papers_dir / "2601.00002.json").read_text())
        self.assertEqual(new_paper["votes"][0]["user"], "octocat")

    def test_skips_jc_selected_in_same_checkout(self) -> None:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        papers_dir = Path(tmp) / "papers"
        papers_dir.mkdir()
        (papers_dir / "journal_club_records.json").write_text(json.dumps({
            "records": [{"arxiv_id": "2601.00001", "week": "2026-W01"}],
        }))
        err = io.StringIO()
        with mock.patch("cuhkvoting.cli._with_repo_checkout", return_value=tmp), \
                mock.patch("cuhkvoting.cli._run_git", return_value=""), \
                mock.patch("cuhkvoting.cli._ensure_commit_identity"), \
                mock.patch("cuhkvoting.cli.shutil.rmtree"), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            result = cli._batch_vote_papers_ssh(
                SimpleNamespace(branch="main"), "octocat",
                [{"paper_id": "2601.00001", "title": "Sel", "url": "u"},
                 {"paper_id": "2601.00002", "title": "New", "url": "u"}],
            )
        self.assertEqual(result.skipped_selected, ["2601.00001"])
        self.assertEqual(result.voted, ["2601.00002"])
        self.assertEqual(result.new, ["2601.00002"])
        self.assertFalse((papers_dir / "2601.00001.json").exists())
        self.assertIn("already selected", err.getvalue())

    def test_empty_input(self) -> None:
        result = cli._batch_vote_papers_ssh(SimpleNamespace(branch="main"), "octocat", [])
        self.assertEqual((result.voted, result.new, result.outdated_msg), ([], [], None))

    def test_prefer_api_when_token(self) -> None:
        self.assertTrue(cli._prefer_api_vote("tok"))
        self.assertFalse(cli._prefer_api_vote(None))
        self.assertFalse(cli._prefer_api_vote(""))


class BatchUnvoteSshTests(unittest.TestCase):
    def _checkout(self, papers: dict[str, dict]) -> tuple[str, Path]:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        papers_dir = Path(tmp) / "papers"
        papers_dir.mkdir()
        for pid, doc in papers.items():
            (papers_dir / f"{pid}.json").write_text(json.dumps(doc))
        return tmp, papers_dir

    def _votes(self, *users: str) -> list[dict]:
        return [cli._make_vote_entry(user) for user in users]

    def test_removal_keeps_other_votes_and_deletes_last_vote(self) -> None:
        tmp, papers_dir = self._checkout({
            "2601.00001": {
                "id": "2601.00001", "title": "Shared", "abstract": "", "url": "u",
                "votes": self._votes("octocat", "alice"),
            },
            "2601.00002": {
                "id": "2601.00002", "title": "Mine", "abstract": "", "url": "u",
                "votes": self._votes("octocat"),
            },
            "2601.00003": {
                "id": "2601.00003", "title": "Not mine", "abstract": "", "url": "u",
                "votes": self._votes("alice"),
            },
        })
        git_calls: list[list[str]] = []

        with mock.patch("cuhkvoting.cli._with_repo_checkout", return_value=tmp), \
                mock.patch("cuhkvoting.cli._run_git",
                           side_effect=lambda args, cwd=None: git_calls.append(args) or ""), \
                mock.patch("cuhkvoting.cli._ensure_commit_identity"), \
                mock.patch("cuhkvoting.cli.shutil.rmtree"), \
                contextlib.redirect_stdout(io.StringIO()):
            result = cli._batch_vote_papers_ssh(
                SimpleNamespace(branch="main"), "octocat", [],
                removals=["2601.00001", "2601.00002", "2601.00003"],
            )

        self.assertEqual(result.removed, ["2601.00001", "2601.00002"])
        self.assertEqual(result.missing, ["2601.00003"])
        kept = json.loads((papers_dir / "2601.00001.json").read_text())
        self.assertEqual([vote["user"] for vote in kept["votes"]], ["alice"])
        self.assertFalse((papers_dir / "2601.00002.json").exists())
        self.assertEqual([call for call in git_calls if call[:1] == ["add"]],
                         [["add", "-A", "papers/"]])
        commit = next(call for call in git_calls if call[:1] == ["commit"])
        self.assertIn("vote-remove: octocat", commit[-1])

    def test_add_and_removal_share_one_commit(self) -> None:
        tmp, papers_dir = self._checkout({
            "2601.00002": {
                "id": "2601.00002", "title": "Old", "abstract": "", "url": "u",
                "votes": self._votes("octocat", "alice"),
            },
        })
        git_calls: list[list[str]] = []

        with mock.patch("cuhkvoting.cli._with_repo_checkout", return_value=tmp), \
                mock.patch("cuhkvoting.cli._run_git",
                           side_effect=lambda args, cwd=None: git_calls.append(args) or ""), \
                mock.patch("cuhkvoting.cli._ensure_commit_identity"), \
                mock.patch("cuhkvoting.cli.shutil.rmtree"), \
                contextlib.redirect_stdout(io.StringIO()):
            result = cli._batch_vote_papers_ssh(
                SimpleNamespace(branch="main"), "octocat",
                [{"paper_id": "2601.00004", "title": "New", "url": "u"}],
                removals=["2601.00002"],
            )

        self.assertEqual(result.new, ["2601.00004"])
        self.assertEqual(result.removed, ["2601.00002"])
        self.assertTrue((papers_dir / "2601.00004.json").exists())
        commits = [call for call in git_calls if call[:1] == ["commit"]]
        self.assertEqual(len(commits), 1)
        self.assertIn("vote: octocat", commits[0][-1])
        self.assertIn("vote-remove: octocat", commits[0][-1])


class BatchUnvoteApiTests(unittest.TestCase):
    def _run(self, repo_node: dict, papers: list[dict], removals: list[str]):
        gql = {"data": {"repository": repo_node}}
        resp = mock.MagicMock()
        resp.read.return_value = json.dumps(gql).encode("utf-8")
        cm = mock.MagicMock()
        cm.__enter__.return_value = resp
        captured: dict = {}

        def fake_commit(base_url, headers, token, branch, tree_entries, commit_msg):
            captured["tree_entries"] = tree_entries
            captured["commit_msg"] = commit_msg

        with mock.patch("cuhkvoting.cli.urllib.request.urlopen", return_value=cm), \
                mock.patch("cuhkvoting.cli._http_json_request", return_value={"sha": "blob"}), \
                mock.patch("cuhkvoting.cli._git_batch_commit", side_effect=fake_commit), \
                contextlib.redirect_stdout(io.StringIO()):
            result = cli._batch_vote_papers_api(
                cli.RepoConfig(owner="o", repo="r", branch="main"), "tok", "octocat",
                papers, removals=removals,
            )
        return result, captured

    @staticmethod
    def _blob(votes: list[dict]) -> dict:
        return {"text": json.dumps({
            "id": "2601.00001", "title": "T", "abstract": "", "url": "u", "votes": votes,
        })}

    def test_removal_updates_or_deletes_and_reports_missing(self) -> None:
        repo_node = {
            "r0": self._blob([cli._make_vote_entry("octocat"), cli._make_vote_entry("alice")]),
            "r1": self._blob([cli._make_vote_entry("octocat")]),
            "r2": self._blob([cli._make_vote_entry("alice")]),
        }
        result, captured = self._run(
            repo_node, [], ["2601.00001", "2601.00002", "2601.00003"],
        )
        self.assertEqual(result.removed, ["2601.00001", "2601.00002"])
        self.assertEqual(result.missing, ["2601.00003"])
        entries = {entry["path"]: entry for entry in captured["tree_entries"]}
        self.assertIn("papers/2601.00001.json", entries)
        self.assertNotIn("papers/2601.00003.json", entries)
        deleted = entries["papers/2601.00002.json"]
        self.assertIsNone(deleted["sha"])
        self.assertIn("vote-remove: octocat", captured["commit_msg"])

    def test_removals_alone_do_not_touch_meta_or_display_names(self) -> None:
        repo_node = {
            "r0": self._blob([cli._make_vote_entry("octocat"), cli._make_vote_entry("alice")]),
            "dn": None,
            "meta": {"text": json.dumps({"client": {"latest_version": "0.1.0"}})},
        }
        result, captured = self._run(repo_node, [], ["2601.00001"])
        self.assertEqual(result.removed, ["2601.00001"])
        paths = [entry["path"] for entry in captured["tree_entries"]]
        self.assertEqual(paths, ["papers/2601.00001.json"])

    def test_add_and_removal_share_one_commit(self) -> None:
        repo_node = {
            "p0": None,
            "r0": self._blob([cli._make_vote_entry("octocat"), cli._make_vote_entry("alice")]),
            "dn": None,
        }
        result, captured = self._run(
            repo_node,
            [{"paper_id": "2601.00004", "title": "New", "url": "u"}],
            ["2601.00001"],
        )
        self.assertEqual(result.new, ["2601.00004"])
        self.assertEqual(result.removed, ["2601.00001"])
        paths = [entry["path"] for entry in captured["tree_entries"]]
        self.assertIn("papers/2601.00004.json", paths)
        self.assertIn("papers/2601.00001.json", paths)
        self.assertIn("vote: octocat", captured["commit_msg"])
        self.assertIn("vote-remove: octocat", captured["commit_msg"])


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
        self.assertEqual(len(session.unvoting), 0)
        self.assertTrue(any("Voting needs auth" in w for w in session.warnings))

    def test_removal_only_batch_updates_state(self) -> None:
        session = self._session()
        session.voting = []
        session.voted = {"2601.00001"}
        session.unvoting = ["2601.00001"]
        batch_result = cli.VoteResult([], [], None, [], ["2601.00001"], [])
        with mock.patch("cuhkvoting.cli._get_token", return_value="tok"), \
                mock.patch("cuhkvoting.cli._resolve_repo_config", return_value=object()), \
                mock.patch("cuhkvoting.cli._resolve_user", return_value="octocat"), \
                mock.patch("cuhkvoting.cli._has_github_ssh_access", return_value=False), \
                mock.patch("cuhkvoting.cli._batch_vote_papers_api",
                           return_value=batch_result) as batch:
            ok = interactive._cast_votes(session, SimpleNamespace(display_name=""))

        self.assertTrue(ok)
        self.assertEqual(batch.call_args.args[3], [])
        self.assertEqual(batch.call_args.kwargs["removals"], ["2601.00001"])
        self.assertEqual(session.voted, set())
        self.assertEqual(session.unvoting, [])
        self.assertEqual(
            [(row["id"], row["status"]) for row in session.session_votes],
            [("2601.00001", "removed")],
        )
        self.assertIn("removed", session.feedback)

    def test_mixed_add_and_removal_updates_state(self) -> None:
        session = self._session()
        session.voting = ["2601.00002"]
        session.voted = {"2601.00001"}
        session.unvoting = ["2601.00001"]
        batch_result = cli.VoteResult(
            ["2601.00002"], ["2601.00002"], None, [], ["2601.00001"], [],
        )
        with mock.patch("cuhkvoting.cli._get_token", return_value="tok"), \
                mock.patch("cuhkvoting.cli._resolve_repo_config", return_value=object()), \
                mock.patch("cuhkvoting.cli._selected_arxiv_ids", return_value=set()), \
                mock.patch("cuhkvoting.cli._resolve_user", return_value="octocat"), \
                mock.patch("cuhkvoting.cli._warn_if_display_name_changed"), \
                mock.patch("cuhkvoting.cli._has_github_ssh_access", return_value=False), \
                mock.patch("cuhkvoting.cli._batch_vote_papers_api",
                           return_value=batch_result) as batch:
            ok = interactive._cast_votes(session, SimpleNamespace(display_name=""))

        self.assertTrue(ok)
        self.assertEqual([m["paper_id"] for m in batch.call_args.args[3]], ["2601.00002"])
        self.assertEqual(batch.call_args.kwargs["removals"], ["2601.00001"])
        self.assertEqual(session.voted, {"2601.00002"})
        statuses = {row["id"]: row["status"] for row in session.session_votes}
        self.assertEqual(statuses, {"2601.00002": "new", "2601.00001": "removed"})

    def test_missing_removal_warns_and_unstages(self) -> None:
        session = self._session()
        session.voting = []
        session.unvoting = ["2601.00001"]
        batch_result = cli.VoteResult([], [], None, [], [], ["2601.00001"])
        with mock.patch("cuhkvoting.cli._get_token", return_value="tok"), \
                mock.patch("cuhkvoting.cli._resolve_repo_config", return_value=object()), \
                mock.patch("cuhkvoting.cli._resolve_user", return_value="octocat"), \
                mock.patch("cuhkvoting.cli._has_github_ssh_access", return_value=False), \
                mock.patch("cuhkvoting.cli._batch_vote_papers_api", return_value=batch_result):
            ok = interactive._cast_votes(session, SimpleNamespace(display_name=""))

        self.assertTrue(ok)
        self.assertEqual(session.unvoting, [])
        self.assertTrue(any("No active vote" in warning for warning in session.warnings))
        self.assertIn("skipped", session.feedback)


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
            {"id": "2601.00004", "title": "Removed", "status": "removed"},
        ]
        session.outdated_msg = "Note: cuhkvoting 9.9.9 is available"
        text = self._summary(session)
        self.assertIn("  2601.00001  Fresh\n", text)
        self.assertIn("  2601.00002  Again  ✗\n", text)
        self.assertIn("  2601.00003  Gone  ⊘\n", text)
        self.assertIn("  2601.00004  Removed  −\n", text)
        self.assertIn("✗ = duplicated an earlier vote", text)
        self.assertIn("⊘ = skipped", text)
        self.assertIn("− = vote removed", text)
        self.assertIn("9.9.9", text)


if __name__ == "__main__":
    unittest.main()
