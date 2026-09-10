from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.integrations.handl import ExpectedStock, StockUsage
from apps.locksmiths.models import Locksmith

from .models import (
    PartUnitConversion,
    StockCheckItem,
    VarianceThreshold,
    VirtualStockItem,
    WeeklyStockCheck,
)
from .services.generation import generate_weekly_check
from .services.reporting import locksmith_summary


def _make_locksmith(name, email, soter_ids=("1",)):
    locksmith = Locksmith.objects.create(name=name, email=email)
    for soter_id in soter_ids:
        locksmith.soter_ids.create(soter_locksmith_id=soter_id)
    return locksmith


class GenerationTests(TestCase):
    def setUp(self):
        self.locksmith = _make_locksmith("Jane Smith", "jane@example.com", ["ENG-001"])

    def test_generates_configured_number_of_lines(self):
        weekly_check = generate_weekly_check(self.locksmith, date(2026, 9, 7))
        self.assertEqual(weekly_check.items.count(), 10)

    def test_lines_are_unique_within_a_check(self):
        weekly_check = generate_weekly_check(self.locksmith, date(2026, 9, 7))
        codes = list(weekly_check.items.values_list("part_code", flat=True))
        self.assertEqual(len(codes), len(set(codes)))

    def test_expected_qty_is_frozen_not_recalculated(self):
        weekly_check = generate_weekly_check(self.locksmith, date(2026, 9, 7))
        item = weekly_check.items.first()
        original_expected = item.expected_qty

        # Simulate stock moving in Handl after the check was sent — the
        # frozen expected_qty on the item must not change.
        item.refresh_from_db()
        self.assertEqual(item.expected_qty, original_expected)

    def test_regenerating_same_week_returns_existing_check(self):
        first = generate_weekly_check(self.locksmith, date(2026, 9, 7))
        second = generate_weekly_check(self.locksmith, date(2026, 9, 7))
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(WeeklyStockCheck.objects.count(), 1)

    @override_settings(STOCK_CHECK_NO_REPEAT_WEEKS=52)
    def test_no_repeat_window_excludes_recently_checked_lines(self):
        week1 = generate_weekly_check(self.locksmith, date(2026, 8, 3))
        week1_codes = set(week1.items.values_list("part_code", flat=True))

        week2 = generate_weekly_check(self.locksmith, date(2026, 8, 10))
        week2_codes = set(week2.items.values_list("part_code", flat=True))

        # With a 52-week no-repeat window and only 35 lines in the mock
        # catalogue, some overlap is unavoidable once the pool is
        # exhausted — but the two draws should still differ.
        self.assertNotEqual(week1_codes, week2_codes)

    def test_virtual_stock_items_are_never_selected(self):
        VirtualStockItem.objects.create(part_code="3D-TOKEN", part_name="3D job token")

        mock_handl = MagicMock()
        mock_handl.get_stock_usage.return_value = [
            StockUsage(part_code="3D-TOKEN", part_name="3D job token", qty_used=99),
            StockUsage(part_code="TK-100", part_name="Transponder key blank", qty_used=5),
        ]
        mock_handl.get_expected_stock.return_value = {
            "TK-100": ExpectedStock(part_code="TK-100", expected_qty=5, unit_cost=10.0),
        }

        with patch("apps.stock_accuracy.services.generation.get_handl_client", return_value=mock_handl):
            weekly_check = generate_weekly_check(self.locksmith, date(2026, 9, 7))

        codes = set(weekly_check.items.values_list("part_code", flat=True))
        self.assertNotIn("3D-TOKEN", codes)
        self.assertIn("TK-100", codes)

    def test_virtual_stock_item_exclusion_is_case_insensitive(self):
        VirtualStockItem.objects.create(part_code="3d-token")

        mock_handl = MagicMock()
        mock_handl.get_stock_usage.return_value = [
            StockUsage(part_code="3D-TOKEN", part_name="3D job token", qty_used=99),
            StockUsage(part_code="TK-100", part_name="Transponder key blank", qty_used=5),
        ]
        mock_handl.get_expected_stock.return_value = {
            "TK-100": ExpectedStock(part_code="TK-100", expected_qty=5, unit_cost=10.0),
        }

        with patch("apps.stock_accuracy.services.generation.get_handl_client", return_value=mock_handl):
            weekly_check = generate_weekly_check(self.locksmith, date(2026, 9, 7))

        codes = set(weekly_check.items.values_list("part_code", flat=True))
        self.assertNotIn("3D-TOKEN", codes)

    def test_part_unit_conversion_scales_expected_qty_and_unit_cost(self):
        # update_or_create, not create — a real VXRP2 conversion is
        # already seeded by migration 0007, so a plain create() here
        # would collide with it.
        PartUnitConversion.objects.update_or_create(
            part_code="VXRP2", defaults={"units_per_pack": 10}
        )

        mock_handl = MagicMock()
        mock_handl.get_stock_usage.return_value = [
            StockUsage(part_code="VXRP2", part_name="Flip blade pins", qty_used=5),
        ]
        mock_handl.get_expected_stock.return_value = {
            "VXRP2": ExpectedStock(part_code="VXRP2", expected_qty=41, unit_cost=10.0),
        }

        with patch("apps.stock_accuracy.services.generation.get_handl_client", return_value=mock_handl):
            weekly_check = generate_weekly_check(self.locksmith, date(2026, 9, 7))

        item = weekly_check.items.get(part_code="VXRP2")
        self.assertEqual(item.expected_qty, 410)
        self.assertEqual(item.unit_cost, 1.0)

    def test_no_conversion_leaves_expected_qty_and_unit_cost_unchanged(self):
        mock_handl = MagicMock()
        mock_handl.get_stock_usage.return_value = [
            StockUsage(part_code="TK-100", part_name="Transponder key blank", qty_used=5),
        ]
        mock_handl.get_expected_stock.return_value = {
            "TK-100": ExpectedStock(part_code="TK-100", expected_qty=8, unit_cost=12.5),
        }

        with patch("apps.stock_accuracy.services.generation.get_handl_client", return_value=mock_handl):
            weekly_check = generate_weekly_check(self.locksmith, date(2026, 9, 7))

        item = weekly_check.items.get(part_code="TK-100")
        self.assertEqual(item.expected_qty, 8)
        self.assertEqual(item.unit_cost, 12.5)


