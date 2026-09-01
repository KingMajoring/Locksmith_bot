from django.contrib import admin

from .models import Locksmith, SoterLocksmithId


class SoterLocksmithIdInline(admin.TabularInline):
    model = SoterLocksmithId
    extra = 1


@admin.register(Locksmith)
class LocksmithAdmin(admin.ModelAdmin):
    list_display = ("name", "soter_ids_display", "email", "active")
    list_filter = ("active",)
    search_fields = ("name", "email", "soter_ids__soter_locksmith_id")
    inlines = [SoterLocksmithIdInline]

    @admin.display(description="Soter IDs")
    def soter_ids_display(self, obj):
        return ", ".join(obj.soter_id_list)
