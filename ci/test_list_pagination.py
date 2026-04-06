"""Tests for ListPagination helper class."""

import os
import sys
import unittest

# Ensure the repo root is on sys.path so myapp is importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('WORKER_API_KEY', 'test')

from myapp.utils.pagination import ListPagination


class TestListPagination(unittest.TestCase):
    """Unit tests for the ListPagination in-memory paginator."""

    def test_basic_pagination(self):
        items = list(range(50))
        p = ListPagination(items, page=1, per_page=10)
        self.assertEqual(p.total, 50)
        self.assertEqual(p.pages, 5)
        self.assertEqual(p.page, 1)
        self.assertEqual(p.items, list(range(10)))
        self.assertFalse(p.has_prev)
        self.assertTrue(p.has_next)
        self.assertIsNone(p.prev_num)
        self.assertEqual(p.next_num, 2)

    def test_middle_page(self):
        items = list(range(50))
        p = ListPagination(items, page=3, per_page=10)
        self.assertEqual(p.items, list(range(20, 30)))
        self.assertTrue(p.has_prev)
        self.assertTrue(p.has_next)
        self.assertEqual(p.prev_num, 2)
        self.assertEqual(p.next_num, 4)

    def test_last_page(self):
        items = list(range(50))
        p = ListPagination(items, page=5, per_page=10)
        self.assertEqual(p.items, list(range(40, 50)))
        self.assertTrue(p.has_prev)
        self.assertFalse(p.has_next)
        self.assertEqual(p.prev_num, 4)
        self.assertIsNone(p.next_num)

    def test_partial_last_page(self):
        items = list(range(23))
        p = ListPagination(items, page=3, per_page=10)
        self.assertEqual(p.pages, 3)
        self.assertEqual(p.items, [20, 21, 22])

    def test_single_page(self):
        items = list(range(5))
        p = ListPagination(items, page=1, per_page=10)
        self.assertEqual(p.pages, 1)
        self.assertEqual(p.items, list(range(5)))
        self.assertFalse(p.has_prev)
        self.assertFalse(p.has_next)

    def test_empty_list(self):
        p = ListPagination([], page=1, per_page=10)
        self.assertEqual(p.total, 0)
        self.assertEqual(p.pages, 1)
        self.assertEqual(p.page, 1)
        self.assertEqual(p.items, [])
        self.assertFalse(p.has_prev)
        self.assertFalse(p.has_next)

    def test_page_clamped_to_max(self):
        items = list(range(10))
        p = ListPagination(items, page=999, per_page=10)
        self.assertEqual(p.page, 1)  # only 1 page, clamped
        self.assertEqual(p.items, list(range(10)))

    def test_page_clamped_to_min(self):
        items = list(range(10))
        p = ListPagination(items, page=0, per_page=10)
        self.assertEqual(p.page, 1)

    def test_iter_pages_small(self):
        items = list(range(30))
        p = ListPagination(items, page=2, per_page=10)
        pages = list(p.iter_pages())
        self.assertEqual(pages, [1, 2, 3])

    def test_iter_pages_with_gaps(self):
        items = list(range(250))
        p = ListPagination(items, page=13, per_page=10)
        pages = list(p.iter_pages(left_edge=1, left_current=2, right_current=3, right_edge=1))
        # Should have: [1, None, 11, 12, 13, 14, 15, 16, None, 25]
        self.assertIn(1, pages)
        self.assertIn(None, pages)
        self.assertIn(13, pages)
        self.assertIn(25, pages)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
