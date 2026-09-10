from collections import Counter

from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

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
    list_display = (
        "name", "soter_ids_display", "soter_user_id", "email", "has_schedule",
        "portal_linked", "preferred_navigation_app", "sees_all_jobs_for_testing",
        "office_access", "active", "preview_link",
    )
    list_filter = ("active", "sees_all_jobs_for_testing", "office_access", "preferred_navigation_app")
    list_editable = ("preferred_navigation_app", "office_access")
    search_fields = ("name", "email", "soter_ids__soter_locksmith_id")
    inlines = [SoterLocksmithIdInline, OptimoDriverIdInline]
    actions = [assign_stock_check_schedule]
    # Editable, not read-only: the automatic email-match linking (see
    # apps/accounts/adapter.py) only ever runs the first time someone
    # signs in. If their Handl/Soter-synced email (here) doesn't match
    # their real Microsoft sign-in email, that first login can't find
    # a match and provisions them as a full office/admin account
    # instead — with no way for that mismatch to self-correct on a
    # later login. This field lets office staff link that existing
    # login to the right Locksmith by hand once the mismatch is found,
    # without deleting and recreating their account.
    autocomplete_fields = ("user",)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("stock_check_schedule", "user")

    @admin.display(description="Portal linked", boolean=True)
    def portal_linked(self, obj):
        # Whether this locksmith has signed in yet and been auto-linked
        # to a self-service portal account (see apps/accounts/adapter.py).
        return obj.user_id is not None

    @admin.display(description="Soter IDs")
    def soter_ids_display(self, obj):
        return ", ".join(obj.soter_id_list)

    @admin.display(description="Has schedule", boolean=True)
    def has_schedule(self, obj):
        return hasattr(obj, "stock_check_schedule")

    @admin.display(description="Portal")
    def preview_link(self, obj):
        # Lets office/admin staff try the self-service portal as this
        # locksmith without their own login becoming locksmith-linked
        # (see apps.locksmith_portal.views.start_preview) — acts on
        # their real data, so use with care.
        url = reverse("locksmith_portal:start_preview", args=[obj.pk])
        return format_html('<a href="{}">Preview portal →</a>', url)
