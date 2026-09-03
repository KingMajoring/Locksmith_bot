"""One-off cleanup for CompletedJob rows pulled before
pull_completed_jobs_for_date started excluding Optimo's internal/admin
housekeeping entries (e.g. "**HALF DAY TODAY** UP TO 20 MIN VAN &
STOCK CHECK", "SEND KEY TO CHARLEY") — these have no numeric ReportID
and were never real locksmith jobs, but earlier pulls/backfills stored
them anyway with blank make/model/etc, polluting job counts and
cost/margin totals. Deletes any CompletedJob whose report_id isn't
purely numeric.
"""
from django.core.management.base import BaseCommand

from apps.job_completion.models import CompletedJob


class Command(BaseCommand):
    help = "Delete CompletedJob rows for non-numeric-ReportID (admin/housekeeping) Optimo entries."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true", help="List what would be deleted without deleting."
        )

    def handle(self, *args, **options):
        candidates = [job for job in CompletedJob.objects.all() if not job.report_id.isdigit()]
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
