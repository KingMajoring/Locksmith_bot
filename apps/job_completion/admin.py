from django.contrib import admin
from django.utils import timezone

from .models import CompletedJob, FailureCategory, SLATarget


@admin.register(FailureCategory)
class FailureCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "master_reason", "active")
    list_editable = ("master_reason",)
    list_filter = ("master_reason", "active")


@admin.register(SLATarget)
class SLATargetAdmin(admin.ModelAdmin):
    list_display = ("loss_type", "target_minutes", "active")
    list_filter = ("active",)


@admin.register(CompletedJob)
class CompletedJobAdmin(admin.ModelAdmin):
    list_display = (
        "order_no",
        "job_date",
        "locksmith",
        "status",
        "service_type",
        "duration_minutes",
        "distance_miles",
        "failure_category",
    )
    list_editable = ("failure_category",)
    list_filter = ("status", "job_date", "locksmith", "service_type")
    search_fields = ("order_no", "report_id", "locksmith__name")
    readonly_fields = (
        "order_no",
        "report_id",
        "job_date",
        "locksmith",
        "driver_serial",
        "status",
        "start_time",
        "end_time",
        "distance_metres",
        "travel_time_seconds",
        "make",
        "model",
        "year",
        "vin",
        "service_type",
        "loss_type",
        "supplied_service",
        "disposed_skus",
        "completion_note",
        "categorized_by",
        "categorized_at",
        "pulled_at",
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("locksmith", "failure_category")

    def save_model(self, request, obj, form, change):
        # Regular single-object edit form (list_editable saves go through
        # save_formset below instead, which Django routes separately).
        if "failure_category" in form.changed_data:
            obj.categorized_by = request.user
            obj.categorized_at = timezone.now()
        super().save_model(request, obj, form, change)

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for obj in instances:
            obj.categorized_by = request.user
            obj.categorized_at = timezone.now()
            obj.save()
        formset.save_m2m()
