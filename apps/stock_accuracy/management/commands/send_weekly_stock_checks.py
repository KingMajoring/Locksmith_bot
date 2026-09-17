"""Run daily (e.g. Azure scheduled WebJob/cron at ~06:00) to generate
today's stock check for every enabled, active locksmith — they fill it in
directly from their dashboard in the locksmith portal.

Used to go out once a week, on whichever weekday StockCheckSchedule
assigned each locksmith (staggered so office wasn't reconciling everyone's
counts on the same day) — now every enabled locksmith gets a fresh check
every day this runs, so the weekday itself no longer matters; only
StockCheckSchedule.enabled still gates participation. Command/file name
kept as-is to avoid also having to update the scheduled GitHub Actions
workflow and the internal run-job allowlist that reference it by name.
"""
from datetime import date

from django.core.management.base import BaseCommand

from apps.stock_accuracy.models import StockCheckSchedule
from apps.stock_accuracy.services.generation import generate_weekly_check


class Command(BaseCommand):
    help = "Generate today's stock check for every enabled locksmith."

    def handle(self, *args, **options):
        today = date.today()

        due = StockCheckSchedule.objects.filter(
            enabled=True, locksmith__active=True
        ).select_related("locksmith")

        if not due:
            self.stdout.write("No locksmiths enabled for a stock check.")
            return

        for schedule in due:
            locksmith = schedule.locksmith
            generate_weekly_check(locksmith, today)
            self.stdout.write(f"{locksmith}: stock check ready for {today}.")
