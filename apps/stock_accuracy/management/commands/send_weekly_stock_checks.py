"""Run daily (e.g. Azure scheduled WebJob/cron at ~06:00) to generate the
weekly stock check for whichever locksmiths are scheduled for today — they
fill it in directly from their dashboard in the locksmith portal.
"""
from datetime import date, timedelta

from django.core.management.base import BaseCommand

from apps.stock_accuracy.models import StockCheckSchedule
from apps.stock_accuracy.services.generation import generate_weekly_check


class Command(BaseCommand):
    help = "Generate the weekly stock check for locksmiths scheduled today."

    def handle(self, *args, **options):
        today = date.today()
        week_starting = today - timedelta(days=today.weekday())

        due = StockCheckSchedule.objects.filter(
            weekday=today.weekday(), enabled=True, locksmith__active=True
        ).select_related("locksmith")

        if not due:
            self.stdout.write("No locksmiths scheduled for today.")
            return

        for schedule in due:
            locksmith = schedule.locksmith
            generate_weekly_check(locksmith, week_starting)
            self.stdout.write(f"{locksmith}: stock check ready for week of {week_starting}.")
