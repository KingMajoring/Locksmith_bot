from datetime import date, datetime, timedelta, timezone as dt_timezone
from unittest.mock import MagicMock, patch

from django.core.mail import EmailMessage
from django.test import TestCase, override_settings

from .google_maps import MockGoogleMapsClient, RealGoogleMapsClient, get_google_maps_client, static_map_url
from .graph_email_backend import MicrosoftGraphEmailBackend
from .handl import MockHandlClient, SQLHandlClient, get_handl_client
from .models import GoogleMapsSettings, OptimoSettings, TeamsShiftsSettings
from .optimo import MockOptimoClient, RealOptimoClient, get_optimo_client
from .teams_shifts import (
    MockTeamsShiftsClient,
    RealTeamsShiftsClient,
    get_teams_shifts_client,
)
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
            self.assertTrue(job.vehicle_address)
            self.assertIsNotNone(job.vehicle_latitude)
            self.assertIsNotNone(job.vehicle_longitude)
            self.assertIsNotNone(job.quoted_price)

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

    def test_get_future_locksmith_attendances_is_deterministic(self):
        # available_from is anchored to the real current time, so it
        # ticks a little between the two calls below — compare
        # everything else, which is seeded purely off soter_locksmith_id.
        first = self.client.get_future_locksmith_attendances()
        second = self.client.get_future_locksmith_attendances()
        self.assertEqual(
            [(a.soter_locksmith_id, a.vehicle_postcode, a.vehicle_reg) for a in first],
            [(a.soter_locksmith_id, a.vehicle_postcode, a.vehicle_reg) for a in second],
        )

    def test_get_future_locksmith_attendances_only_future_dates(self):
        from django.utils import timezone

        attendances = self.client.get_future_locksmith_attendances()
        self.assertTrue(attendances)
        now = timezone.localtime(timezone.now()).replace(tzinfo=None)
        for attendance in attendances:
            self.assertGreater(attendance.available_from, now)
            self.assertTrue(attendance.vehicle_postcode)

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

    def test_list_all_parts_matches_catalogue(self):
        self.assertEqual(self.client.list_all_parts(), self.client._CATALOGUE)

    def test_record_client_supplied_disposal_true_for_known_sku(self):
        self.assertTrue(
            self.client.record_client_supplied_disposal(
                "885",
                "496390",
                "TK-100",
                "Transponder key blank",
                1,
                actioned_by_user_id=522,
                locksmith_display_name="WGTK - Dean S (V)",
            )
        )

    def test_record_client_supplied_disposal_false_for_unknown_sku(self):
        self.assertFalse(
            self.client.record_client_supplied_disposal(
                "885",
                "496390",
                "XYZ-999",
                "Some client part",
                1,
                actioned_by_user_id=522,
                locksmith_display_name="WGTK - Dean S (V)",
            )
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


class HandlNowTests(TestCase):
    """_handl_now() writes timestamps Handl displays as-is with no
    timezone conversion of its own — confirmed live that plain UTC
    (datetime.utcnow(), what this used to be) showed up in Handl's UI a
    full hour behind real UK time whenever BST is in effect."""

    def test_returns_naive_uk_local_time_during_bst(self):
        from .handl import _handl_now

        # 2026-09-10 10:00 UTC is during BST, so UK local is 11:00.
        aware_utc = datetime(2026, 9, 10, 10, 0, 0, tzinfo=dt_timezone.utc)
        with patch("django.utils.timezone.now", return_value=aware_utc):
            result = _handl_now()

        self.assertIsNone(result.tzinfo)
        self.assertEqual(result, datetime(2026, 9, 10, 11, 0, 0))

    def test_returns_naive_uk_local_time_outside_bst(self):
        from .handl import _handl_now

        # 2026-01-10 10:00 UTC is outside BST, so UK local matches UTC.
        aware_utc = datetime(2026, 1, 10, 10, 0, 0, tzinfo=dt_timezone.utc)
        with patch("django.utils.timezone.now", return_value=aware_utc):
            result = _handl_now()

        self.assertIsNone(result.tzinfo)
        self.assertEqual(result, datetime(2026, 1, 10, 10, 0, 0))


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

    def test_get_expected_stock_falls_back_to_last_purchase_cost(self):
        """Regression test: DIYB36, reported live as showing a real
        12-unit stock-check variance but £0 impact — every one of its
        Inventory_Stock batches has Quantity=0 even when priced, so the
        primary basis returns no row for it at all (not a NULL-cost
        row, no row). Falls back to Inventory_Orders.CostPerUnit, which
        the office's own "last purchased cost per unit" screen confirms
        is a real, reliable price for it."""
        qty_rows = [{"part_code": "DIYB36", "expected_qty": 8}]
        primary_cost_rows = []  # no Inventory_Stock batch qualifies
        fallback_cost_rows = [{"part_code": "DIYB36", "unit_cost": 0.70}]
        client = SQLHandlClient()
        fake_conn = _fake_connection_multi(qty_rows, primary_cost_rows, fallback_cost_rows)
        with patch.object(client, "_connection", return_value=fake_conn):
            expected = client.get_expected_stock(["42"], ["DIYB36"])

        self.assertEqual(expected["DIYB36"].unit_cost, 0.70)

        cursor = fake_conn.cursor.return_value
        fallback_query = cursor.execute.call_args_list[2][0][0]
        self.assertIn("Inventory_Orders", fallback_query)
        self.assertIn("Inventory_Order_Requests", fallback_query)
        self.assertIn("CostPerUnit", fallback_query)

    def test_get_expected_stock_does_not_fall_back_when_primary_basis_has_the_sku(self):
        qty_rows = [{"part_code": "TK-100", "expected_qty": 6}]
        cost_rows = [{"part_code": "TK-100", "unit_cost": 12.5}]
        client = SQLHandlClient()
        fake_conn = _fake_connection_multi(qty_rows, cost_rows)
        with patch.object(client, "_connection", return_value=fake_conn):
            client.get_expected_stock(["42"], ["TK-100"])

        # Only two execute() calls — no fallback query issued.
        cursor = fake_conn.cursor.return_value
        self.assertEqual(cursor.execute.call_count, 2)

    def test_get_expected_stock_returns_empty_dict_for_no_codes(self):
        client = SQLHandlClient()
        self.assertEqual(client.get_expected_stock(["42"], []), {})

    def test_get_expected_stock_builds_placeholders_for_ids_and_codes(self):
        fake_conn = _fake_connection_multi([], [], [])
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
        fake_conn = _fake_connection_multi([], [], [])
        client = SQLHandlClient()
        with patch.object(client, "_connection", return_value=fake_conn):
            client.get_expected_stock(["42"], ["TK-100"])
        cursor = fake_conn.cursor.return_value
        qty_query = cursor.execute.call_args_list[0][0][0]
        self.assertNotIn("Inventory_Stock", qty_query)

    def test_get_expected_stock_cost_query_is_not_scoped_to_locksmith(self):
        fake_conn = _fake_connection_multi([], [], [])
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
        fake_conn = _fake_connection_multi([], [], [])
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
        fake_conn = _fake_connection_multi([], [], [])
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
        fake_conn = _fake_connection_multi([], [], [])
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
                "VehicleReg": "AB17 CDE",
                "VehicleVIN": "SJNFAAJ11U1234567",
                "KeyType": "Car",
                "SpareKey": False,
                "LossEvent": "Lost Keys",
                "SuppliedService": "Key Programming",
                "NetCost": 145.5,
                "QuotedPrice": 60.0,
                "ClientName": "Sarah Jones",
                "OrganisationName": None,
                "ClientPhone": "07700900123",
                "BrokerName": "Admiral",
                "DetailOfLoss": "Lost the only key on a dog walk.",
                "VehicleAddress1": "42 Corsehill Crescent",
                "VehicleAddress2": "",
                "VehicleAddress3": "Hamilton",
                "VehicleAddress4": "Lanarkshire",
                "VehicleAddressLatitude": 55.7536673,
                "VehicleAddressLongitude": -4.062251,
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
        self.assertIn("Policy_LocksmithDetails", query)
        self.assertIn("Policy_Financial", query)
        self.assertIn("Policy_HolderDetails", query)
        self.assertIn("Policy_BrokersDetails", query)
        self.assertIn("SubBrokers", query)
        self.assertIn("Policy_ClaimDetails_Key", query)

        job = details["496390"]
        self.assertEqual(job.make, "NISSAN")
        self.assertEqual(job.year, "2017")
        self.assertEqual(job.reg, "AB17 CDE")
        self.assertEqual(job.vin, "SJNFAAJ11U1234567")
        self.assertEqual(job.service_type, "Car")
        self.assertEqual(job.loss_type, "Lost Keys")
        self.assertEqual(job.supplied_service, "Key Programming")
        self.assertEqual(job.net_cost, 145.5)
        self.assertEqual(job.quoted_price, 60.0)
        self.assertIs(job.spare_key, False)
        self.assertEqual(job.vehicle_address, "42 Corsehill Crescent, Hamilton, Lanarkshire")
        self.assertEqual(job.vehicle_latitude, 55.7536673)
        self.assertEqual(job.vehicle_longitude, -4.062251)
        self.assertEqual(job.client_name, "Sarah Jones")
        self.assertEqual(job.client_phone, "07700900123")
        self.assertEqual(job.broker, "Admiral")
        self.assertEqual(job.detail_of_loss, "Lost the only key on a dog walk.")

    def test_get_job_details_null_detail_of_loss_maps_to_empty_string(self):
        rows = [
            {
                "ReportID": "496390",
                "Make": "NISSAN",
                "Model": "X-TRAIL",
                "yearOfManufacture": 2017,
                "VehicleReg": "AB17 CDE",
                "VehicleVIN": "SJNFAAJ11U1234567",
                "KeyType": "Car",
                "SpareKey": False,
                "LossEvent": "Lost Keys",
                "SuppliedService": "Key Programming",
                "NetCost": 145.5,
                "QuotedPrice": 60.0,
                "ClientName": "Sarah Jones",
                "OrganisationName": None,
                "ClientPhone": "07700900123",
                "BrokerName": "Admiral",
                "DetailOfLoss": None,
                "VehicleAddress1": "42 Corsehill Crescent",
                "VehicleAddress2": "",
                "VehicleAddress3": "Hamilton",
                "VehicleAddress4": "Lanarkshire",
                "VehicleAddressLatitude": 55.7536673,
                "VehicleAddressLongitude": -4.062251,
            }
        ]
        fake_conn = _fake_connection(rows)
        client = SQLHandlClient()
        with patch.object(client, "_connection", return_value=fake_conn):
            details = client.get_job_details(["496390"])
        self.assertEqual(details["496390"].detail_of_loss, "")

    def test_get_job_details_falls_back_to_organisation_name_for_trade_clients(self):
        rows = [
            {
                "ReportID": "496390",
                "Make": "NISSAN",
                "Model": "X-TRAIL",
                "yearOfManufacture": 2017,
                "VehicleReg": "AB17 CDE",
                "VehicleVIN": "SJNFAAJ11U1234567",
                "KeyType": "Car",
                "SpareKey": False,
                "LossEvent": "Lost Keys",
                "SuppliedService": "Key Programming",
                "NetCost": 145.5,
                "QuotedPrice": 60.0,
                "ClientName": "",
                "OrganisationName": "Acme Fleet Ltd",
                "ClientPhone": "",
                "BrokerName": None,
                "DetailOfLoss": "Lost the only key on a dog walk.",
                "VehicleAddress1": "42 Corsehill Crescent",
                "VehicleAddress2": "",
                "VehicleAddress3": "Hamilton",
                "VehicleAddress4": "Lanarkshire",
                "VehicleAddressLatitude": 55.7536673,
                "VehicleAddressLongitude": -4.062251,
            }
        ]
        fake_conn = _fake_connection(rows)
        client = SQLHandlClient()
        with patch.object(client, "_connection", return_value=fake_conn):
            details = client.get_job_details(["496390"])

        job = details["496390"]
        self.assertEqual(job.client_name, "Acme Fleet Ltd")
        self.assertEqual(job.client_phone, "")
        self.assertEqual(job.broker, "")

    def test_get_job_details_null_vehicle_address_maps_to_empty_string(self):
        rows = [
            {
                "ReportID": "496390",
                "Make": "NISSAN",
                "Model": "X-TRAIL",
                "yearOfManufacture": 2017,
                "VehicleReg": "AB17 CDE",
                "VehicleVIN": "SJNFAAJ11U1234567",
                "KeyType": "Car",
                "SpareKey": False,
                "LossEvent": "Lost Keys",
                "SuppliedService": "Key Programming",
                "NetCost": 100.0,
                "QuotedPrice": 60.0,
                "ClientName": "Sarah Jones",
                "OrganisationName": None,
                "ClientPhone": "07700900123",
                "BrokerName": "Admiral",
                "DetailOfLoss": "Lost the only key on a dog walk.",
                "VehicleAddress1": None,
                "VehicleAddress2": None,
                "VehicleAddress3": None,
                "VehicleAddress4": None,
                "VehicleAddressLatitude": None,
                "VehicleAddressLongitude": None,
            }
        ]
        fake_conn = _fake_connection(rows)
        client = SQLHandlClient()
        with patch.object(client, "_connection", return_value=fake_conn):
            details = client.get_job_details(["496390"])
        job = details["496390"]
        self.assertEqual(job.vehicle_address, "")
        self.assertIsNone(job.vehicle_latitude)
        self.assertIsNone(job.vehicle_longitude)

    def test_get_job_details_blank_string_coordinates_do_not_raise(self):
        # Regression test: VehicleAddressLatitude/Longitude are varchar
        # columns in the real DB, and a job with no vehicle location
        # captured has '' there, not NULL — confirmed live this took
        # down job details for an ENTIRE dashboard batch (float('')
        # raises ValueError, uncaught here, so the dashboard's own outer
        # except blanked every job in the request, not just this one).
        rows = [
            {
                "ReportID": "496390",
                "Make": "NISSAN",
                "Model": "X-TRAIL",
                "yearOfManufacture": 2017,
                "VehicleReg": "AB17 CDE",
                "VehicleVIN": "SJNFAAJ11U1234567",
                "KeyType": "Car",
                "SpareKey": False,
                "LossEvent": "Lost Keys",
                "SuppliedService": "Key Programming",
                "NetCost": "",
                "QuotedPrice": "",
                "ClientName": "Sarah Jones",
                "OrganisationName": None,
                "ClientPhone": "07700900123",
                "BrokerName": "Admiral",
                "DetailOfLoss": "Lost the only key on a dog walk.",
                "VehicleAddress1": "",
                "VehicleAddress2": "",
                "VehicleAddress3": "",
                "VehicleAddress4": "",
                "VehicleAddressLatitude": "",
                "VehicleAddressLongitude": "",
            }
        ]
        fake_conn = _fake_connection(rows)
        client = SQLHandlClient()
        with patch.object(client, "_connection", return_value=fake_conn):
            details = client.get_job_details(["496390"])
        job = details["496390"]
        self.assertEqual(job.make, "NISSAN")
        self.assertEqual(job.vehicle_address, "")
        self.assertIsNone(job.vehicle_latitude)
        self.assertIsNone(job.vehicle_longitude)
        self.assertIsNone(job.net_cost)
        self.assertIsNone(job.quoted_price)

    def test_get_job_details_keys_result_by_the_requested_report_id_string(self):
        # Regression test: confirmed live against a real dashboard job
        # left completely blank (no exception, nothing logged) because
        # ReportID is a plain int column — the DB happily matches a
        # zero-padded WHERE ... IN (...) value via its own implicit
        # conversion, but re-stringifying the returned int for the
        # result dict's key (str(row["ReportID"])) silently drops the
        # leading zero, so a caller looking the same zero-padded string
        # back up (job_details.get("039364")) got a plain dict miss.
        rows = [
            {
                "ReportID": 39364,
                "Make": "FORD",
                "Model": "TRANSIT",
                "yearOfManufacture": 2008,
                "VehicleReg": "YA08 LCJ",
                "VehicleVIN": "WF0XXXTTFX8C84491",
                "KeyType": "Car",
                "SpareKey": False,
                "LossEvent": "Lost",
                "SuppliedService": "",
                "NetCost": None,
                "QuotedPrice": 60.0,
                "ClientName": "Sarah Jones",
                "OrganisationName": None,
                "ClientPhone": "07700900123",
                "BrokerName": "Admiral",
                "DetailOfLoss": "Lost the only key on a dog walk.",
                "VehicleAddress1": "42 Corsehill Crescent",
                "VehicleAddress2": "",
                "VehicleAddress3": "Hamilton",
                "VehicleAddress4": "Lanarkshire",
                "VehicleAddressLatitude": 55.7536673,
                "VehicleAddressLongitude": -4.062251,
            }
        ]
        fake_conn = _fake_connection(rows)
        client = SQLHandlClient()
        with patch.object(client, "_connection", return_value=fake_conn):
            details = client.get_job_details(["039364"])

        self.assertIn("039364", details)
        job = details["039364"]
        self.assertEqual(job.report_id, "039364")
        self.assertEqual(job.make, "FORD")
        self.assertEqual(job.vehicle_address, "42 Corsehill Crescent, Hamilton, Lanarkshire")

    def test_get_job_details_null_net_cost_maps_to_none(self):
        rows = [
            {
                "ReportID": "496390",
                "Make": "NISSAN",
                "Model": "X-TRAIL",
                "yearOfManufacture": 2017,
                "VehicleReg": "AB17 CDE",
                "VehicleVIN": "SJNFAAJ11U1234567",
                "KeyType": "Car",
                "SpareKey": None,
                "LossEvent": "Lost Keys",
                "SuppliedService": "Key Programming",
                "NetCost": None,
                "QuotedPrice": None,
                "ClientName": "Sarah Jones",
                "OrganisationName": None,
                "ClientPhone": "07700900123",
                "BrokerName": "Admiral",
                "DetailOfLoss": "Lost the only key on a dog walk.",
                "VehicleAddress1": "42 Corsehill Crescent",
                "VehicleAddress2": "",
                "VehicleAddress3": "Hamilton",
                "VehicleAddress4": "Lanarkshire",
                "VehicleAddressLatitude": 55.7536673,
                "VehicleAddressLongitude": -4.062251,
            }
        ]
        fake_conn = _fake_connection(rows)
        client = SQLHandlClient()
        with patch.object(client, "_connection", return_value=fake_conn):
            details = client.get_job_details(["496390"])
        self.assertIsNone(details["496390"].net_cost)
        self.assertIsNone(details["496390"].quoted_price)
        self.assertIsNone(details["496390"].spare_key)

    def test_get_job_details_spare_key_true_maps_to_true(self):
        rows = [
            {
                "ReportID": "496390",
                "Make": "NISSAN",
                "Model": "X-TRAIL",
                "yearOfManufacture": 2017,
                "VehicleReg": "AB17 CDE",
                "VehicleVIN": "SJNFAAJ11U1234567",
                "KeyType": "Car",
                "SpareKey": True,
                "LossEvent": "Lost Keys",
                "SuppliedService": "Key Programming",
                "NetCost": 100.0,
                "QuotedPrice": 60.0,
                "ClientName": "Sarah Jones",
                "OrganisationName": None,
                "ClientPhone": "07700900123",
                "BrokerName": "Admiral",
                "DetailOfLoss": "Lost the only key on a dog walk.",
                "VehicleAddress1": "42 Corsehill Crescent",
                "VehicleAddress2": "",
                "VehicleAddress3": "Hamilton",
                "VehicleAddress4": "Lanarkshire",
                "VehicleAddressLatitude": 55.7536673,
                "VehicleAddressLongitude": -4.062251,
            }
        ]
        fake_conn = _fake_connection(rows)
        client = SQLHandlClient()
        with patch.object(client, "_connection", return_value=fake_conn):
            details = client.get_job_details(["496390"])
        self.assertIs(details["496390"].spare_key, True)

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

    def test_get_future_locksmith_attendances_maps_rows(self):
        rows = [
            {
                "ReportID": 501179,
                "LocksmithID": 1204,
                "LocksmithName": "WGTK - Andrew S",
                "AvailableFromDate": datetime(2026, 9, 20, 9, 0),
                "VehiclePostCode": "NR14 8PL",
                "VehicleReg": "AB20 CDE",
            }
        ]
        fake_conn = _fake_connection(rows)
        client = SQLHandlClient()
        with patch.object(client, "_connection", return_value=fake_conn):
            attendances = client.get_future_locksmith_attendances()

        cursor = fake_conn.cursor.return_value
        query = cursor.execute.call_args_list[0][0][0]
        self.assertIn("Policy_LocksmithDetails", query)
        self.assertIn("StatusID <> 15", query)
        self.assertIn("Selected = 1", query)

        self.assertEqual(len(attendances), 1)
        attendance = attendances[0]
        self.assertEqual(attendance.report_id, "501179")
        self.assertEqual(attendance.soter_locksmith_id, "1204")
        self.assertEqual(attendance.locksmith_name, "WGTK - Andrew S")
        self.assertEqual(attendance.vehicle_postcode, "NR14 8PL")
        self.assertEqual(attendance.vehicle_reg, "AB20 CDE")

    def test_get_future_locksmith_attendances_skips_rows_with_no_locksmith(self):
        rows = [
            {
                "ReportID": 501179,
                "LocksmithID": None,
                "LocksmithName": None,
                "AvailableFromDate": datetime(2026, 9, 20, 9, 0),
                "VehiclePostCode": "NR14 8PL",
                "VehicleReg": "AB20 CDE",
            }
        ]
        fake_conn = _fake_connection(rows)
        client = SQLHandlClient()
        with patch.object(client, "_connection", return_value=fake_conn):
            attendances = client.get_future_locksmith_attendances()
        self.assertEqual(attendances, [])

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

    def test_list_all_parts_maps_rows_with_no_locksmith_filter(self):
        rows = [
            {"part_code": "TK-100", "part_name": "Transponder key blank"},
            {"part_code": "TK-107", "part_name": "Van lock cylinder"},
        ]
        fake_conn = _fake_connection(rows)
        client = SQLHandlClient()
        with patch.object(client, "_connection", return_value=fake_conn):
            parts = client.list_all_parts()

        self.assertEqual(parts, [("TK-100", "Transponder key blank"), ("TK-107", "Van lock cylinder")])
        query = fake_conn.cursor.return_value.execute.call_args[0][0]
        self.assertIn("Inventory_Parts", query)
        self.assertNotIn("Inventory_Locksmith_Stock", query)

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

    def _record_client_supplied_disposal(self, client, part_code="TK-100"):
        return client.record_client_supplied_disposal(
            "885",
            "496390",
            part_code,
            "Transponder key blank",
            1,
            actioned_by_user_id=517,
            locksmith_display_name="WGTK - Blain H (V)",
        )

    def test_record_client_supplied_disposal_inserts_without_touching_locksmith_stock(self):
        fake_conn = MagicMock()
        fake_conn.__enter__.return_value = fake_conn
        fake_conn.__exit__.return_value = False
        cursor = fake_conn.cursor.return_value
        cursor.fetchone.return_value = {"Id": 555}

        client = SQLHandlClient()
        with patch.object(client, "_write_connection", return_value=fake_conn):
            result = self._record_client_supplied_disposal(client)

        self.assertTrue(result)
        calls = cursor.execute.call_args_list
        queries = [c[0][0] for c in calls]
        self.assertFalse(any("Inventory_Locksmith_Stock" in q for q in queries))
        self.assertFalse(any(q.strip().upper().startswith("UPDATE") for q in queries))

        insert_query, insert_params = next(
            c[0] for c in calls if "INSERT INTO Inventory_Disposals" in c[0][0]
        )
        self.assertIn("NEWID()", insert_query)
        self.assertEqual(insert_params["lid"], 885)
        self.assertEqual(insert_params["report_id"], "496390")
        self.assertEqual(insert_params["stock_id"], 555)
        self.assertEqual(insert_params["qty"], 1)
        self.assertEqual(insert_params["created_by"], 517)

        history_query, history_params = next(
            c[0] for c in calls if "INSERT INTO Policy_History" in c[0][0]
        )
        self.assertIn("supplied by the client", history_params["notes"])
        self.assertEqual(history_params["actioned_by"], 517)

        fake_conn.commit.assert_called_once()

    def test_record_client_supplied_disposal_returns_false_when_sku_unknown(self):
        fake_conn = MagicMock()
        fake_conn.__enter__.return_value = fake_conn
        fake_conn.__exit__.return_value = False
        fake_conn.cursor.return_value.fetchone.return_value = None

        client = SQLHandlClient()
        with patch.object(client, "_write_connection", return_value=fake_conn):
            result = self._record_client_supplied_disposal(client, part_code="XYZ-999")

        self.assertFalse(result)
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

    def _set_stock_quantity(self, client, quantity, part_code="TK-100"):
        client.set_locksmith_stock_quantity(
            ["885", "887"],
            part_code,
            quantity,
            actioned_by_user_id=517,
            locksmith_display_name="WGTK - Blain H",
        )

    def test_set_locksmith_stock_quantity_updates_single_row(self):
        fake_conn = MagicMock()
        fake_conn.__enter__.return_value = fake_conn
        fake_conn.__exit__.return_value = False
        cursor = fake_conn.cursor.return_value
        cursor.fetchall.return_value = [{"Id": "row-1", "Quantity": 3}]

        client = SQLHandlClient()
        with patch.object(client, "_write_connection", return_value=fake_conn):
            self._set_stock_quantity(client, 8)

        select_query, select_params = cursor.execute.call_args_list[0][0]
        self.assertIn("Inventory_Locksmith_Stock", select_query)
        self.assertIn("ORDER BY ils.Quantity DESC", select_query)
        self.assertEqual(select_params["lid0"], 885)
        self.assertEqual(select_params["lid1"], 887)
        self.assertEqual(select_params["sku"], "TK-100")

        update_query, update_params = cursor.execute.call_args_list[1][0]
        self.assertIn("UPDATE Inventory_Locksmith_Stock", update_query)
        self.assertEqual(update_params, {"qty": 8, "id": "row-1"})

        # Only one UPDATE — nothing else to zero out.
        update_calls = [c for c in cursor.execute.call_args_list if "UPDATE" in c[0][0]]
        self.assertEqual(len(update_calls), 1)
        fake_conn.commit.assert_called_once()

    def test_set_locksmith_stock_quantity_zeros_other_rows(self):
        fake_conn = MagicMock()
        fake_conn.__enter__.return_value = fake_conn
        fake_conn.__exit__.return_value = False
        cursor = fake_conn.cursor.return_value
        cursor.fetchall.return_value = [
            {"Id": "row-big", "Quantity": 5},
            {"Id": "row-small", "Quantity": 2},
            {"Id": "row-empty", "Quantity": 0},
        ]

        client = SQLHandlClient()
        with patch.object(client, "_write_connection", return_value=fake_conn):
            self._set_stock_quantity(client, 10)

        update_calls = [c for c in cursor.execute.call_args_list if "UPDATE" in c[0][0]]
        self.assertEqual(len(update_calls), 2)
        self.assertEqual(update_calls[0][0][1], {"qty": 10, "id": "row-big"})
        self.assertEqual(update_calls[1][0][1], {"id": "row-small"})

    def test_set_locksmith_stock_quantity_raises_when_no_row_found(self):
        fake_conn = MagicMock()
        fake_conn.__enter__.return_value = fake_conn
        fake_conn.__exit__.return_value = False
        fake_conn.cursor.return_value.fetchall.return_value = []
        client = SQLHandlClient()
        with patch.object(client, "_write_connection", return_value=fake_conn):
            with self.assertRaises(ValueError):
                self._set_stock_quantity(client, 8)
        fake_conn.commit.assert_not_called()


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


class MockOptimoClientListOrdersTests(TestCase):
    def test_every_order_has_a_stop_number_and_arrival_time(self):
        summaries = MockOptimoClient().list_orders_for_date(date(2026, 9, 14))
        self.assertTrue(summaries)
        for summary in summaries:
            self.assertIsNotNone(summary.stop_number)
            self.assertIsNotNone(summary.arrival_time_start)

    def test_stop_numbers_are_not_already_in_list_order(self):
        # Guards against list_orders_for_date accidentally handing back
        # stop_number == index+1 — real search_orders results aren't
        # pre-sorted by route position, so a test built on already-sorted
        # data wouldn't actually exercise the dashboard's own sort.
        summaries = MockOptimoClient().list_orders_for_date(date(2026, 9, 14))
        stop_numbers = [s.stop_number for s in summaries]
        self.assertNotEqual(stop_numbers, sorted(stop_numbers))


class RealOptimoClientListOrdersTests(TestCase):
    def _mock_response(self, orders):
        response = MagicMock()
        response.json.return_value = {"orders": orders}
        return response

    @patch("requests.post")
    def test_parses_stop_number_and_arrival_time(self, mock_post):
        mock_post.return_value = self._mock_response([
            {
                "data": {"orderNo": "496390_2026-09-14"},
                "scheduleInformation": {
                    "driverSerial": "011",
                    "distance": 1200,
                    "travelTime": 300,
                    "stopNumber": 3,
                    "arrivalTimeStart": {"utcTime": "2026-09-14T08:30:00"},
                },
            },
        ])
        client = RealOptimoClient("KEY")

        summaries = client.list_orders_for_date(date(2026, 9, 14))

        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].stop_number, 3)
        self.assertEqual(
            summaries[0].arrival_time_start,
            datetime(2026, 9, 14, 8, 30, 0, tzinfo=dt_timezone.utc),
        )

    @patch("requests.post")
    def test_missing_stop_number_and_arrival_time_are_none(self, mock_post):
        mock_post.return_value = self._mock_response([
            {
                "data": {"orderNo": "496390_2026-09-14"},
                "scheduleInformation": {"driverSerial": "011"},
            },
        ])
        client = RealOptimoClient("KEY")

        summaries = client.list_orders_for_date(date(2026, 9, 14))

        self.assertEqual(len(summaries), 1)
        self.assertIsNone(summaries[0].stop_number)
        self.assertIsNone(summaries[0].arrival_time_start)


