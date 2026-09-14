from django.urls import path

from . import views

app_name = "locksmiths"

urlpatterns = [
    path("sync-from-soter/", views.sync_from_soter, name="sync_from_soter"),
    path("sync-from-optimo/", views.sync_from_optimo, name="sync_from_optimo"),
    path(
        "sync-from-employee-locations/",
        views.sync_from_employee_locations,
        name="sync_from_employee_locations",
    ),
]
