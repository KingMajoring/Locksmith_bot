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

    # Set once this row has at least one PortalDisposalEdit against it
    # (an edit, a void, or a late add) — a quick flag for the "edited"
    # indicator on the locksmith's own job page, without joining out to
    # `edits` just to check whether any exist.
    needs_review = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.locksmith} disposed {self.quantity} x {self.part_code} on {self.order_no}"


class PortalDisposalEdit(models.Model):
    """Audit trail entry for a PortalDisposal that was changed after the
    fact: either an office-visible correction to an already-recorded
    disposal (kind=EDIT — includes voiding one entirely, by editing its
    quantity down to 0), or a brand new disposal added once the job was
    already marked done (kind=LATE_ADD, where old_* is left blank since
    there's nothing to compare against).

    Locksmiths can edit any of their own recorded disposals at any time,
    but a reason is always required — this table, plus the matching
    Policy_History note left on the Handl claim (see
    apps.locksmith_portal.views.edit_disposal/job_detail), is what lets
    office/managers see what changed, when, by whom, and why.
    """

    class Kind(models.TextChoices):
        EDIT = "edit", "Edited"
        LATE_ADD = "late_add", "Added after job completed"

    disposal = models.ForeignKey(PortalDisposal, on_delete=models.CASCADE, related_name="edits")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    reason = models.TextField()
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    performed_at = models.DateTimeField(auto_now_add=True)

    old_part_code = models.CharField(max_length=64, blank=True)
    old_part_name = models.CharField(max_length=200, blank=True)
    old_quantity = models.PositiveIntegerField(null=True, blank=True)

    new_part_code = models.CharField(max_length=64, blank=True)
    new_part_name = models.CharField(max_length=200, blank=True)
    new_quantity = models.PositiveIntegerField(null=True, blank=True)

    # Set if pushing the correction/note to Handl failed — the local
    # record still stands (office can fix Handl directly), same
    # best-effort spirit as PortalDisposal.handl_error.
    handl_note_error = models.TextField(blank=True)

    # Office/manager review, from the disposal_reviews page — separate
    # from the Handl write succeeding: this is "someone looked at this
    # and it's fine", not "Handl was updated".
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["-performed_at"]

    def __str__(self):
        return f"{self.get_kind_display()} — {self.disposal} ({self.performed_at:%Y-%m-%d %H:%M})"


class FaultyPartReport(models.Model):
    """A part a locksmith fitted that turned out to be faulty/didn't
    work on the job — physically consumed from van stock the same as a
    normal disposal, but deliberately kept separate from PortalDisposal/
    Handl's Inventory_Disposals: apps.job_completion's parts-lookup
    "most likely part" suggestion is built from real successful
    disposals, and a faulty part isn't one — mixing it in would skew
    that suggestion toward parts that don't actually work. See
    apps.locksmith_portal.views.job_detail for where this is written
    (get_expected_stock + set_locksmith_stock_quantity, never
    record_disposal).

    Vehicle details are captured at the time (rather than joined out to
    Handl later) so a part-reliability/success-rate report can be built
    from this table alone, without a live Handl query.
    """

    locksmith = models.ForeignKey(
        Locksmith, on_delete=models.CASCADE, related_name="faulty_part_reports"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    order_no = models.CharField(max_length=100)
    report_id = models.CharField(max_length=100)
    part_code = models.CharField(max_length=64)
    part_name = models.CharField(max_length=200)
    quantity = models.PositiveIntegerField()

    vin = models.CharField(max_length=50, blank=True)
    reg = models.CharField(max_length=20, blank=True)
    make = models.CharField(max_length=100, blank=True)
    model_name = models.CharField(max_length=100, blank=True)
    year = models.CharField(max_length=10, blank=True)
    # Null when Handl has no key-claim row to read it from at all —
    # distinct from a genuine False (see JobDetails.spare_key).
    spare_key = models.BooleanField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    handl_synced = models.BooleanField(default=False)
    handl_error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.locksmith} reported {self.part_code} faulty on {self.order_no}"


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
        KEY_CODE = "key_code", "Key code supplied"
        DEALER_KEY = "dealer_key", "Dealer-supplied key (pre-cut)"

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

    # Completed jobs only: is there more work needed here that this
    # visit doesn't cover (e.g. a second fault found on-site) — for
    # office to action, same spirit as failure_sku_needed.
    further_work_required = models.BooleanField(default=False)
    further_work_details = models.CharField(max_length=500, blank=True)

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
        DOOR_OPEN = "door_open", "Door open"
        KEY_IN_HAND = "key_in_hand", "Key in hand"
        FRONT_OF_CAR = "front_of_car", "Front of the car (with the reg plate visible)"
        DOOR_LOCK = "door_lock", "Door with the lock"
        DAMAGE = "damage", "Damage"
        BLADE_IN_DOOR = "blade_in_door", "Blade turned in the door"
        BLADE_IN_IGNITION = "blade_in_ignition", "Blade turned in the ignition"
        KEYS_SUPPLIED = "keys_supplied", "Keys supplied"
        CLIENT_KEY = "client_key", "New key with the client's key"
        IGNITION_ON = "ignition_on", "Ignition on"
        MILEAGE = "mileage", "Mileage"
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