class RealOptimoClientCompletionStatusTests(TestCase):
    def _mock_response(self, updates):
        # The API reference's documented response example uses "orders"
        # as the top-level key, but the live API actually returns
        # "updates" — confirmed live (see update_completion_status).
        response = MagicMock()
        response.json.return_value = {"success": True, "updates": updates}
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
    def test_strips_microseconds_optimo_rejects_as_not_isodatetime(self, mock_post):
        # Confirmed live: timezone.now() (microsecond precision) got
        # "'...' is not a 'isodatetime'" back from Optimo.
        mock_post.return_value = self._mock_response([{"success": True}])
        client = RealOptimoClient("KEY")
        start = datetime(2026, 9, 4, 14, 14, 8, 863645, tzinfo=dt_timezone.utc)

        client.update_completion_status("496390", "servicing", start_time=start)

        data = mock_post.call_args.kwargs["json"]["updates"][0]["data"]
        self.assertEqual(data["startTime"], {"utcTime": "2026-09-04T14:14:08"})

    @patch("requests.post")
    def test_raises_when_optimo_rejects_the_update(self, mock_post):
        response = MagicMock()
        response.json.return_value = {
            "success": False,
            "updates": [
                {"success": False, "orderNo": "XXX", "message": "not found", "code": "ERR_ORD_NOT_FOUND"}
            ],
        }
        mock_post.return_value = response
        client = RealOptimoClient("KEY")

        with self.assertRaisesMessage(ValueError, "not found [ERR_ORD_NOT_FOUND]"):
            client.update_completion_status("XXX", "on_route")

    @patch("requests.post")
    def test_raises_when_no_updates_returned(self, mock_post):
        mock_post.return_value = self._mock_response([])
        client = RealOptimoClient("KEY")
        with self.assertRaises(ValueError):
            client.update_completion_status("496390", "on_route")

    @patch("requests.post")
    def test_rejection_with_no_message_dumps_raw_response_for_diagnosis(self, mock_post):
        # Seen live: Optimo can reject an update with success: false and
        # no message/code at all — the error must still be diagnosable
        # from the logs rather than a dead-end generic string.
        response = MagicMock()
        response.json.return_value = {"success": False, "updates": [{"success": False}]}
        mock_post.return_value = response
        client = RealOptimoClient("KEY")

        with self.assertRaises(ValueError) as ctx:
            client.update_completion_status("496390", "servicing")
        self.assertIn("updates", str(ctx.exception))
        self.assertIn("success", str(ctx.exception))


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


