from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from .handl import MockHandlClient, SQLHandlClient, get_handl_client


class MockHandlClientTests(TestCase):
    def setUp(self):
        self.client = MockHandlClient()
        self.since = date.today() - timedelta(days=90)

    def test_usage_is_deterministic_per_engineer(self):
        first = self.client.get_stock_usage(["ENG-001"], self.since)
        second = self.client.get_stock_usage(["ENG-001"], self.since)
        self.assertEqual(
            [u.part_code for u in first], [u.part_code for u in second]
        )

    def test_different_engineers_can_get_different_pools(self):
        a = self.client.get_stock_usage(["ENG-001"], self.since)
        b = self.client.get_stock_usage(["ENG-999"], self.since)
        self.assertNotEqual(
            [u.part_code for u in a], [u.part_code for u in b]
        )

    def test_expected_stock_returns_all_requested_codes(self):
        codes = ["TK-100", "TK-101", "TK-102"]
        expected = self.client.get_expected_stock(["ENG-001"], codes)
        self.assertEqual(set(expected.keys()), set(codes))
        for stock in expected.values():
            self.assertGreater(stock.expected_qty, 0)

    def test_id_order_does_not_change_result(self):
        """A locksmith's (V) and (A) rows should combine the same way
        regardless of which order they're listed in."""
        a = self.client.get_stock_usage(["ENG-001", "ENG-002"], self.since)
        b = self.client.get_stock_usage(["ENG-002", "ENG-001"], self.since)
        self.assertEqual([u.part_code for u in a], [u.part_code for u in b])


def _fake_connection(rows):
    """A MagicMock usable as `with client._connection() as conn:`, with
    conn.cursor().fetchall() pre-loaded with the given rows."""
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    conn.cursor.return_value.fetchall.return_value = rows
    return conn


def _fake_connection_multi(*fetchall_results):
    """Like _fake_connection, but for code paths that call execute()
    more than once — each fetchall() call returns the next result set
    in sequence."""
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    conn.cursor.return_value.fetchall.side_effect = list(fetchall_results)
    return conn


class SQLHandlClientTests(TestCase):
    """Exercises the real Soter queries against a mocked pymssql connection
    (no live DB access from this environment) — catches SQL/mapping bugs
    without needing a real Soter connection to run the test suite."""

    def test_get_stock_usage_maps_rows_to_stock_usage(self):
        rows = [
            {"part_code": "TK-100", "part_name": "Transponder key blank", "qty_used": 12},
            {"part_code": "TK-107", "part_name": "Van lock cylinder", "qty_used": 4},
        ]
        client = SQLHandlClient()
        with patch.object(client, "_connection", return_value=_fake_connection(rows)):
            usage = client.get_stock_usage(["42"], date(2026, 1, 1))

        self.assertEqual(len(usage), 2)
        self.assertEqual(usage[0].part_code, "TK-100")
        self.assertEqual(usage[0].qty_used, 12)

    def test_get_stock_usage_passes_each_locksmith_id_as_int_and_since_date(self):
        client = SQLHandlClient()
        fake_conn = _fake_connection([])
        with patch.object(client, "_connection", return_value=fake_conn):
            client.get_stock_usage(["42", "43"], date(2026, 1, 1))

        cursor = fake_conn.cursor.return_value
        query, params = cursor.execute.call_args[0]
        self.assertIn("%(lid0)s", query)
        self.assertIn("%(lid1)s", query)
        self.assertEqual(params["lid0"], 42)
        self.assertEqual(params["lid1"], 43)
        self.assertIsInstance(params["lid0"], int)
        self.assertEqual(params["since"], date(2026, 1, 1))

    def test_get_expected_stock_maps_rows_and_defaults_null_cost_to_zero(self):
        qty_rows = [
            {"part_code": "TK-100", "expected_qty": 6},
            {"part_code": "TK-101", "expected_qty": 3},
        ]
        cost_rows = [
            {"part_code": "TK-100", "unit_cost": 12.5},
            {"part_code": "TK-101", "unit_cost": None},
        ]
        client = SQLHandlClient()
        with patch.object(
            client, "_connection", return_value=_fake_connection_multi(qty_rows, cost_rows)
        ):
            expected = client.get_expected_stock(["42"], ["TK-100", "TK-101"])

        self.assertEqual(expected["TK-100"].expected_qty, 6)
        self.assertEqual(expected["TK-100"].unit_cost, 12.5)
        self.assertEqual(expected["TK-101"].unit_cost, 0)

    def test_get_expected_stock_returns_empty_dict_for_no_codes(self):
        client = SQLHandlClient()
        self.assertEqual(client.get_expected_stock(["42"], []), {})

    def test_get_expected_stock_builds_placeholders_for_ids_and_codes(self):
        fake_conn = _fake_connection_multi([], [])
        client = SQLHandlClient()
        with patch.object(client, "_connection", return_value=fake_conn):
            client.get_expected_stock(["42", "43"], ["TK-100", "TK-101", "TK-102"])
        cursor = fake_conn.cursor.return_value
        qty_query, qty_params = cursor.execute.call_args_list[0][0]
        self.assertIn("%(lid0)s", qty_query)
        self.assertIn("%(lid1)s", qty_query)
        self.assertIn("%(code0)s", qty_query)
        self.assertIn("%(code1)s", qty_query)
        self.assertIn("%(code2)s", qty_query)
        self.assertEqual(qty_params["lid0"], 42)
        self.assertEqual(qty_params["code0"], "TK-100")

    def test_get_expected_stock_quantity_query_does_not_join_inventory_stock(self):
        """Regression test: Inventory_Stock is a company-wide batch
        table with no locksmith scoping. Joining it into the quantity
        query (even a LEFT JOIN on PartId) fans out and wildly inflates
        SUM(ils.Quantity) — confirmed against live data (a real
        locksmith's expected quantity for CR2032 batteries came back as
        5589 instead of a plausible van-stock number). Unit cost must
        come from a separate, unfiltered-by-locksmith query instead.
        """
        fake_conn = _fake_connection_multi([], [])
        client = SQLHandlClient()
        with patch.object(client, "_connection", return_value=fake_conn):
            client.get_expected_stock(["42"], ["TK-100"])
        cursor = fake_conn.cursor.return_value
        qty_query = cursor.execute.call_args_list[0][0][0]
        self.assertNotIn("Inventory_Stock", qty_query)

    def test_get_expected_stock_cost_query_is_not_scoped_to_locksmith(self):
        fake_conn = _fake_connection_multi([], [])
        client = SQLHandlClient()
        with patch.object(client, "_connection", return_value=fake_conn):
            client.get_expected_stock(["42"], ["TK-100"])
        cursor = fake_conn.cursor.return_value
        cost_query, cost_params = cursor.execute.call_args_list[1][0]
        self.assertNotIn("lid0", cost_params)
        self.assertIn("Inventory_Stock", cost_query)


class GetHandlClientTests(TestCase):
    @override_settings(HANDL_SQL_SERVER="")
    def test_returns_mock_client_when_unconfigured(self):
        self.assertIsInstance(get_handl_client(), MockHandlClient)

    @override_settings(HANDL_SQL_SERVER="soterlive1.database.windows.net")
    def test_returns_sql_client_when_configured(self):
        self.assertIsInstance(get_handl_client(), SQLHandlClient)
