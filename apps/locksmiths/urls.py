from django.urls import path

from . import views

app_name = "locksmiths"

urlpatterns = [
    path("sync-from-soter/", views.sync_from_soter, name="sync_from_soter"),
]
