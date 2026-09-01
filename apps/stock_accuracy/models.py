from django.conf import settings
from django.db import models

from apps.locksmiths.models import Locksmith


class StockCheckSchedule(models.Model):
    """Which weekday each locksmith's weekly stock check goes out on.

    Staggered per locksmith so office admin isn't reconciling everyone's
    returned counts on the same day.
    """

    class Weekday(models.IntegerChoices):
        MONDAY = 0, "Monday"
        TUESDAY = 1, "Tuesday"
        WEDNESDAY = 2, "Wednesday"
        THURSDAY = 3, "Thursday"
        FRIDAY = 4, "Friday"

    locksmith = models.OneToOneField(
        Locksmith, on_delete=models.CASCADE, related_name="stock_check_schedule"
    )
    weekday = models.IntegerField(choices=Weekday.choices)
    enabled = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.locksmith} → {self.get_weekday_display()}"


class VarianceThreshold(models.Model):
    """Admin-configurable rules for when a variance counts as an 'issue'.

    A single active row is used tool-wide; kept as a model (rather than
    settings) so office admin can tune it without a redeploy.
    """

    unit_threshold = models.PositiveIntegerField(
        default=2, help_text="Flag when |actual - expected| exceeds this many units."
    )
    pct_threshold = models.DecimalField(
        max_digits=5, decimal_places=1, default=10.0,
        help_text="Flag when the variance exceeds this percentage of expected qty.",
    )
    value_threshold = models.DecimalField(
        max_digits=8, decimal_places=2, default=25.00,
        help_text="Flag when the £ impact of the variance exceeds this amount.",
    )
    repeat_offender_occurrences = models.PositiveIntegerField(
        default=3, help_text="Number of flagged weeks that counts as a repeat offender...",
    )
    repeat_offender_window_weeks = models.PositiveIntegerField(
        default=4, help_text="...within this many most recent weeks.",
    )
    active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Variance threshold configuration"
        verbose_name_plural = "Variance threshold configuration"

    def __str__(self):
        return "Active variance thresholds" if self.active else "Inactive threshold config"

    @classmethod
    def current(cls) -> "VarianceThreshold":
        obj = cls.objects.filter(active=True).order_by("-id").first()
        if obj:
            return obj
        return cls.objects.create()


class WeeklyStockCheck(models.Model):
    """One locksmith's stock check for one week: the 10 lines drawn, sent
    out, and (eventually) reconciled against the counts they return."""

    class Status(models.TextChoices):
        GENERATED = "generated", "Generated"
        SENT = "sent", "Sent"
        AWAITING_ENTRY = "awaiting_entry", "Awaiting count entry"
        COMPLETED = "completed", "Completed"

    locksmith = models.ForeignKey(
        Locksmith, on_delete=models.CASCADE, related_name="stock_checks"
    )
    week_starting = models.DateField()
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.GENERATED
    )
    generated_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("locksmith", "week_starting")
        ordering = ["-week_starting"]

    def __str__(self):
        return f"{self.locksmith} — week of {self.week_starting}"

    @property
    def is_fully_entered(self) -> bool:
        return not self.items.filter(actual_qty__isnull=True).exists()


class StockCheckItem(models.Model):
    """A single part line within a weekly stock check.

    expected_qty/unit_cost are frozen from Handl at the moment the check
    is generated and sent, so later stock movement doesn't distort the
    comparison against what the locksmith actually counted.
    """

    weekly_check = models.ForeignKey(
        WeeklyStockCheck, on_delete=models.CASCADE, related_name="items"
    )
    part_code = models.CharField(max_length=64)
    part_name = models.CharField(max_length=200)
    expected_qty = models.PositiveIntegerField()
    unit_cost = models.DecimalField(max_digits=8, decimal_places=2, default=0)

    actual_qty = models.PositiveIntegerField(null=True, blank=True)
    entered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    entered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["part_code"]

    def __str__(self):
        return f"{self.part_code} ({self.weekly_check})"

    @property
    def variance(self) -> int | None:
        if self.actual_qty is None:
            return None
        return self.actual_qty - self.expected_qty

    @property
    def variance_pct(self) -> float | None:
        if self.actual_qty is None or not self.expected_qty:
            return None
        return round(abs(self.variance) / self.expected_qty * 100, 1)

    @property
    def value_impact(self) -> float | None:
        if self.actual_qty is None:
            return None
        return round(abs(self.variance) * float(self.unit_cost), 2)

    def is_flagged(self, thresholds: VarianceThreshold | None = None) -> bool:
        if self.actual_qty is None:
            return False
        thresholds = thresholds or VarianceThreshold.current()
        variance = abs(self.variance)
        if variance == 0:
            return False
        if variance > thresholds.unit_threshold:
            return True
        if self.variance_pct is not None and self.variance_pct > float(thresholds.pct_threshold):
            return True
        if self.value_impact is not None and self.value_impact > float(thresholds.value_threshold):
            return True
        return False
