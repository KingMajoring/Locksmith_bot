from django.urls import path

from . import views

app_name = "job_completion"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("jobs/categorize/", views.categorize_jobs, name="categorize_jobs"),
    path("locksmiths/<int:pk>/", views.locksmith_report, name="locksmith_report"),
]
