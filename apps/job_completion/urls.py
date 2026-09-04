from django.urls import path

from . import views

app_name = "job_completion"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("job-failures/", views.job_failures, name="job_failures"),
    path("jobs/categorize/", views.categorize_jobs, name="categorize_jobs"),
    path("model-analysis/", views.model_analysis, name="model_analysis"),
    path("by-day/", views.jobs_by_day, name="jobs_by_day"),
    path("locksmiths/<int:pk>/", views.locksmith_report, name="locksmith_report"),
    path("margin/", views.margin_makes, name="margin_makes"),
    path("margin/<str:make>/", views.margin_models, name="margin_models"),
    path("margin/<str:make>/<str:model_family>/", views.margin_years, name="margin_years"),
    path("timing/", views.timing_makes, name="timing_makes"),
    path("timing/<str:make>/", views.timing_models, name="timing_models"),
    path("timing/<str:make>/<str:model_family>/", views.timing_years, name="timing_years"),
    path(
        "internal/run-job/<str:command_name>/",
        views.run_scheduled_job,
        name="run_scheduled_job",
    ),
]