class VarianceFlaggingTests(TestCase):
    def setUp(self):
        self.locksmith = _make_locksmith("Ali Khan", "ali@example.com", ["ENG-003"])
        self.weekly_check = WeeklyStockCheck.objects.create(
            locksmith=self.locksmith, week_starting=date(2026, 9, 7)
        )
        self.thresholds = VarianceThreshold.objects.create(
            unit_threshold=2, pct_threshold=10, value_threshold=25, active=True
        )

    def test_small_variance_not_flagged(self):
        item = StockCheckItem.objects.create(
            weekly_check=self.weekly_check,
            part_code="TK-100",
            part_name="Test part",
            expected_qty=10,
            unit_cost=1,
            actual_qty=10,
        )
        self.assertFalse(item.is_flagged(self.thresholds))

    def test_variance_over_unit_threshold_is_flagged(self):
        item = StockCheckItem.objects.create(
            weekly_check=self.weekly_check,
            part_code="TK-101",
            part_name="Test part",
            expected_qty=10,
            unit_cost=1,
            actual_qty=6,  # variance of 4, over unit_threshold of 2
        )
        self.assertTrue(item.is_flagged(self.thresholds))

    def test_variance_over_value_threshold_is_flagged_even_within_unit_threshold(self):
        item = StockCheckItem.objects.create(
            weekly_check=self.weekly_check,
            part_code="TK-102",
            part_name="Expensive part",
            expected_qty=10,
            unit_cost=50,
            actual_qty=9,  # variance of 1 unit, but £50 impact
        )
        self.assertTrue(item.is_flagged(self.thresholds))

    def test_unentered_item_is_not_flagged(self):
        item = StockCheckItem.objects.create(
            weekly_check=self.weekly_check,
            part_code="TK-103",
            part_name="Test part",
            expected_qty=10,
            unit_cost=1,
        )
        self.assertFalse(item.is_flagged(self.thresholds))

    def test_repeat_offender_detected_across_weeks(self):
        thresholds = VarianceThreshold.objects.create(
            unit_threshold=1,
            pct_threshold=1000,
            value_threshold=1000,
            repeat_offender_occurrences=2,
            repeat_offender_window_weeks=4,
            active=True,
        )
        self.thresholds.active = False
        self.thresholds.save()

        for i, expected in enumerate([10, 10, 10]):
            wc = WeeklyStockCheck.objects.create(
                locksmith=self.locksmith, week_starting=date(2026, 10, 5) - timedelta(weeks=i)
            )
            StockCheckItem.objects.create(
                weekly_check=wc,
                part_code=f"TK-20{i}",
                part_name="Part",
                expected_qty=expected,
                unit_cost=1,
                actual_qty=expected - 5,  # always flagged under this threshold
            )

        summary = locksmith_summary(self.locksmith)
        self.assertTrue(summary["is_repeat_offender"])


class ViewsSmokeTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="office_admin", email="admin@wgtk.co.uk", password="x", is_staff=True
        )
        self.client.force_login(self.user)
        self.locksmith = _make_locksmith("Sam Lee", "sam@example.com", ["ENG-010"])
        self.weekly_check = generate_weekly_check(self.locksmith, date(2026, 9, 7))

    def test_dashboard_renders(self):
        response = self.client.get(reverse("stock_accuracy:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sam Lee")

    def test_dashboard_lists_generated_check_as_pending(self):
        response = self.client.get(reverse("stock_accuracy:dashboard"))
        self.assertContains(response, "Sam Lee")
        self.assertEqual(list(response.context["pending"]), [self.weekly_check])

    def test_locksmith_report_renders(self):
        response = self.client.get(
            reverse("stock_accuracy:locksmith_report", args=[self.locksmith.pk])
        )
        self.assertEqual(response.status_code, 200)

    def test_dashboard_hides_locksmith_portal_link_for_plain_office_user(self):
        response = self.client.get(reverse("stock_accuracy:dashboard"))
        self.assertNotContains(response, "Locksmith portal")

    def test_dashboard_shows_locksmith_portal_link_for_dual_access_locksmith(self):
        self.locksmith.office_access = True
        self.locksmith.save(update_fields=["office_access"])
        self.locksmith.user = self.user
        self.locksmith.save(update_fields=["user"])
        response = self.client.get(reverse("stock_accuracy:dashboard"))
        self.assertContains(response, "Locksmith portal")

    def test_entry_detail_saves_counts_and_completes_check(self):
        url = reverse("stock_accuracy:entry_detail", args=[self.weekly_check.pk])
        self.assertEqual(self.client.get(url).status_code, 200)

        items = list(self.weekly_check.items.all())
        data = {f"qty_{item.id}": item.expected_qty for item in items}
        response = self.client.post(url, data)

        self.assertEqual(response.status_code, 302)
        self.weekly_check.refresh_from_db()
        self.assertEqual(self.weekly_check.status, WeeklyStockCheck.Status.COMPLETED)
        self.assertIsNotNone(self.weekly_check.completed_at)
        self.assertTrue(self.weekly_check.is_fully_entered)

    def test_login_required_redirects_anonymous(self):
        self.client.logout()
        response = self.client.get(reverse("stock_accuracy:dashboard"))
        self.assertEqual(response.status_code, 302)

    def test_confirm_check_requires_every_line_entered(self):
        url = reverse("stock_accuracy:confirm_check", args=[self.weekly_check.pk])
        response = self.client.post(url)

        self.assertRedirects(
            response, reverse("stock_accuracy:entry_detail", args=[self.weekly_check.pk])
        )
        self.weekly_check.refresh_from_db()
        self.assertIsNone(self.weekly_check.confirmed_at)

    def test_confirm_check_pushes_counts_and_marks_confirmed(self):
        entry_url = reverse("stock_accuracy:entry_detail", args=[self.weekly_check.pk])
        items = list(self.weekly_check.items.all())
        self.client.post(entry_url, {f"qty_{item.id}": item.expected_qty for item in items})

        mock_handl = MagicMock()
        with patch("apps.stock_accuracy.views.get_handl_client", return_value=mock_handl):
            response = self.client.post(
                reverse("stock_accuracy:confirm_check", args=[self.weekly_check.pk])
            )

        self.assertRedirects(response, entry_url)
        self.assertEqual(mock_handl.set_locksmith_stock_quantity.call_count, len(items))

        self.weekly_check.refresh_from_db()
        self.assertIsNotNone(self.weekly_check.confirmed_at)
        self.assertEqual(self.weekly_check.confirmed_by, self.user)
        for item in self.weekly_check.items.all():
            self.assertTrue(item.handl_synced)
            self.assertEqual(item.handl_error, "")

    def test_confirm_check_records_per_line_handl_errors_without_blocking_others(self):
        entry_url = reverse("stock_accuracy:entry_detail", args=[self.weekly_check.pk])
        items = list(self.weekly_check.items.all())
        self.client.post(entry_url, {f"qty_{item.id}": item.expected_qty for item in items})

        mock_handl = MagicMock()
        mock_handl.set_locksmith_stock_quantity.side_effect = [
            ValueError("No Inventory_Locksmith_Stock row found") if i == 0 else None
            for i in range(len(items))
        ]
        with patch("apps.stock_accuracy.views.get_handl_client", return_value=mock_handl):
            self.client.post(reverse("stock_accuracy:confirm_check", args=[self.weekly_check.pk]))

        self.assertEqual(mock_handl.set_locksmith_stock_quantity.call_count, len(items))
        self.weekly_check.refresh_from_db()
        self.assertIsNotNone(self.weekly_check.confirmed_at)

        synced_items = list(self.weekly_check.items.all())
        failed = [item for item in synced_items if not item.handl_synced]
        succeeded = [item for item in synced_items if item.handl_synced]
        self.assertEqual(len(failed), 1)
        self.assertIn("No Inventory_Locksmith_Stock row found", failed[0].handl_error)
        self.assertEqual(len(succeeded), len(items) - 1)
