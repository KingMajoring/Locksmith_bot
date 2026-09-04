from django.conf import settings
from django.db import models

from apps.locksmiths.models import Locksmith


class PortalDisposal(models.Model):
    """Audit log of every part-disposal attempt made through the
    locksmith self-service portal (/locksmith/), independent of whether
    the write to Handl (Inventory_Disposals, via
    apps.integrations.handl.record_disposal) actually succeeded — so
    office has a record to follow up from (handl_synced/handl_error)
    even when the Handl write itself failed.
    """

    locksmith = models.ForeignKey(
        Locksmith, on_delete=models.CASCADE, related_name="portal_disposals"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    order_no = models.CharField(max_length=100)
    report_id = models.CharField(max_length=100)
    part_code = models.CharField(max_length=64)
    part_name = models.CharField(max_length=200)
    quantity = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    handl_synced = models.BooleanField(default=False)
    handl_error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.locksmith} disposed {self.quantity} x {self.part_code} on {self.order_no}"


class JobVisit(models.Model):
    """One locksmith's progress through a job: on route -> arrived (+
    before photos) -> parts disposed -> completed (+ after photos,
    notes, outcome). Purely a portal-side tracking/audit record — it
    does NOT feed back into job_completion.CompletedJob, which stays
    sourced from the overnight Optimo pull (Optimo remains the source
    of truth for travel time and on-site start/end); this is the
    richer in-the-moment log that gets a note trail written to Handl
    (see apps.integrations.handl.add_report_note) as each stage
    completes, since Handl has no way to receive the photos directly
    (see apps.integrations.photos for why).

    Deliberately one row per (locksmith, order_no), not append-only —
    a locksmith progressing through today's job updates the same visit
    rather than creating a new one each stage.
    """

    class Stage(models.TextChoices):
        NOT_STARTED = "not_started", "Not started"
        ON_ROUTE = "on_route", "On route"
        ARRIVED = "arrived", "Arrived"
        PARTS_DONE = "parts_done", "Parts disposed"
        DONE = "done", "Done"

    class Outcome(models.TextChoices):
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    locksmith = models.ForeignKey(
        Locksmith, on_delete=models.CASCADE, related_name="job_visits"
    )
    order_no = models.CharField(max_length=100)
    report_id = models.CharField(max_length=100)

    stage = models.CharField(max_length=20, choices=Stage.choices, default=Stage.NOT_STARTED)
    on_route_at = models.DateTimeField(null=True, blank=True)
    arrived_at = models.DateTimeField(null=True, blank=True)
    parts_done_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    notes = models.TextField(blank=True)
    outcome = models.CharField(max_length=20, choices=Outcome.choices, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["locksmith", "order_no"], name="one_job_visit_per_locksmith_job"
            )
        ]

    def __str__(self):
        return f"{self.locksmith} — {self.order_no} ({self.get_stage_display()})"


class JobVisitPhoto(models.Model):
    """A before/after photo uploaded against a JobVisit — stored in our
    own blob storage (see apps.integrations.photos), url points there
    directly since we don't proxy/serve them ourselves. CharField, not
    URLField: MockPhotoStorage (local dev/tests) returns a relative
    MEDIA_URL path, not an absolute URL like the real Azure backend."""

    class Kind(models.TextChoices):
        BEFORE = "before", "Before"
        AFTER = "after", "After"

    visit = models.ForeignKey(JobVisit, on_delete=models.CASCADE, related_name="photos")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    url = models.CharField(max_length=1000)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["uploaded_at"]

    def __str__(self):
        return f"{self.get_kind_display()} photo for {self.visit.order_no}"