class JobTimingSummary(models.Model):
    """One flat row per completed JobVisit — vehicle details (from Handl,
    at the moment the job was marked done) plus the travel/on-site
    durations computed from the visit's own stage timestamps, held here
    in our own database so office can query/report on this directly
    without a live Handl SQL round trip each time. Written alongside
    (not instead of) the plain-English timing note left on the Handl
    claim itself — see views.job_complete.

    Purely a reporting summary: never read back to drive behaviour
    anywhere else in the portal, so it's fine for this to be best-effort
    and missing for a handful of visits (e.g. Handl's job details
    temporarily unreachable when the job was completed).
    """

    visit = models.OneToOneField(
        JobVisit, on_delete=models.CASCADE, related_name="timing_summary"
    )
    order_no = models.CharField(max_length=100)
    report_id = models.CharField(max_length=100)
    locksmith = models.ForeignKey(
        Locksmith, on_delete=models.CASCADE, related_name="job_timing_summaries"
    )

    reg = models.CharField(max_length=20, blank=True)
    make = models.CharField(max_length=100, blank=True)
    model_name = models.CharField(max_length=100, blank=True)
    year = models.CharField(max_length=10, blank=True)
    vin = models.CharField(max_length=50, blank=True)

    travel_time = models.DurationField(
        null=True, blank=True,
        help_text="on_route_at to arrived_at — how long the locksmith was travelling to this job.",
    )
    job_time = models.DurationField(
        null=True, blank=True,
        help_text="arrived_at to completed_at — how long the locksmith was on site for this job.",
    )

    # Comma-separated part_code values (see PortalDisposal.part_code) —
    # a plain string rather than a relation since a disposal can be
    # edited/added after this summary is first written and this is a
    # point-in-time snapshot, not a live view.
    skus_used = models.CharField(max_length=500, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Job timing summary"
        verbose_name_plural = "Job timing summaries"

    def __str__(self):
        return f"Timing summary for {self.order_no}"


class SeniorStaffContact(models.Model):
    """Who gets a lone-worker safety alert (panic button, overdue-job
    escalation — see views.panic_alert and the check_overdue_visits
    management command). Admin-managed so office can add/remove people
    without a code change; order controls who the panic button's own
    tap-to-call link dials (lowest order first)."""

    name = models.CharField(max_length=200)
    phone_number = models.CharField(max_length=30)
    active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "name"]
        verbose_name = "Senior staff contact"

    def __str__(self):
        return f"{self.name} ({self.phone_number})"


class SafetyAlert(models.Model):
    """Audit trail of every lone-worker safety alert sent — a panic
    button press, or an overdue-job auto-escalation. Office-visible
    record even though the real-time alert itself goes out over SMS/
    WhatsApp, not through this app."""

    class Kind(models.TextChoices):
        PANIC = "panic", "Panic button"
        OVERDUE = "overdue", "Overdue job"

    locksmith = models.ForeignKey(Locksmith, on_delete=models.CASCADE, related_name="safety_alerts")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    job_visit = models.ForeignKey(
        JobVisit, null=True, blank=True, on_delete=models.SET_NULL, related_name="safety_alerts"
    )
    triggered_at = models.DateTimeField(auto_now_add=True)
    notified_contacts = models.TextField(
        blank=True, help_text="Comma-separated names of who was alerted, for the record."
    )

    class Meta:
        ordering = ["-triggered_at"]

    def __str__(self):
        return f"{self.get_kind_display()} — {self.locksmith} at {self.triggered_at:%Y-%m-%d %H:%M}"
