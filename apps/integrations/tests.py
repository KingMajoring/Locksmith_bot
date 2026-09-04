from datetime import date, datetime, timedelta, timezone as dt_timezone
from unittest.mock import MagicMock, patch

from django.core.mail import EmailMessage
from django.test import TestCase, override_settings

from .graph_email_backend import MicrosoftGraphEmailBackend
from .handl import MockHandlClient, SQLHandlClient, get_handl_client
from .models import OptimoSettings
from .optimo import MockOptimoClient, RealOptimoClient, get_optimo_client
from .photos import AzureBlobPhotoStorage, MockPhotoStorage, get_photo_storage


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

    def test_get_job_details_returns_all_requested_report_ids(self):
        report_ids = ["1001", "1002"]
        details = self.client.get_job_details(report_ids)
        self.assertEqual(set(details.keys()), set(report_ids))
        for job in details.values():
            self.assertTrue(job.make)
            self.assertTrue(job.model)
            self.assertTrue(job.year)
            self.assertTrue(job.vin)
            self.assertTrue(job.service_type)

    def test_get_job_details_is_deterministic_per_report_id(self):
        first = self.client.get_job_details(["1001"])["1001"]
        second = self.client.get_job_details(["1001"])["1001"]
        self.assertEqual(first, second)

    def test_get_disposed_skus_is_deterministic_and_valid_codes(self):
        first = self.client.get_disposed_skus(["1001"])
        second = self.client.get_disposed_skus(["1001"])
        self.assertEqual(first, second)
        valid_codes = {code for code, _name in self.client._CATALOGUE}
        for skus in first.values():
            for sku in skus:
                self.assertIn(sku, valid_codes)

    def test_get_panel_daily_figures_is_deterministic_for_same_range(self):
        start, end = date(2026, 9, 1), date(2026, 9, 8)
        first = self.client.get_panel_daily_figures(start, end)
        second = self.client.get_panel_daily_figures(start, end)
        self.assertEqual(first, second)

    def test_get_panel_daily_figures_only_within_range(self):
        start, end = date(2026, 9, 1), date(2026, 9, 8)
        figures = self.client.get_panel_daily_figures(start, end)
        for figure in figures:
            self.assertGreaterEqual(figure.figure_date, start)
            self.assertLess(figure.figure_date, end)

    def test_get_panel_daily_figures_empty_range_returns_empty(self):
        same_day = date(2026, 9, 1)
        self.assertEqual(self.client.get_panel_daily_figures(same_day, same_day), [])

    def test_get_part_costs_returns_all_requested_skus_and_is_deterministic(self):
        skus = ["TK-100", "TK-101"]
        first = self.client.get_part_costs(skus)
        second = self.client.get_part_costs(skus)
        self.assertEqual(set(first.keys()), set(skus))
        self.assertEqual(first, second)
        for cost in first.values():
            self.assertGreater(cost, 0)

    def test_list_current_stock_is_deterministic_and_positive_qty(self):
        first = self.client.list_current_stock(["ENG-001"])
        second = self.client.list_current_stock(["ENG-001"])
        self.assertEqual(first, second)
        self.assertTrue(first)
        for line in first:
            self.assertGreater(line.qty, 0)

    def test_record_disposal_does_not_raise(self):
        self.client.record_disposal(
            "885",
            "496390",
            "TK-100",
            "Transponder key blank",
            2,
            actioned_by_user_id=522,
            locksmith_display_name="WGTK - Dean S (V)",
        )

    def test_list_locksmith_user_ids_returns_empty_dict(self):
        self.assertEqual(self.client.list_locksmith_user_ids(), {})

    def test_add_report_note_does_not_raise(self):
        self.client.add_report_note("496390", "'Dean S' is on route.", actioned_by_user_id=522)


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

    def test_get_expected_stock_cost_query_uses_most_recent_batch_not_average(self):
        """Regression test: AVG(PartValue) across a part's whole
        purchase history skews badly on real data (CR2032 averaged out
        at £13.13 vs £0.25-£2.45 actually seen across its real
        suppliers). The most recent batch is the right read on
        "current" cost, matching how Soter's own UI shows per-supplier
        cost — so no AVG(, and a recency-ranked ROW_NUMBER() instead.
        """
        fake_conn = _fake_connection_multi([], [])
        client = SQLHandlClient()
        with patch.object(client, "_connection", return_value=fake_conn):
            client.get_expected_stock(["42"], ["TK-100"])
        cursor = fake_conn.cursor.return_value
        cost_query = cursor.execute.call_args_list[1][0][0]
        self.assertNotIn("AVG(", cost_query)
        self.assertIn("ROW_NUMBER()", cost_query)
        self.assertIn("ORDER BY ist.DateCreated DESC", cost_query)
        self.assertIn("Inventory_Stock", cost_query)

    def test_get_expected_stock_cost_query_excludes_zero_value_batches(self):
        """Regression test: also confirmed on real data — the single
        most-recent Inventory_Stock row for several parts had
        PartValue 0/NULL (a stock adjustment/recount, not a purchase),
        so ranking by date alone returned unit_cost=0.0 for everything.
        Non-priced rows must be excluded before ranking.
        """
        fake_conn = _fake_connection_multi([], [])
        client = SQLHandlClient()
        with patch.object(client, "_connection", return_value=fake_conn):
            client.get_expected_stock(["42"], ["TK-100"])
        cursor = fake_conn.cursor.return_value
        cost_query = cursor.execute.call_args_list[1][0][0]
        self.assertIn("ist.PartValue > 0", cost_query)
        self.assertIn("ist.Quantity > 0", cost_query)

    def test_get_expected_stock_cost_query_divides_batch_value_by_quantity(self):
        """Regression test: PartValue is the batch *total*, not a
        per-unit price — confirmed on real data, a batch with
        Quantity=37, PartValue=90.65 gives the real per-unit price
        (90.65/37 = £2.45) only once divided by Quantity.
        """
        fake_conn = _fake_connection_multi([], [])
        client = SQLHandlClient()
        with patch.object(client, "_connection", return_value=fake_conn):
            client.get_expected_stock(["42"], ["TK-100"])
        cursor = fake_conn.cursor.return_value
        cost_query = cursor.execute.call_args_list[1][0][0]
        self.assertIn("ist.PartValue / ist.Quantity", cost_query)

    def test_get_job_details_maps_rows_and_queries_policy_key_claims(self):
        """Regression test: the real table is Policy_KeyClaims, not
        Policy_Details (which has no vehicle fields at all and threw
        "Invalid column name 'Make'" against real Handl data)."""
        rows = [
            {
                "ReportID": "496390",
                "Make": "NISSAN",
                "Model": "X-TRAIL ACENTA DCI 4X4 CVT",
                "yearOfManufacture": 2017,
                "VehicleVIN": "SJNFAAJ11U1234567",
                "KeyType": "Car",
                "LossEvent": "Lost Keys",
                "SuppliedService": "Key Programming",
                "NetCost": 145.5,
            }
        ]
        fake_conn = _fake_connection(rows)
        client = SQLHandlClient()
        with patch.object(client, "_connection", return_value=fake_conn):
            details = client.get_job_details(["496390"])

        cursor = fake_conn.cursor.return_value
        query = cursor.execute.call_args_list[0][0][0]
        self.assertIn("Policy_KeyClaims", query)
        self.assertIn("Lookup_KeyType", query)
        self.assertIn("Lookup_LossEvent_Details", query)
        self.assertIn("Lookup_LocksmithSuppliedServices", query)
        self.assertIn("Policy_Financial", query)

        job = details["496390"]
        self.assertEqual(job.make, "NISSAN")
        self.assertEqual(job.year, "2017")
        self.assertEqual(job.vin, "SJNFAAJ11U1234567")
        self.assertEqual(job.service_type, "Car")
        self.assertEqual(job.loss_type, "Lost Keys")
        self.assertEqual(job.supplied_service, "Key Programming")
        self.assertEqual(job.net_cost, 145.5)

    def test_get_job_details_null_net_cost_maps_to_none(self):
        rows = [
            {
                "ReportID": "496390",
                "Make": "NISSAN",
                "Model": "X-TRAIL",
                "yearOfManufacture": 2017,
                "VehicleVIN": "SJNFAAJ11U1234567",
                "KeyType": "Car",
                "LossEvent": "Lost Keys",
                "SuppliedService": "Key Programming",
                "NetCost": None,
            }
        ]
        fake_conn = _fake_connection(rows)
        client = SQLHandlClient()
        with patch.object(client, "_connection", return_value=fake_conn):
            details = client.get_job_details(["496390"])
        self.assertIsNone(details["496390"].net_cost)

    def test_get_job_details_empty_input_returns_empty_without_querying(self):
        client = SQLHandlClient()
        self.assertEqual(client.get_job_details([]), {})

    def test_get_disposed_skus_groups_rows_by_report_id_in_order(self):
        rows = [
            {"ReportID": "496390", "SKU": "TK-104"},
            {"ReportID": "496390", "SKU": "TK-100"},
            {"ReportID": "500001", "SKU": "TK-113"},
        ]
        fake_conn = _fake_connection(rows)
        client = SQLHandlClient()
        with patch.object(client, "_connection", return_value=fake_conn):
            skus = client.get_disposed_skus(["496390", "500001"])

        cursor = fake_conn.cursor.return_value
        query = cursor.execute.call_args_list[0][0][0]
        self.assertIn("Inventory_Disposals", query)
        self.assertIn("idp.ReportID", query)

        self.assertEqual(skus["496390"], ["TK-104", "TK-100"])
        self.assertEqual(skus["500001"], ["TK-113"])

    def test_get_disposed_skus_empty_input_returns_empty_without_querying(self):
        client = SQLHandlClient()
        self.assertEqual(client.get_disposed_skus([]), {})

    def test_get_panel_daily_figures_maps_rows_from_tableau_panel_figures(self):
        rows = [
            {
                "Date": date(2026, 9, 3),
                "Panel Name": "ABC Locksmiths",
                "Jobs Completed": 2,
                "WGTK Fee": 70.0,
                "NetCost": 540.0,
            },
            {
                "Date": date(2026, 9, 4),
                "Panel Name": "ABC Locksmiths",
                "Jobs Completed": 1,
                "WGTK Fee": None,
                "NetCost": None,
            },
        ]
        fake_conn = _fake_connection(rows)
        client = SQLHandlClient()
        with patch.object(client, "_connection", return_value=fake_conn):
            figures = client.get_panel_daily_figures(date(2026, 9, 1), date(2026, 9, 8))

        cursor = fake_conn.cursor.return_value
        query = cursor.execute.call_args[0][0]
        self.assertIn("Tableau_PanelFigures", query)
        self.assertIn("[Panel Name]", query)
        self.assertIn("[Jobs Completed]", query)
        self.assertIn("[WGTK Fee]", query)

        params = cursor.execute.call_args[0][1]
        self.assertEqual(params, {"start": date(2026, 9, 1), "end": date(2026, 9, 8)})

        self.assertEqual(len(figures), 2)
        self.assertEqual(figures[0].panel_name, "ABC Locksmiths")
        self.assertEqual(figures[0].job_count, 2)
        self.assertEqual(figures[0].wgtk_fee, 70.0)
        self.assertEqual(figures[0].net_cost, 540.0)
        self.assertIsNone(figures[1].wgtk_fee)
        self.assertIsNone(figures[1].net_cost)

    def test_get_part_costs_maps_rows_and_uses_same_cost_basis_as_expected_stock(self):
        rows = [
            {"part_code": "TK-100", "unit_cost": 2.45},
            {"part_code": "TK-101", "unit_cost": None},
        ]
        fake_conn = _fake_connection(rows)
        client = SQLHandlClient()
        with patch.object(client, "_connection", return_value=fake_conn):
            costs = client.get_part_costs(["TK-100", "TK-101"])

        self.assertEqual(costs["TK-100"], 2.45)
        self.assertEqual(costs["TK-101"], 0)

        cursor = fake_conn.cursor.return_value
        query = cursor.execute.call_args[0][0]
        self.assertIn("ROW_NUMBER()", query)
        self.assertIn("ist.PartValue / ist.Quantity", query)
        self.assertIn("ist.PartValue > 0", query)
        self.assertIn("ist.Quantity > 0", query)

    def test_get_part_costs_empty_input_returns_empty_without_querying(self):
        client = SQLHandlClient()
        self.assertEqual(client.get_part_costs([]), {})

    def test_list_current_stock_maps_rows_and_uses_positive_qty_having(self):
        rows = [
            {"part_code": "TK-100", "part_name": "Transponder key blank", "qty": 4},
            {"part_code": "TK-107", "part_name": "Van lock cylinder", "qty": 1},
        ]
        fake_conn = _fake_connection(rows)
        client = SQLHandlClient()
        with patch.object(client, "_connection", return_value=fake_conn):
            lines = client.list_current_stock(["42", "43"])

        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0].part_code, "TK-100")
        self.assertEqual(lines[0].qty, 4)

        cursor = fake_conn.cursor.return_value
        query, params = cursor.execute.call_args[0]
        self.assertIn("Inventory_Locksmith_Stock", query)
        self.assertIn("HAVING SUM(ils.Quantity) > 0", query)
        self.assertEqual(params["lid0"], 42)
        self.assertEqual(params["lid1"], 43)

    def test_list_current_stock_empty_ids_returns_empty_without_querying(self):
        client = SQLHandlClient()
        self.assertEqual(client.list_current_stock([]), [])

    def _record_disposal(self, client, quantity, part_code="TK-100"):
        client.record_disposal(
            "885",
            "496390",
            part_code,
            "Transponder key blank",
            quantity,
            actioned_by_user_id=517,
            locksmith_display_name="WGTK - Blain H (V)",
        )

    def test_record_disposal_inserts_and_decrements_stock_then_commits(self):
        fake_conn = MagicMock()
        fake_conn.__enter__.return_value = fake_conn
        fake_conn.__exit__.return_value = False
        cursor = fake_conn.cursor.return_value
        cursor.fetchone.return_value = {"Id": 555}
        cursor.fetchall.return_value = [{"Id": "loc-stock-1", "Quantity": 5}]

        client = SQLHandlClient()
        with patch.object(client, "_write_connection", return_value=fake_conn):
            self._record_disposal(client, 2)

        calls = cursor.execute.call_args_list

        select_query, select_params = calls[0][0]
        self.assertIn("Inventory_Stock", select_query)
        self.assertIn("ORDER BY ist.DateCreated DESC", select_query)
        self.assertEqual(select_params["sku"], "TK-100")

        stock_query, stock_params = calls[1][0]
        self.assertIn("Inventory_Locksmith_Stock", stock_query)
        self.assertEqual(stock_params["lid"], 885)
        self.assertEqual(stock_params["sku"], "TK-100")

        update_query, update_params = calls[2][0]
        self.assertIn("UPDATE Inventory_Locksmith_Stock", update_query)
        self.assertEqual(update_params, {"take": 2, "id": "loc-stock-1"})

        insert_query, insert_params = calls[3][0]
        self.assertIn("INSERT INTO Inventory_Disposals", insert_query)
        self.assertIn("NEWID()", insert_query)
        self.assertEqual(insert_params["lid"], 885)
        self.assertEqual(insert_params["report_id"], "496390")
        self.assertEqual(insert_params["stock_id"], 555)
        self.assertEqual(insert_params["locksmith_stock_id"], "loc-stock-1")
        self.assertEqual(insert_params["qty"], 2)
        self.assertEqual(insert_params["created_by"], 517)

        history_query, history_params = calls[4][0]
        self.assertIn("INSERT INTO Policy_History", history_query)
        self.assertEqual(history_params["report_id"], "496390")
        self.assertEqual(history_params["actioned_by"], 517)
        self.assertEqual(
            history_params["notes"],
            "'WGTK - Blain H (V)' has disposed of 2 "
            "'Transponder key blank(s) (TK-100)'.",
        )

        fake_conn.commit.assert_called_once()

    def test_record_disposal_decrements_across_multiple_rows_largest_first(self):
        fake_conn = MagicMock()
        fake_conn.__enter__.return_value = fake_conn
        fake_conn.__exit__.return_value = False
        cursor = fake_conn.cursor.return_value
        cursor.fetchone.return_value = {"Id": 555}
        cursor.fetchall.return_value = [
            {"Id": "row-big", "Quantity": 3},
            {"Id": "row-small", "Quantity": 2},
        ]

        client = SQLHandlClient()
        with patch.object(client, "_write_connection", return_value=fake_conn):
            self._record_disposal(client, 4)

        update_calls = [c for c in cursor.execute.call_args_list if "UPDATE" in c[0][0]]
        self.assertEqual(len(update_calls), 2)
        self.assertEqual(update_calls[0][0][1], {"take": 3, "id": "row-big"})
        self.assertEqual(update_calls[1][0][1], {"take": 1, "id": "row-small"})

    def test_record_disposal_raises_when_no_stock_batch_found(self):
        fake_conn = MagicMock()
        fake_conn.__enter__.return_value = fake_conn
        fake_conn.__exit__.return_value = False
        fake_conn.cursor.return_value.fetchone.return_value = None
        client = SQLHandlClient()
        with patch.object(client, "_write_connection", return_value=fake_conn):
            with self.assertRaises(ValueError):
                self._record_disposal(client, 1, part_code="TK-999")
        fake_conn.commit.assert_not_called()

    def test_record_disposal_raises_when_no_locksmith_stock_found(self):
        fake_conn = MagicMock()
        fake_conn.__enter__.return_value = fake_conn
        fake_conn.__exit__.return_value = False
        cursor = fake_conn.cursor.return_value
        cursor.fetchone.return_value = {"Id": 555}
        cursor.fetchall.return_value = []
        client = SQLHandlClient()
        with patch.object(client, "_write_connection", return_value=fake_conn):
            with self.assertRaises(ValueError):
                self._record_disposal(client, 1)
        fake_conn.commit.assert_not_called()

    def test_record_disposal_raises_when_stock_rows_insufficient(self):
        fake_conn = MagicMock()
        fake_conn.__enter__.return_value = fake_conn
        fake_conn.__exit__.return_value = False
        cursor = fake_conn.cursor.return_value
        cursor.fetchone.return_value = {"Id": 555}
        cursor.fetchall.return_value = [{"Id": "row-1", "Quantity": 1}]
        client = SQLHandlClient()
        with patch.object(client, "_write_connection", return_value=fake_conn):
            with self.assertRaises(ValueError):
                self._record_disposal(client, 5)
        fake_conn.commit.assert_not_called()

    def test_list_locksmith_user_ids_maps_receipt_name_to_user_id(self):
        rows = [
            {"UserId": 517, "ReceiptName": "WGTK - Blain H"},
            {"UserId": 522, "ReceiptName": "WGTK - Daryl B"},
        ]
        fake_conn = _fake_connection(rows)
        client = SQLHandlClient()
        with patch.object(client, "_connection", return_value=fake_conn):
            result = client.list_locksmith_user_ids()

        self.assertEqual(result, {"WGTK - Blain H": 517, "WGTK - Daryl B": 522})
        cursor = fake_conn.cursor.return_value
        query = cursor.execute.call_args[0][0]
        self.assertIn("wiki.LocksmithLogin", query)
        self.assertNotIn("password", query.lower())

    def test_add_report_note_inserts_and_commits(self):
        fake_conn = MagicMock()
        fake_conn.__enter__.return_value = fake_conn
        fake_conn.__exit__.return_value = False
        cursor = fake_conn.cursor.return_value

        client = SQLHandlClient()
        with patch.object(client, "_write_connection", return_value=fake_conn):
            client.add_report_note("496390", "'Dean S' is on route.", actioned_by_user_id=517)

        query, params = cursor.execute.call_args[0]
        self.assertIn("INSERT INTO Policy_History", query)
        self.assertEqual(params["report_id"], "496390")
        self.assertEqual(params["notes"], "'Dean S' is on route.")
        self.assertEqual(params["actioned_by"], 517)
        fake_conn.commit.assert_called_once()


