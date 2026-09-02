from django.urls import path

from . import views

app_name = "job_completion"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("jobs/categorize/", views.categorize_jobs, name="categorize_jobs"),
    path("model-analysis/", views.model_analysis, name="model_analysis"),
    path("by-day/", views.jobs_by_day, name="jobs_by_day"),
    path("locksmiths/<int:pk>/", views.locksmith_report, name="locksmith_report"),
]
