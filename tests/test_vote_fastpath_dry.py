"""Dry-run timing: old double-clone SSH vs new single-clone SSH vs API (no push)."""

from __future__ import annotations

import contextlib
import io
import os
import time
import unittest
from types import SimpleNamespace

from cuhkvoting import cli


class VoteFastpathDryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = cli.RepoConfig(
            owner="gravityhub-org",
            repo="cuhkvoting-records",
            branch=os.getenv("CUHKVOTING_BRANCH", "main"),
        )
        cls.paper = {
            "paper_id": "9999.99999",
            "title": "Dry-run timing probe (nonexistent)",
            "url": "https://arxiv.org/abs/9999.99999",
        }
        cls.has_ssh = cli._has_github_ssh_access()
        cls.token = cli._get_token()
        cls.user = os.getenv("CUHKVOTING_USER") or os.getenv("GITHUB_USER") or "dry-run-user"

    def test_ssh_single_checkout_beats_legacy_double_clone(self) -> None:
        if not self.has_ssh:
            self.skipTest("no GitHub SSH auth")

        # Legacy cost: dedicated JC clone + vote clone (both discarded).
        t0 = time.perf_counter()
        with cli._repo_checkout(self.cfg) as clone_dir:
            _ = cli._selected_ids_from_checkout(clone_dir)
        legacy_selected_s = time.perf_counter() - t0

        t0 = time.perf_counter()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = cli._batch_vote_papers_ssh(
                self.cfg, self.user, [self.paper], dry_run=True,
            )
        new_ssh_s = time.perf_counter() - t0
        legacy_total_s = legacy_selected_s + new_ssh_s

        self.assertIsInstance(result, cli.VoteResult)
        # New path is one checkout; legacy measured as JC clone + same vote clone.
        self.assertLess(new_ssh_s, legacy_total_s)
        # Expect roughly ~2x when network dominates (allow slack for noise).
        self.assertLess(new_ssh_s, legacy_total_s * 0.75)
        print(
            f"\nSSH dry: legacy≈{legacy_total_s:.2f}s "
            f"(jc={legacy_selected_s:.2f}+vote={new_ssh_s:.2f}) "
            f"new={new_ssh_s:.2f}s"
        )

    def test_api_dry_run_when_token(self) -> None:
        if not self.token:
            self.skipTest("no GitHub token")
        t0 = time.perf_counter()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = cli._batch_vote_papers_api(
                self.cfg, self.token, self.user, [self.paper], dry_run=True,
            )
        api_s = time.perf_counter() - t0
        self.assertIsInstance(result, cli.VoteResult)
        self.assertTrue(cli._prefer_api_vote(self.token))
        print(f"\nAPI dry: {api_s:.2f}s voted={result.voted} new={result.new}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
