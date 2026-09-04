from datetime import date, datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.integrations.handl import JobDetails
from apps.integrations.optimo import OptimoCompletion, OptimoOrderSummary
from apps.locksmiths.models import Locksmith, OptimoDriverId

from .models import CompletedJob, FailureCategory, SLATarget
from .services.benchmarking import duration_benchmark
from .services.costing import parts_cost_for_jobs
from .services.daily import day_pills, jobs_for_day, next_offset, prev_offset, review_flags, summarize_day
from .services.job_information import (
    available_services,
    makes_summary,
    models_summary,
    years_summary,
)
from .services.model_analysis import (
    company_model_failure_breakdown,
    locksmith_model_failure_breakdown,
)
from .services.model_normalization import normalize_model
from .services.pulling import (
    _report_id_from_order_no,
    pull_completed_jobs_for_date,
    refresh_missing_financials,
)
from .services.reporting import (
    all_locksmith_summaries,
    failure_category_breakdown,
    master_reason_breakdown,
    needs_categorization_queryset,
)
from .services.trends import (
    locksmith_wgtk_fault_trend,
    make_model_failure_trend,
    monthly_failure_trend,
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
                supplied_service="Non-Destructive Entry", net_cost=120.0,
            ),
            "1002": JobDetails(
                report_id="1002", make="BMW", model="335D", year="2019",
                vin="VIN1002", service_type="Car", loss_type="Lost Keys",
                supplied_service="Key Programming", net_cost=None,
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
        self.assertEqual(job.net_cost, 120.0)

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

    def test_non_numeric_report_id_is_not_sent_to_handl_or_stored(self):
        """Regression test: confirmed live that not every Optimo order
        is a Handl claim — an ad-hoc/admin entry's orderNo was literally
        "Sort flat tyre_2026-01-02" (also seen live: "**HALF DAY TODAY**
        UP TO 20 MIN VAN & STOCK CHECK", "SEND KEY TO CHARLEY"), giving a
        non-numeric "report_id" that (a) broke Handl's integer ReportID
        column for the whole day's batch, not just that one job, and
        (b) isn't a real locksmith job at all, so it must not be stored
        as a CompletedJob — polluting job counts and cost/margin totals
        with blank-detail rows."""
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

        self.assertEqual(summary.created, 0)
        self.assertEqual(summary.skipped_admin, 1)
        self.assertFalse(CompletedJob.objects.filter(order_no=order_no).exists())


class ReportIdFromOrderNoTests(TestCase):
    """Regression tests built from real order_no values seen live.

    Two cuts of this parser were already wrong: the first (isdigit()
    on a plain rsplit("_", 1)) misclassified messily-separated real
    jobs ("498074 _2026-08-19") as admin entries; the second (requiring
    a well-formed "20YY" year after the ID) still misclassified real
    jobs whose hand-typed date had a typo ("499063_202-08-25",
    "497795_1016-08-18" — a digit missing or wrong, not a letter in
    sight). Both would have deleted real data via cleanup_admin_jobs.
    The rule that actually holds: nothing after the leading ID is ever
    a letter in a real order, however mangled the date is otherwise.
    """

    def test_clean_hyphenated_date(self):
        self.assertEqual(_report_id_from_order_no("498528-2026-08-22"), "498528")

    def test_underscore_separated_date(self):
        self.assertEqual(_report_id_from_order_no("485801_2026_06_08"), "485801")

    def test_stray_space_before_underscore(self):
        self.assertEqual(_report_id_from_order_no("498074 _2026-08-19"), "498074")

    def test_stray_tilde_separator(self):
        self.assertEqual(_report_id_from_order_no("463565~_2026-02-09"), "463565")

    def test_stray_backslash_separator(self):
        self.assertEqual(_report_id_from_order_no("458155\\-2026-01-12"), "458155")

    def test_slash_separator(self):
        self.assertEqual(_report_id_from_order_no("481836/_2026-05-19"), "481836")

    def test_report_id_alone_with_no_date_suffix(self):
        self.assertEqual(_report_id_from_order_no("498528"), "498528")

    def test_typo_in_year_still_a_real_report_id(self):
        self.assertEqual(_report_id_from_order_no("499063_202-08-25"), "499063")
        self.assertEqual(_report_id_from_order_no("497795_1016-08-18"), "497795")
        self.assertEqual(_report_id_from_order_no("457688-026-01-09"), "457688")

    def test_stray_plus_and_slash_in_date(self):
        self.assertEqual(_report_id_from_order_no("474052_202+-04-08"), "474052")
        self.assertEqual(_report_id_from_order_no("459420_18/0/2026"), "459420")

    def test_underscore_separated_note_instead_of_date_is_still_real(self):
        """Confirmed real by the office: a driver sometimes overwrites
        the date part with a free-text note instead, but the
        "<ID>_..." shape (leading ID, then straight into an underscore)
        is still trusted as the real Handl claim it is — unlike a note
        that merely mentions a job number elsewhere in the string."""
        self.assertEqual(_report_id_from_order_no("479311_cut blades and post"), "479311")
        self.assertEqual(_report_id_from_order_no("479946_met call out"), "479946")

    def test_admin_note_starting_with_a_number_is_not_a_report_id(self):
        """"20 MIN FLEET & STOCK CHECK" and "17 Little Venice Country
        Park & Marina" both start with digits, but the rest is prose,
        not a mangled date — a leading digit run alone isn't enough."""
        self.assertIsNone(_report_id_from_order_no("20 MIN FLEET & STOCK CHECK"))
        self.assertIsNone(_report_id_from_order_no("17 Little Venice Country Park & Marina"))

    def test_note_with_embedded_report_id_is_not_extracted(self):
        """No underscore right after the leading number, so these read
        as a note mentioning a job — not the job's own order entry."""
        self.assertIsNone(_report_id_from_order_no("(MET CALL OUT) 480400_2026-05-11"))
        self.assertIsNone(_report_id_from_order_no("456838 POST KEY"))

    def test_plain_text_note_is_not_a_report_id(self):
        self.assertIsNone(_report_id_from_order_no("SEND KEY TO CHARLEY"))
        self.assertIsNone(_report_id_from_order_no("**HALF DAY TODAY** UP TO 20 MIN VAN & STOCK CHECK"))
        self.assertIsNone(_report_id_from_order_no("1-2-1 with Josh"))


class RefreshMissingFinancialsTests(TestCase):
    """Handl's Policy_Financial rows are often entered days after a job
    completes, after the one-time nightly pull already froze net_cost as
    NULL — refresh_missing_financials re-checks recent jobs still
    missing it and fills in whatever's landed in Handl since."""

    def setUp(self):
        self.locksmith = _make_locksmith()
        self.today = date(2026, 9, 3)

    def _job(self, report_id, job_date, net_cost=None, order_no=None):
        """order_no defaults to the canonical "<ReportID>_<date>" shape
        so _report_id_from_order_no can round-trip it back to
        report_id — pass order_no explicitly to test a mismatch."""
        return CompletedJob.objects.create(
            order_no=order_no or f"{report_id}_{job_date.isoformat()}",
            report_id=report_id,
            job_date=job_date,
            locksmith=self.locksmith,
            status=CompletedJob.Status.SUCCESS,
            net_cost=net_cost,
        )

    def test_fills_in_net_cost_once_available_in_handl(self):
        self._job("2001", self.today - timedelta(days=5))
        details = {
            "2001": JobDetails(
                report_id="2001", make="Ford", model="Focus", year="2020",
                vin="VIN2001", service_type="Car", loss_type="Lockout",
                supplied_service="Non-Destructive Entry", net_cost=145.5,
            ),
        }
        with patch(
            "apps.job_completion.services.pulling.get_handl_client",
            return_value=FakeHandlClient(details),
        ), patch("apps.job_completion.services.pulling.date") as mock_date:
            mock_date.today.return_value = self.today
            refreshed = refresh_missing_financials()

        self.assertEqual(refreshed, 1)
        job = CompletedJob.objects.get(report_id="2001")
        self.assertEqual(job.net_cost, 145.5)
        self.assertEqual(job.make, "Ford")

    def test_leaves_job_alone_when_still_not_in_handl(self):
        self._job("2001", self.today - timedelta(days=5))

        with patch(
            "apps.job_completion.services.pulling.get_handl_client",
            return_value=FakeHandlClient({}),
        ), patch("apps.job_completion.services.pulling.date") as mock_date:
            mock_date.today.return_value = self.today
            refreshed = refresh_missing_financials()

        self.assertEqual(refreshed, 0)
        job = CompletedJob.objects.get(report_id="2001")
        self.assertIsNone(job.net_cost)

    def test_ignores_jobs_that_already_have_net_cost(self):
        self._job("2001", self.today - timedelta(days=5), net_cost=50.0)

        class StrictFakeHandlClient:
            def get_job_details(self, report_ids):
                raise AssertionError("should not query Handl when nothing is missing")

        with patch(
            "apps.job_completion.services.pulling.get_handl_client",
            return_value=StrictFakeHandlClient(),
        ), patch("apps.job_completion.services.pulling.date") as mock_date:
            mock_date.today.return_value = self.today
            refreshed = refresh_missing_financials()

        self.assertEqual(refreshed, 0)

    def test_ignores_jobs_older_than_the_window(self):
        self._job("2001", self.today - timedelta(days=90))
        details = {
            "2001": JobDetails(
                report_id="2001", make="Ford", model="Focus", year="2020",
                vin="VIN2001", service_type="Car", loss_type="Lockout",
                supplied_service="Non-Destructive Entry", net_cost=145.5,
            ),
        }
        with patch(
            "apps.job_completion.services.pulling.get_handl_client",
            return_value=FakeHandlClient(details),
        ), patch("apps.job_completion.services.pulling.date") as mock_date:
            mock_date.today.return_value = self.today
            refreshed = refresh_missing_financials(window_days=60)

        self.assertEqual(refreshed, 0)
        job = CompletedJob.objects.get(report_id="2001")
        self.assertIsNone(job.net_cost)

    def test_no_jobs_missing_net_cost_returns_zero_without_querying_handl(self):
        class StrictFakeHandlClient:
            def get_job_details(self, report_ids):
                raise AssertionError("should not query Handl when nothing is missing")

        with patch(
            "apps.job_completion.services.pulling.get_handl_client",
            return_value=StrictFakeHandlClient(),
        ):
            refreshed = refresh_missing_financials()

        self.assertEqual(refreshed, 0)

    def test_no_window_by_default_catches_years_old_jobs(self):
        """The Margin/Timing reports this feeds are all-time history, so
        a years-old job missing net_cost must still get retried unless a
        window is explicitly requested."""
        self._job("2001", date(2014, 3, 1))
        details = {
            "2001": JobDetails(
                report_id="2001", make="Vauxhall", model="Astra", year="2014",
                vin="VIN2001", service_type="Car", loss_type="Lockout",
                supplied_service="Non-Destructive Entry", net_cost=220.0,
            ),
        }
        with patch(
            "apps.job_completion.services.pulling.get_handl_client",
            return_value=FakeHandlClient(details),
        ):
            refreshed = refresh_missing_financials()

        self.assertEqual(refreshed, 1)
        job = CompletedJob.objects.get(report_id="2001")
        self.assertEqual(job.net_cost, 220.0)

    def test_large_backlog_is_batched_to_stay_under_the_sql_param_limit(self):
        for i in range(1200):
            self._job(str(3000 + i), date(2020, 1, 1))

        seen_chunk_sizes = []

        class ChunkTrackingHandlClient:
            def get_job_details(self, report_ids):
                seen_chunk_sizes.append(len(report_ids))
                return {
                    rid: JobDetails(
                        report_id=rid, make="Ford", model="Focus", year="2020",
                        vin="", service_type="Car", loss_type="Lockout",
                        supplied_service="Non-Destructive Entry", net_cost=100.0,
                    )
                    for rid in report_ids
                }

        with patch(
            "apps.job_completion.services.pulling.get_handl_client",
            return_value=ChunkTrackingHandlClient(),
        ):
            refreshed = refresh_missing_financials()

        self.assertEqual(refreshed, 1200)
        self.assertTrue(all(size <= 500 for size in seen_chunk_sizes))
        self.assertEqual(sum(seen_chunk_sizes), 1200)

    def test_corrupted_stored_report_id_is_repaired_from_order_no(self):
        """Regression test: confirmed live via SSH — a legacy row's
        stored report_id was itself the raw messily-separated order_no
        ("458155\\-2026-01-12") rather than the clean "458155" a newer
        parser fix would have produced, because it was pulled before
        that fix shipped and never re-pulled since. Sending that
        straight to Handl's int-typed ReportID column blew up the whole
        batch ("Conversion failed... to data type int"). Must re-derive
        from order_no instead of trusting the stored field."""
        order_no = "458155\\-2026-01-12"
        job = self._job(order_no, date(2026, 1, 12), order_no=order_no)

        details = {
            "458155": JobDetails(
                report_id="458155", make="Ford", model="Focus", year="2020",
                vin="", service_type="Car", loss_type="Lockout",
                supplied_service="Non-Destructive Entry", net_cost=180.0,
            ),
        }

        class AssertingHandlClient:
            def get_job_details(self, report_ids):
                assert all(rid.isdigit() for rid in report_ids), report_ids
                return {rid: details[rid] for rid in report_ids if rid in details}

        with patch(
            "apps.job_completion.services.pulling.get_handl_client",
            return_value=AssertingHandlClient(),
        ):
            refreshed = refresh_missing_financials()

        self.assertEqual(refreshed, 1)
        job.refresh_from_db()
        self.assertEqual(job.report_id, "458155")
        self.assertEqual(job.net_cost, 180.0)

    def test_job_with_unparseable_order_no_is_skipped_not_crashed(self):
        self._job("1", date(2026, 1, 12), order_no="SEND KEY TO CHARLEY")

        class StrictFakeHandlClient:
            def get_job_details(self, report_ids):
                raise AssertionError("should not query Handl for an unparseable order_no")

        with patch(
            "apps.job_completion.services.pulling.get_handl_client",
            return_value=StrictFakeHandlClient(),
        ):
            refreshed = refresh_missing_financials()

        self.assertEqual(refreshed, 0)


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


class FailureTrendTests(TestCase):
    """Month-by-month trends (services/trends.py) — windowed off the
    real current date (no `today` override on the public functions), so
    tests anchor jobs to "this month"/"last month" relative to
    date.today() rather than a fixed date."""

    def setUp(self):
        self.this_month = date.today().replace(day=1)
        self.last_month = (self.this_month - timedelta(days=1)).replace(day=1)

    def test_monthly_failure_trend_has_twelve_months_oldest_first(self):
        rows = monthly_failure_trend()
        self.assertEqual(len(rows), 12)
        self.assertEqual(rows[-1]["label"], date.today().strftime("%b %Y"))

    def test_monthly_failure_trend_counts_totals_and_failures_per_month(self):
        CompletedJob.objects.create(
            order_no="a", report_id="1", job_date=self.this_month,
            status=CompletedJob.Status.SUCCESS,
        )
        CompletedJob.objects.create(
            order_no="b", report_id="2", job_date=self.this_month,
            status=CompletedJob.Status.FAILED,
        )
        CompletedJob.objects.create(
            order_no="c", report_id="3", job_date=self.last_month,
            status=CompletedJob.Status.FAILED,
        )
        rows = monthly_failure_trend()
        current, previous = rows[-1], rows[-2]
        self.assertEqual((current["total"], current["failed"], current["failure_rate_pct"]), (2, 1, 50.0))
        self.assertEqual((previous["total"], previous["failed"]), (1, 1))

    def test_locksmith_wgtk_fault_trend_only_counts_locksmith_master_reason(self):
        locksmith = _make_locksmith()
        locksmith_fault = FailureCategory.objects.create(
            name="Wrong approach", master_reason=FailureCategory.MasterReason.WGTK_LOCKSMITH
        )
        client_fault = FailureCategory.objects.create(
            name="Customer not present", master_reason=FailureCategory.MasterReason.CLIENT
        )
        CompletedJob.objects.create(
            order_no="a", report_id="1", job_date=self.this_month,
            status=CompletedJob.Status.FAILED, locksmith=locksmith, failure_category=locksmith_fault,
        )
        CompletedJob.objects.create(
            order_no="b", report_id="2", job_date=self.this_month,
            status=CompletedJob.Status.FAILED, locksmith=locksmith, failure_category=client_fault,
        )
        data = locksmith_wgtk_fault_trend()
        self.assertEqual(len(data["rows"]), 1)
        self.assertEqual(data["rows"][0]["locksmith_name"], locksmith.name)
        self.assertEqual(data["rows"][0]["total"], 1)
        self.assertEqual(data["rows"][0]["month_counts"][-1], 1)
        self.assertEqual(data["months"][-1], date.today().strftime("%b %Y"))

    def test_make_model_failure_trend_groups_by_normalized_family(self):
        CompletedJob.objects.create(
            order_no="a", report_id="1", job_date=self.this_month,
            status=CompletedJob.Status.FAILED, make="Vauxhall", model="Corsa Sting",
        )
        CompletedJob.objects.create(
            order_no="b", report_id="2", job_date=self.this_month,
            status=CompletedJob.Status.FAILED, make="Vauxhall", model="Corsa SE Auto",
        )
        data = make_model_failure_trend()
        self.assertEqual(len(data["rows"]), 1)
        self.assertEqual(data["rows"][0]["model_family"], normalize_model("Vauxhall", "Corsa Sting"))
        self.assertEqual(data["rows"][0]["total"], 2)

    def test_make_model_failure_trend_excludes_jobs_with_no_model(self):
        CompletedJob.objects.create(
            order_no="a", report_id="1", job_date=self.this_month,
            status=CompletedJob.Status.FAILED, make="", model="",
        )
        data = make_model_failure_trend()
        self.assertEqual(data["rows"], [])


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

    def test_dashboard_no_longer_shows_failure_breakdowns(self):
        """Moved to Job Failures — the Dashboard is locksmith summary only."""
        response = self.client.get(reverse("job_completion:dashboard"))
        self.assertNotContains(response, "Training focus")
        self.assertNotContains(response, "Failure reasons")

    def test_locksmith_report_renders(self):
        response = self.client.get(
            reverse("job_completion:locksmith_report", args=[self.locksmith.pk])
        )
        self.assertEqual(response.status_code, 200)

    def test_job_failures_renders(self):
        response = self.client.get(reverse("job_completion:job_failures"))
        self.assertEqual(response.status_code, 200)

    def test_job_failures_shows_failure_breakdowns(self):
        CompletedJob.objects.create(
            order_no="a", report_id="1", job_date=date(2026, 9, 1),
            status=CompletedJob.Status.FAILED, locksmith=self.locksmith,
        )
        response = self.client.get(reverse("job_completion:job_failures"))
        self.assertContains(response, "Training focus")
        self.assertContains(response, "Failure reasons")
        self.assertContains(response, "Uncategorized")

    def test_job_failures_shows_parts_cost_and_selling_price(self):
        CompletedJob.objects.create(
            order_no="a", report_id="1", job_date=date(2026, 9, 1),
            status=CompletedJob.Status.FAILED, locksmith=self.locksmith,
            net_cost=150.0, disposed_skus="TK-100",
        )
        with patch(
            "apps.job_completion.views.parts_cost_for_jobs",
            return_value={"a": 25.0},
        ):
            response = self.client.get(reverse("job_completion:job_failures"))
        job = response.context["needs_categorization"][0]
        self.assertEqual(job.parts_cost, 25.0)
        self.assertContains(response, "£25.0")
        self.assertContains(response, "£150.0")

    def test_job_failures_shows_dash_when_parts_cost_unknown(self):
        CompletedJob.objects.create(
            order_no="a", report_id="1", job_date=date(2026, 9, 1),
            status=CompletedJob.Status.FAILED, locksmith=self.locksmith,
            net_cost=150.0,
        )
        response = self.client.get(reverse("job_completion:job_failures"))
        job = response.context["needs_categorization"][0]
        self.assertIsNone(job.parts_cost)

    def test_sidebar_badge_shows_needs_categorization_count(self):
        CompletedJob.objects.create(
            order_no="a", report_id="1", job_date=date(2026, 9, 1),
            status=CompletedJob.Status.FAILED, locksmith=self.locksmith,
        )
        response = self.client.get(reverse("job_completion:dashboard"))
        self.assertEqual(response.context["needs_categorization_count"], 1)
        self.assertContains(response, '<span class="badge">1</span>')

    def test_sidebar_badge_hidden_when_nothing_outstanding(self):
        response = self.client.get(reverse("job_completion:dashboard"))
        self.assertEqual(response.context["needs_categorization_count"], 0)
        self.assertNotContains(response, 'class="badge"')

    def test_jobs_by_day_renders(self):
        response = self.client.get(reverse("job_completion:jobs_by_day"))
        self.assertEqual(response.status_code, 200)

    def test_jobs_by_day_with_offset_and_date_renders(self):
        response = self.client.get(
            reverse("job_completion:jobs_by_day"), {"offset": 7, "date": "2026-01-01"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected"], date(2026, 1, 1))
        self.assertEqual(response.context["prev_offset"], 0)
        self.assertContains(response, "Show more recent days")

    def test_jobs_by_day_hides_more_recent_link_at_offset_zero(self):
        response = self.client.get(reverse("job_completion:jobs_by_day"))
        self.assertNotContains(response, "Show more recent days")

    def test_jobs_by_day_computes_margin_from_net_cost_and_parts_cost(self):
        CompletedJob.objects.create(
            order_no="a", report_id="1", job_date=date(2026, 1, 1),
            locksmith=self.locksmith, status=CompletedJob.Status.SUCCESS,
            net_cost=100.0, disposed_skus="TK-100",
        )
        with patch(
            "apps.job_completion.views.parts_cost_for_jobs",
            return_value={"a": 30.0},
        ):
            response = self.client.get(
                reverse("job_completion:jobs_by_day"), {"date": "2026-01-01"}
            )
        job = response.context["jobs"][0]
        self.assertEqual(job.parts_cost, 30.0)
        self.assertEqual(job.margin, 70.0)
        self.assertEqual(response.context["summary"]["total_income"], 100.0)
        self.assertEqual(response.context["summary"]["total_cost"], 30.0)
        self.assertEqual(response.context["summary"]["total_margin"], 70.0)
        self.assertEqual(response.context["summary"]["job_count"], 1)

    def test_jobs_by_day_highlights_jobs_with_review_flags(self):
        start = datetime(2026, 1, 1, 9, 0, tzinfo=dt_timezone.utc)
        CompletedJob.objects.create(
            order_no="a", report_id="1", job_date=date(2026, 1, 1),
            locksmith=self.locksmith, status=CompletedJob.Status.SUCCESS,
            net_cost=100.0, disposed_skus="", start_time=start, end_time=start.replace(minute=5),
        )
        response = self.client.get(reverse("job_completion:jobs_by_day"), {"date": "2026-01-01"})
        self.assertContains(response, "row-warn")
        self.assertContains(response, "No parts disposed")
        self.assertContains(response, "Completed in 5 min")

    def test_jobs_by_day_no_highlight_when_nothing_to_flag(self):
        start = datetime(2026, 1, 1, 9, 0, tzinfo=dt_timezone.utc)
        CompletedJob.objects.create(
            order_no="a", report_id="1", job_date=date(2026, 1, 1),
            locksmith=self.locksmith, status=CompletedJob.Status.SUCCESS,
            net_cost=100.0, disposed_skus="TK-100", start_time=start, end_time=start.replace(minute=30),
        )
        response = self.client.get(reverse("job_completion:jobs_by_day"), {"date": "2026-01-01"})
        self.assertNotContains(response, "row-warn")

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
        self.assertRedirects(response, reverse("job_completion:job_failures"))
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
        self.assertRedirects(response, reverse("job_completion:job_failures"))
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

    def test_job_failures_shows_dropdown_and_single_save_form(self):
        response = self.client.get(reverse("job_completion:job_failures"))
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


class CleanupAdminJobsCommandTests(TestCase):
    def setUp(self):
        self.locksmith = _make_locksmith()
        self.real_job = CompletedJob.objects.create(
            order_no=f"1001_{date(2026, 1, 2).isoformat()}", report_id="1001",
            job_date=date(2026, 1, 2), locksmith=self.locksmith,
            status=CompletedJob.Status.SUCCESS,
        )
        self.admin_job = CompletedJob.objects.create(
            order_no=f"Sort flat tyre_{date(2026, 1, 2).isoformat()}", report_id="Sort flat tyre",
            job_date=date(2026, 1, 2), locksmith=self.locksmith,
            status=CompletedJob.Status.SUCCESS,
        )
        # Stored under the old, cruder parser: a real job with a messily
        # formatted order_no whose report_id got mangled to something
        # that fails isdigit() ("498074 " has a trailing space) even
        # though it's genuinely real — must survive cleanup.
        self.messy_real_job = CompletedJob.objects.create(
            order_no="498074 _2026-08-19", report_id="498074 ",
            job_date=date(2026, 8, 19), locksmith=self.locksmith,
            status=CompletedJob.Status.SUCCESS,
        )

    def test_deletes_only_non_numeric_report_id_jobs(self):
        from django.core.management import call_command

        call_command("cleanup_admin_jobs")

        self.assertFalse(CompletedJob.objects.filter(pk=self.admin_job.pk).exists())
        self.assertTrue(CompletedJob.objects.filter(pk=self.real_job.pk).exists())
        self.assertTrue(CompletedJob.objects.filter(pk=self.messy_real_job.pk).exists())

    def test_dry_run_deletes_nothing(self):
        from django.core.management import call_command

        call_command("cleanup_admin_jobs", "--dry-run")

        self.assertTrue(CompletedJob.objects.filter(pk=self.admin_job.pk).exists())
        self.assertTrue(CompletedJob.objects.filter(pk=self.real_job.pk).exists())
        self.assertTrue(CompletedJob.objects.filter(pk=self.messy_real_job.pk).exists())


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


class FailureTrendViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="office_admin", email="admin@wgtk.co.uk", password="x", is_staff=True
        )
        self.client.force_login(self.user)

    def test_month_scope_renders(self):
        response = self.client.get(reverse("job_completion:failure_trend"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["scope"], "month")

    def test_locksmith_scope_renders(self):
        response = self.client.get(reverse("job_completion:failure_trend"), {"scope": "locksmith"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["scope"], "locksmith")

    def test_model_scope_renders(self):
        response = self.client.get(reverse("job_completion:failure_trend"), {"scope": "model"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["scope"], "model")

    def test_unknown_scope_defaults_to_month(self):
        response = self.client.get(reverse("job_completion:failure_trend"), {"scope": "bogus"})
        self.assertEqual(response.context["scope"], "month")

    def test_login_required(self):
        self.client.logout()
        response = self.client.get(reverse("job_completion:failure_trend"))
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

    def test_prev_offset_reverses_next_offset(self):
        self.assertEqual(prev_offset(0), 0)
        self.assertEqual(prev_offset(7), 0)
        self.assertEqual(prev_offset(17), 7)
        self.assertEqual(prev_offset(27), 17)

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

    def test_summarize_day_totals_across_jobs(self):
        locksmith_a = _make_locksmith(name="WGTK - A", driver_serial="001")
        locksmith_b = _make_locksmith(name="WGTK - B", driver_serial="002")
        job1 = CompletedJob.objects.create(
            order_no="a", report_id="1", job_date=date(2026, 9, 1),
            status=CompletedJob.Status.SUCCESS, locksmith=locksmith_a,
            distance_metres=1609.344, net_cost=100.0,
        )
        job1.parts_cost = 30.0
        job1.margin = 70.0
        job2 = CompletedJob.objects.create(
            order_no="b", report_id="2", job_date=date(2026, 9, 1),
            status=CompletedJob.Status.SUCCESS, locksmith=locksmith_b,
            distance_metres=1609.344 * 2, net_cost=50.0,
        )
        job2.parts_cost = None
        job2.margin = None

        summary = summarize_day([job1, job2])
        self.assertEqual(summary["job_count"], 2)
        self.assertEqual(summary["locksmith_count"], 2)
        self.assertEqual(summary["total_miles"], 3.0)
        self.assertEqual(summary["total_income"], 150.0)
        self.assertEqual(summary["total_cost"], 30.0)
        self.assertEqual(summary["total_margin"], 70.0)

    def test_summarize_day_empty_jobs_list(self):
        summary = summarize_day([])
        self.assertEqual(summary["job_count"], 0)
        self.assertEqual(summary["total_miles"], 0)

    def _timed_job(self, minutes, loss_type="Lost Keys", disposed_skus="TK-100", status=CompletedJob.Status.SUCCESS):
        start = datetime(2026, 9, 1, 9, 0, tzinfo=dt_timezone.utc)
        end = start.replace(minute=minutes % 60, hour=9 + minutes // 60)
        return CompletedJob.objects.create(
            order_no="a", report_id="1", job_date=date(2026, 9, 1),
            locksmith=_make_locksmith(), status=status, loss_type=loss_type,
            disposed_skus=disposed_skus, start_time=start, end_time=end,
        )

    def test_review_flags_no_parts_disposed(self):
        job = self._timed_job(30, disposed_skus="")
        self.assertEqual(review_flags(job), ["No parts disposed"])

    def test_review_flags_no_parts_disposed_exempt_for_gain_access(self):
        job = self._timed_job(10, loss_type="LOCKED IN PROPERTY", disposed_skus="")
        self.assertEqual(review_flags(job), [])

    def test_review_flags_completed_too_quick_other_service(self):
        job = self._timed_job(14)
        self.assertEqual(review_flags(job), ["Completed in 14 min"])

    def test_review_flags_not_too_quick_at_threshold_other_service(self):
        job = self._timed_job(15)
        self.assertEqual(review_flags(job), [])

    def test_review_flags_completed_too_quick_gain_access(self):
        job = self._timed_job(2, loss_type="LOCKED IN PROPERTY", disposed_skus="")
        self.assertEqual(review_flags(job), ["Completed in 2 min"])

    def test_review_flags_not_too_quick_at_threshold_gain_access(self):
        job = self._timed_job(3, loss_type="LOCKED IN PROPERTY", disposed_skus="")
        self.assertEqual(review_flags(job), [])

    def test_review_flags_both_reasons_together(self):
        job = self._timed_job(5, disposed_skus="")
        self.assertEqual(review_flags(job), ["No parts disposed", "Completed in 5 min"])

    def test_review_flags_never_set_on_failed_jobs(self):
        job = self._timed_job(1, disposed_skus="", status=CompletedJob.Status.FAILED)
        self.assertEqual(review_flags(job), [])

    def test_review_flags_no_duration_flag_when_duration_unknown(self):
        job = CompletedJob.objects.create(
            order_no="a", report_id="1", job_date=date(2026, 9, 1),
            locksmith=_make_locksmith(), status=CompletedJob.Status.SUCCESS,
            disposed_skus="TK-100",
        )
        self.assertEqual(review_flags(job), [])


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


class JobInformationTests(TestCase):
    """Margin/timing drill-down (Make -> model family -> Year), all
    scoped to successful jobs with a make, across the full history on
    file (see services/job_information.py)."""

    def setUp(self):
        self.locksmith = _make_locksmith()
        self.handl_patch = patch(
            "apps.job_completion.services.costing.get_handl_client",
            return_value=FakeCostHandlClient({"TK-100": 20.0}),
        )
        self.handl_patch.start()
        self.addCleanup(self.handl_patch.stop)

    def _job(self, order_no, make, model, year, net_cost, minutes, disposed_skus="", status=CompletedJob.Status.SUCCESS, loss_type=""):
        start = datetime(2026, 9, 1, 9, 0, tzinfo=dt_timezone.utc)
        end = start.replace(minute=minutes % 60, hour=9 + minutes // 60)
        return CompletedJob.objects.create(
            order_no=order_no, report_id=order_no, job_date=date(2026, 9, 1),
            locksmith=self.locksmith, status=status,
            make=make, model=model, year=year, net_cost=net_cost,
            disposed_skus=disposed_skus, start_time=start, end_time=end,
            loss_type=loss_type,
        )

    def _make_ford_data(self):
        # Focus Titanium and Focus ST both normalize to "Focus" (first word).
        self._job("a", "Ford", "Focus Titanium", "2019", 100.0, 30, "TK-100")
        self._job("b", "Ford", "Focus ST", "2019", 200.0, 60, "")
        self._job("c", "Ford", "Fiesta", "2020", 150.0, 45, "TK-100")

    def test_makes_summary_averages_across_every_job_for_the_make(self):
        self._make_ford_data()
        rows = makes_summary()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["make"], "Ford")
        self.assertEqual(row["job_count"], 3)
        self.assertEqual(row["avg_earning"], 150.0)
        self.assertAlmostEqual(row["avg_cost"], 13.33, places=2)
        self.assertAlmostEqual(row["avg_margin"], 136.67, places=2)
        self.assertEqual(row["avg_duration_minutes"], 45.0)

    def test_makes_summary_excludes_failed_and_makeless_jobs(self):
        self._job("failed", "Ford", "Focus", "2019", 999.0, 10, status=CompletedJob.Status.FAILED)
        self._job("no-make", "", "", "", 999.0, 10)
        self._make_ford_data()
        rows = makes_summary()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["job_count"], 3)

    def test_models_summary_groups_by_normalized_family(self):
        self._make_ford_data()
        rows = models_summary("Ford")
        families = {r["model_family"] for r in rows}
        self.assertEqual(families, {"FOCUS", "FIESTA"})
        focus = next(r for r in rows if r["model_family"] == "FOCUS")
        self.assertEqual(focus["job_count"], 2)
        self.assertEqual(focus["avg_earning"], 150.0)
        self.assertEqual(focus["avg_cost"], 10.0)
        self.assertEqual(focus["avg_margin"], 140.0)
        self.assertEqual(focus["avg_duration_minutes"], 45.0)

    def test_years_summary_groups_by_year_within_make_and_family(self):
        self._make_ford_data()
        rows = years_summary("Ford", "FOCUS")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["year"], "2019")
        self.assertEqual(rows[0]["job_count"], 2)

    def test_avg_margin_pct_computed_from_earning_and_margin(self):
        self._job("a", "Ford", "Focus", "2019", 100.0, 30, "TK-100")
        rows = makes_summary()
        # earning 100, cost 20, margin 80 -> 80%
        self.assertEqual(rows[0]["avg_margin_pct"], 80.0)

    def test_no_jobs_returns_empty_list(self):
        self.assertEqual(makes_summary(), [])
        self.assertEqual(models_summary("Ford"), [])
        self.assertEqual(years_summary("Ford", "FOCUS"), [])

    def test_available_services_lists_distinct_display_labels(self):
        self._job("a", "Ford", "Focus", "2019", 100.0, 30, loss_type="LOST")
        self._job("b", "Ford", "Fiesta", "2019", 100.0, 30, loss_type="LOCKED IN PROPERTY")
        self._job("c", "Ford", "Focus", "2020", 100.0, 30, loss_type="Spare Key")
        self._job("d", "Ford", "Focus", "2020", 100.0, 30, loss_type="")
        self.assertEqual(available_services(), ["AKL", "Gain access", "Spare Key"])

    def test_makes_summary_filtered_by_service(self):
        self._job("a", "Ford", "Focus", "2019", 100.0, 30, loss_type="LOST")
        self._job("b", "BMW", "3 Series", "2019", 200.0, 60, loss_type="LOCKED IN PROPERTY")
        rows = makes_summary(service="AKL")
        self.assertEqual([r["make"] for r in rows], ["Ford"])

    def test_models_summary_filtered_by_service(self):
        self._job("a", "Ford", "Focus", "2019", 100.0, 30, loss_type="LOST")
        self._job("b", "Ford", "Fiesta", "2019", 200.0, 60, loss_type="LOCKED IN PROPERTY")
        rows = models_summary("Ford", service="Gain access")
        self.assertEqual([r["model_family"] for r in rows], ["FIESTA"])

    def test_years_summary_filtered_by_service(self):
        self._job("a", "Ford", "Focus", "2019", 100.0, 30, loss_type="LOST")
        self._job("b", "Ford", "Focus", "2020", 200.0, 60, loss_type="LOCKED IN PROPERTY")
        rows = years_summary("Ford", "FOCUS", service="AKL")
        self.assertEqual([r["year"] for r in rows], ["2019"])

    def test_service_filter_is_case_insensitive_on_raw_value(self):
        self._job("a", "Ford", "Focus", "2019", 100.0, 30, loss_type="lost")
        rows = makes_summary(service="AKL")
        self.assertEqual(len(rows), 1)

    def test_no_service_filter_returns_everything(self):
        self._job("a", "Ford", "Focus", "2019", 100.0, 30, loss_type="LOST")
        self._job("b", "BMW", "3 Series", "2019", 200.0, 60, loss_type="LOCKED IN PROPERTY")
        rows = makes_summary(service=None)
        self.assertEqual(len(rows), 2)

    def test_sub_five_minute_job_excluded_from_timing_average_unless_gain_access(self):
        """Locksmiths sometimes start and immediately end the Optimo job
        instead of starting it on arrival, leaving a bogus near-zero
        duration — must not drag the timing average down, but a
        genuinely quick Gain access job should still count."""
        self._job("a", "Ford", "Focus", "2019", 100.0, 30, loss_type="LOST")
        self._job("bogus", "Ford", "Focus", "2019", 100.0, 2, loss_type="LOST")
        rows = makes_summary()
        self.assertEqual(rows[0]["job_count"], 2)  # still counted for margin
        self.assertEqual(rows[0]["avg_duration_minutes"], 30.0)  # bogus one excluded

    def test_gain_access_job_under_five_minutes_still_counts(self):
        self._job("a", "Ford", "Focus", "2019", 100.0, 2, loss_type="LOCKED IN PROPERTY")
        rows = makes_summary()
        self.assertEqual(rows[0]["avg_duration_minutes"], 2.0)

    def test_job_exactly_five_minutes_counts_regardless_of_service(self):
        self._job("a", "Ford", "Focus", "2019", 100.0, 5, loss_type="LOST")
        rows = makes_summary()
        self.assertEqual(rows[0]["avg_duration_minutes"], 5.0)

    def test_all_sub_five_minute_jobs_gives_no_average_but_keeps_job_count(self):
        self._job("a", "Ford", "Focus", "2019", 100.0, 1, loss_type="LOST")
        self._job("b", "Ford", "Fiesta", "2019", 100.0, 3, loss_type="AKL")
        rows = makes_summary()
        self.assertEqual(rows[0]["job_count"], 2)
        self.assertIsNone(rows[0]["avg_duration_minutes"])


class JobInformationViewsTests(TestCase):
    def setUp(self):
        self.locksmith = _make_locksmith()
        self.user = get_user_model().objects.create_user(
            username="office", email="admin@wgtk.co.uk", password="x", is_staff=True
        )
        self.client.force_login(self.user)
        self.handl_patch = patch(
            "apps.job_completion.services.costing.get_handl_client",
            return_value=FakeCostHandlClient({}),
        )
        self.handl_patch.start()
        self.addCleanup(self.handl_patch.stop)
        CompletedJob.objects.create(
            order_no="a", report_id="a", job_date=date(2026, 9, 1),
            locksmith=self.locksmith, status=CompletedJob.Status.SUCCESS,
            make="Ford", model="Focus Titanium", year="2019", net_cost=100.0,
            start_time=datetime(2026, 9, 1, 9, 0, tzinfo=dt_timezone.utc),
            end_time=datetime(2026, 9, 1, 9, 30, tzinfo=dt_timezone.utc),
            loss_type="LOST",
        )
        CompletedJob.objects.create(
            order_no="b", report_id="b", job_date=date(2026, 9, 1),
            locksmith=self.locksmith, status=CompletedJob.Status.SUCCESS,
            make="BMW", model="3 Series", year="2019", net_cost=200.0,
            start_time=datetime(2026, 9, 1, 9, 0, tzinfo=dt_timezone.utc),
            end_time=datetime(2026, 9, 1, 10, 0, tzinfo=dt_timezone.utc),
            loss_type="LOCKED IN PROPERTY",
        )

    def test_margin_makes_renders_and_links_to_models(self):
        response = self.client.get(reverse("job_completion:margin_makes"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ford")

    def test_margin_models_renders_and_links_to_years(self):
        response = self.client.get(reverse("job_completion:margin_models", args=["Ford"]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "FOCUS")

    def test_margin_years_renders(self):
        response = self.client.get(
            reverse("job_completion:margin_years", args=["Ford", "FOCUS"])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2019")

    def test_timing_makes_renders(self):
        response = self.client.get(reverse("job_completion:timing_makes"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ford")

    def test_timing_models_renders(self):
        response = self.client.get(reverse("job_completion:timing_models", args=["Ford"]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "FOCUS")

    def test_timing_years_renders(self):
        response = self.client.get(
            reverse("job_completion:timing_years", args=["Ford", "FOCUS"])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "30.0 min")

    def test_login_required(self):
        self.client.logout()
        response = self.client.get(reverse("job_completion:margin_makes"))
        self.assertEqual(response.status_code, 302)

    def test_margin_makes_shows_service_tags(self):
        response = self.client.get(reverse("job_completion:margin_makes"))
        self.assertContains(response, "AKL")
        self.assertContains(response, "Gain access")

    def test_margin_makes_filtered_by_service_query_param(self):
        response = self.client.get(reverse("job_completion:margin_makes"), {"service": "AKL"})
        self.assertContains(response, "Ford")
        self.assertNotContains(response, "BMW")

    def test_selected_service_carried_into_drill_down_link(self):
        response = self.client.get(reverse("job_completion:margin_makes"), {"service": "AKL"})
        self.assertContains(response, "margin/Ford/?service=AKL")

    def test_selected_service_persists_through_models_and_years(self):
        response = self.client.get(
            reverse("job_completion:margin_models", args=["Ford"]), {"service": "AKL"}
        )
        self.assertContains(response, "FOCUS")
        self.assertContains(response, "margin/Ford/FOCUS/?service=AKL")
        self.assertContains(response, "margin/?service=AKL")  # back-link

    def test_timing_makes_filtered_by_service_query_param(self):
        response = self.client.get(reverse("job_completion:timing_makes"), {"service": "Gain access"})
        self.assertContains(response, "BMW")
        self.assertNotContains(response, "Ford")


class RunScheduledJobViewTests(TestCase):
    """Azure's WebJobs never actually run on this deployment (confirmed
    live: Kudu's WebJobs discovery scans the persistent site directory,
    but this app's real code only ever exists in a per-instance temp
    extraction) — a GitHub Actions scheduled workflow calls this
    endpoint instead, authenticated by a shared secret rather than
    Django login, since it can't go through Microsoft SSO."""

    def _url(self, command_name):
        return reverse("job_completion:run_scheduled_job", args=[command_name])

    @override_settings(SCHEDULED_JOB_TOKEN="secret123")
    def test_valid_token_runs_the_named_command(self):
        with patch("apps.job_completion.views.call_command") as mock_call:
            response = self.client.post(
                self._url("pull_completed_jobs"), HTTP_X_JOB_TOKEN="secret123"
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(mock_call.call_args[0][0], "pull_completed_jobs")

    @override_settings(SCHEDULED_JOB_TOKEN="secret123")
    def test_wrong_token_is_forbidden(self):
        with patch("apps.job_completion.views.call_command") as mock_call:
            response = self.client.post(
                self._url("pull_completed_jobs"), HTTP_X_JOB_TOKEN="wrong"
            )
        self.assertEqual(response.status_code, 403)
        mock_call.assert_not_called()

    @override_settings(SCHEDULED_JOB_TOKEN="")
    def test_unconfigured_token_refuses_every_request(self):
        with patch("apps.job_completion.views.call_command") as mock_call:
            response = self.client.post(
                self._url("pull_completed_jobs"), HTTP_X_JOB_TOKEN="anything"
            )
        self.assertEqual(response.status_code, 403)
        mock_call.assert_not_called()

    @override_settings(SCHEDULED_JOB_TOKEN="secret123")
    def test_send_weekly_stock_checks_is_also_schedulable(self):
        with patch("apps.job_completion.views.call_command") as mock_call:
            response = self.client.post(
                self._url("send_weekly_stock_checks"), HTTP_X_JOB_TOKEN="secret123"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_call.call_args[0][0], "send_weekly_stock_checks")

    @override_settings(SCHEDULED_JOB_TOKEN="secret123")
    def test_command_name_not_on_the_allow_list_is_forbidden(self):
        with patch("apps.job_completion.views.call_command") as mock_call:
            response = self.client.post(
                self._url("migrate"), HTTP_X_JOB_TOKEN="secret123"
            )
        self.assertEqual(response.status_code, 403)
        mock_call.assert_not_called()

    @override_settings(SCHEDULED_JOB_TOKEN="secret123")
    def test_get_request_not_allowed(self):
        response = self.client.get(self._url("pull_completed_jobs"), HTTP_X_JOB_TOKEN="secret123")
        self.assertEqual(response.status_code, 405)

    @override_settings(SCHEDULED_JOB_TOKEN="secret123")
    def test_command_error_is_reported_not_raised(self):
        from django.core.management.base import CommandError

        with patch("apps.job_completion.views.call_command", side_effect=CommandError("boom")):
            response = self.client.post(
                self._url("refresh_job_financials"), HTTP_X_JOB_TOKEN="secret123"
            )
        self.assertEqual(response.status_code, 500)
        self.assertFalse(response.json()["ok"])

    @override_settings(SCHEDULED_JOB_TOKEN="secret123")
    def test_works_for_an_anonymous_caller(self):
        """Unlike every other view in this app, this one must not
        require a logged-in session — GitHub Actions can't sign in."""
        with patch("apps.job_completion.views.call_command"):
            response = self.client.post(
                self._url("refresh_job_financials"), HTTP_X_JOB_TOKEN="secret123"
            )
        self.assertEqual(response.status_code, 200)