class MockGoogleMapsClientTests(TestCase):
    def test_returns_one_result_per_origin_in_order(self):
        origins = ["NR14 8PL", "IP1 2AB", "CO1 1AA"]
        results = MockGoogleMapsClient().get_distances(origins, 52.6309, 1.2974)
        self.assertEqual([r.origin for r in results], origins)
        for result in results:
            self.assertEqual(result.status, "OK")
            self.assertIsNotNone(result.distance_metres)
            self.assertIsNotNone(result.duration_seconds)

    def test_is_deterministic_per_origin_and_destination(self):
        first = MockGoogleMapsClient().get_distances(["NR14 8PL"], 52.6309, 1.2974)
        second = MockGoogleMapsClient().get_distances(["NR14 8PL"], 52.6309, 1.2974)
        self.assertEqual(first[0].distance_metres, second[0].distance_metres)

    def test_different_destination_gives_different_distance(self):
        a = MockGoogleMapsClient().get_distances(["NR14 8PL"], 52.6309, 1.2974)
        b = MockGoogleMapsClient().get_distances(["NR14 8PL"], 55.7536673, -4.062251)
        self.assertNotEqual(a[0].distance_metres, b[0].distance_metres)

    def test_empty_origins_returns_empty(self):
        self.assertEqual(MockGoogleMapsClient().get_distances([], 52.6309, 1.2974), [])


