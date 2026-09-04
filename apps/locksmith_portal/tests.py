from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.integrations.handl import CurrentStockLine
from apps.integrations.optimo import OptimoOrderSummary
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
        mock_client = MagicMock()
        mock_client.list_orders_for_date.return_value = [
            OptimoOrderSummary(
                order_no=self.order_no, driver_serial="011", distance_metres=0, travel_time_seconds=0
            ),
        ]
        mock_get_optimo.return_value = mock_client

        self.handl_patch = patch("apps.locksmith_portal.views.get_handl_client")
        mock_get_handl = self.handl_patch.start()
        self.addCleanup(self.handl_patch.stop)
        self.mock_handl = MagicMock()
        self.mock_handl.list_current_stock.return_value = []
        mock_get_handl.return_value = self.mock_handl

        self.storage_patch = patch("apps.locksmith_portal.views.get_photo_storage")
        mock_get_storage = self.storage_patch.start()
        self.addCleanup(self.storage_patch.stop)
        self.mock_storage = MagicMock()
        self.mock_storage.upload.side_effect = (
            lambda **kwargs: f"https://example.blob.core.windows.net/job-photos/{kwargs['report_id']}/{kwargs['stage']}/{kwargs['filename']}"
        )
        mock_get_storage.return_value = self.mock_storage

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
        response = self.client.post(url, {"photos": [_fake_photo()]})

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
        self.assertIn(visit.photos.first().url, note_text)

    def test_arrived_rejects_non_image_file_and_does_not_advance(self):
        JobVisit.objects.create(
            locksmith=self.locksmith, order_no=self.order_no, report_id="496390",
            stage=JobVisit.Stage.ON_ROUTE, on_route_at=timezone.now(),
        )
        url = reverse("locksmith_portal:job_arrived", args=[self.order_no])
        bad_file = SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain")
        response = self.client.post(url, {"photos": [bad_file]})

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
            url, {"photos": [_fake_photo(name="after.jpg")], "notes": "Left a spare key.", "outcome": "completed"}
        )

        visit = self._visit()
        self.assertEqual(visit.stage, JobVisit.Stage.DONE)
        self.assertEqual(visit.outcome, JobVisit.Outcome.COMPLETED)
        self.assertEqual(visit.notes, "Left a spare key.")
        self.assertIsNotNone(visit.completed_at)
        self.assertEqual(visit.photos.filter(kind=JobVisitPhoto.Kind.AFTER).count(), 1)
        self.assertRedirects(
            response,
            f"{reverse('locksmith_portal:job_overview', args=[self.order_no])}?date={self.today.isoformat()}",
        )
        note_text = self.mock_handl.add_report_note.call_args[0][1]
        self.assertIn("Completed", note_text)
        self.assertIn("Left a spare key.", note_text)

    def test_complete_failed_outcome_recorded(self):
        JobVisit.objects.create(
            locksmith=self.locksmith, order_no=self.order_no, report_id="496390",
            stage=JobVisit.Stage.PARTS_DONE, parts_done_at=timezone.now(),
        )
        url = reverse("locksmith_portal:job_complete", args=[self.order_no])
        self.client.post(url, {"photos": [_fake_photo()], "outcome": "failed"})
        self.assertEqual(self._visit().outcome, JobVisit.Outcome.FAILED)

    def test_complete_already_done_redirects_with_info(self):
        JobVisit.objects.create(
            locksmith=self.locksmith, order_no=self.order_no, report_id="496390",
            stage=JobVisit.Stage.DONE, completed_at=timezone.now(),
        )
        url = reverse("locksmith_portal:job_complete", args=[self.order_no])
        response = self.client.get(url, follow=True)
        self.assertContains(response, "already marked done")

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
