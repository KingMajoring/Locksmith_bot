"""Run daily (e.g. Azure scheduled WebJob at ~03:00, after Optimo's jobs
for the previous day have all had a chance to complete) to pull the
prior day's completed jobs from Optimo.
"""
from datetime import date, timedelta

from django.core.management.base import BaseCommand

from apps.job_completion.services.pulling import pull_completed_jobs_for_date


class Command(BaseCommand):
    help = "Pull completed jobs from Optimo for a given date (default: yesterday)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            type=str,
            default=None,
            help="Date to pull, YYYY-MM-DD (default: yesterday).",
        )

    def handle(self, *args, **options):
        if options["date"]:
            for_date = date.fromisoformat(options["date"])
        else:
            for_date = date.today() - timedelta(days=1)

        summary = pull_completed_jobs_for_date(for_date)
        self.stdout.write(
            f"{for_date}: {summary.created} created, {summary.updated} updated, "
            f"{summary.skipped_not_completed} not yet completed (skipped), "
            f"{summary.skipped_admin} admin/housekeeping entries (skipped)."
        )
