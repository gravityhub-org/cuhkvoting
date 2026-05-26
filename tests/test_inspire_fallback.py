"""Tests for INSPIRE-first behavior with arXiv fallback."""

from __future__ import annotations

import json
import datetime as dt
import unittest
from unittest import mock

from cuhkvoting import cli


class InspireFallbackTests(unittest.TestCase):
    def test_fetch_today_uses_inspire_when_available(self) -> None:
        fake = [{"id": "1234.5678", "title": "T", "abstract": "", "url": "u", "authors": []}]
        with mock.patch("cuhkvoting.cli._inspire_query_retry", return_value=fake) as inspire:
            with mock.patch("cuhkvoting.cli._arxiv_query") as arxiv:
                out = cli._fetch_today_entries(["gr-qc"])
        self.assertEqual(out, fake)
        self.assertEqual(inspire.call_count, 1)
        self.assertEqual(arxiv.call_count, 0)

    def test_fetch_today_falls_back_to_arxiv_when_inspire_down(self) -> None:
        fake_arxiv = [{"id": "9999.0001", "title": "A", "abstract": "x", "url": "u", "authors": []}]
        with mock.patch("cuhkvoting.cli._inspire_query_retry", side_effect=SystemExit("INSPIRE down")):
            with mock.patch("cuhkvoting.cli._arxiv_query", return_value=fake_arxiv) as arxiv:
                out = cli._fetch_today_entries(["gr-qc"])
        self.assertEqual(out, fake_arxiv)
        self.assertEqual(arxiv.call_count, 1)

    def test_fetch_entries_builds_expected_inspire_query(self) -> None:
        start = dt.date(2026, 5, 1)
        end = dt.date(2026, 5, 2)
        with mock.patch("cuhkvoting.cli._inspire_query_retry", return_value=[]) as inspire:
            cli._fetch_entries(["gr-qc", "quant-ph"], start, end, limit=10)
        (q, limit), _kwargs = inspire.call_args
        self.assertEqual(limit, 10)
        self.assertIn("arxiv_eprints.categories:gr-qc", q)
        self.assertIn("arxiv_eprints.categories:quant-ph", q)
        self.assertIn("earliest_date:[2026-05-01 to 2026-05-02]", q)

    def test_validate_prefers_inspire_record(self) -> None:
        inspire_entry = {"id": "2601.09678", "title": "X", "abstract": "", "url": "u", "authors": []}
        with mock.patch("cuhkvoting.cli._inspire_get_by_arxiv_id", return_value=inspire_entry) as get:
            with mock.patch("cuhkvoting.cli._arxiv_query") as arxiv:
                out = cli._validate_arxiv_entry("2601.09678v2")
        self.assertEqual(out, inspire_entry)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(arxiv.call_count, 0)

    def test_validate_falls_back_to_arxiv_when_inspire_missing(self) -> None:
        arxiv_entry = {"id": "2601.09678", "title": "Y", "abstract": "", "url": "u", "authors": []}
        with mock.patch("cuhkvoting.cli._inspire_get_by_arxiv_id", return_value=None):
            with mock.patch("cuhkvoting.cli._arxiv_query", return_value=[arxiv_entry]) as arxiv:
                out = cli._validate_arxiv_entry("2601.09678")
        self.assertEqual(out, arxiv_entry)
        self.assertEqual(arxiv.call_count, 1)

    def test_fetch_entries_fallback_builds_arxiv_submittedDate_query(self) -> None:
        start = dt.date(2026, 5, 1)
        end = dt.date(2026, 5, 2)
        with mock.patch("cuhkvoting.cli._inspire_query_retry", side_effect=SystemExit("down")):
            with mock.patch("cuhkvoting.cli._arxiv_query", return_value=[]) as arxiv:
                cli._fetch_entries(["gr-qc"], start, end, limit=123)
        (params,), _kwargs = arxiv.call_args
        self.assertEqual(params["max_results"], "123")
        self.assertIn("submittedDate:[", params["search_query"])
        self.assertIn("cat:gr-qc", params["search_query"])
        self.assertEqual(params["sortBy"], "submittedDate")
        self.assertEqual(params["sortOrder"], "descending")

    def test_inspire_get_by_arxiv_id_returns_none_on_404(self) -> None:
        import urllib.error

        err = urllib.error.HTTPError(
            url="x",
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=None,
        )
        with mock.patch("cuhkvoting.cli.urllib.request.urlopen", side_effect=err):
            out = cli._inspire_get_by_arxiv_id("2601.09678v2")
        self.assertIsNone(out)

    def test_inspire_get_by_arxiv_id_parses_minimal_record(self) -> None:
        payload = {
            "metadata": {
                "titles": [{"title": "  Hello   World  "}],
                "abstracts": [{"value": "  abs  here "}],
                "authors": [{"full_name": "Ada   Lovelace"}],
                "arxiv_eprints": [{"value": "2601.09678v3", "categories": ["gr-qc", "quant-ph"]}],
                "earliest_date": "2026-01-02",
            }
        }
        body = json.dumps(payload).encode("utf-8")
        resp = mock.MagicMock()
        resp.read.return_value = body
        cm = mock.MagicMock()
        cm.__enter__.return_value = resp
        with mock.patch("cuhkvoting.cli.urllib.request.urlopen", return_value=cm):
            out = cli._inspire_get_by_arxiv_id("2601.09678v3")
        self.assertIsInstance(out, dict)
        assert out is not None
        self.assertEqual(out["id"], "2601.09678")
        self.assertEqual(out["title"], "Hello World")
        self.assertEqual(out["abstract"], "abs here")
        self.assertEqual(out["authors"], ["Ada Lovelace"])
        self.assertEqual(out["primary_category"], "gr-qc")
        self.assertEqual(out["published"], "2026-01-02")

    def test_inspire_query_retry_ignores_hits_without_arxiv_id(self) -> None:
        payload = {
            "hits": {
                "hits": [
                    {"metadata": {"titles": [{"title": "X"}]}},  # missing arxiv_eprints.value
                    {
                        "metadata": {
                            "titles": [{"title": "Y"}],
                            "arxiv_eprints": [{"value": "2601.00001"}],
                        }
                    },
                ]
            }
        }
        body = json.dumps(payload).encode("utf-8")
        resp = mock.MagicMock()
        resp.read.return_value = body
        cm = mock.MagicMock()
        cm.__enter__.return_value = resp
        with mock.patch("cuhkvoting.cli.urllib.request.urlopen", return_value=cm):
            out = cli._inspire_query_retry("q", 10)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], "2601.00001")

    def test_inspire_query_retry_retries_on_timeout_then_succeeds(self) -> None:
        payload = {"hits": {"hits": [{"metadata": {"titles": [{"title": "Y"}], "arxiv_eprints": [{"value": "2601.1"}]}}]}}
        body = json.dumps(payload).encode("utf-8")
        resp = mock.MagicMock()
        resp.read.return_value = body
        cm = mock.MagicMock()
        cm.__enter__.return_value = resp

        calls = {"n": 0}

        def urlopen_side_effect(*_a, **_k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("timed out")
            return cm

        with mock.patch("cuhkvoting.cli.urllib.request.urlopen", side_effect=urlopen_side_effect):
            with mock.patch("cuhkvoting.cli.time.sleep", return_value=None):
                out = cli._inspire_query_retry("q", 10)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(out[0]["id"], "2601.1")


if __name__ == "__main__":
    unittest.main()

