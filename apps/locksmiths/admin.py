from collections import Counter

from django.contrib import admin

from .models import Locksmith, OptimoDriverId, SoterLocksmithId


class SoterLocksmithIdInline(admin.TabularInline):
    model = SoterLocksmithId
    extra = 1


class OptimoDriverIdInline(admin.TabularInline):
    model = OptimoDriverId
    extra = 1


@admin.action(description="Auto-assign stock check schedule (spread across Mon–Fri)")
def assign_stock_check_schedule(modeladmin, request, queryset):
    # Deferred import: stock_accuracy depends on locksmiths, not the
    # other way round, so this stays a local import to avoid a cycle.
    from apps.stock_accuracy.models import StockCheckSchedule

    counts = Counter(StockCheckSchedule.objects.values_list("weekday", flat=True))
    weekdays = [w for w, _ in StockCheckSchedule.Weekday.choices]

    created = 0
    skipped = 0
    for locksmith in queryset:
        if hasattr(locksmith, "stock_check_schedule"):
            skipped += 1
            continue
        weekday = min(weekdays, key=lambda w: counts[w])
        StockCheckSchedule.objects.create(locksmith=locksmith, weekday=weekday, enabled=True)
        counts[weekday] += 1
        created += 1

    modeladmin.message_user(
        request,
        f"Created {created} schedule(s), spread across the week. "
        f"{skipped} already had one and were left as-is.",
    )


@admin.register(Locksmith)
class LocksmithAdmin(admin.ModelAdmin):
    list_display = ("name", "soter_ids_display", "email", "has_schedule", "active")
    list_filter = ("active",)
    search_fields = ("name", "email", "soter_ids__soter_locksmith_id")
    inlines = [SoterLocksmithIdInline, OptimoDriverIdInline]
    actions = [assign_stock_check_schedule]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("stock_check_schedule")

    @admin.display(description="Soter IDs")
    def soter_ids_display(self, obj):
        return ", ".join(obj.soter_id_list)

    @admin.display(description="Has schedule", boolean=True)
    def has_schedule(self, obj):
        return hasattr(obj, "stock_check_schedule")
