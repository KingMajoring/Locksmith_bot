from django.contrib import admin

from .models import (
    PartUnitConversion,
    StockCheckItem,
    StockCheckSchedule,
    VarianceThreshold,
    VirtualStockItem,
    WeeklyStockCheck,
)


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


@admin.register(VirtualStockItem)
class VirtualStockItemAdmin(admin.ModelAdmin):
    list_display = ("part_code", "part_name", "note", "created_at")
    search_fields = ("part_code", "part_name")


@admin.register(PartUnitConversion)
class PartUnitConversionAdmin(admin.ModelAdmin):
    list_display = ("part_code", "part_name", "units_per_pack", "note", "created_at")
    search_fields = ("part_code", "part_name")


class StockCheckItemInline(admin.TabularInline):
    model = StockCheckItem
    extra = 0
    readonly_fields = (
        "part_code", "part_name", "expected_qty", "unit_cost",
        "handl_synced", "handl_error",
    )
    fields = (
        "part_code", "part_name", "expected_qty", "unit_cost", "actual_qty", "entered_by",
        "handl_synced", "handl_error",
    )


@admin.register(WeeklyStockCheck)
class WeeklyStockCheckAdmin(admin.ModelAdmin):
    list_display = (
        "locksmith", "week_starting", "status", "generated_at", "completed_at",
        "confirmed_at", "confirmed_by",
    )
    list_filter = ("status", "week_starting")
    search_fields = ("locksmith__name",)
    readonly_fields = ("confirmed_at", "confirmed_by")
    inlines = [StockCheckItemInline]