class GetHandlClientTests(TestCase):
    @override_settings(HANDL_SQL_SERVER="")
    def test_returns_mock_client_when_unconfigured(self):
        self.assertIsInstance(get_handl_client(), MockHandlClient)

    @override_settings(HANDL_SQL_SERVER="soterlive1.database.windows.net")
    def test_returns_sql_client_when_configured(self):
        self.assertIsInstance(get_handl_client(), SQLHandlClient)


class MockOptimoClientCompletionStatusTests(TestCase):
    def test_update_completion_status_does_not_raise(self):
        MockOptimoClient().update_completion_status("496390_2026-09-04", "on_route")


class RealOptimoClientCompletionStatusTests(TestCase):
    def _mock_response(self, orders):
        response = MagicMock()
        response.json.return_value = {"success": True, "orders": orders}
        return response

    @patch("requests.post")
    def test_posts_status_only_update(self, mock_post):
        mock_post.return_value = self._mock_response(
            [{"success": True, "orderNo": "496390_2026-09-04", "data": {"status": "on_route"}}]
        )
        client = RealOptimoClient("KEY")
        client.update_completion_status("496390_2026-09-04", "on_route")

        body = mock_post.call_args.kwargs["json"]
        self.assertEqual(
            body["updates"],
            [{"orderNo": "496390_2026-09-04", "data": {"status": "on_route"}}],
        )
        self.assertEqual(mock_post.call_args.kwargs["params"], {"key": "KEY"})

    @patch("requests.post")
    def test_formats_start_and_end_times_as_bare_utc_iso(self, mock_post):
        mock_post.return_value = self._mock_response([{"success": True}])
        client = RealOptimoClient("KEY")
        start = datetime(2026, 9, 4, 9, 30, 0, tzinfo=dt_timezone.utc)
        end = datetime(2026, 9, 4, 9, 45, 12, tzinfo=dt_timezone.utc)

        client.update_completion_status("496390", "success", start_time=start, end_time=end)

        data = mock_post.call_args.kwargs["json"]["updates"][0]["data"]
        self.assertEqual(data["startTime"], {"utcTime": "2026-09-04T09:30:00"})
        self.assertEqual(data["endTime"], {"utcTime": "2026-09-04T09:45:12"})

    @patch("requests.post")
    def test_raises_when_optimo_rejects_the_update(self, mock_post):
        response = MagicMock()
        response.json.return_value = {
            "success": False,
            "orders": [
                {"success": False, "orderNo": "XXX", "message": "not found", "code": "ERR_ORD_NOT_FOUND"}
            ],
        }
        mock_post.return_value = response
        client = RealOptimoClient("KEY")

        with self.assertRaises(ValueError):
            client.update_completion_status("XXX", "on_route")

    @patch("requests.post")
    def test_raises_when_no_orders_returned(self, mock_post):
        mock_post.return_value = self._mock_response([])
        client = RealOptimoClient("KEY")
        with self.assertRaises(ValueError):
            client.update_completion_status("496390", "on_route")


