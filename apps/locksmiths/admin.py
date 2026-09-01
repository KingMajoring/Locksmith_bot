from django.contrib import admin

from .models import Locksmith


@admin.register(Locksmith)
class LocksmithAdmin(admin.ModelAdmin):
    list_display = ("name", "handl_engineer_id", "email", "active")
    list_filter = ("active",)
    search_fields = ("name", "handl_engineer_id", "email")
