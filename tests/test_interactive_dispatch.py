"""Tests for the interactive-mode token dispatcher."""

from __future__ import annotations

import unittest

from cuhkvoting import cli, interactive


class DispatchTests(unittest.TestCase):
    def test_default_is_today(self) -> None:
        loader, args = interactive._dispatch([])
        self.assertIs(loader, cli._today_list)
        self.assertEqual(args.limit, 20)
        self.assertIsNone(args.keywords)

    def test_today_with_keywords(self) -> None:
        loader, args = interactive._dispatch(["today", "merger"])
        self.assertIs(loader, cli._today_list)
        self.assertEqual(args.keywords, ["merger"])

    def test_lastweek(self) -> None:
        loader, args = interactive._dispatch(["lastweek"])
        self.assertIs(loader, cli._lastweek_list)
        self.assertEqual(args.limit, 1000)

    def test_last_n(self) -> None:
        loader, args = interactive._dispatch(["last", "3", "merger"])
        self.assertIs(loader, cli._lastdays_list)
        self.assertEqual(args.days, 3)
        self.assertEqual(args.keywords, ["merger"])

    def test_last_1_delegates_to_today(self) -> None:
        loader, _args = interactive._dispatch(["last", "1"])
        self.assertIs(loader, cli._today_list)

    def test_last_7_delegates_to_lastweek(self) -> None:
        loader, _args = interactive._dispatch(["last", "7"])
        self.assertIs(loader, cli._lastweek_list)

    def test_last_requires_a_positive_number(self) -> None:
        for tokens in (["last"], ["last", "x"], ["last", "0"]):
            with self.assertRaises(SystemExit):
                interactive._dispatch(tokens)

    def test_topvoted_has_no_cutoff(self) -> None:
        loader, args = interactive._dispatch(["topvoted"])
        self.assertIs(loader, cli._topvoted_list)
        self.assertIsNone(args.N)

    def test_search(self) -> None:
        loader, args = interactive._dispatch(["search", "kilonova", "merger"])
        self.assertIs(loader, cli._search_list)
        self.assertEqual(args.query, ["kilonova", "merger"])

    def test_show_date(self) -> None:
        loader, args = interactive._dispatch(["show", "2026-07-01"])
        self.assertIs(loader, cli._show_date_list)
        self.assertEqual(len(args.date_spans), 1)

    def test_show_requires_a_date(self) -> None:
        with self.assertRaises(SystemExit):
            interactive._dispatch(["show", "merger"])

    def test_bare_date_goes_to_show(self) -> None:
        loader, _args = interactive._dispatch(["2026-07-01"])
        self.assertIs(loader, cli._show_date_list)

    def test_bare_date_with_keywords(self) -> None:
        loader, args = interactive._dispatch(["2026-07-01", "merger"])
        self.assertIs(loader, cli._show_date_list)
        self.assertEqual(args.keywords, ["merger"])

    def test_bare_keywords_go_to_search(self) -> None:
        loader, args = interactive._dispatch(["neutron", "star"])
        self.assertIs(loader, cli._search_list)
        self.assertEqual(args.query, ["neutron", "star"])


if __name__ == "__main__":
    unittest.main()
