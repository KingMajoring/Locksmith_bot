from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.integrations.handl import CurrentStockLine, JobDetails
from apps.integrations.optimo import OptimoOrderSummary
from apps.job_completion.models import FailureCategory
from apps.locksmiths.models import Locksmith
from apps.stock_accuracy.models import WeeklyStockCheck
from apps.stock_accuracy.services.generation import generate_weekly_check

from .models import JobVisit, JobVisitPhoto, PortalDisposal

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

    @patch("apps.locksmith_portal.views.get_handl_client")
    @patch("apps.locksmith_portal.views.get_optimo_client")
    def test_dashboard_shows_make_model_year_service(self, mock_get_optimo, mock_get_handl):
        today = timezone.localdate()
        order_no = f"1001_{today.isoformat()}"
        mock_optimo = MagicMock()
        mock_optimo.list_orders_for_date.return_value = [
            OptimoOrderSummary(
                order_no=order_no, driver_serial="011", distance_metres=0, travel_time_seconds=0
            ),
        ]
        mock_get_optimo.return_value = mock_optimo
        mock_handl = MagicMock()
        mock_handl.get_job_details.return_value = {
            "1001": JobDetails(
                report_id="1001", make="Ford", model="Focus", year="2020", reg="AB20 CDE", vin="VIN1",
                service_type="Car", loss_type="LOST", supplied_service="", net_cost=100.0,
            )
        }
        mock_get_handl.return_value = mock_handl

        response = self.client.get(reverse("locksmith_portal:dashboard"))
        job = response.context["jobs"][0]
        self.assertEqual(job["make"], "Ford")
        self.assertEqual(job["model"], "Focus")
        self.assertEqual(job["year"], "2020")
        self.assertEqual(job["reg"], "AB20 CDE")
        self.assertEqual(job["service"], "AKL")
        self.assertContains(response, "AB20 CDE")
        self.assertContains(response, "Ford Focus 2020")
        self.assertContains(response, "AKL")

    @patch("apps.locksmith_portal.views.get_optimo_client")
    def test_dashboard_shows_disposed_tick_and_count(self, mock_get_optimo):
        today = timezone.localdate()
        order_no = f"1001_{today.isoformat()}"
        mock_client = MagicMock()
        mock_client.list_orders_for_date.return_value = [
            OptimoOrderSummary(
                order_no=order_no, driver_serial="011", distance_metres=0, travel_time_seconds=0
            ),
        ]
        mock_get_optimo.return_value = mock_client

        PortalDisposal.objects.create(
            locksmith=self.locksmith, order_no=order_no, report_id="1001",
            part_code="TK-100", part_name="Transponder key blank", quantity=2,
        )
        PortalDisposal.objects.create(
            locksmith=self.locksmith, order_no=order_no, report_id="1001",
            part_code="TK-101", part_name="Remote key fob", quantity=1,
        )

        response = self.client.get(reverse("locksmith_portal:dashboard"))
        job = response.context["jobs"][0]
        self.assertEqual(job["disposed_quantity"], 3)
        self.assertEqual(job["disposed_parts"], 2)
        self.assertContains(response, "3 parts disposed")

    @patch("apps.locksmith_portal.views.get_optimo_client")
    def test_dashboard_date_navigation(self, mock_get_optimo):
        mock_client = MagicMock()
        mock_client.list_orders_for_date.return_value = []
        mock_get_optimo.return_value = mock_client
        today = timezone.localdate()

        response = self.client.get(reverse("locksmith_portal:dashboard"))
        self.assertEqual(response.context["selected_date"], today)
        self.assertTrue(response.context["is_today"])
        self.assertIsNone(response.context["next_date"])

        yesterday = today - timedelta(days=1)
        response = self.client.get(
            reverse("locksmith_portal:dashboard"), {"date": yesterday.isoformat()}
        )
        self.assertEqual(response.context["selected_date"], yesterday)
        self.assertFalse(response.context["is_today"])
        self.assertEqual(response.context["next_date"], today)

    @patch("apps.locksmith_portal.views.get_optimo_client")
    def test_dashboard_date_param_clamped_to_today(self, mock_get_optimo):
        mock_client = MagicMock()
        mock_client.list_orders_for_date.return_value = []
        mock_get_optimo.return_value = mock_client
        today = timezone.localdate()
        future = today + timedelta(days=5)

        response = self.client.get(
            reverse("locksmith_portal:dashboard"), {"date": future.isoformat()}
        )
        self.assertEqual(response.context["selected_date"], today)


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
        # The dispose-parts page is gated behind "arrived" (see
        # JobVisitTests for that gating itself) — most of this class is
        # about the disposal mechanics, not the gate, so start each test
        # already past it.
        JobVisit.objects.create(
            locksmith=self.locksmith, order_no=self.order_no, report_id="496390",
            stage=JobVisit.Stage.ARRIVED, arrived_at=timezone.now(),
        )

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
        self.assertRedirects(
            response, f"{reverse('locksmith_portal:dashboard')}?date={self.today.isoformat()}"
        )

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

        self.assertRedirects(response, f"{url}?date={self.today.isoformat()}")
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

    @patch("apps.locksmith_portal.views.get_handl_client")
    @patch("apps.locksmith_portal.views.get_optimo_client")
    def test_get_shows_previously_recorded_disposals(self, mock_get_optimo, mock_get_handl):
        self._mock_optimo(mock_get_optimo)
        mock_handl = MagicMock()
        mock_handl.list_current_stock.return_value = [
            CurrentStockLine(part_code="TK-100", part_name="Transponder key blank", qty=4),
        ]
        mock_get_handl.return_value = mock_handl
        PortalDisposal.objects.create(
            locksmith=self.locksmith, order_no=self.order_no, report_id="496390",
            part_code="TK-100", part_name="Transponder key blank", quantity=1,
        )

        url = reverse("locksmith_portal:job_detail", args=[self.order_no])
        response = self.client.get(url)

        self.assertContains(response, "Already recorded")
        self.assertContains(response, "Transponder key blank")

    @patch("apps.locksmith_portal.views.get_handl_client")
    @patch("apps.locksmith_portal.views.get_optimo_client")
    def test_past_day_job_detail_uses_that_days_schedule(self, mock_get_optimo, mock_get_handl):
        yesterday = self.today - timedelta(days=1)
        past_order_no = f"555555_{yesterday.isoformat()}"
        mock_client = MagicMock()
        mock_client.list_orders_for_date.return_value = [
            OptimoOrderSummary(
                order_no=past_order_no, driver_serial="011", distance_metres=0, travel_time_seconds=0
            ),
        ]
        mock_get_optimo.return_value = mock_client
        mock_handl = MagicMock()
        mock_handl.list_current_stock.return_value = []
        mock_get_handl.return_value = mock_handl
        JobVisit.objects.create(
            locksmith=self.locksmith, order_no=past_order_no, report_id="555555",
            stage=JobVisit.Stage.ARRIVED, arrived_at=timezone.now(),
        )

        url = reverse("locksmith_portal:job_detail", args=[past_order_no])
        response = self.client.get(url, {"date": yesterday.isoformat()})

        self.assertEqual(response.status_code, 200)
        mock_client.list_orders_for_date.assert_called_once_with(yesterday)

    @patch("apps.locksmith_portal.views.get_handl_client")
    @patch("apps.locksmith_portal.views.get_optimo_client")
    def test_job_not_on_that_past_days_schedule_redirects_preserving_date(
        self, mock_get_optimo, mock_get_handl
    ):
        yesterday = self.today - timedelta(days=1)
        self._mock_optimo(mock_get_optimo)  # only self.order_no is scheduled, any date

        other_order_no = f"999999_{yesterday.isoformat()}"
        url = reverse("locksmith_portal:job_detail", args=[other_order_no])
        response = self.client.get(url, {"date": yesterday.isoformat()})

        self.assertRedirects(
            response, f"{reverse('locksmith_portal:dashboard')}?date={yesterday.isoformat()}"
        )


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


