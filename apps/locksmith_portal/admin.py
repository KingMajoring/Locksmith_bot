from django.contrib import admin

from .models import (
    JobVisit,
    JobVisitPhoto,
    PortalDisposal,
    SafetyAlert,
    SeniorStaffContact,
)


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


@admin.register(SeniorStaffContact)
class SeniorStaffContactAdmin(admin.ModelAdmin):
    """Who a lone-worker safety alert (panic button, overdue-job
    escalation) goes to — office-managed, add/remove as needed."""

    list_display = ("name", "phone_number", "active", "order")
    list_editable = ("active", "order")
    ordering = ("order", "name")


@admin.register(SafetyAlert)
class SafetyAlertAdmin(admin.ModelAdmin):
    """Read-only audit trail — written by panic_alert/check_overdue_visits
    only."""

    list_display = ("triggered_at", "kind", "locksmith", "job_visit", "notified_contacts")
    list_filter = ("kind", "locksmith")
    search_fields = ("locksmith__name", "notified_contacts")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class JobVisitPhotoInline(admin.TabularInline):
    model = JobVisitPhoto
    extra = 0
    readonly_fields = ("kind", "url", "uploaded_at")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(JobVisit)
class JobVisitAdmin(admin.ModelAdmin):
    """Read-only — written by the portal's job-progress views only;
    office use is for review, same as PortalDisposal."""

    list_display = ("updated_at", "locksmith", "order_no", "stage", "outcome")
    list_filter = ("stage", "outcome", "locksmith")
    search_fields = ("order_no", "report_id", "locksmith__name")
    inlines = [JobVisitPhotoInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
