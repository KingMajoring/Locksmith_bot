from datetime import date, datetime, timezone as dt_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.integrations.handl import JobDetails
from apps.integrations.optimo import OptimoCompletion, OptimoOrderSummary
from apps.locksmiths.models import Locksmith, OptimoDriverId

from .models import CompletedJob, FailureCategory, SLATarget
from .services.benchmarking import duration_benchmark
from .services.pulling import pull_completed_jobs_for_date
from .services.reporting import (
    all_locksmith_summaries,
    failure_category_breakdown,
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
                vin="VIN1001", service_type="Lockout",
            ),
            "1002": JobDetails(
                report_id="1002", make="BMW", model="3 Series", year="2019",
                vin="VIN1002", service_type="Lockout",
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
        self.assertEqual(job.service_type, "Lockout")

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


class BenchmarkingTests(TestCase):
    def setUp(self):
        self.locksmith = _make_locksmith()
        self.other_locksmith = _make_locksmith(name="WGTK - Other", driver_serial="023")
        SLATarget.objects.create(service_type="Lockout", target_minutes=30, active=True)

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
            service_type="Lockout",
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
        CompletedJob.objects.create(
            order_no="a", report_id="1", job_date=date(2026, 9, 1),
            status=CompletedJob.Status.FAILED,
        )
        CompletedJob.objects.create(
            order_no="b", report_id="2", job_date=date(2026, 9, 1),
            status=CompletedJob.Status.SUCCESS,
        )
        category = FailureCategory.objects.create(name="Wrong parts")
        CompletedJob.objects.create(
            order_no="c", report_id="3", job_date=date(2026, 9, 1),
            status=CompletedJob.Status.FAILED, failure_category=category,
        )
        results = list(needs_categorization_queryset())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].order_no, "a")

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

    def test_login_required_redirects_anonymous(self):
        self.client.logout()
        response = self.client.get(reverse("job_completion:dashboard"))
        self.assertEqual(response.status_code, 302)


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
