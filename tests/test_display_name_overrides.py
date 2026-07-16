"""Tests for local [vote.display_names] overrides and the bundled records+names fetch."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from cuhkvoting import cli


def _write_config(text: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
        f.write(text)
        return f.name


class DisplayNameOverrideConfigTests(unittest.TestCase):
    def test_parses_display_names_subtable(self) -> None:
        path = _write_config(
            '[vote]\n'
            'display_name = "Me"\n'
            '[vote.display_names]\n'
            'octocat = "Mona"\n'
            'hubber = "Hub"\n'
        )
        try:
            with mock.patch("cuhkvoting.cli.CONFIG_PATH", Path(path)):
                cfg = cli._load_config()
        finally:
            os.unlink(path)
        self.assertEqual(cfg.display_name_overrides, {"octocat": "Mona", "hubber": "Hub"})

    def test_missing_subtable_defaults_to_empty(self) -> None:
        path = _write_config('[vote]\ndisplay_name = "Me"\n')
        try:
            with mock.patch("cuhkvoting.cli.CONFIG_PATH", Path(path)):
                cfg = cli._load_config()
        finally:
            os.unlink(path)
        self.assertEqual(cfg.display_name_overrides, {})

    def test_malformed_subtable_defaults_to_empty(self) -> None:
        # display_names as a scalar (not a table) must not crash; guard returns {}.
        path = _write_config('[vote]\ndisplay_names = "oops"\n')
        try:
            with mock.patch("cuhkvoting.cli.CONFIG_PATH", Path(path)):
                cfg = cli._load_config()
        finally:
            os.unlink(path)
        self.assertEqual(cfg.display_name_overrides, {})


class ResolveDisplayNamePrecedenceTests(unittest.TestCase):
    # Call sites build a merged {**shared, **overrides} table (overrides last ⇒ they win).
    def test_override_wins_over_shared(self) -> None:
        merged = {**{"alice": "Alice Shared"}, **{"alice": "Alice Override"}}
        self.assertEqual(cli._resolve_display_name("alice", merged), "Alice Override")

    def test_shared_used_when_no_override(self) -> None:
        merged = {**{"dave": "Dave Shared"}, **{}}
        self.assertEqual(cli._resolve_display_name("dave", merged), "Dave Shared")

    def test_username_when_neither(self) -> None:
        merged = {**{"dave": "Dave Shared"}, **{"alice": "Alice Override"}}
        self.assertEqual(cli._resolve_display_name("carol", merged), "carol")


class BundledFetchTests(unittest.TestCase):
    def _cfg(self):
        return cli.RepoConfig(owner="o", repo="r", branch="main")

    def test_token_path_parses_records_sha_and_table(self) -> None:
        records_payload = {"records": [{"arxiv_id": "2601.1", "selected_by": "octocat"}]}
        gql = {
            "data": {
                "repository": {
                    "rec": {"text": json.dumps(records_payload), "oid": "abc123"},
                    "dn": {"text": json.dumps({"octocat": "Mona"})},
                }
            }
        }
        resp = mock.MagicMock()
        resp.read.return_value = json.dumps(gql).encode("utf-8")
        cm = mock.MagicMock()
        cm.__enter__.return_value = resp
        with mock.patch("cuhkvoting.cli.urllib.request.urlopen", return_value=cm):
            records, sha, table = cli._load_jc_records_and_display_names(self._cfg(), token="x")
        self.assertEqual(records, records_payload["records"])
        self.assertEqual(sha, "abc123")  # GraphQL Blob oid doubles as the Contents-API sha
        self.assertEqual(table, {"octocat": "Mona"})

    def test_token_path_missing_files(self) -> None:
        gql = {"data": {"repository": {"rec": None, "dn": None}}}
        resp = mock.MagicMock()
        resp.read.return_value = json.dumps(gql).encode("utf-8")
        cm = mock.MagicMock()
        cm.__enter__.return_value = resp
        with mock.patch("cuhkvoting.cli.urllib.request.urlopen", return_value=cm):
            records, sha, table = cli._load_jc_records_and_display_names(self._cfg(), token="x")
        self.assertEqual(records, [])
        self.assertIsNone(sha)
        self.assertEqual(table, {})

    def test_falls_back_to_two_reads_on_graphql_error(self) -> None:
        with mock.patch("cuhkvoting.cli.urllib.request.urlopen", side_effect=urllib.error.URLError("boom")):
            with mock.patch("cuhkvoting.cli._load_jc_records", return_value=([{"x": 1}], "sha9")) as lj:
                with mock.patch("cuhkvoting.cli._fetch_display_names", return_value={"u": "U"}) as fd:
                    with mock.patch("cuhkvoting.cli._warn_if_client_outdated") as warn:
                        records, sha, table = cli._load_jc_records_and_display_names(self._cfg(), token="x")
        self.assertEqual((records, sha, table), ([{"x": 1}], "sha9", {"u": "U"}))
        lj.assert_called_once()
        fd.assert_called_once()
        # The degraded two-read path must not attempt a (non-free) meta check.
        warn.assert_not_called()

    def test_meta_warn_rides_bundled_read_without_changing_signature(self) -> None:
        records_payload = {"records": [{"arxiv_id": "2601.1"}]}
        gql = {
            "data": {
                "repository": {
                    "rec": {"text": json.dumps(records_payload), "oid": "abc123"},
                    "dn": {"text": json.dumps({})},
                    "meta": {"text": json.dumps({"schema": 1, "client": {"latest_version": "9.9.9"}})},
                }
            }
        }
        resp = mock.MagicMock()
        resp.read.return_value = json.dumps(gql).encode("utf-8")
        cm = mock.MagicMock()
        cm.__enter__.return_value = resp
        with mock.patch("cuhkvoting.cli.urllib.request.urlopen", return_value=cm):
            with mock.patch("cuhkvoting.cli._warn_if_client_outdated") as warn:
                result = cli._load_jc_records_and_display_names(self._cfg(), token="x")
        # Signature is unchanged: still a 3-tuple, and the meta node drove the warn.
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0], records_payload["records"])
        warn.assert_called_once()
        self.assertEqual(warn.call_args[0][0], {"schema": 1, "client": {"latest_version": "9.9.9"}})


if __name__ == "__main__":
    unittest.main()
