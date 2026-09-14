from django.contrib import admin

from .models import GoogleMapsSettings, OptimoSettings


@admin.register(OptimoSettings)
class OptimoSettingsAdmin(admin.ModelAdmin):
    list_display = ("__str__", "updated_at")

    def has_add_permission(self, request):
        # Single row, tool-wide — same pattern as VarianceThreshold.
        return not OptimoSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(GoogleMapsSettings)
class GoogleMapsSettingsAdmin(admin.ModelAdmin):
    list_display = ("__str__", "updated_at")

    def has_add_permission(self, request):
        return not GoogleMapsSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False
