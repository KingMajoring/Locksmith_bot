from datetime import date, timedelta

from django.test import TestCase

from .handl import MockHandlClient


class MockHandlClientTests(TestCase):
    def setUp(self):
        self.client = MockHandlClient()
        self.since = date.today() - timedelta(days=90)

    def test_usage_is_deterministic_per_engineer(self):
        first = self.client.get_stock_usage("ENG-001", self.since)
        second = self.client.get_stock_usage("ENG-001", self.since)
        self.assertEqual(
            [u.part_code for u in first], [u.part_code for u in second]
        )

    def test_different_engineers_can_get_different_pools(self):
        a = self.client.get_stock_usage("ENG-001", self.since)
        b = self.client.get_stock_usage("ENG-999", self.since)
        self.assertNotEqual(
            [u.part_code for u in a], [u.part_code for u in b]
        )

    def test_expected_stock_returns_all_requested_codes(self):
        codes = ["TK-100", "TK-101", "TK-102"]
        expected = self.client.get_expected_stock("ENG-001", codes)
        self.assertEqual(set(expected.keys()), set(codes))
        for stock in expected.values():
            self.assertGreater(stock.expected_qty, 0)
