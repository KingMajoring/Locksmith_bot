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