class LocksmithDistanceTests(TestCase):
    def test_distance_miles_converts_from_metres(self):
        from .google_maps import LocksmithDistance

        result = LocksmithDistance(origin="NR14 8PL", distance_metres=16093.44, duration_seconds=None, status="OK")
        self.assertEqual(result.distance_miles, 10.0)

    def test_duration_minutes_converts_from_seconds(self):
        from .google_maps import LocksmithDistance

        result = LocksmithDistance(origin="NR14 8PL", distance_metres=None, duration_seconds=660, status="OK")
        self.assertEqual(result.duration_minutes, 11)

    def test_none_values_stay_none(self):
        from .google_maps import LocksmithDistance

        result = LocksmithDistance(origin="NR14 8PL", distance_metres=None, duration_seconds=None, status="NOT_FOUND")
        self.assertIsNone(result.distance_miles)
        self.assertIsNone(result.duration_minutes)


class RealGoogleMapsClientTests(TestCase):
    def _mock_response(self, status="OK", rows=None):
        response = MagicMock()
        response.json.return_value = {"status": status, "rows": rows or []}
        return response

    @patch("requests.get")
    def test_parses_distance_and_duration_per_origin(self, mock_get):
        mock_get.return_value = self._mock_response(rows=[
            {"elements": [{"status": "OK", "distance": {"value": 8369}, "duration": {"value": 720}}]},
            {"elements": [{"status": "OK", "distance": {"value": 3218}, "duration": {"value": 300}}]},
        ])
        client = RealGoogleMapsClient("KEY")

        results = client.get_distances(["NR14 8PL", "IP1 2AB"], 52.6309, 1.2974)

        self.assertEqual(results[0].origin, "NR14 8PL")
        self.assertEqual(results[0].distance_metres, 8369.0)
        self.assertEqual(results[0].duration_seconds, 720)
        self.assertEqual(results[1].distance_metres, 3218.0)

        params = mock_get.call_args.kwargs["params"]
        self.assertEqual(params["origins"], "NR14 8PL|IP1 2AB")
        self.assertEqual(params["destinations"], "52.6309,1.2974")
        self.assertEqual(params["key"], "KEY")

    @patch("requests.get")
    def test_unresolvable_origin_gets_null_distance_not_an_exception(self, mock_get):
        mock_get.return_value = self._mock_response(rows=[
            {"elements": [{"status": "NOT_FOUND"}]},
        ])
        client = RealGoogleMapsClient("KEY")

        results = client.get_distances(["not a real postcode"], 52.6309, 1.2974)

        self.assertEqual(results[0].status, "NOT_FOUND")
        self.assertIsNone(results[0].distance_metres)
        self.assertIsNone(results[0].duration_seconds)

    @patch("requests.get")
    def test_raises_on_overall_request_failure(self, mock_get):
        mock_get.return_value = self._mock_response(status="REQUEST_DENIED")
        client = RealGoogleMapsClient("KEY")

        with self.assertRaises(ValueError):
            client.get_distances(["NR14 8PL"], 52.6309, 1.2974)

    def test_empty_origins_returns_empty_without_a_request(self):
        client = RealGoogleMapsClient("KEY")
        with patch("requests.get") as mock_get:
            self.assertEqual(client.get_distances([], 52.6309, 1.2974), [])
            mock_get.assert_not_called()

    @patch("requests.get")
    def test_batches_requests_over_25_origins(self, mock_get):
        # Distance Matrix rejects more than 25 origins in one request
        # with MAX_DIMENSIONS_EXCEEDED (confirmed live once enough
        # locksmiths had a home location set) — 30 origins should split
        # into a 25 + 5 pair of requests, not one oversized call.
        origins = [f"PC{i}" for i in range(30)]

        def fake_get(url, params, timeout):
            batch = params["origins"].split("|")
            return self._mock_response(rows=[
                {"elements": [{"status": "OK", "distance": {"value": 1000}, "duration": {"value": 100}}]}
                for _ in batch
            ])

        mock_get.side_effect = fake_get
        client = RealGoogleMapsClient("KEY")

        results = client.get_distances(origins, 52.6309, 1.2974)

        self.assertEqual(mock_get.call_count, 2)
        first_batch = mock_get.call_args_list[0].kwargs["params"]["origins"].split("|")
        second_batch = mock_get.call_args_list[1].kwargs["params"]["origins"].split("|")
        self.assertEqual(len(first_batch), 25)
        self.assertEqual(len(second_batch), 5)
        self.assertEqual([r.origin for r in results], origins)  # stitched back in order

    @patch("requests.get")
    def test_batch_failure_propagates(self, mock_get):
        origins = [f"PC{i}" for i in range(30)]
        mock_get.return_value = self._mock_response(status="OVER_QUERY_LIMIT")
        client = RealGoogleMapsClient("KEY")

        with self.assertRaises(ValueError):
            client.get_distances(origins, 52.6309, 1.2974)


