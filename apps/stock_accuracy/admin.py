from django.contrib import admin

from .models import (
    EmailSettings,
    StockCheckItem,
    StockCheckSchedule,
    VarianceThreshold,
    WeeklyStockCheck,
)


@admin.register(EmailSettings)
class EmailSettingsAdmin(admin.ModelAdmin):
    list_display = ("__str__", "updated_at")

    def has_add_permission(self, request):
        # Single row, tool-wide — same pattern as OptimoSettings/VarianceThreshold.
        return not EmailSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(StockCheckSchedule)
class StockCheckScheduleAdmin(admin.ModelAdmin):
    list_display = ("locksmith", "weekday", "enabled")
    list_filter = ("weekday", "enabled")


@admin.register(VarianceThreshold)
class VarianceThresholdAdmin(admin.ModelAdmin):
    list_display = (
        "active",
        "unit_threshold",
        "pct_threshold",
        "value_threshold",
        "repeat_offender_occurrences",
        "repeat_offender_window_weeks",
    )
    list_filter = ("active",)


class StockCheckItemInline(admin.TabularInline):
    model = StockCheckItem
    extra = 0
    readonly_fields = ("part_code", "part_name", "expected_qty", "unit_cost")
    fields = ("part_code", "part_name", "expected_qty", "unit_cost", "actual_qty", "entered_by")


@admin.register(WeeklyStockCheck)
class WeeklyStockCheckAdmin(admin.ModelAdmin):
    list_display = ("locksmith", "week_starting", "status", "sent_at", "completed_at")
    list_filter = ("status", "week_starting")
    search_fields = ("locksmith__name",)
    inlines = [StockCheckItemInline]