@override_settings(OPTIMO_API_KEY="")
class GetOptimoClientTests(TestCase):
    def test_returns_mock_client_when_unconfigured(self):
        self.assertIsInstance(get_optimo_client(), MockOptimoClient)

    def test_returns_real_client_using_admin_stored_key(self):
        OptimoSettings.objects.create(api_key="admin-set-key")
        client = get_optimo_client()
        self.assertIsInstance(client, RealOptimoClient)
        self.assertEqual(client._api_key, "admin-set-key")

    @override_settings(OPTIMO_API_KEY="app-setting-key")
    def test_falls_back_to_app_setting_when_no_admin_key(self):
        client = get_optimo_client()
        self.assertIsInstance(client, RealOptimoClient)
        self.assertEqual(client._api_key, "app-setting-key")

    @override_settings(OPTIMO_API_KEY="app-setting-key")
    def test_admin_stored_key_takes_priority_over_app_setting(self):
        OptimoSettings.objects.create(api_key="admin-set-key")
        client = get_optimo_client()
        self.assertEqual(client._api_key, "admin-set-key")


def _fake_token_response():
    resp = MagicMock()
    resp.json.return_value = {"access_token": "fake-token"}
    resp.raise_for_status.return_value = None
    return resp


def _fake_send_response():
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    return resp