@override_settings(GOOGLE_MAPS_API_KEY="")
class GetGoogleMapsClientTests(TestCase):
    def test_returns_mock_client_when_unconfigured(self):
        self.assertIsInstance(get_google_maps_client(), MockGoogleMapsClient)

    def test_returns_real_client_using_admin_stored_key(self):
        GoogleMapsSettings.objects.create(api_key="admin-set-key")
        client = get_google_maps_client()
        self.assertIsInstance(client, RealGoogleMapsClient)
        self.assertEqual(client._api_key, "admin-set-key")

    @override_settings(GOOGLE_MAPS_API_KEY="app-setting-key")
    def test_falls_back_to_app_setting_when_no_admin_key(self):
        client = get_google_maps_client()
        self.assertIsInstance(client, RealGoogleMapsClient)
        self.assertEqual(client._api_key, "app-setting-key")

    @override_settings(GOOGLE_MAPS_API_KEY="app-setting-key")
    def test_admin_stored_key_takes_priority_over_app_setting(self):
        GoogleMapsSettings.objects.create(api_key="admin-set-key")
        client = get_google_maps_client()
        self.assertEqual(client._api_key, "admin-set-key")


@override_settings(GOOGLE_MAPS_API_KEY="test-key")
class StaticMapUrlTests(TestCase):
    def test_builds_url_with_one_marker_param_per_style(self):
        url = static_map_url([
            ("color:blue|label:J", ["52.63,1.30"]),
            ("color:red", ["NR1 1AA", "IP1 2AB"]),
        ])
        self.assertIn("size=400x300", url)
        self.assertIn("key=test-key", url)
        self.assertIn("markers=color%3Ablue%7Clabel%3AJ%7C52.63%2C1.30", url)
        self.assertIn("markers=color%3Ared%7CNR1+1AA%7CIP1+2AB", url)

    def test_empty_without_an_api_key(self):
        with override_settings(GOOGLE_MAPS_API_KEY=""):
            self.assertEqual(static_map_url([("color:blue", ["52.63,1.30"])]), "")

    def test_empty_with_no_markers(self):
        self.assertEqual(static_map_url([("color:red", [])]), "")

    def test_skips_styles_with_no_locations(self):
        url = static_map_url([
            ("color:blue|label:J", ["52.63,1.30"]),
            ("color:red", []),
        ])
        self.assertEqual(url.count("markers="), 1)


