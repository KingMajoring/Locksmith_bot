from datetime import date, datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.integrations.handl import JobDetails
from apps.integrations.optimo import OptimoCompletion, OptimoOrderSummary
from apps.locksmiths.models import Locksmith, OptimoDriverId

from .models import CompletedJob, FailureCategory, SLATarget
from .services.benchmarking import duration_benchmark
from .services.costing import parts_cost_for_jobs
from .services.daily import day_pills, jobs_for_day, next_offset
from .services.model_analysis import (
    company_model_failure_breakdown,
    locksmith_model_failure_breakdown,
)
from .services.model_normalization import normalize_model
from .services.pulling import pull_completed_jobs_for_date
from .services.reporting import (
    all_locksmith_summaries,
    failure_category_breakdown,
    master_reason_breakdown,
    needs_categorization_queryset,
)


class FakeOptimoClient:
    """Deterministic stand-in for OptimoClient, for tests that need exact
    control rather than the (seeded-but-opaque) MockOptimoClient."""

    def __init__(self, summaries, completions):
        self._summaries = summaries
        self._completions = completions

    def list_orders_for_date(self, for_date):
        return self._summaries

    def get_completion_details(self, order_nos):
        return {no: self._completions[no] for no in order_nos if no in self._completions}


class FakeHandlClient:
    def __init__(self, details_by_report_id, disposed_skus_by_report_id=None):
        self._details = details_by_report_id
        self._disposed_skus = disposed_skus_by_report_id or {}

    def get_job_details(self, report_ids):
        return {rid: self._details[rid] for rid in report_ids if rid in self._details}

    def get_disposed_skus(self, report_ids):
        return {rid: self._disposed_skus[rid] for rid in report_ids if rid in self._disposed_skus}


class FakeCostHandlClient:
    def __init__(self, costs):
        self._costs = costs

    def get_part_costs(self, skus):
        return {sku: self._costs[sku] for sku in skus if sku in self._costs}


def _make_locksmith(name="WGTK - Test", driver_serial="011"):
    locksmith = Locksmith.objects.create(name=name)
    OptimoDriverId.objects.create(locksmith=locksmith, optimo_driver_serial=driver_serial)
    return locksmith


