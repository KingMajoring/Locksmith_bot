from django.urls import path

from . import views

app_name = "job_completion"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("jobs/categorize/", views.categorize_jobs, name="categorize_jobs"),
    path("model-analysis/", views.model_analysis, name="model_analysis"),
    path("locksmiths/<int:pk>/", views.locksmith_report, name="locksmith_report"),
]