def _fake_photo(name="site.jpg", content=b"fake-bytes", content_type="image/jpeg"):
    return SimpleUploadedFile(name, content, content_type=content_type)


class JobVisitWorkflowTests(TestCase):
    """On route -> arrived (+ before photos) -> parts disposed ->
    complete (+ after photos, notes, outcome) — see views._job_visit_context
    and the JobVisit.Stage gating in job_arrived/job_detail/job_complete."""

    def setUp(self):
        self.locksmith, self.user = _make_locksmith_user(soter_ids=("885",), driver_serials=("011",))
        self.client.force_login(self.user)
        self.today = timezone.localdate()
        self.order_no = f"496390_{self.today.isoformat()}"

        self.optimo_patch = patch("apps.locksmith_portal.views.get_optimo_client")
        mock_get_optimo = self.optimo_patch.start()
        self.addCleanup(self.optimo_patch.stop)
        self.mock_optimo = MagicMock()
        self.mock_optimo.list_orders_for_date.return_value = [
            OptimoOrderSummary(
                order_no=self.order_no, driver_serial="011", distance_metres=0, travel_time_seconds=0
            ),
        ]
        mock_get_optimo.return_value = self.mock_optimo

        self.handl_patch = patch("apps.locksmith_portal.views.get_handl_client")
        mock_get_handl = self.handl_patch.start()
        self.addCleanup(self.handl_patch.stop)
        self.mock_handl = MagicMock()
        self.mock_handl.list_current_stock.return_value = []
        # No known loss_type by default (falls back to a single generic
        # "photo_after" slot) — tests exercising a specific service
        # (Gain access/AKL/Spare Key) override this per-test.
        self.mock_handl.get_job_details.return_value = {}
        mock_get_handl.return_value = self.mock_handl

        self.storage_patch = patch("apps.locksmith_portal.views.get_photo_storage")
        mock_get_storage = self.storage_patch.start()
        self.addCleanup(self.storage_patch.stop)
        self.mock_storage = MagicMock()
        self.mock_storage.upload.side_effect = (
            lambda **kwargs: f"https://example.blob.core.windows.net/job-photos/{kwargs['report_id']}/{kwargs['stage']}/{kwargs['filename']}"
        )
        mock_get_storage.return_value = self.mock_storage

        # A handful of office-configured failure categories — one from
        # each sub-question group, plus one hidden from the locksmith —
        # covering what real office data looks like (see
        # views._FAILURE_CATEGORIES_*).
        self.category_wrong_parts = FailureCategory.objects.create(
            name="Incorrect parts - ordered by WGTK", master_reason=FailureCategory.MasterReason.WGTK_OFFICE,
        )
        self.category_programmer_issue = FailureCategory.objects.create(
            name="programmer issue", master_reason=FailureCategory.MasterReason.WGTK_LOCKSMITH,
        )
        self.category_notes_only = FailureCategory.objects.create(
            name="Vehicle Issues", master_reason=FailureCategory.MasterReason.NONE,
        )
        FailureCategory.objects.create(
            name="Skill set - locksmith", master_reason=FailureCategory.MasterReason.WGTK_LOCKSMITH,
        )
        FailureCategory.objects.create(name="Not a failure", master_reason=FailureCategory.MasterReason.NONE)
        FailureCategory.objects.create(name="Uncategorized", master_reason=FailureCategory.MasterReason.NONE)

    def _visit(self):
        return JobVisit.objects.get(locksmith=self.locksmith, order_no=self.order_no)

    # --- overview -----------------------------------------------------

    def test_overview_creates_visit_and_shows_on_route_action(self):
        url = reverse("locksmith_portal:job_overview", args=[self.order_no])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["visit"].stage, JobVisit.Stage.NOT_STARTED)
        self.assertContains(response, "Mark on route")
        self.assertNotContains(response, "Arrived</a>")

    def test_overview_not_on_schedule_redirects(self):
        url = reverse("locksmith_portal:job_overview", args=[f"999999_{self.today.isoformat()}"])
        response = self.client.get(url)
        self.assertRedirects(
            response, f"{reverse('locksmith_portal:dashboard')}?date={self.today.isoformat()}"
        )

    def test_overview_gain_access_shows_access_method_step_before_parts(self):
        self._set_loss_type("LOCKED IN PROPERTY")
        self._arrived_visit()
        url = reverse("locksmith_portal:job_overview", args=[self.order_no])
        response = self.client.get(url)
        self.assertTrue(response.context["is_gain_access"])
        self.assertContains(response, "Record access method")
        self.assertNotContains(response, "Dispose parts")

    def test_overview_non_gain_access_hides_access_method_step(self):
        self._set_loss_type("LOST")
        self._arrived_visit()
        url = reverse("locksmith_portal:job_overview", args=[self.order_no])
        response = self.client.get(url)
        self.assertFalse(response.context["is_gain_access"])
        self.assertNotContains(response, "Access method")
        self.assertContains(response, "Dispose parts")

    # --- on route -------------------------------------------------------

    def test_on_route_advances_stage_and_writes_handl_note(self):
        url = reverse("locksmith_portal:job_on_route", args=[self.order_no])
        response = self.client.post(url)

        visit = self._visit()
        self.assertEqual(visit.stage, JobVisit.Stage.ON_ROUTE)
        self.assertIsNotNone(visit.on_route_at)
        self.assertRedirects(
            response,
            f"{reverse('locksmith_portal:job_overview', args=[self.order_no])}?date={self.today.isoformat()}",
        )
        self.mock_handl.add_report_note.assert_called_once()
        args, kwargs = self.mock_handl.add_report_note.call_args
        self.assertEqual(args[0], "496390")
        self.assertIn("on route", args[1])
        self.mock_optimo.update_completion_status.assert_called_once_with(
            self.order_no, "on_route", start_time=None, end_time=None
        )

    def test_on_route_get_not_allowed(self):
        url = reverse("locksmith_portal:job_on_route", args=[self.order_no])
        self.assertEqual(self.client.get(url).status_code, 405)

    def test_on_route_is_idempotent(self):
        url = reverse("locksmith_portal:job_on_route", args=[self.order_no])
        self.client.post(url)
        first_time = self._visit().on_route_at
        self.client.post(url)
        self.assertEqual(self._visit().on_route_at, first_time)
        self.mock_handl.add_report_note.assert_called_once()

    # --- arrived (before photos) ----------------------------------------

    def test_arrived_requires_on_route_first(self):
        url = reverse("locksmith_portal:job_arrived", args=[self.order_no])
        response = self.client.get(url)
        self.assertRedirects(
            response,
            f"{reverse('locksmith_portal:job_overview', args=[self.order_no])}?date={self.today.isoformat()}",
        )

    def test_arrived_requires_at_least_one_photo(self):
        JobVisit.objects.create(
            locksmith=self.locksmith, order_no=self.order_no, report_id="496390",
            stage=JobVisit.Stage.ON_ROUTE, on_route_at=timezone.now(),
        )
        url = reverse("locksmith_portal:job_arrived", args=[self.order_no])
        response = self.client.post(url, {})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._visit().stage, JobVisit.Stage.ON_ROUTE)
        self.assertContains(response, "Add at least one")

    def test_arrived_uploads_photo_advances_stage_and_writes_note(self):
        JobVisit.objects.create(
            locksmith=self.locksmith, order_no=self.order_no, report_id="496390",
            stage=JobVisit.Stage.ON_ROUTE, on_route_at=timezone.now(),
        )
        url = reverse("locksmith_portal:job_arrived", args=[self.order_no])
        response = self.client.post(url, {"photo_before": [_fake_photo()]})

        visit = self._visit()
        self.assertEqual(visit.stage, JobVisit.Stage.ARRIVED)
        self.assertIsNotNone(visit.arrived_at)
        self.assertEqual(visit.photos.count(), 1)
        self.assertEqual(visit.photos.first().kind, JobVisitPhoto.Kind.BEFORE)
        self.assertRedirects(
            response,
            f"{reverse('locksmith_portal:job_overview', args=[self.order_no])}?date={self.today.isoformat()}",
        )
        note_text = self.mock_handl.add_report_note.call_args[0][1]
        self.assertIn("arrived", note_text)
        # A real <a> tag, not a bare URL — Handl's Notes field renders raw
        # HTML (confirmed live: an existing note's <strong> tag renders as
        # bold, not literal angle brackets), so this is a clickable link.
        self.assertIn(f'<a href="{visit.photos.first().url}" target="_blank">Photo 1</a>', note_text)
        self.mock_optimo.update_completion_status.assert_called_once_with(
            self.order_no, "servicing", start_time=visit.arrived_at, end_time=None
        )

    def test_arrived_rejects_non_image_file_and_does_not_advance(self):
        JobVisit.objects.create(
            locksmith=self.locksmith, order_no=self.order_no, report_id="496390",
            stage=JobVisit.Stage.ON_ROUTE, on_route_at=timezone.now(),
        )
        url = reverse("locksmith_portal:job_arrived", args=[self.order_no])
        bad_file = SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain")
        response = self.client.post(url, {"photo_before": [bad_file]})

        self.assertEqual(self._visit().stage, JobVisit.Stage.ON_ROUTE)
        self.assertContains(response, "isn&#x27;t an image")
        self.mock_handl.add_report_note.assert_not_called()

    # --- parts continue ---------------------------------------------------

    def test_parts_continue_advances_to_parts_done(self):
        JobVisit.objects.create(
            locksmith=self.locksmith, order_no=self.order_no, report_id="496390",
            stage=JobVisit.Stage.ARRIVED, arrived_at=timezone.now(),
        )
        url = reverse("locksmith_portal:job_parts_continue", args=[self.order_no])
        response = self.client.post(url)

        visit = self._visit()
        self.assertEqual(visit.stage, JobVisit.Stage.PARTS_DONE)
        self.assertIsNotNone(visit.parts_done_at)
        self.assertRedirects(
            response,
            f"{reverse('locksmith_portal:job_complete', args=[self.order_no])}?date={self.today.isoformat()}",
        )

    # --- complete -----------------------------------------------------

    def test_complete_requires_parts_done_first(self):
        JobVisit.objects.create(
            locksmith=self.locksmith, order_no=self.order_no, report_id="496390",
            stage=JobVisit.Stage.ARRIVED, arrived_at=timezone.now(),
        )
        url = reverse("locksmith_portal:job_complete", args=[self.order_no])
        response = self.client.get(url)
        self.assertRedirects(
            response,
            f"{reverse('locksmith_portal:job_overview', args=[self.order_no])}?date={self.today.isoformat()}",
        )

    def test_complete_requires_photo_and_outcome(self):
        JobVisit.objects.create(
            locksmith=self.locksmith, order_no=self.order_no, report_id="496390",
            stage=JobVisit.Stage.PARTS_DONE, parts_done_at=timezone.now(),
        )
        url = reverse("locksmith_portal:job_complete", args=[self.order_no])
        response = self.client.post(url, {"notes": "all good"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._visit().stage, JobVisit.Stage.PARTS_DONE)
        self.assertContains(response, "Add at least one photo")
        self.assertContains(response, "Choose Completed or Failed")

    def test_complete_success_marks_done_and_writes_note_with_outcome_and_notes(self):
        JobVisit.objects.create(
            locksmith=self.locksmith, order_no=self.order_no, report_id="496390",
            stage=JobVisit.Stage.PARTS_DONE, parts_done_at=timezone.now(),
        )
        url = reverse("locksmith_portal:job_complete", args=[self.order_no])
        response = self.client.post(
            url,
            {
                "photo_after": [_fake_photo(name="after.jpg")], "notes": "Left a spare key.",
                "outcome": "completed", "completion_signature": "data:image/png;base64,aGVsbG8=",
            },
        )

        visit = self._visit()
        self.assertEqual(visit.stage, JobVisit.Stage.DONE)
        self.assertEqual(visit.outcome, JobVisit.Outcome.COMPLETED)
        self.assertEqual(visit.notes, "Left a spare key.")
        self.assertIsNotNone(visit.completed_at)
        self.assertIsNotNone(visit.completion_signed_at)
        self.assertEqual(visit.photos.filter(kind=JobVisitPhoto.Kind.AFTER).count(), 1)
        self.assertEqual(visit.photos.filter(kind=JobVisitPhoto.Kind.COMPLETION_SIGNATURE).count(), 1)
        self.assertRedirects(
            response,
            f"{reverse('locksmith_portal:job_overview', args=[self.order_no])}?date={self.today.isoformat()}",
        )
        note_text = self.mock_handl.add_report_note.call_args[0][1]
        self.assertIn("Completed", note_text)
        self.assertIn("Left a spare key.", note_text)
        self.assertIn("happy with the job", note_text)
        self.assertIn(f'<a href="{visit.photos.get(kind=JobVisitPhoto.Kind.AFTER).url}" target="_blank">', note_text)
        self.mock_optimo.update_completion_status.assert_called_once_with(
            self.order_no, "success", start_time=visit.arrived_at, end_time=visit.completed_at
        )

    def test_complete_requires_completion_signature(self):
        JobVisit.objects.create(
            locksmith=self.locksmith, order_no=self.order_no, report_id="496390",
            stage=JobVisit.Stage.PARTS_DONE, parts_done_at=timezone.now(),
        )
        url = reverse("locksmith_portal:job_complete", args=[self.order_no])
        response = self.client.post(
            url, {"photo_after": [_fake_photo()], "outcome": "completed"},
        )
        self.assertContains(response, "customer needs to sign")
        self.assertEqual(self._visit().stage, JobVisit.Stage.PARTS_DONE)

    def test_complete_failed_outcome_pushes_failed_status_to_optimo(self):
        JobVisit.objects.create(
            locksmith=self.locksmith, order_no=self.order_no, report_id="496390",
            stage=JobVisit.Stage.PARTS_DONE, parts_done_at=timezone.now(),
        )
        url = reverse("locksmith_portal:job_complete", args=[self.order_no])
        self.client.post(url, {
            "photo_after": [_fake_photo()], "outcome": "failed",
            "failure_category": self.category_wrong_parts.pk, "failure_sku_needed": "TK-100",
        })
        self.mock_optimo.update_completion_status.assert_called_once_with(
            self.order_no, "failed", start_time=None, end_time=self._visit().completed_at
        )

    def test_complete_notes_are_html_escaped_before_writing_to_handl(self):
        # Handl's Notes field renders raw HTML — a locksmith's free-text
        # notes must be escaped so they can't inject markup into it,
        # unlike the photo URLs (ours, safe to embed as real <a> tags).
        JobVisit.objects.create(
            locksmith=self.locksmith, order_no=self.order_no, report_id="496390",
            stage=JobVisit.Stage.PARTS_DONE, parts_done_at=timezone.now(),
        )
        url = reverse("locksmith_portal:job_complete", args=[self.order_no])
        self.client.post(
            url,
            {
                "photo_after": [_fake_photo()], "notes": "<script>alert(1)</script>",
                "outcome": "completed", "completion_signature": "data:image/png;base64,aGVsbG8=",
            },
        )
        note_text = self.mock_handl.add_report_note.call_args[0][1]
        self.assertNotIn("<script>", note_text)
        self.assertIn("&lt;script&gt;", note_text)

    def test_complete_failed_outcome_recorded(self):
        JobVisit.objects.create(
            locksmith=self.locksmith, order_no=self.order_no, report_id="496390",
            stage=JobVisit.Stage.PARTS_DONE, parts_done_at=timezone.now(),
        )
        url = reverse("locksmith_portal:job_complete", args=[self.order_no])
        self.client.post(url, {
            "photo_after": [_fake_photo()], "outcome": "failed",
            "failure_category": self.category_wrong_parts.pk, "failure_sku_needed": "TK-100",
        })
        self.assertEqual(self._visit().outcome, JobVisit.Outcome.FAILED)
        self.assertEqual(self._visit().failure_category, self.category_wrong_parts)

    def test_complete_already_done_redirects_with_info(self):
        JobVisit.objects.create(
            locksmith=self.locksmith, order_no=self.order_no, report_id="496390",
            stage=JobVisit.Stage.DONE, completed_at=timezone.now(),
        )
        url = reverse("locksmith_portal:job_complete", args=[self.order_no])
        response = self.client.get(url, follow=True)
        self.assertContains(response, "already marked done")

    # --- per-service completion flow: Gain access ------------------------

    def _set_loss_type(self, raw_loss_type):
        self.mock_handl.get_job_details.return_value = {
            "496390": JobDetails(
                report_id="496390", make="Ford", model="Focus", year="2020", reg="AB20 CDE", vin="VIN1",
                service_type="Car", loss_type=raw_loss_type, supplied_service="", net_cost=100.0,
            )
        }

    def _parts_done_visit(self):
        return JobVisit.objects.create(
            locksmith=self.locksmith, order_no=self.order_no, report_id="496390",
            stage=JobVisit.Stage.PARTS_DONE, parts_done_at=timezone.now(),
        )

    def _arrived_visit(self):
        return JobVisit.objects.create(
            locksmith=self.locksmith, order_no=self.order_no, report_id="496390",
            stage=JobVisit.Stage.ARRIVED, arrived_at=timezone.now(),
        )

    def test_gain_access_get_shows_picked_and_airbag_choice(self):
        self._set_loss_type("LOCKED IN PROPERTY")
        self._arrived_visit()
        url = reverse("locksmith_portal:job_access_method", args=[self.order_no])
        response = self.client.get(url)
        self.assertContains(response, "Picked")
        self.assertContains(response, "Airbag")
        self.assertContains(response, "signature-pad")

    def test_gain_access_requires_an_access_method_choice(self):
        self._set_loss_type("LOCKED IN PROPERTY")
        self._arrived_visit()
        url = reverse("locksmith_portal:job_access_method", args=[self.order_no])
        response = self.client.post(url, {})
        self.assertContains(response, "Choose whether you picked the lock or used the airbag")
        self.assertEqual(self._visit().stage, JobVisit.Stage.ARRIVED)
        self.assertEqual(self._visit().access_method, "")

    def test_gain_access_picked_requires_pick_used_text(self):
        self._set_loss_type("LOCKED IN PROPERTY")
        self._arrived_visit()
        url = reverse("locksmith_portal:job_access_method", args=[self.order_no])
        response = self.client.post(url, {"access_method": "picked"})
        self.assertContains(response, "Enter what pick was used")

    def test_gain_access_picked_success_records_pick_used_and_notes_handl(self):
        self._set_loss_type("LOCKED IN PROPERTY")
        self._arrived_visit()
        url = reverse("locksmith_portal:job_access_method", args=[self.order_no])
        response = self.client.post(url, {"access_method": "picked", "pick_used": "Slim jim"})
        visit = self._visit()
        self.assertEqual(visit.stage, JobVisit.Stage.ARRIVED)
        self.assertEqual(visit.access_method, JobVisit.AccessMethod.PICKED)
        self.assertEqual(visit.pick_used, "Slim jim")
        self.assertRedirects(
            response,
            f"{reverse('locksmith_portal:job_overview', args=[self.order_no])}?date={self.today.isoformat()}",
        )
        note_text = self.mock_handl.add_report_note.call_args[0][1]
        self.assertIn("picking (pick used: Slim jim)", note_text)

    def test_gain_access_airbag_requires_signature(self):
        self._set_loss_type("LOCKED IN PROPERTY")
        self._arrived_visit()
        url = reverse("locksmith_portal:job_access_method", args=[self.order_no])
        response = self.client.post(
            url, {"access_method": "airbag", "photo_door_frame": [_fake_photo()]}
        )
        self.assertContains(response, "customer needs to sign the disclaimer")

    def test_gain_access_airbag_requires_door_frame_photo(self):
        self._set_loss_type("LOCKED IN PROPERTY")
        self._arrived_visit()
        url = reverse("locksmith_portal:job_access_method", args=[self.order_no])
        response = self.client.post(url, {
            "access_method": "airbag", "disclaimer_signature": "data:image/png;base64,aGVsbG8=",
        })
        self.assertContains(response, "Add at least one photo: Door frame")

    def test_gain_access_airbag_success_stores_signature_and_notes_handl(self):
        self._set_loss_type("LOCKED IN PROPERTY")
        self._arrived_visit()
        url = reverse("locksmith_portal:job_access_method", args=[self.order_no])
        response = self.client.post(url, {
            "access_method": "airbag",
            "disclaimer_signature": "data:image/png;base64,aGVsbG8=",
            "photo_door_frame": [_fake_photo()],
        })
        visit = self._visit()
        self.assertEqual(visit.stage, JobVisit.Stage.ARRIVED)
        self.assertEqual(visit.access_method, JobVisit.AccessMethod.AIRBAG)
        self.assertIsNotNone(visit.disclaimer_signed_at)
        self.assertEqual(visit.photos.filter(kind=JobVisitPhoto.Kind.DISCLAIMER_SIGNATURE).count(), 1)
        self.assertEqual(visit.photos.filter(kind=JobVisitPhoto.Kind.DOOR_FRAME).count(), 1)
        self.assertRedirects(
            response,
            f"{reverse('locksmith_portal:job_overview', args=[self.order_no])}?date={self.today.isoformat()}",
        )
        note_text = self.mock_handl.add_report_note.call_args[0][1]
        self.assertIn("signed the damage disclaimer", note_text)
        self.assertIn("Door frame:", note_text)

    def test_gain_access_non_gain_access_job_redirects_to_overview(self):
        self._set_loss_type("LOST")
        self._arrived_visit()
        url = reverse("locksmith_portal:job_access_method", args=[self.order_no])
        response = self.client.get(url)
        self.assertRedirects(
            response,
            f"{reverse('locksmith_portal:job_overview', args=[self.order_no])}?date={self.today.isoformat()}",
        )

    def test_gain_access_parts_disposal_redirects_until_access_method_recorded(self):
        self._set_loss_type("LOCKED IN PROPERTY")
        self._arrived_visit()
        url = reverse("locksmith_portal:job_detail", args=[self.order_no])
        response = self.client.get(url)
        self.assertRedirects(
            response,
            f"{reverse('locksmith_portal:job_access_method', args=[self.order_no])}?date={self.today.isoformat()}",
        )

    def test_gain_access_parts_disposal_allowed_once_access_method_recorded(self):
        self._set_loss_type("LOCKED IN PROPERTY")
        visit = self._arrived_visit()
        visit.access_method = JobVisit.AccessMethod.PICKED
        visit.pick_used = "Slim jim"
        visit.save(update_fields=["access_method", "pick_used"])
        url = reverse("locksmith_portal:job_detail", args=[self.order_no])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    # --- per-service completion flow: AKL / Spare Key --------------------

    def test_akl_arrival_get_shows_named_photo_slots(self):
        self._set_loss_type("LOST")
        JobVisit.objects.create(
            locksmith=self.locksmith, order_no=self.order_no, report_id="496390",
            stage=JobVisit.Stage.ON_ROUTE, on_route_at=timezone.now(),
        )
        url = reverse("locksmith_portal:job_arrived", args=[self.order_no])
        response = self.client.get(url)
        self.assertContains(response, "Front of the car")
        self.assertContains(response, "Door with the lock")

    def test_akl_arrival_missing_required_slot_is_rejected(self):
        self._set_loss_type("LOST")
        JobVisit.objects.create(
            locksmith=self.locksmith, order_no=self.order_no, report_id="496390",
            stage=JobVisit.Stage.ON_ROUTE, on_route_at=timezone.now(),
        )
        url = reverse("locksmith_portal:job_arrived", args=[self.order_no])
        response = self.client.post(url, {"photo_front_of_car": [_fake_photo()]})
        self.assertContains(response, "Add at least one photo: Door with the lock")
        self.assertEqual(self._visit().stage, JobVisit.Stage.ON_ROUTE)

    def test_akl_arrival_success_advances_stage(self):
        self._set_loss_type("LOST")
        JobVisit.objects.create(
            locksmith=self.locksmith, order_no=self.order_no, report_id="496390",
            stage=JobVisit.Stage.ON_ROUTE, on_route_at=timezone.now(),
        )
        url = reverse("locksmith_portal:job_arrived", args=[self.order_no])
        response = self.client.post(url, {
            "photo_front_of_car": [_fake_photo(name="a.jpg")],
            "photo_door_lock": [_fake_photo(name="b.jpg")],
        })
        self.assertRedirects(
            response,
            f"{reverse('locksmith_portal:job_overview', args=[self.order_no])}?date={self.today.isoformat()}",
        )
        self.assertEqual(self._visit().stage, JobVisit.Stage.ARRIVED)
        self.assertEqual(self._visit().photos.filter(kind=JobVisitPhoto.Kind.DAMAGE).count(), 0)

    def test_akl_get_shows_named_photo_slots(self):
        self._set_loss_type("LOST")
        self._parts_done_visit()
        url = reverse("locksmith_portal:job_complete", args=[self.order_no])
        response = self.client.get(url)
        self.assertEqual(response.context["loss_label"], "AKL")
        self.assertContains(response, "Ignition on")
        self.assertContains(response, "Keys supplied")
        self.assertNotContains(response, "Client&#x27;s key")

    def test_akl_missing_required_slot_is_rejected(self):
        self._set_loss_type("LOST")
        self._parts_done_visit()
        url = reverse("locksmith_portal:job_complete", args=[self.order_no])
        response = self.client.post(url, {
            "photo_keys_supplied": [_fake_photo()],
            # missing photo_ignition_on
            "outcome": "completed",
        })
        self.assertContains(response, "Add at least one photo: Ignition on")
        self.assertEqual(self._visit().stage, JobVisit.Stage.PARTS_DONE)

    def test_akl_damage_slot_is_optional(self):
        self._set_loss_type("LOST")
        self._parts_done_visit()
        url = reverse("locksmith_portal:job_complete", args=[self.order_no])
        response = self.client.post(url, {
            "photo_keys_supplied": [_fake_photo(name="c.jpg")],
            "photo_ignition_on": [_fake_photo(name="d.jpg")],
            "outcome": "completed", "completion_signature": "data:image/png;base64,aGVsbG8=",
        })
        self.assertRedirects(
            response,
            f"{reverse('locksmith_portal:job_overview', args=[self.order_no])}?date={self.today.isoformat()}",
        )
        self.assertEqual(self._visit().photos.filter(kind=JobVisitPhoto.Kind.DAMAGE).count(), 0)

    def test_spare_key_requires_client_key_photo(self):
        self._set_loss_type("Spare Key")
        self._parts_done_visit()
        url = reverse("locksmith_portal:job_complete", args=[self.order_no])
        response = self.client.post(url, {
            "photo_front_of_car": [_fake_photo(name="a.jpg")],
            "photo_door_lock": [_fake_photo(name="b.jpg")],
            "photo_keys_supplied": [_fake_photo(name="c.jpg")],
            "photo_ignition_on": [_fake_photo(name="d.jpg")],
            "outcome": "completed",
        })
        self.assertContains(response, "Add at least one photo: Client&#x27;s key")

    def test_spare_key_success_uploads_all_named_slots(self):
        self._set_loss_type("Spare Key")
        self._parts_done_visit()
        url = reverse("locksmith_portal:job_complete", args=[self.order_no])
        response = self.client.post(url, {
            "photo_front_of_car": [_fake_photo(name="a.jpg")],
            "photo_door_lock": [_fake_photo(name="b.jpg")],
            "photo_keys_supplied": [_fake_photo(name="c.jpg")],
            "photo_client_key": [_fake_photo(name="d.jpg")],
            "photo_ignition_on": [_fake_photo(name="e.jpg")],
            "outcome": "completed", "completion_signature": "data:image/png;base64,aGVsbG8=",
        })
        self.assertRedirects(
            response,
            f"{reverse('locksmith_portal:job_overview', args=[self.order_no])}?date={self.today.isoformat()}",
        )
        visit = self._visit()
        self.assertEqual(visit.photos.count(), 6)
        self.assertEqual(visit.photos.filter(kind=JobVisitPhoto.Kind.CLIENT_KEY).count(), 1)
        self.assertEqual(visit.photos.filter(kind=JobVisitPhoto.Kind.COMPLETION_SIGNATURE).count(), 1)

    # --- failure reasons (office-configured FailureCategory) -------------

    def test_failed_requires_a_failure_reason(self):
        self._parts_done_visit()
        url = reverse("locksmith_portal:job_complete", args=[self.order_no])
        response = self.client.post(url, {"photo_after": [_fake_photo()], "outcome": "failed"})
        self.assertContains(response, "Choose a reason for the failure")

    def test_failed_category_get_excludes_hidden_categories(self):
        self._parts_done_visit()
        url = reverse("locksmith_portal:job_complete", args=[self.order_no])
        response = self.client.get(url)
        names = {c["name"] for c in response.context["failure_categories"]}
        self.assertIn("Incorrect parts - ordered by WGTK", names)
        self.assertIn("programmer issue", names)
        self.assertIn("Vehicle Issues", names)
        self.assertNotIn("Skill set - locksmith", names)
        self.assertNotIn("Not a failure", names)
        self.assertNotIn("Uncategorized", names)

    def test_failed_sku_category_requires_sku(self):
        self._parts_done_visit()
        url = reverse("locksmith_portal:job_complete", args=[self.order_no])
        response = self.client.post(url, {
            "photo_after": [_fake_photo()], "outcome": "failed",
            "failure_category": self.category_wrong_parts.pk,
        })
        self.assertContains(response, "Enter the SKU / part needed")

    def test_failed_sku_category_success_records_sku_and_notes_handl(self):
        self._parts_done_visit()
        url = reverse("locksmith_portal:job_complete", args=[self.order_no])
        response = self.client.post(url, {
            "photo_after": [_fake_photo()], "outcome": "failed",
            "failure_category": self.category_wrong_parts.pk, "failure_sku_needed": "TK-100",
        })
        visit = self._visit()
        self.assertEqual(visit.failure_category, self.category_wrong_parts)
        self.assertEqual(visit.failure_sku_needed, "TK-100")
        self.assertRedirects(
            response,
            f"{reverse('locksmith_portal:job_overview', args=[self.order_no])}?date={self.today.isoformat()}",
        )
        note_text = self.mock_handl.add_report_note.call_args[0][1]
        self.assertIn("Incorrect parts - ordered by WGTK (SKU / part needed: TK-100)", note_text)

    def test_failed_reattend_category_requires_reattend_choice(self):
        self._parts_done_visit()
        url = reverse("locksmith_portal:job_complete", args=[self.order_no])
        response = self.client.post(url, {
            "photo_after": [_fake_photo()], "outcome": "failed",
            "failure_category": self.category_programmer_issue.pk,
        })
        self.assertContains(response, "Choose a reattend option")

    def test_failed_reattend_category_success_records_reattend_action(self):
        self._parts_done_visit()
        url = reverse("locksmith_portal:job_complete", args=[self.order_no])
        response = self.client.post(url, {
            "photo_after": [_fake_photo()], "outcome": "failed",
            "failure_category": self.category_programmer_issue.pk,
            "failure_reattend_action": "different_locksmith",
        })
        visit = self._visit()
        self.assertEqual(visit.failure_category, self.category_programmer_issue)
        self.assertEqual(visit.failure_reattend_action, JobVisit.ReattendAction.DIFFERENT_LOCKSMITH)
        note_text = self.mock_handl.add_report_note.call_args[0][1]
        self.assertIn("programmer issue (Reattend with a different locksmith)", note_text)

    def test_failed_notes_only_category_needs_no_sub_field(self):
        self._parts_done_visit()
        url = reverse("locksmith_portal:job_complete", args=[self.order_no])
        response = self.client.post(url, {
            "photo_after": [_fake_photo()], "outcome": "failed",
            "failure_category": self.category_notes_only.pk,
        })
        self.assertRedirects(
            response,
            f"{reverse('locksmith_portal:job_overview', args=[self.order_no])}?date={self.today.isoformat()}",
        )
        self.assertEqual(self._visit().failure_category, self.category_notes_only)
        note_text = self.mock_handl.add_report_note.call_args[0][1]
        self.assertIn("Failure reason: Vehicle Issues.", note_text)

    def test_failed_hidden_category_id_is_rejected(self):
        self._parts_done_visit()
        hidden = FailureCategory.objects.get(name="Skill set - locksmith")
        url = reverse("locksmith_portal:job_complete", args=[self.order_no])
        response = self.client.post(url, {
            "photo_after": [_fake_photo()], "outcome": "failed", "failure_category": hidden.pk,
        })
        self.assertContains(response, "Choose a reason for the failure")

    # --- parts (job_detail) gated behind arrived ------------------------

    def test_job_detail_gated_until_arrived(self):
        url = reverse("locksmith_portal:job_detail", args=[self.order_no])
        response = self.client.get(url)
        self.assertRedirects(
            response,
            f"{reverse('locksmith_portal:job_overview', args=[self.order_no])}?date={self.today.isoformat()}",
        )

    # --- login required across the new views -----------------------------

    def test_login_required_on_new_views(self):
        self.client.logout()
        for name in ("job_overview", "job_arrived", "job_complete"):
            url = reverse(f"locksmith_portal:{name}", args=[self.order_no])
            self.assertEqual(self.client.get(url).status_code, 302, name)
        for name in ("job_on_route", "job_parts_continue"):
            url = reverse(f"locksmith_portal:{name}", args=[self.order_no])
            self.assertEqual(self.client.post(url).status_code, 302, name)


class DecodeDataUrlTests(TestCase):
    def test_decodes_content_type_and_bytes(self):
        from apps.locksmith_portal.views import _decode_data_url

        content_type, content = _decode_data_url("data:image/png;base64,aGVsbG8=")
        self.assertEqual(content_type, "image/png")
        self.assertEqual(content, b"hello")
