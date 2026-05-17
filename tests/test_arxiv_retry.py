"""Tests for bounded arXiv query retries."""

from __future__ import annotations

import unittest
from unittest import mock

from cuhkvoting.cli import ARXIV_RETRY_DELAYS, _arxiv_query


class ArxivRetryTests(unittest.TestCase):
    def test_stops_after_max_retries(self) -> None:
        calls = 0

        def fail_http_text(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise TimeoutError("timed out")

        with mock.patch("cuhkvoting.cli._http_text", side_effect=fail_http_text):
            with self.assertRaises(SystemExit) as ctx:
                _arxiv_query({"search_query": "id:2601.09678", "start": "0", "max_results": "1"})
        self.assertEqual(calls, len(ARXIV_RETRY_DELAYS) + 1)
        self.assertIn("after 3 retries", str(ctx.exception))

    def test_succeeds_without_extra_retries(self) -> None:
        calls = 0
        xml = (
            '<?xml version="1.0"?>'
            '<feed xmlns="http://www.w3.org/2005/Atom">'
            "<entry>"
            "<id>http://arxiv.org/abs/2601.09678</id>"
            "<title>Test Paper</title>"
            "<summary>Abstract.</summary>"
            "<published>2026-01-01T00:00:00Z</published>"
            "</entry>"
            "</feed>"
        )

        def ok_http_text(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return xml

        with mock.patch("cuhkvoting.cli._http_text", side_effect=ok_http_text):
            entries = _arxiv_query({"search_query": "id:2601.09678", "start": "0", "max_results": "1"})
        self.assertEqual(calls, 1)
        self.assertEqual(entries[0]["title"], "Test Paper")


if __name__ == "__main__":
    unittest.main()
