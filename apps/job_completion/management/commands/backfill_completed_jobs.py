"""One-off/occasional command to backfill completed jobs across a date
range, reusing the same per-day pull logic as the daily WebJob
(pull_completed_jobs). Useful for loading history in one go — e.g. all
of this year — rather than waiting for the daily job to accumulate it.

Runs day-by-day sequentially rather than one big multi-month Optimo
call, since that's the exact path already proven against real data;
a wide date range would need pagination handling this doesn't have.
"""
from datetime import date, timedelta

from django.core.management.base import BaseCommand

from apps.job_completion.services.pulling import pull_completed_jobs_for_date


class Command(BaseCommand):
    help = "Backfill completed jobs for every day in a date range (default: 1 Jan this year to yesterday)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--start", type=str, default=None, help="YYYY-MM-DD (default: 1 Jan this year)"
        )
        parser.add_argument("--end", type=str, default=None, help="YYYY-MM-DD (default: yesterday)")

    def handle(self, *args, **options):
        today = date.today()
        start = date.fromisoformat(options["start"]) if options["start"] else date(today.year, 1, 1)
        end = date.fromisoformat(options["end"]) if options["end"] else today - timedelta(days=1)

        if start > end:
            self.stderr.write(f"--start ({start}) is after --end ({end}); nothing to do.")
            return

        total_days = (end - start).days + 1
        total_created = 0
        total_updated = 0
        current = start
        day_num = 0
        while current <= end:
            day_num += 1
            summary = pull_completed_jobs_for_date(current)
            total_created += summary.created
            total_updated += summary.updated
            self.stdout.write(
                f"[{day_num}/{total_days}] {current}: {summary.created} created, "
                f"{summary.updated} updated, {summary.skipped_not_completed} skipped, "
                f"{summary.skipped_admin} admin entries skipped."
            )
            current += timedelta(days=1)

        self.stdout.write(
            self.style.SUCCESS(
                f"Backfill complete: {total_created} created, {total_updated} updated "
                f"across {total_days} day(s) ({start} to {end})."
            )
        )
