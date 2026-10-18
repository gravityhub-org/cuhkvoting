"""`vote remove <index>`: the index refers to the last printed list (today/lastweek/topvoted/show/search)."""

from __future__ import annotations

import copy
import unittest
from types import SimpleNamespace
from unittest import mock

from typer.testing import CliRunner

from cuhkvoting import cli

_LAST_LIST = [
    {"id": "2604.19435", "title": "First paper"},
    {"id": "2604.22380v1", "title": "Second paper"},
]

_FIRST_PAPER = {
    "id": "2604.19435",
    "title": "First paper",
    "votes": [
        {"user": "tester", "voted_at": "2026-01-01T00:00:00Z"},
        {"user": "alice", "voted_at": "2026-01-02T00:00:00Z"},
    ],
}

_SECOND_PAPER = {
    "id": "2604.22380",
    "title": "Second paper",
    "votes": [{"user": "tester", "voted_at": "2026-01-01T00:00:00Z"}],
}


class ResolvePaperIdsTest(unittest.TestCase):
    def test_index_points_at_last_list_entry(self) -> None:
        with mock.patch("cuhkvoting.cli._load_last_list", return_value=list(_LAST_LIST)):
            self.assertEqual(
                cli._resolve_paper_ids(["2"]),
                [("2604.22380", "Second paper", 2)],
            )

    def test_out_of_range_index_errors(self) -> None:
        with mock.patch("cuhkvoting.cli._load_last_list", return_value=list(_LAST_LIST)), \
             self.assertRaises(SystemExit):
            cli._resolve_paper_ids(["3"])


class VoteRemoveByIndexTest(unittest.TestCase):
    def _invoke(self, argv: list[str], paper: dict, save_path: str):
        saved = mock.Mock()
        deleted = mock.Mock()
        with mock.patch("cuhkvoting.cli._load_last_list", return_value=list(_LAST_LIST)), \
             mock.patch("cuhkvoting.cli._resolve_repo_config", return_value=object()), \
             mock.patch("cuhkvoting.cli._get_token", return_value="tok"), \
             mock.patch("cuhkvoting.cli._resolve_user", return_value="tester"), \
             mock.patch(
                 "cuhkvoting.cli._load_vote_paper",
                 return_value=(copy.deepcopy(paper), "sha", save_path),
             ), \
             mock.patch("cuhkvoting.cli._save_vote_paper", saved), \
             mock.patch("cuhkvoting.cli._delete_vote_paper", deleted):
            result = CliRunner().invoke(cli.app, argv)
        return result.exit_code, saved, deleted

    def test_remove_by_index_keeps_other_voters(self) -> None:
        code, saved, deleted = self._invoke(
            ["vote", "remove", "1"], _FIRST_PAPER, "papers/2604.19435.json"
        )
        self.assertEqual(code, 0)
        deleted.assert_not_called()
        saved.assert_called_once()
        saved_paper = saved.call_args.args[3]
        self.assertEqual(saved_paper["id"], "2604.19435")
        self.assertEqual([v["user"] for v in saved_paper["votes"]], ["alice"])

    def test_remove_by_index_deletes_last_vote_record(self) -> None:
        code, saved, deleted = self._invoke(
            ["vote", "remove", "2"], _SECOND_PAPER, "papers/2604.22380.json"
        )
        self.assertEqual(code, 0)
        saved.assert_not_called()
        deleted.assert_called_once()
        self.assertEqual(deleted.call_args.args[3], "papers/2604.22380.json")

    def test_out_of_range_index_does_not_touch_records(self) -> None:
        code, saved, deleted = self._invoke(
            ["vote", "remove", "3"], _FIRST_PAPER, "papers/2604.19435.json"
        )
        self.assertEqual(code, 1)
        saved.assert_not_called()
        deleted.assert_not_called()


class ListingsFeedVoteIndexTest(unittest.TestCase):
    """Listings must cache what they print: that cache is what the index addresses."""

    def test_render_list_saves_printed_entries(self) -> None:
        # today / lastweek / last <#> / show <date> all render through _render_list.
        data = cli.ListData([{"id": "2604.19435", "title": "First paper"}], [])
        args = SimpleNamespace(abstract=0, highlight_keywords=0)
        with mock.patch("cuhkvoting.cli._save_last_list") as save, \
             mock.patch("cuhkvoting.cli._print_entry_list"):
            code = cli._render_list(data, None, args)
        self.assertEqual(code, 0)
        save.assert_called_once_with(data.entries)

    def test_topvoted_saves_printed_entries(self) -> None:
        rows = [
            {
                "id": "2604.19435",
                "title": "First paper",
                "abstract": "",
                "votes": 1,
                "voters": "alice",
                "expires_in_days": 5,
            }
        ]
        args = SimpleNamespace(N=10, abstract=0)
        with mock.patch("cuhkvoting.cli._topvoted_list", return_value=cli.ListData(rows, [])), \
             mock.patch("cuhkvoting.cli._save_last_list") as save:
            code = cli.cmd_topvoted(args)
        self.assertEqual(code, 0)
        save.assert_called_once_with(rows)


if __name__ == "__main__":
    unittest.main()