class PullingTests(TestCase):
    def setUp(self):
        self.locksmith = _make_locksmith()
        self.for_date = date(2026, 9, 1)
        self.summaries = [
            OptimoOrderSummary(
                order_no=f"1001_{self.for_date.isoformat()}",
                driver_serial="011",
                distance_metres=5000.0,
                travel_time_seconds=600,
            ),
            OptimoOrderSummary(
                order_no=f"1002_{self.for_date.isoformat()}",
                driver_serial="999",  # unmapped driver
                distance_metres=2000.0,
                travel_time_seconds=300,
            ),
            OptimoOrderSummary(
                order_no=f"1003_{self.for_date.isoformat()}",
                driver_serial="011",
                distance_metres=1000.0,
                travel_time_seconds=120,
            ),
        ]
        self.completions = {
            f"1001_{self.for_date.isoformat()}": OptimoCompletion(
                order_no=f"1001_{self.for_date.isoformat()}",
                status="success",
                start_time=datetime(2026, 9, 1, 9, 0, tzinfo=dt_timezone.utc),
                end_time=datetime(2026, 9, 1, 9, 30, tzinfo=dt_timezone.utc),
                note="",
            ),
            f"1002_{self.for_date.isoformat()}": OptimoCompletion(
                order_no=f"1002_{self.for_date.isoformat()}",
                status="failed",
                start_time=datetime(2026, 9, 1, 10, 0, tzinfo=dt_timezone.utc),
                end_time=datetime(2026, 9, 1, 10, 10, tzinfo=dt_timezone.utc),
                note="Customer not available",
            ),
            f"1003_{self.for_date.isoformat()}": OptimoCompletion(
                order_no=f"1003_{self.for_date.isoformat()}",
                status="scheduled",
                start_time=None,
                end_time=None,
                note="",
            ),
        }
        self.job_details = {
            "1001": JobDetails(
                report_id="1001", make="Ford", model="Focus", year="2020",
                vin="VIN1001", service_type="Car", loss_type="Lockout",
                supplied_service="Non-Destructive Entry",
            ),
            "1002": JobDetails(
                report_id="1002", make="BMW", model="335D", year="2019",
                vin="VIN1002", service_type="Car", loss_type="Lost Keys",
                supplied_service="Key Programming",
            ),
        }
        self.disposed_skus = {"1001": ["TK-100", "TK-104"]}

    def _run_pull(self):
        with patch(
            "apps.job_completion.services.pulling.get_optimo_client",
            return_value=FakeOptimoClient(self.summaries, self.completions),
        ), patch(
            "apps.job_completion.services.pulling.get_handl_client",
            return_value=FakeHandlClient(self.job_details, self.disposed_skus),
        ):
            return pull_completed_jobs_for_date(self.for_date)

    def test_only_completed_orders_are_stored(self):
        summary = self._run_pull()
        self.assertEqual(summary.created, 2)
        self.assertEqual(summary.skipped_not_completed, 1)
        self.assertEqual(CompletedJob.objects.count(), 2)

    def test_locksmith_resolved_from_optimo_driver_id(self):
        self._run_pull()
        job = CompletedJob.objects.get(order_no=f"1001_{self.for_date.isoformat()}")
        self.assertEqual(job.locksmith, self.locksmith)

    def test_unmapped_driver_leaves_locksmith_blank(self):
        self._run_pull()
        job = CompletedJob.objects.get(order_no=f"1002_{self.for_date.isoformat()}")
        self.assertIsNone(job.locksmith)
        self.assertEqual(job.driver_serial, "999")

    def test_job_details_resolved_from_handl(self):
        self._run_pull()
        job = CompletedJob.objects.get(order_no=f"1001_{self.for_date.isoformat()}")
        self.assertEqual(job.make, "Ford")
        self.assertEqual(job.service_type, "Car")
        self.assertEqual(job.loss_type, "Lockout")
        self.assertEqual(job.supplied_service, "Non-Destructive Entry")

    def test_duration_computed_from_start_end_time(self):
        self._run_pull()
        job = CompletedJob.objects.get(order_no=f"1001_{self.for_date.isoformat()}")
        self.assertEqual(job.duration_minutes, 30)

    def test_disposed_skus_stored_comma_separated(self):
        self._run_pull()
        job = CompletedJob.objects.get(order_no=f"1001_{self.for_date.isoformat()}")
        self.assertEqual(job.disposed_skus, "TK-100, TK-104")

    def test_no_disposed_skus_leaves_field_blank(self):
        self._run_pull()
        job = CompletedJob.objects.get(order_no=f"1002_{self.for_date.isoformat()}")
        self.assertEqual(job.disposed_skus, "")

    def test_rerun_does_not_duplicate_or_overwrite_category(self):
        self._run_pull()
        job = CompletedJob.objects.get(order_no=f"1002_{self.for_date.isoformat()}")
        category = FailureCategory.objects.create(name="Customer not present")
        job.failure_category = category
        job.save()

        second_summary = self._run_pull()
        self.assertEqual(second_summary.created, 0)
        self.assertEqual(second_summary.updated, 2)
        self.assertEqual(CompletedJob.objects.count(), 2)

        job.refresh_from_db()
        self.assertEqual(job.failure_category, category)

    def test_non_numeric_report_id_is_not_sent_to_handl(self):
        """Regression test: confirmed live that not every Optimo order
        is a Handl claim — an ad-hoc job's orderNo was literally
        "Sort flat tyre_2026-01-02", giving a non-numeric "report_id"
        that broke Handl's integer ReportID column for the whole day's
        batch, not just that one job."""
        for_date = date(2026, 1, 2)
        order_no = f"Sort flat tyre_{for_date.isoformat()}"
        summaries = [
            OptimoOrderSummary(
                order_no=order_no, driver_serial="011", distance_metres=100.0, travel_time_seconds=60
            )
        ]
        completions = {
            order_no: OptimoCompletion(
                order_no=order_no,
                status="success",
                start_time=datetime(2026, 1, 2, 9, 0, tzinfo=dt_timezone.utc),
                end_time=datetime(2026, 1, 2, 9, 20, tzinfo=dt_timezone.utc),
                note="",
            )
        }

        class StrictFakeHandlClient:
            def get_job_details(self, report_ids):
                assert all(rid.isdigit() for rid in report_ids), report_ids
                return {}

            def get_disposed_skus(self, report_ids):
                assert all(rid.isdigit() for rid in report_ids), report_ids
                return {}

        with patch(
            "apps.job_completion.services.pulling.get_optimo_client",
            return_value=FakeOptimoClient(summaries, completions),
        ), patch(
            "apps.job_completion.services.pulling.get_handl_client",
            return_value=StrictFakeHandlClient(),
        ):
            summary = pull_completed_jobs_for_date(for_date)

        self.assertEqual(summary.created, 1)
        job = CompletedJob.objects.get(order_no=order_no)
        self.assertEqual(job.report_id, "Sort flat tyre")
        self.assertEqual(job.make, "")


