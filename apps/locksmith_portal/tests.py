from datetime import date
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.integrations.handl import CurrentStockLine
from apps.integrations.optimo import OptimoOrderSummary
from apps.locksmiths.models import Locksmith
from apps.stock_accuracy.models import WeeklyStockCheck
from apps.stock_accuracy.services.generation import generate_weekly_check

from .models import PortalDisposal

User = get_user_model()


def _make_locksmith_user(email="dean@wgtk.co.uk", soter_ids=("885",), driver_serials=("011",)):
    locksmith = Locksmith.objects.create(name="Dean S", email=email, active=True)
    for soter_id in soter_ids:
        locksmith.soter_ids.create(soter_locksmith_id=soter_id)
    for serial in driver_serials:
        locksmith.optimo_driver_ids.create(optimo_driver_serial=serial)
    user = User.objects.create_user(username=email, email=email, password="x")
    locksmith.user = user
    locksmith.save(update_fields=["user"])
    return locksmith, user


class DashboardTests(TestCase):
    def setUp(self):
        self.locksmith, self.user = _make_locksmith_user()
        self.client.force_login(self.user)

    def test_non_locksmith_user_redirected_to_office_dashboard(self):
        office_user = User.objects.create_user(
            username="office@wgtk.co.uk", email="office@wgtk.co.uk", password="x", is_staff=True
        )
        self.client.force_login(office_user)
        response = self.client.get(reverse("locksmith_portal:dashboard"))
        self.assertRedirects(response, reverse("stock_accuracy:dashboard"))

    def test_dashboard_with_no_stock_check_shows_empty_state(self):
        response = self.client.get(reverse("locksmith_portal:dashboard"))
        self.assertIsNone(response.context["latest_check"])
        self.assertContains(response, "No stock check has been sent")

    def test_dashboard_shows_latest_stock_check(self):
        generate_weekly_check(self.locksmith, date(2026, 9, 7))
        response = self.client.get(reverse("locksmith_portal:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.context["latest_check"])

    @patch("apps.locksmith_portal.views.get_optimo_client")
    def test_dashboard_lists_only_own_jobs(self, mock_get_optimo):
        today = timezone.localdate()
        mock_client = MagicMock()
        mock_client.list_orders_for_date.return_value = [
            OptimoOrderSummary(
                order_no=f"1001_{today.isoformat()}",
                driver_serial="011",
                distance_metres=0,
                travel_time_seconds=0,
            ),
            OptimoOrderSummary(
                order_no=f"2002_{today.isoformat()}",
                driver_serial="999",
                distance_metres=0,
                travel_time_seconds=0,
            ),
        ]
        mock_get_optimo.return_value = mock_client

        response = self.client.get(reverse("locksmith_portal:dashboard"))
        jobs = response.context["jobs"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["report_id"], "1001")

    @patch("apps.locksmith_portal.views.get_optimo_client")
    def test_sees_all_jobs_for_testing_bypasses_driver_filter(self, mock_get_optimo):
        # A test/admin account has no real Optimo driverSerial of its own
        # to filter by, so the flag shows every job scheduled today
        # regardless of which driver it's assigned to.
        self.locksmith.sees_all_jobs_for_testing = True
        self.locksmith.save(update_fields=["sees_all_jobs_for_testing"])
        self.locksmith.optimo_driver_ids.all().delete()

        today = timezone.localdate()
        mock_client = MagicMock()
        mock_client.list_orders_for_date.return_value = [
            OptimoOrderSummary(
                order_no=f"1001_{today.isoformat()}",
                driver_serial="011",
                distance_metres=0,
                travel_time_seconds=0,
            ),
            OptimoOrderSummary(
                order_no=f"2002_{today.isoformat()}",
                driver_serial="999",
                distance_metres=0,
                travel_time_seconds=0,
            ),
        ]
        mock_get_optimo.return_value = mock_client

        response = self.client.get(reverse("locksmith_portal:dashboard"))
        jobs = response.context["jobs"]
        self.assertEqual({job["report_id"] for job in jobs}, {"1001", "2002"})

    def test_login_required(self):
        self.client.logout()
        response = self.client.get(reverse("locksmith_portal:dashboard"))
        self.assertEqual(response.status_code, 302)


class StockCheckEntryTests(TestCase):
    def setUp(self):
        self.locksmith, self.user = _make_locksmith_user()
        self.client.force_login(self.user)
        self.weekly_check = generate_weekly_check(self.locksmith, date(2026, 9, 7))

    def test_get_renders_form(self):
        url = reverse("locksmith_portal:stock_check_entry", args=[self.weekly_check.pk])
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_post_saves_counts_and_completes_check(self):
        url = reverse("locksmith_portal:stock_check_entry", args=[self.weekly_check.pk])
        items = list(self.weekly_check.items.all())
        data = {f"qty_{item.id}": item.expected_qty for item in items}
        response = self.client.post(url, data)

        self.assertRedirects(response, reverse("locksmith_portal:dashboard"))
        self.weekly_check.refresh_from_db()
        self.assertEqual(self.weekly_check.status, WeeklyStockCheck.Status.COMPLETED)
        self.assertTrue(self.weekly_check.is_fully_entered)

    def test_cannot_access_another_locksmiths_check(self):
        other_locksmith, _other_user = _make_locksmith_user(
            email="other@wgtk.co.uk", soter_ids=("999",), driver_serials=("999",)
        )
        other_check = generate_weekly_check(other_locksmith, date(2026, 9, 7))
        url = reverse("locksmith_portal:stock_check_entry", args=[other_check.pk])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_login_required(self):
        self.client.logout()
        url = reverse("locksmith_portal:stock_check_entry", args=[self.weekly_check.pk])
        self.assertEqual(self.client.get(url).status_code, 302)


class JobDetailTests(TestCase):
    def setUp(self):
        self.locksmith, self.user = _make_locksmith_user(soter_ids=("885",), driver_serials=("011",))
        self.client.force_login(self.user)
        self.today = timezone.localdate()
        self.order_no = f"496390_{self.today.isoformat()}"

    def _mock_optimo(self, mock_get_optimo):
        mock_client = MagicMock()
        mock_client.list_orders_for_date.return_value = [
            OptimoOrderSummary(
                order_no=self.order_no, driver_serial="011", distance_metres=0, travel_time_seconds=0
            ),
        ]
        mock_get_optimo.return_value = mock_client
        return mock_client

    @patch("apps.locksmith_portal.views.get_handl_client")
    @patch("apps.locksmith_portal.views.get_optimo_client")
    def test_job_not_on_todays_schedule_redirects(self, mock_get_optimo, mock_get_handl):
        self._mock_optimo(mock_get_optimo)
        url = reverse("locksmith_portal:job_detail", args=[f"999999_{self.today.isoformat()}"])
        response = self.client.get(url)
        self.assertRedirects(response, reverse("locksmith_portal:dashboard"))

    @patch("apps.locksmith_portal.views.get_handl_client")
    @patch("apps.locksmith_portal.views.get_optimo_client")
    def test_get_shows_current_stock_scoped_to_own_soter_ids(self, mock_get_optimo, mock_get_handl):
        self._mock_optimo(mock_get_optimo)
        mock_handl = MagicMock()
        mock_handl.list_current_stock.return_value = [
            CurrentStockLine(part_code="TK-100", part_name="Transponder key blank", qty=4),
        ]
        mock_get_handl.return_value = mock_handl

        url = reverse("locksmith_portal:job_detail", args=[self.order_no])
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "TK-100")
        mock_handl.list_current_stock.assert_called_once_with(["885"])

    @patch("apps.locksmith_portal.views.get_handl_client")
    @patch("apps.locksmith_portal.views.get_optimo_client")
    def test_post_disposes_within_stock_and_logs_portal_disposal(self, mock_get_optimo, mock_get_handl):
        self._mock_optimo(mock_get_optimo)
        mock_handl = MagicMock()
        mock_handl.list_current_stock.return_value = [
            CurrentStockLine(part_code="TK-100", part_name="Transponder key blank", qty=4),
        ]
        mock_get_handl.return_value = mock_handl

        url = reverse("locksmith_portal:job_detail", args=[self.order_no])
        response = self.client.post(
            url, {"part_code": ["TK-100 — Transponder key blank"], "quantity": ["2"]}
        )

        self.assertRedirects(response, url)
        mock_handl.record_disposal.assert_called_once_with(
            "885",
            "496390",
            "TK-100",
            "Transponder key blank",
            2,
            actioned_by_user_id=0,
            locksmith_display_name="Dean S",
        )

        disposal = PortalDisposal.objects.get()
        self.assertEqual(disposal.quantity, 2)
        self.assertEqual(disposal.part_code, "TK-100")
        self.assertEqual(disposal.report_id, "496390")
        self.assertTrue(disposal.handl_synced)

    @patch("apps.locksmith_portal.views.get_handl_client")
    @patch("apps.locksmith_portal.views.get_optimo_client")
    def test_post_resolves_bare_sku_or_part_name_typed_without_picking_suggestion(
        self, mock_get_optimo, mock_get_handl
    ):
        self._mock_optimo(mock_get_optimo)
        mock_handl = MagicMock()
        mock_handl.list_current_stock.return_value = [
            CurrentStockLine(part_code="TK-100", part_name="Transponder key blank", qty=4),
            CurrentStockLine(part_code="TK-101", part_name="Remote key fob", qty=3),
        ]
        mock_get_handl.return_value = mock_handl

        url = reverse("locksmith_portal:job_detail", args=[self.order_no])
        self.client.post(
            url,
            {"part_code": ["tk-100", "Remote key fob"], "quantity": ["1", "1"]},
        )

        self.assertEqual(
            {(d.part_code, d.quantity) for d in PortalDisposal.objects.all()},
            {("TK-100", 1), ("TK-101", 1)},
        )

    @patch("apps.locksmith_portal.views.get_handl_client")
    @patch("apps.locksmith_portal.views.get_optimo_client")
    def test_post_aggregates_same_part_added_across_multiple_rows(
        self, mock_get_optimo, mock_get_handl
    ):
        self._mock_optimo(mock_get_optimo)
        mock_handl = MagicMock()
        mock_handl.list_current_stock.return_value = [
            CurrentStockLine(part_code="TK-100", part_name="Transponder key blank", qty=4),
        ]
        mock_get_handl.return_value = mock_handl

        url = reverse("locksmith_portal:job_detail", args=[self.order_no])
        self.client.post(
            url,
            {"part_code": ["TK-100", "TK-100"], "quantity": ["1", "2"]},
        )

        mock_handl.record_disposal.assert_called_once_with(
            "885",
            "496390",
            "TK-100",
            "Transponder key blank",
            3,
            actioned_by_user_id=0,
            locksmith_display_name="Dean S",
        )
        self.assertEqual(PortalDisposal.objects.count(), 1)
        self.assertEqual(PortalDisposal.objects.get().quantity, 3)

    @patch("apps.locksmith_portal.views.get_handl_client")
    @patch("apps.locksmith_portal.views.get_optimo_client")
    def test_post_unknown_part_search_is_rejected(self, mock_get_optimo, mock_get_handl):
        self._mock_optimo(mock_get_optimo)
        mock_handl = MagicMock()
        mock_handl.list_current_stock.return_value = [
            CurrentStockLine(part_code="TK-100", part_name="Transponder key blank", qty=4),
        ]
        mock_get_handl.return_value = mock_handl

        url = reverse("locksmith_portal:job_detail", args=[self.order_no])
        response = self.client.post(
            url, {"part_code": ["NOT-A-REAL-PART"], "quantity": ["1"]}, follow=True
        )

        mock_handl.record_disposal.assert_not_called()
        self.assertEqual(PortalDisposal.objects.count(), 0)
        self.assertContains(response, "Couldn&#x27;t find")

    @patch("apps.locksmith_portal.views.get_handl_client")
    @patch("apps.locksmith_portal.views.get_optimo_client")
    def test_post_rejects_quantity_over_current_stock(self, mock_get_optimo, mock_get_handl):
        self._mock_optimo(mock_get_optimo)
        mock_handl = MagicMock()
        mock_handl.list_current_stock.return_value = [
            CurrentStockLine(part_code="TK-100", part_name="Transponder key blank", qty=2),
        ]
        mock_get_handl.return_value = mock_handl

        url = reverse("locksmith_portal:job_detail", args=[self.order_no])
        self.client.post(url, {"part_code": ["TK-100"], "quantity": ["5"]})

        mock_handl.record_disposal.assert_not_called()
        self.assertEqual(PortalDisposal.objects.count(), 0)

    @patch("apps.locksmith_portal.views.get_handl_client")
    @patch("apps.locksmith_portal.views.get_optimo_client")
    def test_handl_write_failure_still_logs_disposal_with_error(self, mock_get_optimo, mock_get_handl):
        self._mock_optimo(mock_get_optimo)
        mock_handl = MagicMock()
        mock_handl.list_current_stock.return_value = [
            CurrentStockLine(part_code="TK-100", part_name="Transponder key blank", qty=4),
        ]
        mock_handl.record_disposal.side_effect = RuntimeError("boom")
        mock_get_handl.return_value = mock_handl

        url = reverse("locksmith_portal:job_detail", args=[self.order_no])
        self.client.post(url, {"part_code": ["TK-100"], "quantity": ["2"]})

        disposal = PortalDisposal.objects.get()
        self.assertFalse(disposal.handl_synced)
        self.assertIn("boom", disposal.handl_error)

    def test_login_required(self):
        self.client.logout()
        url = reverse("locksmith_portal:job_detail", args=[self.order_no])
        self.assertEqual(self.client.get(url).status_code, 302)


class StaffPreviewTests(TestCase):
    """Lets office/admin staff try the portal as a chosen locksmith
    without their own login becoming locksmith-linked (which would
    trigger RestrictLocksmithsToPortalMiddleware and lock them out of
    office/admin pages) — see views.start_preview/stop_preview."""

    def setUp(self):
        self.locksmith, _real_user = _make_locksmith_user(
            email="dean@wgtk.co.uk", soter_ids=("885",), driver_serials=("011",)
        )
        self.staff_user = User.objects.create_user(
            username="office@wgtk.co.uk", email="office@wgtk.co.uk", password="x", is_staff=True
        )
        self.client.force_login(self.staff_user)

    def test_non_staff_cannot_start_preview(self):
        non_staff = User.objects.create_user(
            username="nobody@wgtk.co.uk", email="nobody@wgtk.co.uk", password="x"
        )
        self.client.force_login(non_staff)
        url = reverse("locksmith_portal:start_preview", args=[self.locksmith.pk])
        response = self.client.get(url)
        self.assertRedirects(response, reverse("stock_accuracy:dashboard"))
        self.assertNotIn("locksmith_portal_preview_id", self.client.session)

    def test_staff_start_preview_then_dashboard_shows_that_locksmith(self):
        start_url = reverse("locksmith_portal:start_preview", args=[self.locksmith.pk])
        response = self.client.get(start_url)
        self.assertRedirects(response, reverse("locksmith_portal:dashboard"))

        response = self.client.get(reverse("locksmith_portal:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["locksmith"], self.locksmith)
        self.assertTrue(response.context["is_preview"])

    def test_staff_stays_staff_while_previewing(self):
        # Previewing must not make RestrictLocksmithsToPortalMiddleware
        # treat this account as a real locksmith — it should still be
        # able to reach office pages.
        self.client.get(reverse("locksmith_portal:start_preview", args=[self.locksmith.pk]))
        response = self.client.get(reverse("stock_accuracy:dashboard"))
        self.assertEqual(response.status_code, 200)

    def test_stop_preview_clears_session_and_blocks_portal_again(self):
        self.client.get(reverse("locksmith_portal:start_preview", args=[self.locksmith.pk]))
        response = self.client.get(reverse("locksmith_portal:stop_preview"))
        self.assertRedirects(response, reverse("stock_accuracy:dashboard"))

        response = self.client.get(reverse("locksmith_portal:dashboard"))
        self.assertRedirects(response, reverse("stock_accuracy:dashboard"))

    def test_preview_of_inactive_locksmith_404s(self):
        self.locksmith.active = False
        self.locksmith.save(update_fields=["active"])
        url = reverse("locksmith_portal:start_preview", args=[self.locksmith.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
