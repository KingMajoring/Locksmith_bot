from django.urls import path

from . import views

app_name = "stock_accuracy"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("checks/<int:pk>/", views.entry_detail, name="entry_detail"),
    path("checks/<int:pk>/confirm/", views.confirm_check, name="confirm_check"),
    path("locksmiths/<int:pk>/", views.locksmith_report, name="locksmith_report"),
]
