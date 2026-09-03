"""One-off cleanup for CompletedJob rows pulled before
pull_completed_jobs_for_date started excluding Optimo's internal/admin
housekeeping entries (e.g. "**HALF DAY TODAY** UP TO 20 MIN VAN &
STOCK CHECK", "SEND KEY TO CHARLEY") — these have no numeric ReportID
and were never real locksmith jobs, but earlier pulls/backfills stored
them anyway with blank make/model/etc, polluting job counts and
cost/margin totals.

Classifies by re-deriving the ReportID from order_no (via the same
_report_id_from_order_no used at pull time), not by checking the
already-stored report_id field — an earlier, cruder version of that
parser mangled some real jobs' report_id (e.g. a messily-formatted
orderNo like "498074 _2026-08-19" got stored as report_id "498074 ",
which fails an isdigit() check even though it's a real job), so
trusting the stored value would delete real data.
"""
from django.core.management.base import BaseCommand

from apps.job_completion.models import CompletedJob
from apps.job_completion.services.pulling import _report_id_from_order_no


class Command(BaseCommand):
    help = "Delete CompletedJob rows for non-numeric-ReportID (admin/housekeeping) Optimo entries."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true", help="List what would be deleted without deleting."
        )

    def handle(self, *args, **options):
        candidates = [
            job
            for job in CompletedJob.objects.all()
            if _report_id_from_order_no(job.order_no) is None
        ]
        if not candidates:
            self.stdout.write("Nothing to clean up.")
            return

        for job in candidates:
            self.stdout.write(f"{job.order_no} (report_id={job.report_id!r})")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING(f"{len(candidates)} would be deleted (dry run)."))
            return

        CompletedJob.objects.filter(pk__in=[job.pk for job in candidates]).delete()
        self.stdout.write(
            self.style.SUCCESS(f"Deleted {len(candidates)} admin/housekeeping entries.")
        )
