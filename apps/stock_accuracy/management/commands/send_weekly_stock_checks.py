"""Run daily (e.g. Azure scheduled WebJob/cron at ~06:00) to send the
weekly stock check to whichever locksmiths are scheduled for today.
"""
from datetime import date, timedelta

from django.core.management.base import BaseCommand

from apps.stock_accuracy.models import StockCheckSchedule
from apps.stock_accuracy.services.emailing import send_weekly_check
from apps.stock_accuracy.services.generation import generate_weekly_check


class Command(BaseCommand):
    help = "Generate and send the weekly stock check for locksmiths scheduled today."

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
            weekly_check = generate_weekly_check(locksmith, week_starting)
            if weekly_check.sent_at:
                self.stdout.write(f"{locksmith}: already sent for {week_starting}, skipping.")
                continue
            try:
                send_weekly_check(weekly_check)
            except ValueError as exc:
                self.stderr.write(f"{locksmith}: {exc}")
                continue
            self.stdout.write(f"{locksmith}: sent stock check for week of {week_starting}.")