class MockTeamsShiftsClientTests(TestCase):
    def test_deterministic_for_same_date(self):
        client = MockTeamsShiftsClient()
        first = client.list_shifts_for_date(date(2026, 9, 15))
        second = client.list_shifts_for_date(date(2026, 9, 15))
        self.assertEqual(first, second)

    def test_shift_covers_the_requested_date(self):
        client = MockTeamsShiftsClient()
        for shift in client.list_shifts_for_date(date(2026, 9, 15)):
            self.assertEqual(shift.shift_start.date(), date(2026, 9, 15))
            self.assertLess(shift.shift_start, shift.shift_end)
            self.assertIn("@", shift.email)

    def test_date_range_matches_concatenated_single_date_calls(self):
        client = MockTeamsShiftsClient()
        ranged = client.list_shifts_for_date_range(date(2026, 9, 15), date(2026, 9, 17))
        single_days = (
            client.list_shifts_for_date(date(2026, 9, 15))
            + client.list_shifts_for_date(date(2026, 9, 16))
            + client.list_shifts_for_date(date(2026, 9, 17))
        )
        self.assertEqual(ranged, single_days)


def _fake_json_response(payload):
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


class RealTeamsShiftsClientTests(TestCase):
    def _client(self):
        return RealTeamsShiftsClient(
            client_id="client-id", client_secret="client-secret",
            tenant_id="tenant-id", team_id="team-id",
        )

    @patch("requests.get")
    @patch("requests.post")
    def test_maps_shifts_to_emails_via_team_membership(self, mock_post, mock_get):
        mock_post.return_value = _fake_token_response()
        mock_get.side_effect = [
            _fake_json_response({"value": [
                {"id": "user-1", "mail": "andrew.s@wgtk.co.uk"},
                {"id": "user-2", "mail": None, "userPrincipalName": "blain.h@wgtk.co.uk"},
            ]}),
            _fake_json_response({"value": [
                {
                    "userId": "user-1",
                    "sharedShift": {"startDateTime": "2026-09-15T07:00:00Z", "endDateTime": "2026-09-15T16:00:00Z"},
                },
                {
                    "userId": "user-2",
                    "sharedShift": {"startDateTime": "2026-09-15T08:00:00Z", "endDateTime": "2026-09-15T17:00:00Z"},
                },
            ]}),
        ]
        client = self._client()

        shifts = client.list_shifts_for_date(date(2026, 9, 15))

        self.assertEqual({s.email for s in shifts}, {"andrew.s@wgtk.co.uk", "blain.h@wgtk.co.uk"})
        token_call = mock_post.call_args
        self.assertIn("tenant-id", token_call.args[0])
        self.assertEqual(token_call.kwargs["data"]["client_id"], "client-id")

    @patch("requests.get")
    @patch("requests.post")
    def test_shifts_request_is_date_filtered_server_side(self, mock_post, mock_get):
        # Confirmed live: fetching the whole team's unfiltered shift
        # history (months of published shifts across ~28 people) made
        # a Logs Engine lookup hang. A $filter on the shifts request
        # itself keeps this to a narrow window instead of relying on
        # client-side filtering alone to do the work after the fact.
        mock_post.return_value = _fake_token_response()
        mock_get.side_effect = [
            _fake_json_response({"value": []}),
            _fake_json_response({"value": []}),
        ]
        client = self._client()

        client.list_shifts_for_date(date(2026, 9, 15))

        shifts_call_url = mock_get.call_args_list[1].args[0]
        self.assertIn("/teams/team-id/schedule/shifts?$filter=", shifts_call_url)
        self.assertIn("sharedShift/startDateTime", shifts_call_url)
        self.assertIn("2026-09-14", shifts_call_url)  # window starts the day before
        self.assertIn("2026-09-16", shifts_call_url)  # window ends the day after

    @patch("requests.get")
    @patch("requests.post")
    def test_date_range_fetches_once_not_once_per_day(self, mock_post, mock_get):
        # A caller needing several different dates' shifts (e.g. Logs
        # Engine, one per future-job option) must be able to get them
        # all in ONE Graph round trip — looping list_shifts_for_date
        # per date would be exactly the kind of chatty usage that made
        # a lookup hang once already.
        mock_post.return_value = _fake_token_response()
        mock_get.side_effect = [
            _fake_json_response({"value": [{"id": "user-1", "mail": "andrew.s@wgtk.co.uk"}]}),
            _fake_json_response({"value": [
                {
                    "userId": "user-1",
                    "sharedShift": {"startDateTime": "2026-09-15T07:00:00Z", "endDateTime": "2026-09-15T16:00:00Z"},
                },
                {
                    "userId": "user-1",
                    "sharedShift": {"startDateTime": "2026-09-17T08:00:00Z", "endDateTime": "2026-09-17T17:00:00Z"},
                },
            ]}),
        ]
        client = self._client()

        shifts = client.list_shifts_for_date_range(date(2026, 9, 15), date(2026, 9, 21))

        self.assertEqual(mock_get.call_count, 2)  # one for members, one for shifts — not one per day
        self.assertEqual({s.shift_start.date() for s in shifts}, {date(2026, 9, 15), date(2026, 9, 17)})
        shifts_call_url = mock_get.call_args_list[1].args[0]
        self.assertIn("2026-09-14", shifts_call_url)  # window starts the day before the range
        self.assertIn("2026-09-22", shifts_call_url)  # window ends the day after the range

    @patch("requests.get")
    @patch("requests.post")
    def test_draft_only_shift_excluded(self, mock_post, mock_get):
        # A shift with no sharedShift hasn't been published — office
        # staff can't see it in Teams either, so it isn't a real
        # commitment yet.
        mock_post.return_value = _fake_token_response()
        mock_get.side_effect = [
            _fake_json_response({"value": [{"id": "user-1", "mail": "andrew.s@wgtk.co.uk"}]}),
            _fake_json_response({"value": [
                {"userId": "user-1", "draftShift": {"startDateTime": "2026-09-15T07:00:00Z", "endDateTime": "2026-09-15T16:00:00Z"}},
            ]}),
        ]
        client = self._client()

        shifts = client.list_shifts_for_date(date(2026, 9, 15))

        self.assertEqual(shifts, [])

    @patch("requests.get")
    @patch("requests.post")
    def test_shift_on_a_different_date_excluded(self, mock_post, mock_get):
        mock_post.return_value = _fake_token_response()
        mock_get.side_effect = [
            _fake_json_response({"value": [{"id": "user-1", "mail": "andrew.s@wgtk.co.uk"}]}),
            _fake_json_response({"value": [
                {
                    "userId": "user-1",
                    "sharedShift": {"startDateTime": "2026-09-16T07:00:00Z", "endDateTime": "2026-09-16T16:00:00Z"},
                },
            ]}),
        ]
        client = self._client()

        shifts = client.list_shifts_for_date(date(2026, 9, 15))

        self.assertEqual(shifts, [])

    @patch("requests.get")
    @patch("requests.post")
    def test_follows_odata_next_link_for_both_lookups(self, mock_post, mock_get):
        mock_post.return_value = _fake_token_response()
        mock_get.side_effect = [
            _fake_json_response({
                "value": [{"id": "user-1", "mail": "andrew.s@wgtk.co.uk"}],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/next-members-page",
            }),
            _fake_json_response({"value": [{"id": "user-2", "mail": "blain.h@wgtk.co.uk"}]}),
            _fake_json_response({
                "value": [{
                    "userId": "user-1",
                    "sharedShift": {"startDateTime": "2026-09-15T07:00:00Z", "endDateTime": "2026-09-15T16:00:00Z"},
                }],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/next-shifts-page",
            }),
            _fake_json_response({"value": [{
                "userId": "user-2",
                "sharedShift": {"startDateTime": "2026-09-15T08:00:00Z", "endDateTime": "2026-09-15T17:00:00Z"},
            }]}),
        ]
        client = self._client()

        shifts = client.list_shifts_for_date(date(2026, 9, 15))

        self.assertEqual(mock_get.call_count, 4)
        self.assertEqual({s.email for s in shifts}, {"andrew.s@wgtk.co.uk", "blain.h@wgtk.co.uk"})


