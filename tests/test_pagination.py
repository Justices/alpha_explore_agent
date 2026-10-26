import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nastu_quant.pagination import PaginationError, collect_pages


class TestPagination(unittest.TestCase):
    def test_collects_complete_exact_origin_catalog(self):
        first = {"count": 3, "results": [{"id": "a"}, {"id": "b"}],
                 "next": "https://api.worldquantbrain.com/data-fields?offset=2"}
        result = collect_pages(first,
            lambda url: {"count": 3, "results": [{"id": "c"}], "next": None},
            "https://api.worldquantbrain.com")
        self.assertEqual([row["id"] for row in result["results"]], ["a", "b", "c"])
        self.assertTrue(result["complete"])

    def test_rejects_truncated_catalog_without_next_page(self):
        with self.assertRaises(PaginationError):
            collect_pages({"count": 3, "results": [{"id": "a"}], "next": None},
                          lambda url: self.fail("unexpected page fetch"),
                          "https://api.worldquantbrain.com")

    def test_rejects_cross_origin_next_link(self):
        with self.assertRaises(PaginationError):
            collect_pages({"count": 2, "results": [{"id": "a"}],
                           "next": "https://example.com/steal"},
                          lambda url: self.fail("external request"),
                          "https://api.worldquantbrain.com")


if __name__ == "__main__":
    unittest.main()
