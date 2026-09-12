"""arXiv API helpers are disabled at CUHK (use INSPIRE instead)."""

from __future__ import annotations

import unittest

from cuhkvoting.cli import _arxiv_query


class ArxivDisabledTests(unittest.TestCase):
    def test_arxiv_query_raises(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            _arxiv_query({"search_query": "id:2601.09678", "start": "0", "max_results": "1"})
        self.assertIn("INSPIRE", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
