from django.contrib import admin

from .models import PortalDisposal


@admin.register(PortalDisposal)
class PortalDisposalAdmin(admin.ModelAdmin):
    """Read-only — this table is written by the portal views only,
    office use is for review/follow-up on failed Handl syncs."""

    list_display = (
        "created_at", "locksmith", "order_no", "part_code", "quantity", "handl_synced",
    )
    list_filter = ("handl_synced", "locksmith")
    search_fields = ("order_no", "report_id", "part_code", "locksmith__name")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
