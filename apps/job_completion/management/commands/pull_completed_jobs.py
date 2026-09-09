"""Run daily (see .github/workflows/scheduled-pull-completed-jobs.yml —
GitHub Actions, not an Azure WebJob, which never actually ran on this
deployment) to pull recently completed jobs from Optimo.

Re-checks the last CATCHUP_DAYS days, not just yesterday, every run —
update_or_create in pull_completed_jobs_for_date is idempotent (keyed by
order_no), so re-pulling a day already fully pulled costs nothing. This
means a job still mid-way through Optimo (skipped_not_completed) the
first time it's checked gets picked up automatically on a later run,
and a one-off missed/delayed scheduled run doesn't lose a day — rather
than relying on every single run landing exactly on time.
"""
from datetime import date, timedelta

from django.core.management.base import BaseCommand

from apps.job_completion.services.pulling import pull_completed_jobs_for_date

CATCHUP_DAYS = 3


class Command(BaseCommand):
    help = "Pull completed jobs from Optimo for a given date (default: the last few days, to catch up anything missed)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            type=str,
            default=None,
            help="Pull only this one date, YYYY-MM-DD (default: the last "
            f"{CATCHUP_DAYS} days, ending yesterday).",
        )

    def handle(self, *args, **options):
        if options["date"]:
            dates = [date.fromisoformat(options["date"])]
        else:
            yesterday = date.today() - timedelta(days=1)
            dates = [yesterday - timedelta(days=i) for i in range(CATCHUP_DAYS)]

        for for_date in dates:
            summary = pull_completed_jobs_for_date(for_date)
            self.stdout.write(
                f"{for_date}: {summary.created} created, {summary.updated} updated, "
                f"{summary.skipped_not_completed} not yet completed (skipped), "
                f"{summary.skipped_admin} admin/housekeeping entries (skipped)."
            )
