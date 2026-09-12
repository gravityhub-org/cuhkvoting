"""GraphQL disconnect must fall back — never surface as a fatal arXiv error."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from cuhkvoting import cli


class ListPapersFallbackTests(unittest.TestCase):
    def test_tarball_connection_error_falls_back_to_ssh(self) -> None:
        cfg = cli.RepoConfig(owner="o", repo="r", branch="main")
        papers = [{"id": "2601.00001", "title": "T", "votes": [{"user": "u"}]}]

        with mock.patch(
            "cuhkvoting.cli._list_papers_via_tarball",
            side_effect=ConnectionError("Remote end closed connection without response"),
        ) as tarball:
            with mock.patch(
                "cuhkvoting.cli._list_papers_via_graphql",
                side_effect=ConnectionError("graphql also down"),
            ) as graphql:
                with mock.patch("cuhkvoting.cli._has_github_ssh_access", return_value=True):
                    with mock.patch("cuhkvoting.cli._list_papers_via_git_clone", return_value=papers) as clone:
                        out = cli._list_papers_via_api(cfg, token="tok")

        self.assertEqual(out, papers)
        self.assertEqual(tarball.call_count, 1)
        self.assertEqual(graphql.call_count, 1)
        self.assertEqual(clone.call_count, 1)

    def test_graphql_connection_error_falls_back_to_ssh(self) -> None:
        # Legacy name kept: when tarball is skipped/unavailable, GraphQL disconnect
        # must still fall through to SSH rather than raising.
        cfg = cli.RepoConfig(owner="o", repo="r", branch="main")
        papers = [{"id": "2601.00001", "title": "T", "votes": [{"user": "u"}]}]

        with mock.patch(
            "cuhkvoting.cli._list_papers_via_tarball",
            side_effect=ConnectionError("tarball down"),
        ):
            with mock.patch(
                "cuhkvoting.cli._list_papers_via_graphql",
                side_effect=ConnectionError("Remote end closed connection without response"),
            ) as graphql:
                with mock.patch("cuhkvoting.cli._has_github_ssh_access", return_value=True):
                    with mock.patch("cuhkvoting.cli._list_papers_via_git_clone", return_value=papers) as clone:
                        out = cli._list_papers_via_api(cfg, token="tok")

        self.assertEqual(out, papers)
        self.assertEqual(graphql.call_count, 1)
        self.assertEqual(clone.call_count, 1)

    def test_topvoted_connection_error_does_not_blame_arxiv(self) -> None:
        # Even if listing still fails hard, the CLI must not say "arXiv closed".
        err = ConnectionError("Remote end closed connection without response")
        with mock.patch("cuhkvoting.cli._topvoted_list", side_effect=err):
            buf = io.StringIO()
            with redirect_stderr(buf), redirect_stdout(io.StringIO()):
                code = cli._invoke_cmd(cli.cmd_topvoted, N=10, repo=None, branch="main", abstract=None)
        self.assertEqual(code, 1)
        msg = buf.getvalue()
        self.assertNotIn("arXiv closed", msg)
        self.assertIn("Network connection failed", msg)


if __name__ == "__main__":
    unittest.main()
