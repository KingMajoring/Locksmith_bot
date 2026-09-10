from django.contrib import admin

from .models import (
    JobTimingSummary,
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


@admin.register(JobTimingSummary)
class JobTimingSummaryAdmin(admin.ModelAdmin):
    """Read-only — written by job_complete only, for office reporting
    on travel/job durations without a live Handl round trip each time."""

    list_display = (
        "created_at", "locksmith", "order_no", "reg", "make", "model_name",
        "travel_time", "job_time", "skus_used",
    )
    list_filter = ("locksmith",)
    search_fields = ("order_no", "report_id", "reg", "vin", "locksmith__name")

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
    office use is for review, same as PortalDisposal. The one exception
    is reset_for_testing below, so a test job can be replayed through
    the portal without shelling into the container to delete the row
    by hand."""

    list_display = ("updated_at", "locksmith", "order_no", "stage", "outcome")
    list_filter = ("stage", "outcome", "locksmith")
    search_fields = ("order_no", "report_id", "locksmith__name")
    inlines = [JobVisitPhotoInline]
    actions = ["reset_for_testing"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Reset for testing (clears all steps — deletes the visit)")
    def reset_for_testing(self, request, queryset):
        count = queryset.count()
        queryset.delete()
        self.message_user(
            request,
            f"Reset {count} job visit(s) — next time that locksmith opens the job "
            "in the portal, it starts fresh from \"Mark on route\".",
        )
