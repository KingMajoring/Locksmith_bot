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

    # The client supplied this part themselves (e.g. already had a
    # replacement key blank) — no real WGTK stock was used, so van
    # stock isn't decremented and this never goes through
    # apps.integrations.handl.record_disposal, but the SKU is still
    # recorded (as a plain Handl note, via _write_handl_note) so
    # historical parts-usage reporting still has it.
    client_supplied = models.BooleanField(default=False)

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
        CANCELLED = "cancelled", "Cancelled"

    class CancelReason(models.TextChoices):
        """Never attended jobs only (see job_cancel) — distinct from a
        FailureCategory, which is for a job the locksmith did attend."""
        CLIENT_CANCELLED = "client_cancelled", "Client cancelled"
        OFFICE_PULLED = "office_pulled", "Office pulled the job"
        COULDNT_ATTEND = "couldnt_attend", "Couldn't attend — traffic/weather"
        WRONG_ADDRESS = "wrong_address", "Wrong address/details"
        OTHER = "other", "Other"

    class AccessMethod(models.TextChoices):
        """Gain access jobs only — how the locksmith actually got in."""
        PICKED = "picked", "Picked"
        AIRBAG = "airbag", "Airbag"

    class FailureReason(models.TextChoices):
        WRONG_PARTS = "wrong_parts", "Wrong parts"
        PROGRAMMER_ISSUE = "programmer_issue", "Programmer issue"

    class ReattendAction(models.TextChoices):
        """Programmer-issue failures only — the locksmith's own
        recommendation for what happens next, for office to action."""
        SELF = "self", "Reattend myself"
        DIFFERENT_LOCKSMITH = "different_locksmith", "Reattend with a different locksmith"
        NONE = "none", "No reattend"

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

    # Best-effort browser Geolocation captured when the locksmith submits
    # arrival photos — never blocks progress (a denied/unavailable
    # permission just leaves these null); included as a Google Maps
    # link in the arrival Handl note when present.
    arrival_latitude = models.FloatField(null=True, blank=True)
    arrival_longitude = models.FloatField(null=True, blank=True)

    notes = models.TextField(blank=True)
    outcome = models.CharField(max_length=20, choices=Outcome.choices, blank=True)

    # Never-attended jobs only (see job_cancel) — cancelled before the
    # locksmith arrived, so there's nothing to fail. notes carries any
    # free-text detail, same field the completion flow uses.
    cancel_reason = models.CharField(max_length=20, choices=CancelReason.choices, blank=True)

    # Gain access jobs: how they got in, and the disclaimer the customer
    # signs on the locksmith's phone before an airbag attempt (see
    # JobVisitPhoto.Kind.DISCLAIMER_SIGNATURE for the actual signature
    # image — disclaimer_signed_at is just the attestation timestamp).
    access_method = models.CharField(max_length=20, choices=AccessMethod.choices, blank=True)
    pick_used = models.CharField(max_length=200, blank=True)
    disclaimer_signed_at = models.DateTimeField(null=True, blank=True)

    # Failed jobs: why, and (for reasons that need it) the SKU still
    # needed or the locksmith's own reattend recommendation — office's
    # to action, not automated. failure_category is picked from office's
    # own admin-configured apps.job_completion.models.FailureCategory
    # list (see views._FAILURE_CATEGORIES_HIDDEN_FROM_LOCKSMITH etc. for
    # which categories show which sub-field) — deliberately not the
    # locksmith self-rating their own competence; that classification
    # (FailureCategory.master_reason) happens office-side, invisibly to
    # the locksmith. failure_reason/FailureReason predate this and are
    # unused going forward, kept only so any already-recorded failures
    # aren't silently blanked.
    failure_reason = models.CharField(max_length=20, choices=FailureReason.choices, blank=True)
    failure_category = models.ForeignKey(
        "job_completion.FailureCategory", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )
    failure_sku_needed = models.CharField(max_length=200, blank=True)
    failure_reattend_action = models.CharField(max_length=20, choices=ReattendAction.choices, blank=True)

    # Every completed (not failed) job: the customer signs on the
    # locksmith's phone to confirm they're happy with the completed
    # work — see JobVisitPhoto.Kind.COMPLETION_SIGNATURE for the image.
    # If the customer isn't there to sign, completion_signed_at stays
    # null and customer_not_present_reason explains why instead.
    completion_signed_at = models.DateTimeField(null=True, blank=True)
    customer_not_present_reason = models.CharField(max_length=200, blank=True)

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
        DOOR_FRAME = "door_frame", "Door frame"
        FRONT_OF_CAR = "front_of_car", "Front of the car (with the reg plate visible)"
        DOOR_LOCK = "door_lock", "Door with the lock"
        DAMAGE = "damage", "Damage"
        KEYS_SUPPLIED = "keys_supplied", "Keys supplied"
        CLIENT_KEY = "client_key", "Client's key"
        IGNITION_ON = "ignition_on", "Ignition on"
        DISCLAIMER_SIGNATURE = "disclaimer_signature", "Disclaimer signature"
        COMPLETION_SIGNATURE = "completion_signature", "Completion sign-off signature"

    visit = models.ForeignKey(JobVisit, on_delete=models.CASCADE, related_name="photos")
    kind = models.CharField(max_length=25, choices=Kind.choices)
    url = models.CharField(max_length=1000)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["uploaded_at"]

    def __str__(self):
        return f"{self.get_kind_display()} photo for {self.visit.order_no}"