@override_settings(
    MS_GRAPH_MAIL_CLIENT_ID="client-id",
    MS_GRAPH_MAIL_CLIENT_SECRET="client-secret",
    MS_GRAPH_MAIL_TENANT_ID="tenant-id",
    MS_GRAPH_MAIL_SENDER="admin@wgtk.co.uk",
    MS_GRAPH_MAIL_FROM="parts@wgtk.co.uk",
)
class MicrosoftGraphEmailBackendTests(TestCase):
    def _message(self, **kwargs):
        defaults = dict(
            subject="Weekly stock check",
            body="Please count the parts.",
            to=["bob@example.com"],
        )
        defaults.update(kwargs)
        return EmailMessage(**defaults)

    @patch("apps.integrations.graph_email_backend.requests.post")
    def test_acquires_token_then_sends_and_returns_count(self, mock_post):
        mock_post.side_effect = [_fake_token_response(), _fake_send_response()]
        backend = MicrosoftGraphEmailBackend()

        sent = backend.send_messages([self._message()])

        self.assertEqual(sent, 1)
        self.assertEqual(mock_post.call_count, 2)

        token_call, send_call = mock_post.call_args_list
        self.assertIn("tenant-id", token_call.args[0])
        self.assertEqual(token_call.kwargs["data"]["client_id"], "client-id")
        self.assertEqual(token_call.kwargs["data"]["client_secret"], "client-secret")

        self.assertIn("admin@wgtk.co.uk", send_call.args[0])
        self.assertEqual(send_call.kwargs["headers"]["Authorization"], "Bearer fake-token")

    @patch("apps.integrations.graph_email_backend.requests.post")
    def test_send_payload_sets_from_subject_body_and_recipients(self, mock_post):
        mock_post.side_effect = [_fake_token_response(), _fake_send_response()]
        backend = MicrosoftGraphEmailBackend()

        backend.send_messages([self._message(subject="Hi", body="Body text", to=["a@x.com", "b@x.com"])])

        _, send_call = mock_post.call_args_list
        payload = send_call.kwargs["json"]["message"]
        self.assertEqual(payload["subject"], "Hi")
        self.assertEqual(payload["body"], {"contentType": "Text", "content": "Body text"})
        self.assertEqual(
            payload["toRecipients"],
            [{"emailAddress": {"address": "a@x.com"}}, {"emailAddress": {"address": "b@x.com"}}],
        )
        self.assertEqual(payload["from"], {"emailAddress": {"address": "parts@wgtk.co.uk"}})

    @patch("apps.integrations.graph_email_backend.requests.post")
    def test_send_payload_base64_encodes_attachments(self, mock_post):
        mock_post.side_effect = [_fake_token_response(), _fake_send_response()]
        backend = MicrosoftGraphEmailBackend()

        message = self._message()
        message.attach("sheet.xlsx", b"binary-content", "application/vnd.ms-excel")
        backend.send_messages([message])

        _, send_call = mock_post.call_args_list
        attachments = send_call.kwargs["json"]["message"]["attachments"]
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["name"], "sheet.xlsx")
        self.assertEqual(attachments[0]["@odata.type"], "#microsoft.graph.fileAttachment")
        import base64

        self.assertEqual(base64.b64decode(attachments[0]["contentBytes"]), b"binary-content")

    @override_settings(MS_GRAPH_MAIL_FROM="")
    @patch("apps.integrations.graph_email_backend.requests.post")
    def test_omits_from_override_when_not_configured(self, mock_post):
        mock_post.side_effect = [_fake_token_response(), _fake_send_response()]
        backend = MicrosoftGraphEmailBackend()

        backend.send_messages([self._message()])

        _, send_call = mock_post.call_args_list
        self.assertNotIn("from", send_call.kwargs["json"]["message"])

    @patch("apps.integrations.graph_email_backend.requests.post")
    def test_returns_zero_for_empty_message_list(self, mock_post):
        backend = MicrosoftGraphEmailBackend()
        self.assertEqual(backend.send_messages([]), 0)
        mock_post.assert_not_called()

    @patch("apps.integrations.graph_email_backend.requests.post")
    def test_raises_on_send_failure_by_default(self, mock_post):
        failing_response = MagicMock()
        failing_response.raise_for_status.side_effect = Exception("boom")
        mock_post.side_effect = [_fake_token_response(), failing_response]
        backend = MicrosoftGraphEmailBackend()

        with self.assertRaises(Exception):
            backend.send_messages([self._message()])

    @patch("apps.integrations.graph_email_backend.requests.post")
    def test_fail_silently_suppresses_send_errors(self, mock_post):
        failing_response = MagicMock()
        failing_response.raise_for_status.side_effect = Exception("boom")
        mock_post.side_effect = [_fake_token_response(), failing_response]
        backend = MicrosoftGraphEmailBackend(fail_silently=True)

        sent = backend.send_messages([self._message()])
        self.assertEqual(sent, 0)