class TeamsShiftsSettingsTests(TestCase):
    def test_current_team_id_blank_with_no_row(self):
        self.assertEqual(TeamsShiftsSettings.current_team_id(), "")

    def test_current_team_id_reads_the_row(self):
        TeamsShiftsSettings.objects.create(team_id="the-team-id")
        self.assertEqual(TeamsShiftsSettings.current_team_id(), "the-team-id")


@override_settings(
    MS_GRAPH_MAIL_CLIENT_ID="client-id", MS_GRAPH_MAIL_CLIENT_SECRET="client-secret",
    MS_GRAPH_MAIL_TENANT_ID="tenant-id", MS_GRAPH_TEAM_ID="",
)
class GetTeamsShiftsClientTests(TestCase):
    def test_returns_mock_client_when_no_team_id(self):
        self.assertIsInstance(get_teams_shifts_client(), MockTeamsShiftsClient)

    def test_returns_real_client_using_admin_stored_team_id(self):
        TeamsShiftsSettings.objects.create(team_id="admin-set-team")
        client = get_teams_shifts_client()
        self.assertIsInstance(client, RealTeamsShiftsClient)
        self.assertEqual(client._team_id, "admin-set-team")

    @override_settings(MS_GRAPH_TEAM_ID="app-setting-team")
    def test_falls_back_to_app_setting_team_id(self):
        client = get_teams_shifts_client()
        self.assertIsInstance(client, RealTeamsShiftsClient)
        self.assertEqual(client._team_id, "app-setting-team")

    def test_returns_mock_client_when_team_id_set_but_no_graph_credentials(self):
        TeamsShiftsSettings.objects.create(team_id="admin-set-team")
        with override_settings(MS_GRAPH_MAIL_CLIENT_ID=""):
            self.assertIsInstance(get_teams_shifts_client(), MockTeamsShiftsClient)
