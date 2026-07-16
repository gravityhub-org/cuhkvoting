"""Tests for the passive client-version notice and the root meta.json high-water-mark."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from cuhkvoting import cli


class ParseReleaseVersionTests(unittest.TestCase):
    def test_numeric_not_lexicographic(self) -> None:
        # The whole point: 0.10.0 must sort above 0.2.2 (tuple compare, not string).
        self.assertGreater(cli._parse_release_version("0.10.0"), cli._parse_release_version("0.2.2"))

    def test_clean_triple(self) -> None:
        self.assertEqual(cli._parse_release_version("0.2.2"), (0, 2, 2))

    def test_none_for_non_release(self) -> None:
        for bad in ["dev", "0.3.0rc1", "0.3.0.dev1", "0.3.0+local",
                    "1.2", "1.2.3.4", "v1.2.3", "", "1000.0.0", None, 123]:
            self.assertIsNone(cli._parse_release_version(bad), bad)


class InstallProvenanceTests(unittest.TestCase):
    def _run(self, read_text_value=None, read_text_exc=None):
        dist = mock.MagicMock()
        if read_text_exc is not None:
            dist.read_text.side_effect = read_text_exc
        else:
            dist.read_text.return_value = read_text_value
        with mock.patch("cuhkvoting.cli.importlib.metadata.distribution", return_value=dist):
            return cli._install_provenance()

    def test_editable_dir_info_is_none(self) -> None:
        payload = json.dumps({"url": "file:///x", "dir_info": {"editable": True}})
        self.assertIsNone(self._run(payload))

    def test_local_dir_install_is_none(self) -> None:
        payload = json.dumps({"url": "file:///x", "dir_info": {}})
        self.assertIsNone(self._run(payload))

    def test_vcs_install_is_vcs(self) -> None:
        payload = json.dumps({"url": "git+ssh://git@github.com/o/r.git", "vcs_info": {"vcs": "git"}})
        self.assertEqual(self._run(payload), "vcs")

    def test_url_install_is_url(self) -> None:
        payload = json.dumps({"url": "https://example.com/pkg.whl"})
        self.assertEqual(self._run(payload), "url")

    def test_no_pep610_record_is_index(self) -> None:
        self.assertEqual(self._run(read_text_value=None), "index")

    def test_read_text_error_is_none(self) -> None:
        self.assertIsNone(self._run(read_text_exc=Exception("boom")))

    def test_malformed_json_is_none(self) -> None:
        self.assertIsNone(self._run("{not json"))

    def test_distribution_missing_is_none(self) -> None:
        with mock.patch("cuhkvoting.cli.importlib.metadata.distribution",
                        side_effect=cli.importlib.metadata.PackageNotFoundError):
            self.assertIsNone(cli._install_provenance())


class ParseMetaTextTests(unittest.TestCase):
    def test_absent_is_empty_dict(self) -> None:
        self.assertEqual(cli._parse_meta_text(None), {})

    def test_empty_object(self) -> None:
        self.assertEqual(cli._parse_meta_text("{}"), {})

    def test_valid_dict(self) -> None:
        self.assertEqual(cli._parse_meta_text('{"a": 1}'), {"a": 1})

    def test_invalid_json_is_none(self) -> None:
        self.assertIsNone(cli._parse_meta_text("not json"))

    def test_non_dict_is_none(self) -> None:
        self.assertIsNone(cli._parse_meta_text("[1, 2]"))


class WarnIfClientOutdatedTests(unittest.TestCase):
    def _warn(self, meta_doc, pkg="0.2.2"):
        with mock.patch("cuhkvoting.cli._PKG_VERSION", pkg):
            with mock.patch("cuhkvoting.cli.typer.style", side_effect=lambda text, **kw: text):
                with mock.patch("cuhkvoting.cli.typer.echo") as echo:
                    cli._warn_if_client_outdated(meta_doc)
        return echo

    def test_warns_when_behind(self) -> None:
        echo = self._warn({"client": {"latest_version": "0.9.0"}})
        echo.assert_called_once()
        msg = echo.call_args[0][0]
        self.assertIn("0.9.0", msg)
        self.assertIn("0.2.2", msg)
        self.assertIn(cli.UPGRADE_COMMAND, msg)

    def test_silent_when_equal(self) -> None:
        self._warn({"client": {"latest_version": "0.2.2"}}).assert_not_called()

    def test_silent_when_ahead(self) -> None:
        self._warn({"client": {"latest_version": "0.1.0"}}).assert_not_called()

    def test_silent_when_absent(self) -> None:
        self._warn({}).assert_not_called()

    def test_silent_when_meta_none(self) -> None:
        self._warn(None).assert_not_called()

    def test_silent_when_client_not_dict(self) -> None:
        self._warn({"client": "oops"}).assert_not_called()

    def test_silent_when_local_dev(self) -> None:
        self._warn({"client": {"latest_version": "9.9.9"}}, pkg="dev").assert_not_called()

    def test_silent_when_recorded_garbage(self) -> None:
        self._warn({"client": {"latest_version": "9.9.9rc1"}}).assert_not_called()

    def test_ansi_injection_recorded_version_not_echoed(self) -> None:
        # A recorded version carrying a terminal escape must fail the strict parse
        # and therefore never reach a TTY.
        self._warn({"client": {"latest_version": "0.9.0\x1b[2J"}}).assert_not_called()


class BumpMetaClientVersionTests(unittest.TestCase):
    def _bump(self, meta_doc, user="octocat", pkg="0.2.2", provenance="vcs"):
        with mock.patch("cuhkvoting.cli._PKG_VERSION", pkg):
            with mock.patch("cuhkvoting.cli._install_provenance", return_value=provenance):
                return cli._bump_meta_client_version(meta_doc, user)

    def test_preserves_unknown_keys(self) -> None:
        doc = {"schema": 1, "announcements": {"x": 1},
               "client": {"latest_version": "0.1.0", "custom": "keep"}}
        out = self._bump(doc)
        self.assertEqual(out["announcements"], {"x": 1})
        self.assertEqual(out["client"]["custom"], "keep")
        self.assertEqual(out["client"]["latest_version"], "0.2.2")
        self.assertEqual(out["client"]["source"], "vcs")
        self.assertEqual(out["client"]["updated_by"], "octocat")
        self.assertTrue(out["client"]["updated_at"].endswith("Z"))
        # The original is not mutated in place.
        self.assertEqual(doc["client"]["latest_version"], "0.1.0")

    def test_none_when_equal(self) -> None:
        self.assertIsNone(self._bump({"client": {"latest_version": "0.2.2"}}))

    def test_none_when_recorded_newer(self) -> None:
        self.assertIsNone(self._bump({"client": {"latest_version": "0.5.0"}}))

    def test_none_when_local_dev(self) -> None:
        self.assertIsNone(self._bump({"client": {"latest_version": "0.1.0"}}, pkg="dev"))

    def test_none_when_no_provenance(self) -> None:
        self.assertIsNone(self._bump({"client": {"latest_version": "0.1.0"}}, provenance=None))

    def test_none_when_meta_unusable(self) -> None:
        self.assertIsNone(self._bump(None))

    def test_none_when_future_schema(self) -> None:
        self.assertIsNone(self._bump({"schema": 2, "client": {"latest_version": "0.1.0"}}))

    def test_seeds_when_absent(self) -> None:
        out = self._bump({})
        self.assertEqual(out["schema"], cli.META_SCHEMA_VERSION)
        self.assertEqual(out["client"]["latest_version"], "0.2.2")
        self.assertEqual(out["client"]["source"], "vcs")

    def test_allows_minor_skip(self) -> None:
        # Guards against anyone "helpfully" adding a delta clamp that would strand
        # the file at an old version when everyone has jumped ahead.
        out = self._bump({"client": {"latest_version": "0.2.2"}}, pkg="0.5.0")
        self.assertIsNotNone(out)
        self.assertEqual(out["client"]["latest_version"], "0.5.0")


class BatchVoteApiMetaTests(unittest.TestCase):
    def _cfg(self):
        return cli.RepoConfig(owner="o", repo="r", branch="main")

    def _run(self, repo_node, papers, *, user="octocat", pkg="0.2.2", provenance="vcs"):
        gql = {"data": {"repository": repo_node}}
        resp = mock.MagicMock()
        resp.read.return_value = json.dumps(gql).encode("utf-8")
        cm = mock.MagicMock()
        cm.__enter__.return_value = resp
        captured: dict = {}

        def fake_commit(base_url, headers, token, branch, tree_entries, commit_msg):
            captured["tree_entries"] = tree_entries
            captured["commit_msg"] = commit_msg

        with mock.patch("cuhkvoting.cli.urllib.request.urlopen", return_value=cm) as up, \
                mock.patch("cuhkvoting.cli._http_json_request", return_value={"sha": "blob"}) as hjr, \
                mock.patch("cuhkvoting.cli._git_batch_commit", side_effect=fake_commit) as gbc, \
                mock.patch("cuhkvoting.cli._install_provenance", return_value=provenance), \
                mock.patch("cuhkvoting.cli._PKG_VERSION", pkg):
            voted = cli._batch_vote_papers_api(self._cfg(), "tok", user, papers)
        return voted, captured, up, hjr, gbc

    def _paper_blob(self, votes):
        return {"text": json.dumps({"id": "2601.1", "title": "T", "url": "u", "votes": votes})}

    def _older_meta(self):
        return {"text": json.dumps({"schema": 1, "client": {"latest_version": "0.1.0"}})}

    def test_meta_alias_in_query(self) -> None:
        repo_node = {"p0": self._paper_blob([]), "dn": None, "meta": self._older_meta()}
        _, _, up, _, _ = self._run(repo_node, [{"paper_id": "2601.1", "title": "T", "url": "u"}])
        req = up.call_args[0][0]
        query = json.loads(req.data.decode("utf-8"))["query"]
        self.assertIn('meta: object(expression: "main:meta.json")', query)

    def test_no_write_when_no_updates(self) -> None:
        # Already voted, no dn change: even with a bump-worthy meta, nothing commits.
        repo_node = {"p0": self._paper_blob([cli._make_vote_entry("octocat")]),
                     "dn": None, "meta": self._older_meta()}
        voted, captured, _, hjr, gbc = self._run(
            repo_node, [{"paper_id": "2601.1", "title": "T", "url": "u"}]
        )
        self.assertEqual(voted, ["2601.1"])
        gbc.assert_not_called()
        hjr.assert_not_called()
        self.assertEqual(captured, {})

    def test_meta_rides_existing_commit(self) -> None:
        repo_node = {"p0": self._paper_blob([]), "dn": None, "meta": self._older_meta()}
        _, captured, _, _, gbc = self._run(
            repo_node, [{"paper_id": "2601.1", "title": "T", "url": "u"}]
        )
        gbc.assert_called_once()
        paths = [e["path"] for e in captured["tree_entries"]]
        self.assertIn("meta.json", paths)
        self.assertEqual(sum(p.startswith("papers/") for p in paths), 1)

    def test_meta_excluded_from_commit_message(self) -> None:
        repo_node = {"p0": self._paper_blob([]), "dn": None, "meta": self._older_meta()}
        _, captured, _, _, _ = self._run(
            repo_node, [{"paper_id": "2601.1", "title": "T", "url": "u"}]
        )
        msg = captured["commit_msg"]
        self.assertIn("(1 papers)", msg)
        self.assertNotIn("__meta__", msg)
        self.assertNotIn("meta.json", msg)

    def test_missing_meta_node_editable_client_does_not_write(self) -> None:
        # No meta key = absent file; an editable install (no provenance) must not seed it.
        repo_node = {"p0": self._paper_blob([]), "dn": None}
        voted, captured, _, _, gbc = self._run(
            repo_node, [{"paper_id": "2601.1", "title": "T", "url": "u"}], provenance=None
        )
        self.assertEqual(voted, ["2601.1"])
        gbc.assert_called_once()
        self.assertNotIn("meta.json", [e["path"] for e in captured["tree_entries"]])

    def test_absent_meta_is_seeded_by_publishable_client(self) -> None:
        # No meta key = absent file; a vcs install bootstraps it as part of the vote commit.
        repo_node = {"p0": self._paper_blob([]), "dn": None}
        voted, captured, _, _, gbc = self._run(
            repo_node, [{"paper_id": "2601.1", "title": "T", "url": "u"}], provenance="vcs"
        )
        self.assertEqual(voted, ["2601.1"])
        gbc.assert_called_once()
        self.assertIn("meta.json", [e["path"] for e in captured["tree_entries"]])

    def test_unparseable_meta_is_not_clobbered(self) -> None:
        repo_node = {"p0": self._paper_blob([]), "dn": None, "meta": {"text": "{{{"}}
        voted, captured, _, _, gbc = self._run(
            repo_node, [{"paper_id": "2601.1", "title": "T", "url": "u"}]
        )
        self.assertEqual(voted, ["2601.1"])
        gbc.assert_called_once()
        self.assertNotIn("meta.json", [e["path"] for e in captured["tree_entries"]])


class AdminSetVersionTests(unittest.TestCase):
    def _args(self, **kw):
        base = dict(version=None, clear=False, repo=None, branch="main", dry_run=False)
        base.update(kw)
        return SimpleNamespace(**base)

    def _run(self, args, loaded):
        with mock.patch.object(cli, "_get_token", return_value="t"), \
                mock.patch.object(cli, "_resolve_user", return_value="paul"), \
                mock.patch.object(cli, "_load_json_via_api", return_value=(loaded, "sha1")), \
                mock.patch.object(cli, "_save_meta") as save:
            code = cli.cmd_admin_set_version(args)
        return code, save

    def test_rejects_invalid_version(self) -> None:
        with self.assertRaises(SystemExit):
            self._run(self._args(version="0.3"), {})

    def test_requires_version_without_clear(self) -> None:
        with self.assertRaises(SystemExit):
            self._run(self._args(), {})

    def test_set_preserves_unknown_keys_and_passes_sha(self) -> None:
        loaded = {"announcements": {"x": 1}, "client": {"custom": "keep", "latest_version": "0.1.0"}}
        _, save = self._run(self._args(version="0.4.0"), loaded)
        cfg, token, user, body, sha, message = save.call_args[0]
        self.assertEqual(sha, "sha1")
        self.assertEqual(body["announcements"], {"x": 1})
        self.assertEqual(body["client"]["custom"], "keep")
        self.assertEqual(body["client"]["latest_version"], "0.4.0")
        self.assertEqual(body["client"]["source"], "admin")

    def test_clear_removes_client_keeps_others(self) -> None:
        loaded = {"announcements": {"x": 1}, "client": {"latest_version": "0.4.0"}}
        _, save = self._run(self._args(clear=True), loaded)
        body = save.call_args[0][3]
        self.assertNotIn("client", body)
        self.assertEqual(body["announcements"], {"x": 1})

    def test_clear_on_absent_client_does_not_write(self) -> None:
        _, save = self._run(self._args(clear=True), {})
        save.assert_not_called()

    def test_dry_run_does_not_write(self) -> None:
        _, save = self._run(self._args(version="0.4.0", dry_run=True), {})
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
