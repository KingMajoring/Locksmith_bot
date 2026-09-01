from django.urls import path

from . import views

app_name = "job_completion"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("locksmiths/<int:pk>/", views.locksmith_report, name="locksmith_report"),
]