class BenchmarkingTests(TestCase):
    def setUp(self):
        self.locksmith = _make_locksmith()
        self.other_locksmith = _make_locksmith(name="WGTK - Other", driver_serial="023")
        SLATarget.objects.create(loss_type="Lockout", target_minutes=30, active=True)

    def _make_job(self, locksmith, minutes, status=CompletedJob.Status.SUCCESS, order_no=None):
        start = datetime(2026, 9, 1, 9, 0, tzinfo=dt_timezone.utc)
        end = start.replace(minute=minutes % 60, hour=9 + minutes // 60)
        CompletedJob.objects.create(
            order_no=order_no or f"job-{CompletedJob.objects.count()}",
            report_id="1001",
            job_date=date(2026, 9, 1),
            locksmith=locksmith,
            status=status,
            start_time=start,
            end_time=end,
            loss_type="Lockout",
        )

    def test_benchmark_includes_sla_target(self):
        result = duration_benchmark(self.locksmith, "Lockout")
        self.assertEqual(result.sla_target_minutes, 30)

    def test_company_average_includes_all_locksmiths(self):
        self._make_job(self.locksmith, 20, order_no="a")
        self._make_job(self.other_locksmith, 40, order_no="b")
        result = duration_benchmark(self.locksmith, "Lockout")
        self.assertEqual(result.company_avg_minutes, 30.0)
        self.assertEqual(result.company_sample_size, 2)

    def test_own_average_only_this_locksmith(self):
        self._make_job(self.locksmith, 20, order_no="a")
        self._make_job(self.other_locksmith, 40, order_no="b")
        result = duration_benchmark(self.locksmith, "Lockout")
        self.assertEqual(result.locksmith_avg_minutes, 20.0)
        self.assertEqual(result.locksmith_sample_size, 1)

    def test_failed_jobs_excluded_from_averages(self):
        self._make_job(self.locksmith, 20, status=CompletedJob.Status.FAILED, order_no="a")
        result = duration_benchmark(self.locksmith, "Lockout")
        self.assertIsNone(result.company_avg_minutes)
        self.assertEqual(result.company_sample_size, 0)


class ReportingTests(TestCase):
    def setUp(self):
        self.locksmith = _make_locksmith()

    def test_needs_categorization_only_uncategorized_failures(self):
        locksmith = _make_locksmith()
        CompletedJob.objects.create(
            order_no="a", report_id="1", job_date=date(2026, 9, 1),
            status=CompletedJob.Status.FAILED, locksmith=locksmith,
        )
        CompletedJob.objects.create(
            order_no="b", report_id="2", job_date=date(2026, 9, 1),
            status=CompletedJob.Status.SUCCESS, locksmith=locksmith,
        )
        category = FailureCategory.objects.create(name="Wrong parts")
        CompletedJob.objects.create(
            order_no="c", report_id="3", job_date=date(2026, 9, 1),
            status=CompletedJob.Status.FAILED, failure_category=category, locksmith=locksmith,
        )
        results = list(needs_categorization_queryset())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].order_no, "a")

    def test_needs_categorization_excludes_unmatched_driver_jobs(self):
        CompletedJob.objects.create(
            order_no="unmatched", report_id="9", job_date=date(2026, 9, 1),
            status=CompletedJob.Status.FAILED, driver_serial="SomeoneNew",
        )
        results = list(needs_categorization_queryset())
        self.assertEqual(results, [])

    def test_locksmith_summary_failure_rate(self):
        for i in range(3):
            CompletedJob.objects.create(
                order_no=f"s{i}", report_id=str(i), job_date=date(2026, 9, 1),
                locksmith=self.locksmith, status=CompletedJob.Status.SUCCESS,
            )
        CompletedJob.objects.create(
            order_no="f1", report_id="9", job_date=date(2026, 9, 1),
            locksmith=self.locksmith, status=CompletedJob.Status.FAILED,
        )
        summaries = all_locksmith_summaries()
        summary = next(s for s in summaries if s["locksmith"] == self.locksmith)
        self.assertEqual(summary["total_jobs"], 4)
        self.assertEqual(summary["failed_jobs"], 1)
        self.assertEqual(summary["failure_rate_pct"], 25.0)

    def test_failure_category_breakdown_groups_uncategorized(self):
        CompletedJob.objects.create(
            order_no="a", report_id="1", job_date=date(2026, 9, 1),
            status=CompletedJob.Status.FAILED,
        )
        breakdown = failure_category_breakdown()
        self.assertEqual(breakdown, [{"category": "Uncategorized", "count": 1}])

    def test_master_reason_breakdown_groups_by_category_master_reason(self):
        locksmith_fault = FailureCategory.objects.create(
            name="Wrong approach", master_reason=FailureCategory.MasterReason.WGTK_LOCKSMITH
        )
        client_fault = FailureCategory.objects.create(
            name="Customer not present", master_reason=FailureCategory.MasterReason.CLIENT
        )
        CompletedJob.objects.create(
            order_no="a", report_id="1", job_date=date(2026, 9, 1),
            status=CompletedJob.Status.FAILED, failure_category=locksmith_fault,
        )
        CompletedJob.objects.create(
            order_no="b", report_id="2", job_date=date(2026, 9, 1),
            status=CompletedJob.Status.FAILED, failure_category=locksmith_fault,
        )
        CompletedJob.objects.create(
            order_no="c", report_id="3", job_date=date(2026, 9, 1),
            status=CompletedJob.Status.FAILED, failure_category=client_fault,
        )
        CompletedJob.objects.create(
            order_no="d", report_id="4", job_date=date(2026, 9, 1),
            status=CompletedJob.Status.FAILED,
        )
        breakdown = master_reason_breakdown()
        self.assertEqual(
            breakdown,
            [
                {"master_reason": "WGTK Locksmith", "count": 2},
                {"master_reason": "Client", "count": 1},
                {"master_reason": "Uncategorized", "count": 1},
            ],
        )


class AdminCategorizationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="office_admin", email="admin@wgtk.co.uk", password="x",
            is_staff=True, is_superuser=True,
        )
        self.client.force_login(self.user)
        self.job = CompletedJob.objects.create(
            order_no="a", report_id="1", job_date=date(2026, 9, 1),
            status=CompletedJob.Status.FAILED,
        )
        self.category = FailureCategory.objects.create(name="Customer not present")

    def test_setting_category_via_change_form_stamps_audit_fields(self):
        url = reverse("admin:job_completion_completedjob_change", args=[self.job.pk])
        response = self.client.post(url, {"failure_category": self.category.pk})
        self.assertEqual(response.status_code, 302)
        self.job.refresh_from_db()
        self.assertEqual(self.job.failure_category, self.category)
        self.assertEqual(self.job.categorized_by, self.user)
        self.assertIsNotNone(self.job.categorized_at)


class ViewsSmokeTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="office_admin", email="admin@wgtk.co.uk", password="x", is_staff=True
        )
        self.client.force_login(self.user)
        self.locksmith = _make_locksmith()

    def test_dashboard_renders(self):
        response = self.client.get(reverse("job_completion:dashboard"))
        self.assertEqual(response.status_code, 200)

    def test_locksmith_report_renders(self):
        response = self.client.get(
            reverse("job_completion:locksmith_report", args=[self.locksmith.pk])
        )
        self.assertEqual(response.status_code, 200)

    def test_jobs_by_day_renders(self):
        response = self.client.get(reverse("job_completion:jobs_by_day"))
        self.assertEqual(response.status_code, 200)

    def test_jobs_by_day_with_offset_and_date_renders(self):
        response = self.client.get(
            reverse("job_completion:jobs_by_day"), {"offset": 7, "date": "2026-01-01"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected"], date(2026, 1, 1))

    def test_login_required_redirects_anonymous(self):
        self.client.logout()
        response = self.client.get(reverse("job_completion:dashboard"))
        self.assertEqual(response.status_code, 302)


class CategorizeJobsBulkViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="office_admin", email="admin@wgtk.co.uk", password="x", is_staff=True
        )
        self.client.force_login(self.user)
        self.locksmith = _make_locksmith()
        self.job1 = CompletedJob.objects.create(
            order_no="a", report_id="1", job_date=date(2026, 9, 1),
            status=CompletedJob.Status.FAILED, locksmith=self.locksmith,
        )
        self.job2 = CompletedJob.objects.create(
            order_no="b", report_id="2", job_date=date(2026, 9, 1),
            status=CompletedJob.Status.FAILED, locksmith=self.locksmith,
        )
        self.category = FailureCategory.objects.create(name="Customer not present")
        self.other_category = FailureCategory.objects.create(name="Wrong parts")

    def test_post_sets_category_and_audit_fields_for_multiple_jobs_at_once(self):
        response = self.client.post(
            reverse("job_completion:categorize_jobs"),
            {f"category_{self.job1.pk}": self.category.pk, f"category_{self.job2.pk}": self.other_category.pk},
        )
        self.assertRedirects(response, reverse("job_completion:dashboard"))
        self.job1.refresh_from_db()
        self.job2.refresh_from_db()
        self.assertEqual(self.job1.failure_category, self.category)
        self.assertEqual(self.job2.failure_category, self.other_category)
        self.assertEqual(self.job1.categorized_by, self.user)
        self.assertIsNotNone(self.job1.categorized_at)

    def test_rows_left_blank_are_skipped(self):
        response = self.client.post(
            reverse("job_completion:categorize_jobs"),
            {f"category_{self.job1.pk}": self.category.pk, f"category_{self.job2.pk}": ""},
        )
        self.assertRedirects(response, reverse("job_completion:dashboard"))
        self.job1.refresh_from_db()
        self.job2.refresh_from_db()
        self.assertEqual(self.job1.failure_category, self.category)
        self.assertIsNone(self.job2.failure_category)

    def test_nothing_selected_leaves_jobs_unchanged(self):
        self.client.post(
            reverse("job_completion:categorize_jobs"),
            {f"category_{self.job1.pk}": "", f"category_{self.job2.pk}": ""},
        )
        self.job1.refresh_from_db()
        self.assertIsNone(self.job1.failure_category)

    def test_login_required(self):
        self.client.logout()
        response = self.client.post(
            reverse("job_completion:categorize_jobs"), {f"category_{self.job1.pk}": self.category.pk}
        )
        self.assertEqual(response.status_code, 302)
        self.job1.refresh_from_db()
        self.assertIsNone(self.job1.failure_category)

    def test_dashboard_shows_dropdown_and_single_save_form(self):
        response = self.client.get(reverse("job_completion:dashboard"))
        self.assertContains(response, "Customer not present")
        self.assertContains(response, reverse("job_completion:categorize_jobs"))
        self.assertContains(response, f'name="category_{self.job1.pk}"')
        self.assertContains(response, f'name="category_{self.job2.pk}"')


class BackfillCompletedJobsCommandTests(TestCase):
    def test_calls_pull_for_every_day_in_range_and_sums_totals(self):
        from io import StringIO

        from django.core.management import call_command

        from apps.job_completion.services.pulling import PullSummary

        with patch(
            "apps.job_completion.management.commands.backfill_completed_jobs.pull_completed_jobs_for_date",
            side_effect=[
                PullSummary(created=2, updated=0, skipped_not_completed=1),
                PullSummary(created=0, updated=3, skipped_not_completed=0),
                PullSummary(created=1, updated=1, skipped_not_completed=2),
            ],
        ) as mock_pull:
            out = StringIO()
            call_command(
                "backfill_completed_jobs", "--start", "2026-01-01", "--end", "2026-01-03", stdout=out
            )

        self.assertEqual(mock_pull.call_count, 3)
        called_dates = [call.args[0] for call in mock_pull.call_args_list]
        self.assertEqual(
            called_dates, [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]
        )
        self.assertIn("3 created, 4 updated", out.getvalue())

    def test_start_after_end_does_nothing(self):
        from io import StringIO

        from django.core.management import call_command

        with patch(
            "apps.job_completion.management.commands.backfill_completed_jobs.pull_completed_jobs_for_date"
        ) as mock_pull:
            err = StringIO()
            call_command(
                "backfill_completed_jobs", "--start", "2026-02-01", "--end", "2026-01-01", stderr=err
            )
        mock_pull.assert_not_called()


class NormalizeModelTests(TestCase):
    def test_default_rule_takes_first_word(self):
        self.assertEqual(normalize_model("Vauxhall", "CORSA STING"), "CORSA")
        self.assertEqual(normalize_model("Vauxhall", "CORSA SE AUTO"), "CORSA")
        self.assertEqual(normalize_model("Toyota", "PROACE ICON"), "PROACE")

    def test_bmw_numeric_code_maps_to_series(self):
        self.assertEqual(normalize_model("BMW", "335D"), "3 Series")
        self.assertEqual(normalize_model("BMW", "430"), "4 Series")
        self.assertEqual(normalize_model("BMW", "320i"), "3 Series")
        self.assertEqual(normalize_model("BMW", "118d"), "1 Series")

    def test_bmw_non_numeric_model_falls_back_to_first_word(self):
        self.assertEqual(normalize_model("BMW", "X5"), "X5")
        self.assertEqual(normalize_model("BMW", "i3"), "I3")

    def test_mercedes_class_letter_prefix(self):
        self.assertEqual(normalize_model("Mercedes-Benz", "C220"), "C-Class")
        self.assertEqual(normalize_model("Mercedes-Benz", "C300 AMG Line"), "C-Class")
        self.assertEqual(normalize_model("Mercedes-Benz", "E250"), "E-Class")

    def test_mercedes_multi_letter_code_not_shadowed_by_single_letter(self):
        # "CLA45" must match the 3-letter "CLA" code, not the 1-letter
        # "C" alternative that happens to prefix it.
        self.assertEqual(normalize_model("Mercedes-Benz", "CLA45"), "CLA-Class")
        self.assertEqual(normalize_model("Mercedes-Benz", "GLC300"), "GLC-Class")

    def test_blank_model_returns_blank(self):
        self.assertEqual(normalize_model("Ford", ""), "")
        self.assertEqual(normalize_model("Ford", None), "")


class LocksmithModelFailureBreakdownTests(TestCase):
    def setUp(self):
        self.locksmith = _make_locksmith()

    def _make_job(self, make, model, status, order_no, locksmith=None, failure_category=None):
        CompletedJob.objects.create(
            order_no=order_no, report_id=order_no, job_date=date(2026, 9, 1),
            locksmith=locksmith or self.locksmith, status=status, make=make, model=model,
            failure_category=failure_category,
        )

    def test_groups_by_normalized_model_and_counts_failures(self):
        self._make_job("Vauxhall", "CORSA STING", CompletedJob.Status.FAILED, "a")
        self._make_job("Vauxhall", "CORSA SE AUTO", CompletedJob.Status.FAILED, "b")
        self._make_job("Vauxhall", "CORSA LIMITED", CompletedJob.Status.SUCCESS, "c")

        breakdown = locksmith_model_failure_breakdown()
        self.assertEqual(len(breakdown), 1)
        row = breakdown[0]
        self.assertEqual(row["model_family"], "CORSA")
        self.assertEqual(row["total"], 3)
        self.assertEqual(row["failed"], 2)
        self.assertAlmostEqual(row["failure_rate_pct"], 66.7, places=1)

    def test_jobs_with_no_failures_are_excluded(self):
        self._make_job("Ford", "FOCUS ST", CompletedJob.Status.SUCCESS, "a")
        breakdown = locksmith_model_failure_breakdown()
        self.assertEqual(breakdown, [])

    def test_jobs_without_locksmith_or_model_are_excluded(self):
        CompletedJob.objects.create(
            order_no="unmatched", report_id="9", job_date=date(2026, 9, 1),
            status=CompletedJob.Status.FAILED, make="Ford", model="Focus",
        )
        CompletedJob.objects.create(
            order_no="nomodel", report_id="10", job_date=date(2026, 9, 1),
            locksmith=self.locksmith, status=CompletedJob.Status.FAILED,
        )
        breakdown = locksmith_model_failure_breakdown()
        self.assertEqual(breakdown, [])

    def test_master_reason_breakdown_percentages(self):
        # 5 failed jobs on the same model family, 1 of them client-fault —
        # the breakdown should read "Client: 1/5 (20%)".
        client_fault = FailureCategory.objects.create(
            name="Customer not present", master_reason=FailureCategory.MasterReason.CLIENT
        )
        self._make_job("Ford", "FOCUS ST", CompletedJob.Status.FAILED, "a", failure_category=client_fault)
        self._make_job("Ford", "FOCUS TITANIUM", CompletedJob.Status.FAILED, "b")
        self._make_job("Ford", "FOCUS ZETEC", CompletedJob.Status.FAILED, "c")
        self._make_job("Ford", "FOCUS RS", CompletedJob.Status.FAILED, "d")
        self._make_job("Ford", "FOCUS ECOBOOST", CompletedJob.Status.FAILED, "e")

        breakdown = locksmith_model_failure_breakdown()
        row = breakdown[0]
        self.assertEqual(row["failed"], 5)
        cells = {c["label"]: c for c in row["master_reason_cells"]}
        self.assertEqual(cells["Client"]["count"], 1)
        self.assertAlmostEqual(cells["Client"]["pct"], 20.0, places=1)
        self.assertEqual(cells["Uncategorized"]["count"], 4)
        self.assertEqual(cells["WGTK Office"]["count"], 0)

    def test_company_breakdown_aggregates_across_locksmiths(self):
        other = _make_locksmith(name="WGTK - Other", driver_serial="099")
        self._make_job("Ford", "FOCUS ST", CompletedJob.Status.FAILED, "a", locksmith=self.locksmith)
        self._make_job("Ford", "FOCUS TITANIUM", CompletedJob.Status.FAILED, "b", locksmith=other)
        self._make_job("Ford", "FOCUS ZETEC", CompletedJob.Status.SUCCESS, "c", locksmith=other)

        breakdown = company_model_failure_breakdown()
        self.assertEqual(len(breakdown), 1)
        row = breakdown[0]
        self.assertNotIn("locksmith", row)
        self.assertEqual(row["model_family"], "FOCUS")
        self.assertEqual(row["total"], 3)
        self.assertEqual(row["failed"], 2)


class ModelAnalysisViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="office_admin", email="admin@wgtk.co.uk", password="x", is_staff=True
        )
        self.client.force_login(self.user)

    def test_renders(self):
        response = self.client.get(reverse("job_completion:model_analysis"))
        self.assertEqual(response.status_code, 200)

    def test_company_scope_renders(self):
        response = self.client.get(reverse("job_completion:model_analysis"), {"scope": "company"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["scope"], "company")

    def test_login_required(self):
        self.client.logout()
        response = self.client.get(reverse("job_completion:model_analysis"))
        self.assertEqual(response.status_code, 302)


class DailyJobsTests(TestCase):
    def test_day_pills_offset_zero_is_last_seven_days_ending_today(self):
        pills = day_pills(0)
        self.assertEqual(len(pills), 7)
        self.assertEqual(pills[0], date.today())
        self.assertEqual(pills[-1], date.today() - timedelta(days=6))

    def test_day_pills_pages_ten_days_at_a_time(self):
        pills = day_pills(7)
        self.assertEqual(len(pills), 10)
        self.assertEqual(pills[0], date.today() - timedelta(days=7))
        self.assertEqual(pills[-1], date.today() - timedelta(days=16))

    def test_next_offset_starts_at_seven_then_pages_by_ten(self):
        self.assertEqual(next_offset(0), 7)
        self.assertEqual(next_offset(7), 17)
        self.assertEqual(next_offset(17), 27)

    def test_jobs_for_day_filters_by_job_date(self):
        locksmith = _make_locksmith()
        CompletedJob.objects.create(
            order_no="a", report_id="1", job_date=date(2026, 9, 1),
            status=CompletedJob.Status.SUCCESS, locksmith=locksmith,
        )
        CompletedJob.objects.create(
            order_no="b", report_id="2", job_date=date(2026, 9, 2),
            status=CompletedJob.Status.SUCCESS, locksmith=locksmith,
        )
        jobs = jobs_for_day(date(2026, 9, 1))
        self.assertEqual([j.order_no for j in jobs], ["a"])


class PartsCostForJobsTests(TestCase):
    def setUp(self):
        self.locksmith = _make_locksmith()

    def _make_job(self, order_no, disposed_skus):
        return CompletedJob.objects.create(
            order_no=order_no, report_id=order_no, job_date=date(2026, 9, 1),
            locksmith=self.locksmith, status=CompletedJob.Status.SUCCESS,
            disposed_skus=disposed_skus,
        )

    def test_sums_unit_cost_across_disposed_skus(self):
        job = self._make_job("a", "TK-100, TK-100, TK-101")
        with patch(
            "apps.job_completion.services.costing.get_handl_client",
            return_value=FakeCostHandlClient({"TK-100": 2.5, "TK-101": 10.0}),
        ):
            costs = parts_cost_for_jobs([job])
        self.assertEqual(costs["a"], 15.0)

    def test_jobs_with_no_disposed_skus_are_omitted(self):
        job = self._make_job("a", "")
        costs = parts_cost_for_jobs([job])
        self.assertEqual(costs, {})

    def test_missing_cost_for_a_sku_treated_as_zero(self):
        job = self._make_job("a", "TK-100, TK-999")
        with patch(
            "apps.job_completion.services.costing.get_handl_client",
            return_value=FakeCostHandlClient({"TK-100": 2.5}),
        ):
            costs = parts_cost_for_jobs([job])
        self.assertEqual(costs["a"], 2.5)