class MockPhotoStorageTests(TestCase):
    def test_upload_saves_file_and_returns_a_url(self):
        storage = MockPhotoStorage()
        url = storage.upload(
            report_id="496390", stage="before", filename="site.jpg",
            content=b"fake-image-bytes", content_type="image/jpeg",
        )
        self.assertIn("job_photos/496390/before/", url)
        self.assertTrue(url.endswith("site.jpg"))

    def test_upload_namespaces_by_report_and_stage_to_avoid_collisions(self):
        # Unlike Handl's own flat /Uploads/ folder (confirmed live to
        # collide on same-named files), two different reports/stages
        # uploading a same-named photo must not overwrite each other.
        storage = MockPhotoStorage()
        url_a = storage.upload(
            report_id="1", stage="before", filename="photo.jpg",
            content=b"aaa", content_type="image/jpeg",
        )
        url_b = storage.upload(
            report_id="2", stage="after", filename="photo.jpg",
            content=b"bbb", content_type="image/jpeg",
        )
        self.assertNotEqual(url_a, url_b)

    def test_upload_sanitizes_path_traversal_in_filename(self):
        storage = MockPhotoStorage()
        url = storage.upload(
            report_id="496390", stage="before", filename="../../etc/passwd",
            content=b"x", content_type="image/jpeg",
        )
        self.assertNotIn("..", url)
        self.assertIn("job_photos/496390/before/", url)


class GetPhotoStorageTests(TestCase):
    @override_settings(AZURE_STORAGE_CONNECTION_STRING="")
    def test_returns_mock_when_no_connection_string(self):
        self.assertIsInstance(get_photo_storage(), MockPhotoStorage)

    @override_settings(AZURE_STORAGE_CONNECTION_STRING="DefaultEndpointsProtocol=https;AccountName=x;AccountKey=y")
    def test_returns_azure_when_connection_string_set(self):
        self.assertIsInstance(get_photo_storage(), AzureBlobPhotoStorage)
