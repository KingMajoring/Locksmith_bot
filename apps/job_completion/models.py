from django.conf import settings
from django.db import models

from apps.locksmiths.models import Locksmith


class FailureCategory(models.Model):
    """Admin-configurable reason a job failed (e.g. "customer not
    present"). Starts empty — office staff build the list as real
    failures come in, rather than presets that might not match how
    WGTK actually talks about failures.

    master_reason buckets each category into who/what was actually at
    fault, for a training-needs view: is it WGTK's own office process,
    the client, a supplier, or the locksmith themselves that most
    failures trace back to.
    """

    class MasterReason(models.TextChoices):
        WGTK_OFFICE = "wgtk_office", "WGTK Office"
        CLIENT = "client", "Client"
        SUPPLIER = "supplier", "Supplier"
        WGTK_LOCKSMITH = "wgtk_locksmith", "WGTK Locksmith"
        NONE = "none", "None"

    name = models.CharField(max_length=200, unique=True)
    master_reason = models.CharField(
        max_length=20, choices=MasterReason.choices, default=MasterReason.NONE
    )
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "Failure categories"

    def __str__(self):
        return self.name


class SLATarget(models.Model):
    """Admin-configurable target on-site duration for a service type,
    used as one of three benchmarks shown for a completed job (the
    others being the company average and the locksmith's own average).
    """

    service_type = models.CharField(max_length=100, unique=True)
    target_minutes = models.PositiveIntegerField()
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["service_type"]

    def __str__(self):
        return f"{self.service_type}: {self.target_minutes} min"


class CompletedJob(models.Model):
    """One Optimo order for one day, pulled daily via
    pull_completed_jobs (see management/commands). Covers every
    completed job (not just failures) so duration/mileage benchmarks
    are built from real company-wide data — failed ones additionally
    get a failure_category assigned by office staff.
    """

    class Status(models.TextChoices):
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"

    order_no = models.CharField(max_length=100, unique=True)
    report_id = models.CharField(
        max_length=100, help_text="Handl ReportID, parsed from order_no."
    )
    job_date = models.DateField()

    locksmith = models.ForeignKey(
        Locksmith, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="completed_jobs",
    )
    driver_serial = models.CharField(
        max_length=64, blank=True,
        help_text="Raw Optimo driverSerial — kept even when it couldn't "
        "be matched to a locksmith, so unmatched drivers are visible.",
    )

    status = models.CharField(max_length=20, choices=Status.choices)
    start_time = models.DateTimeField(null=True, blank=True)
    end_time = models.DateTimeField(null=True, blank=True)
    distance_metres = models.FloatField(default=0)
    travel_time_seconds = models.PositiveIntegerField(default=0)

    make = models.CharField(max_length=100, blank=True)
    model = models.CharField(max_length=100, blank=True)
    year = models.CharField(max_length=10, blank=True)
    vin = models.CharField(max_length=32, blank=True, verbose_name="VIN")
    service_type = models.CharField(max_length=100, blank=True)
    disposed_skus = models.CharField(
        max_length=255, blank=True,
        help_text="SKUs of parts disposed against this job (comma-separated, most recent first).",
    )

    failure_category = models.ForeignKey(
        FailureCategory, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="jobs",
    )
    completion_note = models.TextField(
        blank=True,
        help_text="Driver's free-text completion note from Optimo — written "
        "for any job, not just failures (e.g. a summary of work done).",
    )
    categorized_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    categorized_at = models.DateTimeField(null=True, blank=True)

    pulled_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-job_date", "order_no"]

    def __str__(self):
        return f"{self.order_no} ({self.get_status_display()})"

    @property
    def duration_minutes(self) -> int | None:
        if not self.start_time or not self.end_time:
            return None
        return round((self.end_time - self.start_time).total_seconds() / 60)

    @property
    def distance_miles(self) -> float | None:
        if not self.distance_metres:
            return None
        return round(self.distance_metres / 1609.344, 1)

    @property
    def needs_categorization(self) -> bool:
        return self.status == self.Status.FAILED and self.failure_category_id is None
