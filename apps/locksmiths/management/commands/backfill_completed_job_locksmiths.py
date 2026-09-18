"""One-off/occasional fix for CompletedJob rows stuck showing
"Unmatched" in job_completion reports (e.g. jobs_by_day) even though
their driver now has a real OptimoDriverId mapping.

CompletedJob.locksmith is resolved and stored once, at pull time, from
whatever OptimoDriverId mappings exist that night (see
apps.job_completion.services.pulling) — a job pulled before its driver
was ever matched stays unmatched forever otherwise, even once the
mapping is created later via "Sync from Optimo" or admin. As of the fix
this command exists alongside, a newly-created mapping backfills this
automatically going forward (see
apps.locksmiths.services.commit_optimo_driver_matches); this covers
whatever's already stuck from before that existed.

Safe to run anytime — DB-only, no live Optimo/Handl calls, and only
ever touches a row that's genuinely fixable right now.

Usage:
    python manage.py backfill_completed_job_locksmiths
"""
from django.core.management.base import BaseCommand

from apps.locksmiths.services import backfill_completed_job_locksmiths


class Command(BaseCommand):
    help = "Fix CompletedJob rows still showing Unmatched despite an existing OptimoDriverId mapping."

    def handle(self, *args, **options):
        updated = backfill_completed_job_locksmiths()
        if updated:
            self.stdout.write(self.style.SUCCESS(f"Backfilled locksmith on {updated} completed job(s)."))
        else:
            self.stdout.write("Nothing to backfill — every mapped driver's jobs are already up to date.")
