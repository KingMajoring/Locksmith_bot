from django.contrib import admin

from .models import (
    JobVisit,
    JobVisitPhoto,
    PortalDisposal,
    PortalPhotoPrompt,
    PortalSettings,
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


@admin.register(PortalPhotoPrompt)
class PortalPhotoPromptAdmin(admin.ModelAdmin):
    """What photos the portal's completion flow asks for, per service
    and step, and in what order — see PortalPhotoPrompt's docstring.
    Edit here instead of in code; a service with no rows falls back to
    the "Default" rows for that step."""

    list_display = ("service_label", "step", "kind", "display_label", "required", "order", "active")
    list_editable = ("required", "order", "active")
    list_filter = ("service_label", "step", "active")
    ordering = ("service_label", "step", "order", "id")


@admin.register(PortalSettings)
class PortalSettingsAdmin(admin.ModelAdmin):
    """Single row (see PortalSettings.load) — the airbag disclaimer
    wording shown/signed on the access-method step."""

    def has_add_permission(self, request):
        return not PortalSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False
