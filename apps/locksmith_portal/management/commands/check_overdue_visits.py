"""Run every 15-30 min (see .github/workflows/scheduled-check-overdue-visits.yml
— Azure WebJobs never actually run on this deployment, see that
workflow's own comment) — lone-worker safety escalation for a
locksmith who's gone quiet after arriving on site.

Deliberately alerts once per JobVisit, not on every run it's still
overdue — a SafetyAlert(kind=OVERDUE) already existing for that visit
is treated as "someone's already been told, they're following up",
not something to keep re-firing.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.locksmith_portal.models import JobVisit, SafetyAlert
from apps.locksmith_portal.views import _alert_senior_staff

OVERDUE_AFTER_MINUTES = 60


class Command(BaseCommand):
    help = "Alert senior staff about any job visit that's gone quiet too long after arriving."

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(minutes=OVERDUE_AFTER_MINUTES)
        already_alerted = SafetyAlert.objects.filter(
            kind=SafetyAlert.Kind.OVERDUE, job_visit__isnull=False
        ).values_list("job_visit_id", flat=True)

        overdue = JobVisit.objects.filter(
            stage=JobVisit.Stage.ARRIVED, arrived_at__lte=cutoff,
        ).exclude(pk__in=already_alerted)

        count = 0
        for visit in overdue:
            minutes = int((timezone.now() - visit.arrived_at).total_seconds() // 60)
            message = (
                f"WGTK SAFETY CHECK: {visit.locksmith.name} arrived on job {visit.order_no} "
                f"{minutes} min ago with no update since. Please check in with them."
            )
            notified = _alert_senior_staff(message)
            SafetyAlert.objects.create(
                locksmith=visit.locksmith, kind=SafetyAlert.Kind.OVERDUE,
                job_visit=visit, notified_contacts=", ".join(notified),
            )
            count += 1

        self.stdout.write(f"Alerted on {count} overdue job visit(s).")
