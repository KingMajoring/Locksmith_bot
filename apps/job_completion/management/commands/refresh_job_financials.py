"""Run daily (e.g. Azure scheduled WebJob, shortly after
pull_completed_jobs) to re-check CompletedJob rows still missing
net_cost — Handl's Policy_Financial rows are often entered days after a
job completes, after the one-time nightly pull already froze net_cost
as NULL. No age limit by default, since the Margin/Timing reports this
feeds are explicitly all-time history.
"""
from django.core.management.base import BaseCommand

from apps.job_completion.services.pulling import refresh_missing_financials


class Command(BaseCommand):
    help = "Re-fetch net_cost from Handl for jobs still missing it."

    def add_arguments(self, parser):
        parser.add_argument(
            "--window-days",
            type=int,
            default=None,
            help="Only look at jobs from the last N days (default: no limit).",
        )

    def handle(self, *args, **options):
        refreshed = refresh_missing_financials(window_days=options["window_days"])
        self.stdout.write(f"Refreshed net_cost for {refreshed} job(s).")
